"""Operações do operador fora de uma execução: consultar, destravar e podar registros."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

from . import gitops, plan, process, state
from .state import Workspace

LOG_TAIL = 4000
SUMMARY_ERROR_LIMIT = 500


def read_reservation(ws: Workspace) -> tuple[str, dict[str, Any] | None]:
    """Estado da reserva: ('active', registro público), ('none', None) ou ('incomplete', None)."""
    try:
        record = json.loads(ws.lock.read_text(encoding='utf-8'))
    except FileNotFoundError:
        # O término normal pode liberar a reserva durante a consulta.
        return 'none', None
    except json.JSONDecodeError:
        # A consulta pode ver o arquivo exclusivo antes de o criador terminar de escrevê-lo.
        return 'incomplete', None
    # Só o identificador público sai deste processo, nunca a credencial do worker.
    public = {key: record[key] for key in ('run_id', 'pid') if key in record}
    run_id = public.get('run_id')
    if isinstance(run_id, str) and state.RUN_ID.fullmatch(run_id):
        public['stop_requested'] = bool(record.get('supports_stop')) and ws.stop_file(run_id).is_file()
    else:
        public.pop('run_id', None)
    return 'active', public


def selected_run(ws: Workspace, reservation: dict[str, Any] | None) -> str | None:
    """A execução reservada tem precedência sobre a última publicada."""
    return (reservation or {}).get('run_id') or ws.latest_run_id()


def read_log(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ''
    try:
        return data.decode('utf-8')[-LOG_TAIL:]
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')[-LOG_TAIL:]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def status_report(ws: Workspace) -> dict[str, Any]:
    """Estado da execução reservada ou, sem reserva, da mais recente."""
    reservation_state, reservation = read_reservation(ws)
    run_id = selected_run(ws, reservation)
    if reservation is not None:
        # Um worker encerrado à força deixa a reserva e o estado `running` para trás.
        lock_pid = reservation.get('pid') if isinstance(reservation.get('pid'), int) else None
        pids = _worker_pids(ws, reservation.get('run_id'), lock_pid)
        reservation['worker_alive'] = any(process.pid_alive(pid) for pid in pids)
    run = None
    log = ''
    if run_id is not None:
        run = _read_json(ws.status(run_id))
        log = read_log(ws.log(run_id))
    return {'run': run, 'reservation_state': reservation_state, 'reservation': reservation, 'log': log}


def settled(report: dict[str, Any]) -> bool:
    """Se o relatório não vai mais mudar sem intervenção: execução encerrada ou worker morto."""
    if report['reservation_state'] == 'none':
        return True
    if report['reservation_state'] == 'incomplete':
        return False
    run = report['run'] or {}
    reservation = report['reservation'] or {}
    return run.get('state') in state.TERMINAL_STATES or not reservation.get('worker_alive', True)


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Versão curta do relatório: estados e erros, sem pareceres, tempos por fase nem log."""
    run = report['run'] or {}
    reservation = report['reservation'] or {}
    details = run.get('details') or {}
    errors = {
        task_id: str(detail['error'])[:SUMMARY_ERROR_LIMIT]
        for task_id, detail in details.items()
        if isinstance(detail, dict) and detail.get('error')
    }
    return {
        'run_id': run.get('run_id'),
        'state': run.get('state'),
        'recovery_required': bool(run.get('recovery_required')),
        'error': run.get('error'),
        'duration_seconds': run.get('duration_seconds'),
        'statuses': run.get('statuses') or {},
        'task_errors': errors,
        'reservation_state': report['reservation_state'],
        'worker_alive': reservation.get('worker_alive'),
        'stop_requested': bool(reservation.get('stop_requested')),
    }


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (FileNotFoundError, ValueError):
        return None


def _worker_pids(ws: Workspace, run_id: str | None, lock_pid: int | None) -> list[int]:
    """Processos que ainda podem estar trabalhando na execução reservada.

    O worker grava o próprio PID ao assumir a execução; antes disso, vale o PID
    do worker lançado pelo MCP ou, sem ele, o do processo que criou a reserva.
    """
    if run_id is not None:
        for path in (ws.worker_pid(run_id), ws.pid(run_id)):
            pid = _read_pid(path)
            if pid is not None:
                return [pid]
    return [lock_pid] if lock_pid is not None else []


def _running_commands(ws: Workspace, run_id: str | None) -> list[int]:
    """Comandos do worker (agy, revisor, testes) que continuam rodando sem ele."""
    if run_id is None:
        return []
    try:
        names = [path.name for path in ws.commands(run_id).iterdir()]
    except OSError:
        return []
    return [int(name) for name in sorted(names) if name.isdecimal() and process.command_alive(int(name))]


def unlock(ws: Workspace) -> str:
    """Remove uma reserva deixada por um worker que não está mais em execução.

    Recusa enquanto o worker estiver vivo, com operação Git pendente ou com o
    repositório sujo: são os pontos que o procedimento de recuperação exige
    resolver antes de liberar a reserva.
    """
    try:
        raw = ws.lock.read_text(encoding='utf-8')
    except FileNotFoundError:
        return 'Nenhuma reserva ativa.'
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        record = {}
    if not isinstance(record, dict):
        record = {}
    run_id = record.get('run_id')
    run_id = run_id if isinstance(run_id, str) and state.RUN_ID.fullmatch(run_id) else None
    lock_pid = record.get('pid') if isinstance(record.get('pid'), int) else None
    alive = [pid for pid in _worker_pids(ws, run_id, lock_pid) if process.pid_alive(pid)]
    if alive:
        return (
            f'Recusado: o processo {alive[0]} ainda está em execução. '
            'Use --stop ou confirme no Gerenciador de Tarefas que ele não pertence a esta execução.'
        )
    running = _running_commands(ws, run_id)
    if running:
        return (
            f'Recusado: um comando iniciado pelo worker (PID {running[0]}) ainda está em execução. '
            'Encerre-o, com seus subprocessos, antes de liberar a reserva.'
        )
    try:
        gitops.check_git_operation(ws.root)
    except RuntimeError as error:
        return f'Recusado: {error}. Conclua ou aborte a operação antes de liberar a reserva.'
    changed = gitops.changed_files(ws.root)
    if changed:
        return (
            f'Recusado: o repositório tem {len(changed)} arquivo(s) alterado(s), por exemplo {changed[0]}. '
            'Preserve ou descarte as alterações antes de liberar a reserva.'
        )
    if run_id is not None:
        current = _read_json(ws.status(run_id))
        if current is not None:
            if current.get('state') not in state.TERMINAL_STATES:
                current['state'] = 'failed'
                current['finished_at'] = state.now()
                current.setdefault('error', 'Worker encerrado sem registrar o estado final')
            statuses = current.get('statuses')
            if isinstance(statuses, dict):
                # Nenhuma tarefa continua em andamento depois que o worker morreu.
                for task_id, status in statuses.items():
                    if status in state.UNFINISHED_STATUSES:
                        statuses[task_id] = 'blocked'
            current['unlocked_at'] = state.now()
            current['recovery_required_at_unlock'] = bool(current.get('recovery_required'))
            state.write_json(ws.status(run_id), {**current, 'recovery_required': False})
    state.retry_permission_error(ws.lock.unlink)
    return f'Reserva {run_id or "sem identificador"} removida. Uma nova execução pode ser iniciada.'


def resume_plan(ws: Workspace, run_id: str | None = None) -> plan.Plan:
    """Plano com as tarefas que não foram aprovadas em uma execução encerrada.

    Sem `run_id`, vale a execução mais recente. As dependências já aprovadas saem
    do plano, então cada uma delas precisa continuar no histórico do HEAD atual.
    """
    if run_id is not None and not state.RUN_ID.fullmatch(run_id):
        raise RuntimeError(f'Identificador de execução inválido: {run_id}')
    if read_reservation(ws)[0] != 'none':
        raise RuntimeError('Há uma execução reservada; aguarde o término ou libere a reserva antes de retomar')
    run_id = run_id or ws.latest_run_id()
    current = _read_json(ws.status(run_id)) if run_id else None
    previous = _read_json(ws.plan(run_id)) if run_id else None
    if run_id is None or current is None or previous is None:
        raise RuntimeError('Nenhuma execução com plano e estado registrados para retomar')
    if current.get('state') not in state.TERMINAL_STATES:
        raise RuntimeError(f'A execução {run_id[:12]} não tem estado final; use --unlock antes de retomar')
    plan.validate(previous)
    statuses = current.get('statuses') or {}
    details = current.get('details') or {}
    for task in previous['tasks']:
        task_id = task['id']
        detail = details.get(task_id) or {}
        commit = detail.get('integrated_commit')
        if statuses.get(task_id) != 'approved':
            # Um worker encerrado entre o cherry-pick e a aprovação deixa o commit na branch.
            if (
                commit
                and not detail.get('revert_commit')
                and gitops.contains_commit(ws.root, commit)
                and not gitops.reverted(ws.root, commit)
            ):
                raise RuntimeError(
                    f'A tarefa {task_id} não foi aprovada na execução {run_id[:12]}, mas o commit {commit[:12]} '
                    'dela está no HEAD atual. Desfaça-o com git revert, ou submeta um plano novo sem a tarefa'
                )
            continue
        if not commit or not gitops.contains_commit(ws.root, commit):
            raise RuntimeError(
                f'A tarefa {task_id} foi aprovada na execução {run_id[:12]}, mas o commit dela não está no '
                'HEAD atual. Volte para a branch que recebeu a execução ou submeta um plano novo'
            )
    pending = plan.remaining(cast(plan.Plan, previous), statuses)
    if not pending['tasks']:
        raise RuntimeError(f'Todas as tarefas da execução {run_id[:12]} já foram aprovadas')
    return pending


def prune_runs(ws: Workspace, keep: int, force: bool = False) -> dict[str, Any]:
    """Remove os registros das execuções encerradas além das `keep` mais recentes.

    Execuções reservadas, sem estado final ou com recuperação pendente são
    mantidas. Os worktrees de cada execução saem com `git worktree remove`,
    que recusa os que têm alterações; nesse caso a execução inteira fica, salvo
    com `force`, que descarta essas alterações.
    """
    if keep < 0:
        raise ValueError('keep deve ser zero ou maior')
    _, reservation = read_reservation(ws)
    reserved = (reservation or {}).get('run_id')
    run_ids = ws.run_ids()
    candidates = run_ids[: max(0, len(run_ids) - keep)]
    removed: list[str] = []
    kept: dict[str, str] = {}
    for run_id in candidates:
        current = _read_json(ws.status(run_id)) or {}
        if run_id == reserved:
            kept[run_id] = 'execução reservada'
        elif current.get('state') not in state.TERMINAL_STATES:
            kept[run_id] = 'sem estado final'
        elif current.get('recovery_required'):
            kept[run_id] = 'recuperação pendente'
        else:
            error = _remove_run(ws, run_id, force)
            if error is None:
                removed.append(run_id)
            else:
                kept[run_id] = error
    return {'removed': removed, 'kept': kept}


def _registered_worktrees(root: Path) -> dict[Path, str | None]:
    """Worktrees registrados no repositório e a branch de cada um."""
    worktrees: dict[Path, str | None] = {}
    current: Path | None = None
    # Esquece registros cujo diretório já não existe.
    gitops.git('worktree', 'prune', cwd=root)
    for line in gitops.git('worktree', 'list', '--porcelain', cwd=root).stdout.splitlines():
        if line.startswith('worktree '):
            current = Path(line.removeprefix('worktree ')).resolve()
            worktrees[current] = None
        elif line.startswith('branch ') and current is not None:
            worktrees[current] = line.removeprefix('branch ')
    return worktrees


def _remove_run(ws: Workspace, run_id: str, force: bool) -> str | None:
    run_dir = ws.run_dir(run_id).resolve()
    try:
        for worktree, branch in _registered_worktrees(ws.root).items():
            if worktree.parent == run_dir:
                gitops.remove_worktree(ws.root, worktree, branch, force)
    except RuntimeError as error:
        lines = str(error).strip().splitlines()
        return f'worktree não removido: {lines[-1] if lines else error}'
    try:
        shutil.rmtree(run_dir)
    except OSError as error:
        return f'diretório não removido: {error}'

    if ws.latest_run_id() == run_id:
        ws.latest.unlink(missing_ok=True)
    return None
