"""Operações do operador fora de uma execução: consultar, destravar e podar registros."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import gitops, process, state
from .state import Workspace

LOG_TAIL = 4000


def read_reservation(ws: Workspace) -> tuple[str, dict[str, Any] | None]:
    """Estado da reserva: ('active', registro público), ('none', None) ou ('incomplete', None)."""
    try:
        record = json.loads(ws.lock.read_text(encoding='utf-8'))
    except FileNotFoundError:
        # Normal completion may release the reservation while status is polled.
        return 'none', None
    except json.JSONDecodeError:
        # Polling may observe an exclusive file before its creator finishes writing.
        return 'incomplete', None
    # Only the public identifier leaves this process, never the worker's credential.
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
    run = None
    log = ''
    if run_id is not None:
        run = _read_json(ws.status(run_id))
        log = read_log(ws.log(run_id))
    return {'run': run, 'reservation_state': reservation_state, 'reservation': reservation, 'log': log}


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
    run_id = record.get('run_id')
    run_id = run_id if isinstance(run_id, str) and state.RUN_ID.fullmatch(run_id) else None
    lock_pid = record.get('pid') if isinstance(record.get('pid'), int) else None
    alive = [pid for pid in _worker_pids(ws, run_id, lock_pid) if process.pid_alive(pid)]
    if alive:
        return (
            f'Recusado: o processo {alive[0]} ainda está em execução. '
            'Use --stop ou confirme no Gerenciador de Tarefas que ele não pertence a esta execução.'
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
            current['unlocked_at'] = state.now()
            current['recovery_required_at_unlock'] = bool(current.get('recovery_required'))
            state.write_json(ws.status(run_id), {**current, 'recovery_required': False})
    state.retry_permission_error(ws.lock.unlink)
    return f'Reserva {run_id or "sem identificador"} removida. Uma nova execução pode ser iniciada.'


def prune_runs(ws: Workspace, keep: int) -> dict[str, Any]:
    """Remove os registros das execuções encerradas além das `keep` mais recentes.

    Execuções reservadas, sem estado final ou com recuperação pendente são
    mantidas. Os worktrees de cada execução saem com `git worktree remove`,
    que recusa os que têm alterações; nesse caso a execução inteira fica.
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
            error = _remove_run(ws, run_id)
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


def _remove_run(ws: Workspace, run_id: str) -> str | None:
    run_dir = ws.run_dir(run_id).resolve()
    try:
        for worktree, branch in _registered_worktrees(ws.root).items():
            if worktree.parent == run_dir:
                gitops.remove_worktree(ws.root, worktree, branch)
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
