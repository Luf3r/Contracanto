"""Arquivos da execução: reserva, pedido de parada e status persistido."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RecoveryRequired, RunCancelled

TERMINAL_STATES = ('completed', 'failed', 'cancelled')
UNFINISHED_STATUSES = ('pending', 'running', 'review')
RUN_ID = re.compile('[0-9a-f]{64}')


@dataclass(frozen=True)
class Workspace:
    """Repositório orquestrado e o diretório onde as execuções são registradas."""

    root: Path
    store: Path

    @classmethod
    def for_root(cls, root: Path) -> Workspace:
        root = Path(root).resolve()
        return cls(root, root / '.agent-runs')

    @property
    def lock(self) -> Path:
        return self.store / 'run.lock'

    @property
    def latest(self) -> Path:
        """Ponteiro para a execução mais recente; sobrevive à liberação da reserva."""
        return self.store / 'latest-run'

    def run_dir(self, run_id: str) -> Path:
        return self.store / 'runs' / run_id

    def status(self, run_id: str) -> Path:
        return self.run_dir(run_id) / 'status.json'

    def plan(self, run_id: str) -> Path:
        return self.run_dir(run_id) / 'plan.json'

    def log(self, run_id: str) -> Path:
        return self.run_dir(run_id) / 'run.log'

    def pid(self, run_id: str) -> Path:
        return self.run_dir(run_id) / 'pid.txt'

    def stop_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / 'stop'

    def claim(self, run_id: str) -> Path:
        """Diretório criado uma única vez pelo worker que assume a execução."""
        return self.run_dir(run_id) / 'claim'

    def latest_run_id(self) -> str | None:
        try:
            run_id = self.latest.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            return None
        return run_id if RUN_ID.fullmatch(run_id) else None

    def publish_run(self, run_id: str) -> None:
        """Cria o diretório da execução e a torna a consultada por padrão."""
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        tmp = self.latest.with_suffix('.tmp')
        tmp.write_text(run_id, encoding='utf-8')
        retry_permission_error(tmp.replace, self.latest)


REPO_ENV = 'CONTRACANTO_REPO'


def default_workspace(repo: str | Path | None = None) -> Workspace:
    """Repositório orquestrado: `repo`, a variável CONTRACANTO_REPO ou,
    na ausência de ambos, o diretório que contém este pacote."""
    root = Path(repo or os.environ.get(REPO_ENV) or Path(__file__).resolve().parent.parent)
    if not root.is_dir():
        raise ValueError(f'Repositório não encontrado: {root}')
    return Workspace.for_root(root)


class StopSignal:
    """Pedido cooperativo de encerramento, sinalizado por um arquivo."""

    def __init__(self, path: Path):
        self.path = path

    def check(self) -> None:
        if self.path.is_file():
            raise RunCancelled('Execução encerrada a pedido do operador')


def retry_permission_error(operation: Callable[..., Any], *args: Any) -> Any:
    # A Windows reader may briefly prevent replacing or removing an open file.
    for attempt in range(21):
        try:
            return operation(*args)
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(0.05)


def write_json(path: Path, obj: Mapping[str, Any]) -> None:
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')
        retry_permission_error(tmp.replace, path)
    except (Exception, KeyboardInterrupt) as error:
        # Recovery is required even if its first durable record cannot be saved.
        if obj.get('recovery_required'):
            raise RecoveryRequired(f'Falha ao persistir recuperação pendente: {error}') from error
        raise


class RunState:
    """Único escritor de status.json; a cópia em memória é a fonte da verdade."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.state = 'starting'
        self.statuses: dict[str, str] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.recovery_required = False
        self.error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            'state': self.state,
            'run_id': self.run_id,
            'statuses': self.statuses,
            'details': self.details,
            'recovery_required': self.recovery_required,
        }
        if self.error is not None:
            data['error'] = self.error
        return data

    def save(self) -> None:
        write_json(self.path, self.snapshot())

    def close_unfinished(self, status: str = 'blocked') -> None:
        for task_id, value in self.statuses.items():
            if value in UNFINISHED_STATUSES:
                self.statuses[task_id] = status

    def require_recovery(self) -> None:
        """Nenhuma outra tarefa pode prosseguir até a inspeção manual."""
        self.recovery_required = True
        self.close_unfinished()
        self.save()


@dataclass
class RunContext:
    """Tudo o que uma execução em andamento precisa, passado explicitamente."""

    ws: Workspace
    run_id: str
    state: RunState
    stop: StopSignal
    # Mantém worktree e branch das tarefas aprovadas em vez de removê-los após a integração.
    keep_worktrees: bool = False

    @classmethod
    def open(cls, ws: Workspace, run_id: str, keep_worktrees: bool = False) -> RunContext:
        return cls(ws, run_id, RunState(ws.status(run_id), run_id), StopSignal(ws.stop_file(run_id)), keep_worktrees)

    @property
    def root(self) -> Path:
        return self.ws.root

    @property
    def run_dir(self) -> Path:
        return self.ws.run_dir(self.run_id)


def reservation_id(token: str) -> str:
    """Only this public identifier is persisted or returned by status queries."""
    return hashlib.sha256(token.encode()).hexdigest()


def reserve_run(ws: Workspace) -> str:
    """Reserva atômica; após crash, exige inspeção e recuperação manual."""
    token = uuid.uuid4().hex
    ws.store.mkdir(exist_ok=True)
    try:
        with ws.lock.open('x', encoding='utf-8') as handle:
            json.dump({'run_id': reservation_id(token), 'pid': os.getpid(), 'supports_stop': True}, handle)
    except FileExistsError:
        raise RuntimeError(
            'Já existe execução reservada; consulte get_run_status e o procedimento de recuperação'
        ) from None
    return token


def release_run(ws: Workspace, token: str) -> None:
    run_id = reservation_id(token)

    def remove_owned_lock() -> None:
        try:
            if json.loads(ws.lock.read_text(encoding='utf-8')).get('run_id') == run_id:
                ws.lock.unlink()
        except FileNotFoundError:
            pass

    retry_permission_error(remove_owned_lock)


def owns_reservation(ws: Workspace, run_id: str) -> bool:
    return json.loads(ws.lock.read_text(encoding='utf-8')).get('run_id') == run_id


def request_stop(ws: Workspace, run_id: str | None = None) -> str:
    try:
        record = json.loads(ws.lock.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 'Nenhuma execução ativa.'
    except json.JSONDecodeError:
        return 'Reserva em criação ou incompleta. Consulte o status e tente novamente.'
    active_id = record.get('run_id', '')
    if run_id is not None and run_id != active_id:
        return 'A reserva atual pertence a outra execução.'
    if not record.get('supports_stop') or not RUN_ID.fullmatch(active_id):
        return 'Esta reserva não oferece encerramento cooperativo. Consulte o procedimento de recuperação.'
    try:
        current = json.loads(ws.status(active_id).read_text(encoding='utf-8'))
    except FileNotFoundError:
        current = {}
    if current.get('state') in TERMINAL_STATES:
        if current.get('recovery_required'):
            return 'Execução finalizada com recuperação manual pendente.'
        return 'Execução já finalizada.'
    # The request may precede the worker's claim or race a new run.
    ws.run_dir(active_id).mkdir(parents=True, exist_ok=True)
    ws.stop_file(active_id).touch()
    return f'Encerramento solicitado para {active_id}. Consulte get_run_status para confirmar o término.'
