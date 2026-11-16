"""Ciclo de uma execução: agenda lotes, revisa, integra e registra o resultado.

Implementação e revisão rodam em paralelo, cada tarefa no próprio worktree.
A integração na branch de destino é sempre serial.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import agents, config, gitops, state
from .agents import Artifact
from .errors import IntegrationStateError, RecoveryRequired, RunCancelled, describe, recovery_on_failure
from .gitops import TargetCheckout
from .plan import Plan, Task, overlaps, validate
from .state import RunContext, TaskStatus, Workspace

log = logging.getLogger('contracanto')


def _summary(error: BaseException) -> str:
    """Primeira linha do erro, para o log de eventos."""
    return describe(error).splitlines()[0][:300]


def resolve_command(command: Sequence[str], root: Path, label: str) -> list[str]:
    """Confere, antes de gastar uma chamada ao agente, que o executável do comando existe.

    No Windows, um script `.cmd` ou `.bat` encontrado no PATH (como `npm`) é
    trocado pelo caminho completo, porque o sistema só acha sozinho os `.exe`.
    """
    name = command[0]
    candidates = [shutil.which(name)]
    if sys.platform == 'win32':
        # O Windows procura primeiro no diretório do interpretador em execução.
        beside = Path(sys.executable).parent / name
        candidates += [str(path) for path in (beside, beside.with_name(beside.name + '.exe')) if path.is_file()]
    found = next((candidate for candidate in candidates if candidate), None)
    if found is None:
        if (root / name).is_file():
            return list(command)
        raise RuntimeError(f'{label} não encontrado: {name}. Confira o comando e o PATH do worker')
    if sys.platform == 'win32' and Path(found).suffix.lower() in ('.cmd', '.bat'):
        return [found, *command[1:]]
    return list(command)


def execute(
    ws: Workspace,
    plan: Mapping[str, Any] | Path,
    max_workers: int,
    retries: int,
    tests: Sequence[str] | None,
    reservation: str | None = None,
    keep_worktrees: bool = False,
    setup: Sequence[str] | None = None,
    branch: str | None = None,
) -> dict[str, TaskStatus]:
    """Assume a reserva, executa o plano e grava o estado final.

    A reserva só é liberada quando o repositório está em estado conhecido.
    """
    token = reservation or state.reserve_run(ws)
    run_id = state.reservation_id(token)
    if not state.owns_reservation(ws, run_id):
        raise RuntimeError('Reserva de execução inválida')
    # O mkdir do claim é a passagem de uso único. O worker que perde nunca entra na limpeza.
    try:
        ws.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        ws.claim(run_id).mkdir()
    except FileExistsError:
        raise RuntimeError('Reserva já assumida por outro worker') from None
    except (OSError, KeyboardInterrupt) as error:
        raise RecoveryRequired(
            'Não foi possível assumir a reserva; inspecione run.lock e o diretório da execução'
        ) from error
    # Diagnóstico para --unlock; a execução não depende dele.
    with contextlib.suppress(OSError):
        ws.worker_pid(run_id).write_text(str(os.getpid()), encoding='utf-8')
    ctx = RunContext.open(ws, run_id, keep_worktrees, setup, branch)
    run = ctx.state
    keep_lock = False
    try:
        # CLI e MCP começam com estado próprio antes de qualquer preparação.
        ws.publish_run(run_id)
        run.save()
        loaded: Any = json.loads(plan.read_text(encoding='utf-8')) if isinstance(plan, Path) else plan
        # O plano da CLI ou do MCP só é publicado depois de provada a posse da reserva.
        state.write_json(ws.plan(run_id), loaded)
        ctx.stop.check()
        result = _execute(ctx, loaded, max_workers, retries, tests)
        keep_lock = run.recovery_required
        run.finish('completed' if all(status == 'approved' for status in result.values()) else 'failed')
        run.save()
        log.info('Execução encerrada: %s', run.state)
        return result
    except (Exception, KeyboardInterrupt) as error:
        # Um erro posterior de gravação nunca pode apagar uma recuperação já exigida.
        keep_lock = keep_lock or run.recovery_required or isinstance(error, RecoveryRequired)
        cancelled = isinstance(error, RunCancelled) and not keep_lock
        run.finish('cancelled' if cancelled else 'failed')
        run.error = describe(error)
        run.recovery_required = keep_lock
        run.close_unfinished('cancelled' if cancelled else 'blocked')
        log.info('Execução encerrada: %s: %s', run.state, _summary(error))
        run.save()
        raise
    finally:
        if not keep_lock:
            state.release_run(ws, token)


def _execute(
    ctx: RunContext, plan: Plan, max_workers: int, retries: int, tests: Sequence[str] | None
) -> dict[str, TaskStatus]:
    validate(plan)
    config.check_limits(max_workers, retries)
    if not tests:
        raise RuntimeError('Configure um comando de testes via --test (obrigatório)')
    config.check_pinned(tests, ctx.setup)
    ctx.setup = config.pinned_setup(ctx.setup)
    if not shutil.which('agy') or not shutil.which('claude'):
        raise RuntimeError('Instale/autentique agy e claude; ambos devem estar no PATH')
    tests = resolve_command(tests, ctx.root, 'Comando de testes')
    if ctx.setup:
        ctx.setup = resolve_command(ctx.setup, ctx.root, 'Comando de preparação')
    if not gitops.is_top_level(ctx.root):
        raise RuntimeError(f'{ctx.root} é um subdiretório do repositório Git; informe a raiz dele')
    if gitops.changed_files(ctx.root):
        raise RuntimeError('Repositório precisa estar limpo antes de executar')
    origin = gitops.checkout_state(ctx.root)
    if ctx.branch:
        gitops.create_branch(ctx.root, ctx.branch)
        log.info('Branch de integração criada: %s', ctx.branch)
    target = TargetCheckout.current(ctx.root)
    if target.branch == 'HEAD':
        raise RuntimeError(
            'HEAD destacado: os commits integrados ficariam fora de qualquer branch. '
            'Faça checkout de uma branch ou informe uma branch de integração (--branch)'
        )
    ctx.state.target = {'branch': target.branch, 'base': target.head}
    try:
        result = _run_tasks(ctx, plan, max_workers, retries, tests, target)
    except RecoveryRequired:
        raise
    except (Exception, KeyboardInterrupt):
        # Uma interrupção pode chegar entre duas chamadas ao Git. A reserva só é
        # liberada se o destino ainda corresponder à última revisão verificada.
        target.verify()
        _drop_empty_branch(ctx, target, origin)
        raise
    _drop_empty_branch(ctx, target, origin)
    return result


def _drop_empty_branch(ctx: RunContext, target: TargetCheckout, origin: gitops.CheckoutState) -> None:
    """Desfaz a branch de integração que terminou sem nenhum commit.

    É apenas limpeza, feita com o destino já verificado: uma falha fica no log
    e a branch permanece.
    """
    run = ctx.state
    if not ctx.branch or run.recovery_required or target.head != origin.head or run.target is None:
        return
    try:
        gitops.drop_branch(ctx.root, ctx.branch, origin)
    except Exception as error:
        log.info('Branch de integração vazia não removida: %s', _summary(error))
        return
    run.target['branch_removed'] = True
    log.info('Branch de integração %s removida: nenhuma tarefa foi integrada', ctx.branch)


def schedule_batch(tasks: Sequence[Task], statuses: Mapping[str, TaskStatus], max_workers: int) -> list[Task]:
    """Tarefas prontas que podem rodar juntas sem disputar os mesmos arquivos."""
    ready = [
        task
        for task in tasks
        if statuses[task['id']] == 'pending' and all(statuses[dep] == 'approved' for dep in task['deps'])
    ]
    batch: list[Task] = []
    owned: list[str] = []
    for task in ready:
        if any(overlaps(name, other) for name in task['files'] for other in owned):
            continue
        batch.append(task)
        owned.extend(task['files'])
        if len(batch) >= max_workers:
            break
    return batch


def block_descendants(tasks: Sequence[Task], statuses: dict[str, TaskStatus]) -> None:
    """Uma falha bloqueia todas as tarefas que dependem dela, direta ou indiretamente."""
    while True:
        blocked = [
            task['id']
            for task in tasks
            if statuses[task['id']] == 'pending' and any(statuses[dep] in ('failed', 'blocked') for dep in task['deps'])
        ]
        if not blocked:
            return
        for task_id in blocked:
            statuses[task_id] = 'blocked'


def _run_tasks(
    ctx: RunContext, plan: Plan, max_workers: int, retries: int, tests: Sequence[str], target: TargetCheckout
) -> dict[str, TaskStatus]:
    run = ctx.state
    tasks = plan['tasks']
    run.state = 'running'
    log.info('Execução %s: %d tarefa(s) sobre %s em %s', ctx.run_id[:12], len(tasks), target.head[:12], target.branch)
    for task in tasks:
        run.mark(task['id'], 'pending')
    run.save()
    for _ in range(len(tasks)):
        batch = schedule_batch(tasks, run.statuses, max_workers)
        if not batch:
            break
        target.verify()
        ctx.stop.check()
        base = target.head
        log.info('Lote sobre %s: %s', base[:12], ', '.join(task['id'] for task in batch))
        context = 'Base do código: ' + base
        if plan.get('goal'):
            context += '\nObjetivo geral: ' + str(plan['goal'])
        artifacts = run_batch(ctx, batch, base, context, max_workers)
        if run.recovery_required:
            return run.statuses
        ctx.stop.check()
        approved = review_batch(ctx, artifacts, base, context, retries, tests, max_workers)
        if run.recovery_required:
            return run.statuses
        for artifact, feedback in approved:
            integrate_task(ctx, artifact, base, feedback, tests, target)
            if run.recovery_required:
                return run.statuses
    block_descendants(tasks, run.statuses)
    run.save()
    target.verify()
    return run.statuses


def _run_parallel[Item](
    items: Sequence[Item],
    work: Callable[[Item], object],
    max_workers: int,
    collect: Callable[[Item, concurrent.futures.Future], None],
) -> None:
    """Executa `work` para cada item em threads e entrega cada resultado a `collect`.

    Uma falha ao submeter ou coletar aguarda e inspeciona todos os workers já
    iniciados: se algum deixou o estado incerto, ou se nem todos foram submetidos,
    a execução exige recuperação.
    """
    jobs: dict[concurrent.futures.Future, Item] = {}
    collected: set[concurrent.futures.Future] = set()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        with pool:
            for item in items:
                jobs[pool.submit(work, item)] = item
            for future in concurrent.futures.as_completed(jobs):
                collected.add(future)
                collect(jobs[future], future)
    except (Exception, KeyboardInterrupt) as interruption:
        with recovery_on_failure('Espera dos workers interrompida; encerramento não confirmado'):
            pool.shutdown(wait=True)
        recovery_error = None
        for future, item in jobs.items():
            failure = future.exception()
            if isinstance(failure, RecoveryRequired):
                recovery_error = failure
            if future not in collected:
                with contextlib.suppress(Exception):
                    collect(item, future)
        if recovery_error is not None:
            raise RecoveryRequired(
                f'Execução interrompida com recuperação pendente: {recovery_error}'
            ) from interruption
        if len(jobs) != len(items):
            raise RecoveryRequired('Submissão dos workers interrompida; resultados incompletos') from interruption
        raise


def run_batch(ctx: RunContext, batch: Sequence[Task], base: str, context: str, max_workers: int) -> list[Artifact]:
    """Implementa o lote em paralelo; devolve os candidatos prontos para revisão.

    Se algum worker deixar processos ou Git em estado incerto, marca a
    recuperação pendente em `ctx.state`.
    """
    run = ctx.state
    for task in batch:
        run.mark(task['id'], 'running')
    run.save()
    artifacts: list[Artifact] = []
    recovery_error: BaseException | None = None

    def implement(task: Task) -> Artifact:
        with run.phase(task['id'], 'implementation'):
            return agents.run_executor(ctx, task, base, context)

    def collect(task: Task, future: concurrent.futures.Future) -> None:
        nonlocal recovery_error
        try:
            artifacts.append(future.result())
            run.mark(task['id'], 'review')
        except RunCancelled:
            run.mark(task['id'], 'cancelled')
        except Exception as error:
            log.info('%s: implementação falhou: %s', task['id'], _summary(error))
            run.mark(task['id'], 'failed')
            run.set_details(task['id'], {'error': str(error)})
            if isinstance(error, RecoveryRequired):
                recovery_error = error

    _run_parallel(batch, implement, max_workers, collect)
    if recovery_error is not None:
        run.require_recovery()
        return []
    run.save()
    return artifacts


def review_task(
    ctx: RunContext, artifact: Artifact, base: str, context: str, retries: int, tests: Sequence[str]
) -> str | None:
    """Revisa o candidato e pede correções quando rejeitado.

    Devolve o parecer quando aprovado, ou None depois de registrar a falha.
    RunCancelled e RecoveryRequired são propagados depois de registrados.
    """
    run = ctx.state
    task = artifact.task
    task_id = task['id']
    run.set_details(task_id, {'worktree': artifact.worktree, 'commit': artifact.commit})
    feedback = ''
    try:
        for attempt in range(retries + 1):
            ctx.stop.check()
            gitops.check_scope(task, Path(artifact.worktree), base)
            with run.phase(task_id, 'review'):
                approved, feedback = agents.review(artifact, tests, ctx.stop)
            log.info('%s: revisão %d %s', task_id, attempt + 1, 'aprovada' if approved else 'rejeitada')
            if approved:
                return feedback
            if attempt == retries:
                raise RuntimeError('Rejeitado após tentativas: ' + feedback[-config.ERROR_TAIL :])
            with run.phase(task_id, 'repair'):
                agents.repair(ctx, artifact, base, context, feedback)
            run.update_details(task_id, commit=artifact.commit)
    except RunCancelled:
        raise
    except Exception as error:
        log.info('%s: revisão falhou: %s', task_id, _summary(error))
        run.mark(task_id, 'failed')
        run.update_details(task_id, error=str(error), review=feedback)
        if isinstance(error, RecoveryRequired):
            raise
        run.save()
    return None


def review_batch(
    ctx: RunContext,
    artifacts: Sequence[Artifact],
    base: str,
    context: str,
    retries: int,
    tests: Sequence[str],
    max_workers: int,
) -> list[tuple[Artifact, str]]:
    """Revisa os candidatos em paralelo; devolve os aprovados na ordem recebida."""
    run = ctx.state
    verdicts: dict[str, str] = {}
    cancelled: RunCancelled | None = None
    recovery_error: RecoveryRequired | None = None

    def collect(artifact: Artifact, future: concurrent.futures.Future) -> None:
        nonlocal cancelled, recovery_error
        try:
            feedback = future.result()
        except RunCancelled as error:
            cancelled = error
        except RecoveryRequired as error:
            recovery_error = error
        else:
            if feedback is not None:
                verdicts[artifact.task['id']] = feedback

    _run_parallel(
        artifacts,
        lambda artifact: review_task(ctx, artifact, base, context, retries, tests),
        max_workers,
        collect,
    )
    if recovery_error is not None:
        run.require_recovery()
        return []
    if cancelled is not None:
        raise cancelled
    return [(artifact, verdicts[artifact.task['id']]) for artifact in artifacts if artifact.task['id'] in verdicts]


def integrate_task(
    ctx: RunContext, artifact: Artifact, base: str, feedback: str, tests: Sequence[str], target: TargetCheckout
) -> None:
    """Integra um candidato aprovado e registra uma eventual falha."""
    run = ctx.state
    task_id = artifact.task['id']
    try:
        with run.phase(task_id, 'integration'):
            integrate(ctx, artifact, base, feedback, tests, target)
    except (RunCancelled, IntegrationStateError):
        raise
    except Exception as error:
        log.info('%s: integração falhou: %s', task_id, _summary(error))
        run.mark(task_id, 'failed')
        run.update_details(task_id, error=str(error), review=feedback)
        if isinstance(error, RecoveryRequired):
            run.require_recovery()
            return
        run.save()


def _persist_integration(ctx: RunContext) -> None:
    try:
        ctx.state.save()
    except (Exception, KeyboardInterrupt) as error:
        # O estado gravado antes pode ser anterior ao cherry-pick.
        raise IntegrationStateError(error) from error


def integrate(
    ctx: RunContext, artifact: Artifact, base: str, feedback: str, tests: Sequence[str], target: TargetCheckout
) -> None:
    """Aplica o candidato na branch de destino e confirma com os testes de integração."""
    run = ctx.state
    root = ctx.root
    task = artifact.task
    task_id = task['id']
    target.verify()
    candidate = artifact.candidate
    # Tarefas anteriores do lote podem ter mudado a árvore de destino; nesse caso
    # vale o resultado real do merge de três pontas.
    merged_tree = candidate if target.head == base else gitops.merge_tree(root, target.head, candidate)
    gitops.check_tree_scope(task, root, target.head, merged_tree)
    gitops.protect_untracked_files(root, target.head, merged_tree)
    run.update_details(task_id, commit=candidate, target_before=target.head, review=feedback)
    run.save()
    ctx.stop.check()
    # O comando pode ter aplicado o commit antes de perdermos seu resultado.
    with recovery_on_failure('Resultado do cherry-pick desconhecido'):
        applied = gitops.git('cherry-pick', candidate, cwd=root, check=False)
    if applied.returncode:
        _abort_cherry_pick(root, target)
        raise RuntimeError('Conflito ao integrar commit; requer intervenção ou replanejamento')
    with recovery_on_failure('Não foi possível identificar o HEAD integrado'):
        integrated_head = gitops.head(root)
    run.update_details(task_id, integrated_commit=integrated_head)
    _persist_integration(ctx)
    log.info('%s: commit %s aplicado em %s', task_id, integrated_head[:12], target.branch)
    with recovery_on_failure('Escopo do commit integrado não confirmado'):
        gitops.check_tree_scope(task, root, target.head, integrated_head)
    try:
        integrated = gitops.run_tests(
            tests, root, integrated_head, recovery=True, expected_branch=target.branch, stop=ctx.stop
        )
        ctx.stop.check()
        if integrated.returncode:
            output = (integrated.stdout + integrated.stderr)[-config.ERROR_TAIL :]
            raise RuntimeError('Testes de integração falharam: ' + output)
    except RecoveryRequired:
        raise
    except (Exception, KeyboardInterrupt) as integration_error:
        _rollback(ctx, task_id, integrated_head, target)
        if isinstance(integration_error, (KeyboardInterrupt, RunCancelled)):
            raise
        raise RuntimeError(f'Integração revertida: {integration_error}') from integration_error
    run.mark(task_id, 'approved')
    _persist_integration(ctx)
    target.head = integrated_head
    if not ctx.keep_worktrees:
        _discard_worktree(ctx, artifact)


def _discard_worktree(ctx: RunContext, artifact: Artifact) -> None:
    """Remove o worktree e a branch de uma tarefa já integrada.

    É apenas limpeza: uma falha fica registrada e não altera o resultado da tarefa.
    O Git recusa remover um worktree com alterações, que então permanece para inspeção.
    """
    task_id = artifact.task['id']
    try:
        gitops.remove_worktree(ctx.root, artifact.worktree, artifact.branch)
        ctx.state.update_details(task_id, worktree_removed=True)
    except Exception as error:
        ctx.state.update_details(task_id, cleanup_error=str(error))


def _abort_cherry_pick(root: Path, target: TargetCheckout) -> None:
    with recovery_on_failure('Falha ao executar abort de cherry-pick'):
        # Conflitos podem sujar a árvore, mas o abort precisa acontecer na branch original.
        if gitops.branch_ref(root) != target.branch:
            raise RecoveryRequired('Branch de destino alterada antes do abort')
        aborted = gitops.git('cherry-pick', '--abort', cwd=root, check=False)
    if aborted.returncode:
        raise RecoveryRequired('Falha ao abortar cherry-pick; revisão manual necessária')
    target.verify()


def _rollback(ctx: RunContext, task_id: str, integrated_head: str, target: TargetCheckout) -> None:
    """Reverte a integração com um novo commit, sem reescrever o histórico."""
    root = ctx.root
    gitops.check_revision(root, integrated_head, target.branch, recovery=True)
    with recovery_on_failure('Falha ao executar revert'):
        gitops.protect_untracked_files(root, integrated_head, target.head)
        revert = gitops.git('revert', '--no-edit', integrated_head, cwd=root, check=False)
    if revert.returncode:
        raise RecoveryRequired('Testes falharam e o revert automático também; revisão manual necessária')
    with recovery_on_failure('Não foi possível identificar o HEAD após revert'):
        reverted_head = gitops.head(root)
    gitops.check_revision(root, reverted_head, target.branch, recovery=True)
    ctx.state.update_details(task_id, revert_commit=reverted_head)
    _persist_integration(ctx)
    log.info('%s: integração revertida pelo commit %s', task_id, reverted_head[:12])
    target.head = reverted_head
