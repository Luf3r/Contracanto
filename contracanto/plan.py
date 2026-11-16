"""Formato do plano, validação e regras de escopo de arquivos."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
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
