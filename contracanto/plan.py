"""Formato do plano, validação e regras de escopo de arquivos."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from . import config

SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
RESERVED_DIRECTORIES = ('.git', '.agent-runs')


class Task(TypedDict):
    id: str
    description: str
    deps: list[str]
    files: list[str]
    acceptance: list[str]


class Plan(TypedDict):
    tasks: list[Task]
    goal: NotRequired[str]


def parse_command(raw: str, name: str) -> list[str]:
    """Comando recebido como lista JSON de argumentos; `name` identifica o campo no erro."""
    try:
        command = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'{name} não é JSON válido: {error}') from error
    if not isinstance(command, list) or not command or any(not isinstance(value, str) for value in command):
        raise ValueError(f'{name} deve ser uma lista não vazia de strings')
    return command


def covers(entry: str, path: str) -> bool:
    """Uma entrada de escopo cobre o próprio caminho e tudo abaixo dele."""
    base = entry.rstrip('/')
    return path == base or path.startswith(base + '/')


def in_scope(files: Iterable[str], path: str) -> bool:
    return any(covers(entry, path) for entry in files)


def overlaps(first: str, second: str) -> bool:
    return covers(first, second) or covers(second, first)


def _check_scope_entry(name: str) -> None:
    parts = name.rstrip('/').split('/')
    if (
        '\\' in name
        or ':' in name
        or any(part in ('', '.', '..') for part in parts)
        or any(part.lower() in RESERVED_DIRECTORIES for part in parts)
    ):
        raise ValueError(f'Caminho de escopo inválido: {name}')


def _check_task(task: dict[str, Any], known_ids: set[str]) -> None:
    description = task.get('description')
    if not isinstance(description, str) or not description.strip() or not isinstance(task.get('deps'), list):
        raise ValueError(f'Tarefa incompleta: {task}')
    if any(not isinstance(dep, str) or dep not in known_ids or dep == task['id'] for dep in task['deps']):
        raise ValueError(f'Dependência inválida: {task["id"]}')
    for key in ('files', 'acceptance'):
        values = task.get(key)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError(f'{key} deve ser lista não vazia de strings')
    for name in task['files']:
        _check_scope_entry(name)


def _check_acyclic(tasks: list[Task]) -> None:
    done: set[str] = set()
    while len(done) < len(tasks):
        possible = {task['id'] for task in tasks if task['id'] not in done and set(task['deps']) <= done}
        if not possible:
            raise ValueError('Ciclo de dependências')
        done |= possible


def validate(plan: Any) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get('tasks'), list):
        raise ValueError('Plano deve ser objeto com lista tasks')
    tasks = plan['tasks']
    if not tasks or len(tasks) > config.MAX_TASKS:
        raise ValueError(f'O plano deve ter entre 1 e {config.MAX_TASKS} tarefas')
    if any(not isinstance(task, dict) or not isinstance(task.get('id'), str) for task in tasks):
        raise ValueError('Cada tarefa precisa de id textual')
    ids = [task['id'] for task in tasks]
    if len({value.casefold() for value in ids}) != len(ids) or not all(SAFE_ID.fullmatch(value) for value in ids):
        raise ValueError('IDs duplicados (sem distinguir maiúsculas/minúsculas) ou inválidos')
    known_ids = set(ids)
    for task in tasks:
        _check_task(task, known_ids)
    _check_acyclic(tasks)


# Comandos de verificação que um critério de aceitação costuma citar.
CITED_COMMAND = re.compile(
    r'(?<![\w./-])(?:'
    r'(?:npm|pnpm|yarn|bun)(?:\s+run)?\s+[a-z][\w:.-]*'
    r'|npx\s+[@\w][\w@/.:-]*'
    r'|(?:python3?|py)\s+-m\s+[a-z][\w.]*'
    r'|cargo\s+[a-z]+|go\s+test|make\s+[a-z][\w.-]*|wrangler\s+[a-z]+'
    r'|pytest|vitest|jest|tsc|eslint|prettier|ruff|mypy'
    r')(?![\w-])',
    re.IGNORECASE,
)


def _canonical(command: str) -> str:
    """Forma comparável de um comando: minúsculas, sem `run` e com um só nome para o Python."""
    words = command.lower().split()
    if words and words[0] in ('py', 'python3'):
        words[0] = 'python'
    if len(words) > 2 and words[0] in ('npm', 'pnpm', 'yarn', 'bun') and words[1] == 'run':
        del words[1]
    return ' '.join(words)


def _test_line(test: list[str]) -> str:
    # O executável pode vir como caminho completo, como C:/.../npm.cmd.
    return _canonical(' '.join([Path(test[0]).stem, *test[1:]]))


def warnings(plan: Plan, test: list[str], root: Path) -> list[str]:
    """Avisos que não bloqueiam a submissão: tarefas largas demais ou critérios que o comando
    de testes não comprova. Cabe ao arquiteto decidir se replaneja."""
    found: list[str] = []
    test_line = f' {_test_line(test)} '
    shown = ' '.join(test)
    for task in plan['tasks']:
        task_id = task['id']
        roots = sorted(
            {
                entry.rstrip('/')
                for entry in task['files']
                if '/' not in entry.rstrip('/') and (root / entry.rstrip('/')).is_dir()
            }
        )
        if len(roots) >= 2:
            found.append(
                f'{task_id}: files inclui diretórios raiz inteiros ({", ".join(roots)}); '
                'divida a tarefa por módulo para caber na revisão'
            )
        if len(task['files']) > config.TASK_FILES_WARNING:
            found.append(
                f'{task_id}: files tem {len(task["files"])} entradas, acima de {config.TASK_FILES_WARNING}; '
                'uma tarefa grande tende a passar do limite de revisão'
            )
        for criterion in task['acceptance']:
            for match in CITED_COMMAND.finditer(criterion):
                cited = _canonical(match.group(0))
                if f' {cited} ' not in test_line:
                    found.append(
                        f'{task_id}: o critério "{criterion[:100]}" cita `{match.group(0)}`, fora do comando de '
                        f'testes (`{shown}`); só esse comando roda, e o revisor rejeita o critério que ele não comprovar'
                    )
                    break
    return found


def remaining(plan: Plan, statuses: Mapping[str, str]) -> Plan:
    """O plano sem as tarefas já aprovadas; as dependências delas deixam de ser exigidas."""
    done = {task_id for task_id, status in statuses.items() if status == 'approved'}
    tasks: list[Task] = []
    for task in plan['tasks']:
        if task['id'] not in done:
            pending = dict(task, deps=[dep for dep in task['deps'] if dep not in done])
            tasks.append(pending)  # type: ignore[arg-type]
    result = dict(plan, tasks=tasks)
    return result  # type: ignore[return-value]
