"""Arquivos da execução: reserva, pedido de parada e status persistido."""

from __future__ import annotations

import concurrent.futures
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import config, pricing, usage
from .errors import RecoveryRequired, RunCancelled

RunStatus = Literal['starting', 'running', 'completed', 'failed', 'cancelled']
TaskStatus = Literal['pending', 'running', 'review', 'approved', 'failed', 'blocked', 'cancelled']
Phase = Literal['implementation', 'review', 'repair', 'integration']

TERMINAL_STATES: tuple[RunStatus, ...] = ('completed', 'failed', 'cancelled')
UNFINISHED_STATUSES: tuple[TaskStatus, ...] = ('pending', 'running', 'review')
FINISHED_STATUSES: tuple[TaskStatus, ...] = ('approved', 'failed', 'blocked', 'cancelled')
# Tarefas cujo tempo e tokens não viraram commit; uma bloqueada nem chegou a gastar.
WASTED_STATUSES: tuple[TaskStatus, ...] = ('failed', 'cancelled')
# Casas decimais dos custos gravados, em dólares.
COST_DIGITS = 6
RUN_ID = re.compile('[0-9a-f]{64}')

log = logging.getLogger('contracanto')


def now() -> str:
    """Instante atual em ISO 8601, UTC, com precisão de segundos."""
    return datetime.now(UTC).isoformat(timespec='seconds')


@dataclass(frozen=True)
class Workspace:
    """Repositório orquestrado, o diretório onde as execuções são registradas e o dos worktrees.

    Sem `worktrees`, os worktrees ficam em `store/worktrees`; `for_root` os põe fora do
    repositório, em worktrees_parent(). `legacy_worktrees` é a pasta do padrão antigo, onde
    execuções anteriores podem ter deixado worktrees que a limpeza ainda remove.
    """

    root: Path
    store: Path
    worktrees: Path | None = None
    legacy_worktrees: Path | None = None

    @classmethod
    def for_root(cls, root: Path) -> Workspace:
        root = Path(root).resolve()
        # Um nome por repositório: o do diretório, para reconhecer, e um hash do caminho, para
        # separar repositórios com o mesmo nome.
        key = str(root).casefold() if os.name == 'nt' else str(root)
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]
        name = f'{root.name}-{digest}'
        legacy = legacy_worktrees_parent()
        return cls(root, root / '.agent-runs', worktrees_parent() / name, legacy / name if legacy else None)

    def worktree_dir(self, run_id: str) -> Path:
        """Diretório dos worktrees de uma execução; o prefixo do id mantém o caminho curto."""
        return (self.worktrees if self.worktrees is not None else self.store / 'worktrees') / run_id[:16]

    def legacy_worktree_dir(self, run_id: str) -> Path | None:
        """Onde o padrão antigo punha os worktrees da execução, se houver um padrão antigo."""
        return self.legacy_worktrees / run_id[:16] if self.legacy_worktrees is not None else None

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

    def options(self, run_id: str) -> Path:
        """Parâmetros com que a execução foi iniciada, para poder repeti-la."""
        return self.run_dir(run_id) / 'options.json'

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


def worktrees_parent() -> Path:
    """Onde ficam os worktrees de todos os repositórios: CONTRACANTO_WORKTREES_DIR ou ~/.cache.

    Também no Windows, fora de %LOCALAPPDATA%: um app empacotado em MSIX, como o app Claude,
    redireciona o que seus processos gravam ali para a pasta do pacote, que o usuário do
    sandbox do Codex não alcança, e todo comando de shell dele falharia.
    """
    if config.WORKTREES_DIR:
        return Path(config.WORKTREES_DIR).expanduser().resolve()
    base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return (base / 'Contracanto' / 'worktrees').resolve()


def legacy_worktrees_parent() -> Path | None:
    """O padrão anterior no Windows, em %LOCALAPPDATA%; só vale sem CONTRACANTO_WORKTREES_DIR."""
    if config.WORKTREES_DIR or os.name != 'nt':
        return None
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    legacy = (base / 'Contracanto' / 'worktrees').resolve()
    return None if legacy == worktrees_parent() else legacy


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
        # Tokens e custo somados por papel (executor, revisor), além dos de cada fase em `timings`.
        self.usage: dict[str, dict[str, Any]] = {}
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
                'agents': configured_agents(),
            }
            if self.usage:
                data['usage'] = self.usage
                data['cost'] = self._cost()
            if waste := self._waste():
                data['waste'] = waste
            if self.error is not None:
                data['error'] = self.error
            if self.target is not None:
                data['target'] = self.target
            # Uma cópia independente: a gravação não pode observar uma alteração pela metade.
            return json.loads(json.dumps(data))

    def _waste(self) -> dict[str, Any] | None:
        """Tempo e tokens gastos nas tarefas que falharam ou foram canceladas, e as categorias das falhas."""
        lost = [task_id for task_id, status in self.statuses.items() if status in WASTED_STATUSES]
        if not lost:
            return None
        seconds = 0.0
        cost = 0.0
        tokens: dict[str, int] = {}
        categories: dict[str, int] = {}
        for task_id in lost:
            for entry in self.timings.get(task_id, {}).get('phases', {}).values():
                seconds += entry.get('seconds', 0.0)
                cost += entry.get('cost_usd', 0.0)
                for name, value in (entry.get('tokens') or {}).items():
                    tokens[name] = tokens.get(name, 0) + value
            failure = self.details.get(task_id, {}).get('failure')
            if isinstance(failure, dict) and failure.get('category'):
                categories[failure['category']] = categories.get(failure['category'], 0) + 1
        waste: dict[str, Any] = {'tasks': len(lost), 'seconds': round(seconds, 1), 'by_category': categories}
        if tokens:
            waste['tokens'] = tokens
            waste['cost_usd'] = round(cost, COST_DIGITS)
        return waste

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

    def add_review(self, task_id: str, attempt: int, approved: bool, feedback: str) -> None:
        """Acrescenta um parecer ao histórico `reviews` da tarefa; `review` fica com o mais recente."""
        with self._lock:
            detail = self.details.setdefault(task_id, {})
            detail['review'] = feedback
            entry = {'attempt': attempt, 'approved': approved, 'at': now(), 'feedback': feedback}
            detail.setdefault('reviews', []).append(entry)

    def set_details(self, task_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            self.details[task_id] = fields

    def _phase_entry(self, task_id: str, name: Phase) -> dict[str, Any]:
        phases = self.timings.setdefault(task_id, {'phases': {}})['phases']
        entry: dict[str, Any] = phases.setdefault(name, {'seconds': 0.0, 'count': 0})
        return entry

    def add_usage(self, task_id: str, name: Phase, role: usage.Role, used: usage.Usage) -> None:
        """Soma os tokens e o custo de uma chamada à fase da tarefa e ao total do papel."""
        cost = pricing.call_cost(config.PRICES, used.model, used.as_dict(), used.peak_input, used.reported_usd)
        with self._lock:
            entry = self._phase_entry(task_id, name)
            entry['tokens'] = usage.add(entry.get('tokens'), used)
            _add_cost(entry, cost)
            total = usage.add(self.usage.get(role), used)
            _add_cost(total, cost)
            total['billing'] = BILLING[role]()
            if used.model and used.model not in total.setdefault('models', []):
                total['models'].append(used.model)
            self.usage[role] = total
            # Grava já, para que quem acompanha veja o custo subir a cada chamada concluída, e não
            # só no fim do lote. A medição nunca faz a chamada falhar: um erro aqui fica para a
            # próxima gravação, que leva o mesmo total da memória.
            try:
                self.save()
            except (OSError, RecoveryRequired) as error:
                log.info('%s: custo parcial não gravado agora: %s', task_id, error)

    def _cost(self) -> dict[str, Any]:
        """Custo da execução separado entre cobrança por token e equivalente de assinatura."""
        by_billing = {'api': 0.0, 'plan': 0.0}
        unpriced = 0
        for total in self.usage.values():
            by_billing[total.get('billing', 'plan')] += total.get('cost_usd', 0.0)
            unpriced += total.get('unpriced_calls', 0)
        return {
            'api_usd': round(by_billing['api'], COST_DIGITS),
            'plan_equivalent_usd': round(by_billing['plan'], COST_DIGITS),
            'unpriced_calls': unpriced,
        }

    @contextlib.contextmanager
    def phase(self, task_id: str, name: Phase) -> Iterator[None]:
        """Acumula o tempo e os tokens gastos em uma fase da tarefa, mesmo quando ela falha."""
        started = time.monotonic()
        log.info('%s: início de %s', task_id, name)

        def sink(role: usage.Role, used: usage.Usage) -> None:
            self.add_usage(task_id, name, role, used)

        try:
            with usage.collecting(sink):
                yield
        finally:
            elapsed = time.monotonic() - started
            log.info('%s: fim de %s após %.1fs', task_id, name, elapsed)
            with self._lock:
                entry = self._phase_entry(task_id, name)
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


def configured_agents() -> dict[str, dict[str, str | None]]:
    """CLI e modelo de cada papel, como o processo que grava o status os configurou.

    Um modelo vazio deixa o padrão do CLI; o esforço só existe no Codex, porque no agy ele faz
    parte do id do modelo.
    """
    codex = config.EXECUTOR == 'codex'
    return {
        'executor': {
            'cli': config.EXECUTOR,
            'model': config.AGENT_MODEL or None,
            'effort': (config.AGENT_EFFORT or None) if codex else None,
        },
        'reviewer': {'cli': 'claude', 'model': config.REVIEW_MODEL},
    }


BILLING: dict[str, Callable[[], str]] = {
    'executor': lambda: config.EXECUTOR_BILLING,
    'reviewer': lambda: config.REVIEWER_BILLING,
}


def _add_cost(entry: dict[str, Any], cost: pricing.CallCost) -> None:
    """Soma o custo de uma chamada a uma entrada do status; sem preço, conta a chamada à parte."""
    if cost.usd is None:
        entry['unpriced_calls'] = entry.get('unpriced_calls', 0) + 1
    else:
        entry['cost_usd'] = round(entry.get('cost_usd', 0.0) + cost.usd, COST_DIGITS)
    if cost.long_context:
        entry['long_context_calls'] = entry.get('long_context_calls', 0) + 1


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
    # Comando de testes da execução; sem ele, a base não é testada antes da implementação.
    tests: Sequence[str] | None = None
    # Commit cujas dependências a preparação instalou na raiz, para a integração.
    root_prepared: str | None = None
    _baselines: dict[str, concurrent.futures.Future] = field(default_factory=dict, repr=False)
    _baseline_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def baseline(self, base: str, compute: Callable[[], Any]) -> Any:
        """Resultado de `compute` para a base, calculado uma vez e compartilhado pelo lote.

        A primeira tarefa sobre a base calcula; as outras esperam o mesmo resultado ou erro.
        """
        with self._baseline_lock:
            future = self._baselines.get(base)
            owner = future is None
            if future is None:
                future = self._baselines[base] = concurrent.futures.Future()
        if owner:
            try:
                future.set_result(compute())
            except BaseException as error:
                future.set_exception(error)
                raise
        return future.result()

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
            json.dump({'run_id': reservation_id(token), 'pid': os.getpid()}, handle)
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
    if not RUN_ID.fullmatch(active_id):
        return 'Reserva sem identificador de execução válido. Consulte o procedimento de recuperação.'
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
