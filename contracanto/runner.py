"""Ciclo de uma execução: agenda lotes, revisa, integra e registra o resultado."""

from __future__ import annotations

import concurrent.futures
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from . import agents, config, gitops, state
from .agents import Artifact
from .errors import IntegrationStateError, RecoveryRequired, RunCancelled, describe, recovery_on_failure
from .gitops import TargetCheckout
from .plan import Plan, Task, overlaps, validate
from .state import RunContext, Workspace


def execute(
    ws: Workspace,
    plan: Plan | Path,
    max_workers: int,
    retries: int,
    tests: Sequence[str] | None,
    reservation: str | None = None,
) -> dict[str, str]:
    """Assume a reserva, executa o plano e grava o estado final.

    A reserva só é liberada quando o repositório está em estado conhecido.
    """
    token = reservation or state.reserve_run(ws)
    run_id = state.reservation_id(token)
    if not state.owns_reservation(ws, run_id):
        raise RuntimeError('Reserva de execução inválida')
    # mkdir is the single-use handoff. A losing worker must never enter cleanup.
    try:
        ws.run_dir(run_id).mkdir(parents=True)
    except FileExistsError:
        raise RuntimeError('Reserva já assumida por outro worker') from None
    except (OSError, KeyboardInterrupt) as error:
        raise RecoveryRequired(
            'Não foi possível assumir a reserva; inspecione run.lock e o diretório da execução'
        ) from error
    ctx = RunContext.open(ws, run_id)
    run = ctx.state
    keep_lock = False
    try:
        # CLI e MCP começam com estado próprio antes de qualquer preparação.
        run.save()
        if isinstance(plan, Path):
            plan = json.loads(plan.read_text(encoding='utf-8'))
        # Publish the current CLI or MCP plan only after proving lock ownership.
        state.write_json(ws.plan, plan)
        ctx.stop.check()
        result = _execute(ctx, plan, max_workers, retries, tests)
        keep_lock = run.recovery_required
        run.state = 'completed' if all(status == 'approved' for status in result.values()) else 'failed'
        run.save()
        return result
    except (Exception, KeyboardInterrupt) as error:
        # A later persistence error must never clear an earlier recovery flag.
        keep_lock = keep_lock or run.recovery_required or isinstance(error, RecoveryRequired)
        cancelled = isinstance(error, RunCancelled) and not keep_lock
        run.state = 'cancelled' if cancelled else 'failed'
        run.error = describe(error)
        run.recovery_required = keep_lock
        run.close_unfinished('cancelled' if cancelled else 'blocked')
        run.save()
        raise
    finally:
        if not keep_lock:
            state.release_run(ws, token)


def _execute(
    ctx: RunContext, plan: Plan, max_workers: int, retries: int, tests: Sequence[str] | None
) -> dict[str, str]:
    validate(plan)
    config.check_limits(max_workers, retries)
    if not tests:
        raise RuntimeError('Configure um comando de testes via --test (obrigatório)')
    if not shutil.which('agy') or not shutil.which('claude'):
        raise RuntimeError('Instale/autentique agy e claude; ambos devem estar no PATH')
    if gitops.changed_files(ctx.root):
        raise RuntimeError('Repositório precisa estar limpo antes de executar')
    target = TargetCheckout.current(ctx.root)
    try:
        return _run_tasks(ctx, plan, max_workers, retries, tests, target)
    except RecoveryRequired:
        raise
    except (Exception, KeyboardInterrupt):
        # An interruption may arrive between two Git calls. Only release the
        # reservation if the target still matches the last verified revision.
        target.verify()
        raise


def schedule_batch(tasks: Sequence[Task], statuses: dict[str, str], max_workers: int) -> list[Task]:
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


def block_descendants(tasks: Sequence[Task], statuses: dict[str, str]) -> None:
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
) -> dict[str, str]:
    run = ctx.state
    tasks = plan['tasks']
    run.state = 'running'
    run.statuses.update({task['id']: 'pending' for task in tasks})
    run.save()
    for _ in range(len(tasks)):
        batch = schedule_batch(tasks, run.statuses, max_workers)
        if not batch:
            break
        target.verify()
        ctx.stop.check()
        base = target.head
        context = 'Base do código: ' + base
        if plan.get('goal'):
            context += '\nObjetivo geral: ' + str(plan['goal'])
        artifacts = run_batch(ctx, batch, base, context, max_workers)
        if run.recovery_required:
            return run.statuses
        ctx.stop.check()
        for artifact in artifacts:
            review_and_integrate(ctx, artifact, base, context, retries, tests, target)
            if run.recovery_required:
                return run.statuses
    block_descendants(tasks, run.statuses)
    run.save()
    target.verify()
    return run.statuses


def run_batch(ctx: RunContext, batch: Sequence[Task], base: str, context: str, max_workers: int) -> list[Artifact]:
    """Implementa o lote em paralelo; devolve os candidatos prontos para revisão.

    Se algum worker deixar processos ou Git em estado incerto, marca a
    recuperação pendente em `ctx.state`.
    """
    run = ctx.state
    for task in batch:
        run.statuses[task['id']] = 'running'
    run.save()
    jobs: dict[concurrent.futures.Future, Task] = {}
    artifacts: list[Artifact] = []
    recovery_error: BaseException | None = None

    def record_failure(task: Task, error: BaseException) -> None:
        nonlocal recovery_error
        run.statuses[task['id']] = 'failed'
        run.details[task['id']] = {'error': str(error)}
        if isinstance(error, RecoveryRequired):
            recovery_error = error

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        with pool:
            for task in batch:
                jobs[pool.submit(agents.run_executor, ctx, task, base, context)] = task
            for future in concurrent.futures.as_completed(jobs):
                task = jobs[future]
                try:
                    artifact = future.result()
                    run.statuses[task['id']] = 'review'
                    artifacts.append(artifact)
                except RunCancelled:
                    run.statuses[task['id']] = 'cancelled'
                except Exception as error:
                    record_failure(task, error)
    except (Exception, KeyboardInterrupt) as interruption:
        # Submission/collection failures must also inspect existing workers.
        with recovery_on_failure('Espera dos workers interrompida; encerramento não confirmado'):
            pool.shutdown(wait=True)
        for future, task in jobs.items():
            error = future.exception()
            if error is not None:
                record_failure(task, error)
        if recovery_error is not None:
            raise RecoveryRequired(
                f'Execução interrompida com recuperação pendente: {recovery_error}'
            ) from interruption
        if len(jobs) != len(batch):
            raise RecoveryRequired('Submissão dos workers interrompida; resultados incompletos') from interruption
        raise
    if recovery_error is not None:
        run.require_recovery()
        return []
    run.save()
    return artifacts


def review_and_integrate(
    ctx: RunContext,
    artifact: Artifact,
    base: str,
    context: str,
    retries: int,
    tests: Sequence[str],
    target: TargetCheckout,
) -> None:
    """Revisa o candidato, pede correções quando rejeitado e integra quando aprovado."""
    run = ctx.state
    task = artifact.task
    task_id = task['id']
    run.details[task_id] = {'worktree': artifact.worktree, 'commit': artifact.commit}
    feedback = ''
    for attempt in range(retries + 1):
        try:
            ctx.stop.check()
            gitops.check_scope(task, Path(artifact.worktree), base)
            approved, feedback, _ = agents.review(task, artifact, tests, ctx.stop)
            if approved:
                integrate(ctx, artifact, base, feedback, tests, target)
                break
            if attempt == retries:
                raise RuntimeError('Rejeitado após tentativas: ' + feedback[-config.ERROR_TAIL :])
            agents.repair(ctx, artifact, base, context, feedback)
        except (RunCancelled, IntegrationStateError):
            raise
        except Exception as error:
            run.statuses[task_id] = 'failed'
            run.details[task_id].update(error=str(error), review=feedback)
            if isinstance(error, RecoveryRequired):
                run.require_recovery()
                return
            break
    if run.statuses[task_id] != 'approved':
        run.save()


def _persist_integration(ctx: RunContext) -> None:
    try:
        ctx.state.save()
    except (Exception, KeyboardInterrupt) as error:
        # The previous on-disk snapshot can predate the cherry-pick.
        raise IntegrationStateError(error) from error


def integrate(
    ctx: RunContext, artifact: Artifact, base: str, feedback: str, tests: Sequence[str], target: TargetCheckout
) -> None:
    """Aplica o candidato na branch de destino e confirma com os testes de integração."""
    run = ctx.state
    root = ctx.root
    task = artifact.task
    details = run.details[task['id']]
    target.verify()
    candidate = artifact.commit
    # Earlier tasks in a parallel batch may have changed the destination tree.
    # Inspect the actual three-way merge.
    if target.head == base:
        merged_tree = candidate
    else:
        merged_tree = gitops.git('merge-tree', '--write-tree', target.head, candidate, cwd=root).stdout.strip()
    gitops.check_tree_scope(task, root, target.head, merged_tree)
    gitops.protect_untracked_files(root, target.head, merged_tree)
    details.update(commit=candidate, target_before=target.head, review=feedback)
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
    details['integrated_commit'] = integrated_head
    _persist_integration(ctx)
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
        _rollback(ctx, details, integrated_head, target)
        if isinstance(integration_error, (KeyboardInterrupt, RunCancelled)):
            raise
        raise RuntimeError(f'Integração revertida: {integration_error}') from integration_error
    run.statuses[task['id']] = 'approved'
    _persist_integration(ctx)
    target.head = integrated_head


def _abort_cherry_pick(root: Path, target: TargetCheckout) -> None:
    with recovery_on_failure('Falha ao executar abort de cherry-pick'):
        # Conflicts may dirty the tree, but abort must stay on the original branch.
        if gitops.branch_ref(root) != target.branch:
            raise RecoveryRequired('Branch de destino alterada antes do abort')
        aborted = gitops.git('cherry-pick', '--abort', cwd=root, check=False)
    if aborted.returncode:
        raise RecoveryRequired('Falha ao abortar cherry-pick; revisão manual necessária')
    target.verify()


def _rollback(ctx: RunContext, details: dict, integrated_head: str, target: TargetCheckout) -> None:
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
    details['revert_commit'] = reverted_head
    _persist_integration(ctx)
    target.head = reverted_head
