"""Painel da execução no terminal: `contracanto --watch`.

Lê o mesmo estado que `--status` e o log do worker, e redesenha a tela até a execução
terminar ou o worker morrer. Fora de um terminal interativo, por exemplo quando o Claude
roda o comando em segundo plano, escreve uma linha por mudança de estado e, no fim, o
resumo JSON de `wait_for_run`. O código de saída diz como a execução terminou, para que
quem lançou o comando seja avisado quando ele sair.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

from . import maintenance, state

INTERVAL = 1.0
EVENT_LINES = 8
# Códigos de saída: todas as tarefas aprovadas, execução encerrada com alguma não aprovada,
# worker morto sem registrar o fim (retome com wait_for_run ou --resume), Ctrl+C.
EXIT_APPROVED = 0
EXIT_NOT_APPROVED = 1
EXIT_WORKER_DEAD = 3
EXIT_INTERRUPTED = 130

PHASE_NAMES = {
    'implementation': 'implementação',
    'review': 'revisão',
    'repair': 'correção',
    'integration': 'integração',
}
# Linhas do log do worker (formato de cli.run): "HH:MM:SS T01: início de review".
PHASE_LINE = re.compile(r'^(\d\d:\d\d:\d\d) ([A-Za-z0-9_-]+): (início|fim) de (\w+)')
COLORS = {
    'approved': '32',
    'completed': '32',
    'failed': '31',
    'running': '33',
    'review': '36',
    'starting': '33',
    'pending': '90',
    'blocked': '90',
    'cancelled': '90',
}


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ''
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f'{hours}h{minutes:02d}m'
    return f'{minutes}m{seconds:02d}s' if minutes else f'{seconds}s'


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def task_elapsed(timing: dict[str, Any] | None, now: datetime) -> float | None:
    """Tempo da tarefa desde o início até o fim registrado ou, se ainda corre, até agora."""
    started = _parse_time((timing or {}).get('started_at'))
    if started is None:
        return None
    finished = _parse_time((timing or {}).get('finished_at')) or now
    return (finished - started).total_seconds()


def current_phases(log: str) -> dict[str, str]:
    """Fase em andamento de cada tarefa, segundo as linhas de início e fim do log."""
    phases: dict[str, str] = {}
    for line in log.splitlines():
        match = PHASE_LINE.match(line)
        if match is None:
            continue
        _, task_id, edge, phase = match.groups()
        if edge == 'início':
            phases[task_id] = phase
        elif phases.get(task_id) == phase:
            del phases[task_id]
    return phases


def outcome(report: dict[str, Any]) -> int:
    run = report['run'] or {}
    if not run:
        return EXIT_NOT_APPROVED
    if run.get('state') not in state.TERMINAL_STATES:
        return EXIT_WORKER_DEAD
    statuses = run.get('statuses') or {}
    approved = run.get('state') == 'completed' and statuses and all(s == 'approved' for s in statuses.values())
    return EXIT_APPROVED if approved else EXIT_NOT_APPROVED


def closing_line(report: dict[str, Any]) -> str:
    run = report['run'] or {}
    code = outcome(report)
    if code == EXIT_WORKER_DEAD:
        return 'O worker morreu sem registrar o fim. Retome com wait_for_run (MCP) ou contracanto --resume.'
    if not run:
        return 'Nenhuma execução registrada neste repositório.'
    if code == EXIT_APPROVED:
        return f'Execução {run.get("state")}: todas as tarefas aprovadas.'
    return f'Execução {run.get("state")}: há tarefas não aprovadas; veja os erros acima ou get_run_status.'


class Painter:
    def __init__(self, color: bool) -> None:
        self.color = color

    def paint(self, text: str, key: str | None, width: int | None = None) -> str:
        padded = text.ljust(width) if width else text
        code = COLORS.get(key or '')
        return f'\x1b[{code}m{padded}\x1b[0m' if self.color and code else padded

    def dim(self, text: str) -> str:
        return f'\x1b[2m{text}\x1b[0m' if self.color else text

    def bold(self, text: str) -> str:
        return f'\x1b[1m{text}\x1b[0m' if self.color else text


def _clip(text: str, width: int) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= width else text[: max(0, width - 1)] + '…'


def render(report: dict[str, Any], log: str, now: datetime, width: int = 100, color: bool = False) -> list[str]:
    """Linhas do painel para um relatório de maintenance.status_report."""
    p = Painter(color)
    run = report['run'] or {}
    reservation = report['reservation'] or {}
    if not run:
        waiting = report['reservation_state'] != 'none'
        return [p.bold('Contracanto'), '', 'Aguardando o worker publicar o estado…' if waiting else 'Nenhuma execução.']
    run_state = run.get('state') or '?'
    started = _parse_time(run.get('started_at'))
    elapsed = run.get('duration_seconds') if run_state in state.TERMINAL_STATES else None
    if elapsed is None and started is not None:
        elapsed = (now - started).total_seconds()
    header = f'Contracanto · execução {str(run.get("run_id") or "")[:12]} · '
    lines = [p.bold(header) + p.paint(run_state, run_state) + f' · {_duration(elapsed)}']
    target = run.get('target') or {}
    notes = []
    if target:
        notes.append(f'destino {target.get("branch") or "?"} @ {str(target.get("base") or "")[:12]}')
    if reservation.get('stop_requested'):
        notes.append('encerramento solicitado')
    if reservation and reservation.get('worker_alive') is False and run_state not in state.TERMINAL_STATES:
        notes.append('worker não está mais vivo')
    if run.get('recovery_required'):
        notes.append('exige recuperação manual')
    if notes:
        lines.append(p.dim(' · '.join(notes)))
    lines.append('')

    statuses: dict[str, str] = run.get('statuses') or {}
    timings: dict[str, Any] = run.get('timings') or {}
    details: dict[str, Any] = run.get('details') or {}
    phases = current_phases(log) if run_state not in state.TERMINAL_STATES else {}
    id_width = max([len('TAREFA'), *map(len, statuses)])
    lines.append(p.dim(f'  {"TAREFA":<{id_width}}  {"ESTADO":<10}  {"FASE":<14}  TEMPO'))
    for task_id, status in statuses.items():
        phase = PHASE_NAMES.get(phases.get(task_id, ''), '') if status in state.UNFINISHED_STATUSES else ''
        took = _duration(task_elapsed(timings.get(task_id), now))
        lines.append(f'  {task_id:<{id_width}}  {p.paint(status, status, 10)}  {phase:<14}  {took}')
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status] = counts.get(status, 0) + 1
    order = ('approved', 'review', 'running', 'pending', 'failed', 'blocked', 'cancelled')
    tally = ' · '.join(f'{counts[s]} {s}' for s in order if counts.get(s))
    lines += ['', f'  {sum(counts.values())} tarefa(s): {tally}' if tally else '  sem tarefas']

    errors = [(task_id, detail.get('error')) for task_id, detail in details.items() if isinstance(detail, dict)]
    errors = [(task_id, error) for task_id, error in errors if error]
    if run.get('error'):
        errors.insert(0, ('execução', run['error']))
    if errors:
        lines += ['', p.bold('Erros')]
        for task_id, error in errors:
            lines.append('  ' + p.paint(task_id, 'failed') + ': ' + _clip(str(error), width - len(task_id) - 4))

    events = [line for line in log.splitlines() if line.strip()][-EVENT_LINES:]
    if events:
        lines += ['', p.bold('Últimos eventos')]
        lines += [p.dim('  ' + _clip(line, width - 2)) for line in events]
    return lines


def transitions(previous: dict[str, Any] | None, report: dict[str, Any]) -> list[str]:
    """Mudanças de estado entre dois relatórios, uma por linha, para saída sem terminal."""
    run = report['run'] or {}
    before = (previous or {}).get('run') or {}
    if before.get('run_id') != run.get('run_id'):
        before = {}
    stamp = datetime.now().strftime('%H:%M:%S')
    lines = []
    if run.get('state') != before.get('state') and run.get('state'):
        lines.append(f'{stamp} execução {str(run.get("run_id"))[:12]}: {run["state"]}')
    old: dict[str, str] = before.get('statuses') or {}
    for task_id, status in (run.get('statuses') or {}).items():
        if old.get(task_id) != status:
            lines.append(f'{stamp} {task_id}: {old.get(task_id, "—")} → {status}')
    return lines


def read_log(ws: state.Workspace, report: dict[str, Any]) -> str:
    run_id = (report['run'] or {}).get('run_id')
    if not run_id:
        return ''
    try:
        data = ws.log(run_id).read_bytes()
    except OSError:
        return ''
    return data.decode('utf-8', errors='replace')


def enable_ansi(stream: TextIO) -> bool:
    """Liga as sequências ANSI no console do Windows; devolve se o terminal as aceita."""
    if not stream.isatty():
        return False
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def watch(
    ws: state.Workspace,
    out: TextIO = sys.stdout,
    interval: float = INTERVAL,
    interactive: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Acompanha a execução até ela se resolver e devolve o código de saída.

    Com `interactive` (padrão: saída em um terminal que aceita ANSI) redesenha o painel;
    sem ele, escreve uma linha por mudança e termina com o resumo JSON.
    """
    if interactive is None:
        interactive = enable_ansi(out)
    color = interactive and not os.environ.get('NO_COLOR')
    previous: dict[str, Any] | None = None
    try:
        if interactive:
            out.write('\x1b[?25l\x1b[2J')
        while True:
            report = maintenance.status_report(ws)
            done = maintenance.settled(report)
            if interactive:
                width = max(40, shutil.get_terminal_size().columns - 1)
                lines = render(report, read_log(ws, report), datetime.now(UTC), width, color)
                lines += ['', closing_line(report) if done else 'Ctrl+C fecha o painel; a execução continua.']
                out.write('\x1b[H' + ''.join(line + '\x1b[K\n' for line in lines) + '\x1b[J')
            else:
                for line in transitions(previous, report):
                    out.write(line + '\n')
            out.flush()
            previous = report
            if done:
                break
            sleep(interval)
    except KeyboardInterrupt:
        out.write('\nPainel fechado; a execução continua.\n')
        return EXIT_INTERRUPTED
    finally:
        if interactive:
            out.write('\x1b[?25h')
            out.flush()
    if not interactive:
        out.write(closing_line(report) + '\n')
        out.write(json.dumps(maintenance.summarize(report), ensure_ascii=False, indent=2) + '\n')
    out.flush()
    return outcome(report)
