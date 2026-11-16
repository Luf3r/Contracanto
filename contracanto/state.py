"""Arquivos da execução: reserva, pedido de parada e status persistido."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .errors import RecoveryRequired, RunCancelled

RunStatus = Literal['starting', 'running', 'completed', 'failed', 'cancelled']
TaskStatus = Literal['pending', 'running', 'review', 'approved', 'failed', 'blocked', 'cancelled']
Phase = Literal['implementation', 'review', 'repair', 'integration']

TERMINAL_STATES: tuple[RunStatus, ...] = ('completed', 'failed', 'cancelled')
UNFINISHED_STATUSES: tuple[TaskStatus, ...] = ('pending', 'running', 'review')
FINISHED_STATUSES: tuple[TaskStatus, ...] = ('approved', 'failed', 'blocked', 'cancelled')
RUN_ID = re.compile('[0-9a-f]{64}')

log = logging.getLogger('contracanto')


def now() -> str:
    """Instante atual em ISO 8601, UTC, com precisão de segundos."""
    return datetime.now(UTC).isoformat(timespec='seconds')


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

    def commands(self, run_id: str) -> Path:
        """Um arquivo por comando demorado em andamento, com o PID do processo raiz no nome."""
        return self.run_dir(run_id) / 'commands'

    def claim(self, run_id: str) -> Path:
        """Diretório criado uma única vez pelo worker que assume a execução."""
        return self.run_dir(run_id) / 'claim'

    def worker_pid(self, run_id: str) -> Path:
        """PID gravado pelo worker ao assumir a execução, pela CLI ou pelo MCP."""
        return self.claim(run_id) / 'pid'

    def run_ids(self) -> list[str]:
        """Execuções registradas, da mais antiga para a mais recente."""
        runs = self.store / 'runs'
        if not runs.is_dir():
            return []
        found = [path.name for path in runs.iterdir() if path.is_dir() and RUN_ID.fullmatch(path.name)]

        def started(run_id: str) -> float:
            # plan.json é gravado uma vez no início; o diretório muda a cada atualização de estado.
            for path in (self.plan(run_id), self.run_dir(run_id)):
                with contextlib.suppress(OSError):
                    return path.stat().st_mtime
            return 0.0

        return sorted(found, key=started)

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
    """Repositório orquestrado: `repo`, a variável CONTRACANTO_REPO ou o diretório atual."""
    root = Path(repo or os.environ.get(REPO_ENV) or Path.cwd())

    if not root.is_dir():
        raise ValueError(f'Repositório não encontrado: {root}')
    return Workspace.for_root(root)


class StopSignal:
    """Pedido cooperativo de encerramento, sinalizado por um arquivo.

    Acompanha os comandos interrompíveis; `commands` é o diretório onde eles são
    registrados enquanto rodam, para que `--unlock` encontre os que sobreviveram
    a um worker encerrado à força.
    """

    def __init__(self, path: Path, commands: Path | None = None):
        self.path = path
        self.commands = commands

    def check(self) -> None:
        if self.path.is_file():
            raise RunCancelled('Execução encerrada a pedido do operador')


def retry_permission_error(operation: Callable[..., Any], *args: Any) -> Any:
    # No Windows, um leitor pode impedir por instantes a troca ou a remoção de um arquivo aberto.
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
        # A recuperação continua exigida mesmo que seu primeiro registro durável não possa ser gravado.
        if obj.get('recovery_required'):
            raise RecoveryRequired(f'Falha ao persistir recuperação pendente: {error}') from error
        raise


class RunState:
    """Único escritor de status.json; a cópia em memória é a fonte da verdade.

    Workers em paralelo atualizam o estado pelos métodos desta classe, que
    serializam as alterações e a gravação.
    """

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.state: RunStatus = 'starting'
        self.statuses: dict[str, TaskStatus] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.timings: dict[str, dict[str, Any]] = {}
        self.recovery_required = False
        self.error: str | None = None
        # Branch de destino e commit sobre o qual a execução começou.
        self.target: dict[str, Any] | None = None
        self.started_at = now()
        self.finished_at: str | None = None
        self._started = time.monotonic()
        self._ended: float | None = None
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                'state': self.state,
                'run_id': self.run_id,
                'started_at': self.started_at,
                'updated_at': now(),
                'finished_at': self.finished_at,
                'duration_seconds': round((self._ended or time.monotonic()) - self._started, 1),
                'statuses': self.statuses,
                'details': self.details,
                'timings': self.timings,
                'recovery_required': self.recovery_required,
            }
            if self.error is not None:
                data['error'] = self.error
            if self.target is not None:
                data['target'] = self.target
            # Uma cópia independente: a gravação não pode observar uma alteração pela metade.
            return json.loads(json.dumps(data))

    def save(self) -> None:
        with self._lock:
            write_json(self.path, self.snapshot())

    def mark(self, task_id: str, status: TaskStatus) -> None:
        """Muda o estado de uma tarefa e registra quando ela começou e terminou."""
        with self._lock:
            if self.statuses.get(task_id) != status:
                log.info('%s: %s', task_id, status)
            self.statuses[task_id] = status
            timing = self.timings.setdefault(task_id, {'phases': {}})
            if status not in ('pending', *FINISHED_STATUSES):
                timing.setdefault('started_at', now())
            if status in FINISHED_STATUSES and 'started_at' in timing:
                timing['finished_at'] = now()

    def update_details(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            self.details.setdefault(task_id, {}).update(fields)

    def set_details(self, task_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            self.details[task_id] = fields

    @contextlib.contextmanager
    def phase(self, task_id: str, name: Phase) -> Iterator[None]:
        """Acumula o tempo gasto em uma fase da tarefa, mesmo quando ela falha."""
        started = time.monotonic()
        log.info('%s: início de %s', task_id, name)
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            log.info('%s: fim de %s após %.1fs', task_id, name, elapsed)
            with self._lock:
                phases = self.timings.setdefault(task_id, {'phases': {}})['phases']
                entry = phases.setdefault(name, {'seconds': 0.0, 'count': 0})
                entry['seconds'] = round(entry['seconds'] + elapsed, 1)
                entry['count'] += 1

    def finish(self, state: RunStatus) -> None:
        with self._lock:
            self.state = state
            self.finished_at = now()
            self._ended = time.monotonic()

    def close_unfinished(self, status: TaskStatus = 'blocked') -> None:
        with self._lock:
            for task_id, value in list(self.statuses.items()):
                if value in UNFINISHED_STATUSES:
                    self.mark(task_id, status)

    def require_recovery(self) -> None:
        """Nenhuma outra tarefa pode prosseguir até a inspeção manual."""
        with self._lock:
            log.info('Recuperação manual exigida; a reserva será mantida')
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
    # Comando que prepara cada worktree (dependências, por exemplo) antes da implementação.
    setup: Sequence[str] | None = None
    # Branch nova, criada no início, que recebe os commits em vez da branch em uso.
    branch: str | None = None

    @classmethod
    def open(
        cls,
        ws: Workspace,
        run_id: str,
        keep_worktrees: bool = False,
        setup: Sequence[str] | None = None,
        branch: str | None = None,
    ) -> RunContext:
        stop = StopSignal(ws.stop_file(run_id), ws.commands(run_id))
        return cls(ws, run_id, RunState(ws.status(run_id), run_id), stop, keep_worktrees, setup, branch)

    @property
    def root(self) -> Path:
        return self.ws.root

    @property
    def run_dir(self) -> Path:
        return self.ws.run_dir(self.run_id)


def reservation_id(token: str) -> str:
    """Identificador público da execução; só ele é gravado ou devolvido nas consultas."""
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
    # O pedido pode chegar antes de o worker assumir a execução ou disputar com uma nova.
    ws.run_dir(active_id).mkdir(parents=True, exist_ok=True)
    ws.stop_file(active_id).touch()
    return f'Encerramento solicitado para {active_id}. Consulte get_run_status para confirmar o término.'
