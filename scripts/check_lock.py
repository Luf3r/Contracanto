"""Confere se requirements.lock.txt fixa as mesmas versões declaradas em pyproject.toml.

Sem isso, `pip install -e ".[dev]"` trocaria em silêncio a versão instalada pelo lock.
Uso: python scripts/check_lock.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)')


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def pins(lines: Iterable[str]) -> dict[str, str]:
    """Versões fixadas com `==`, por nome normalizado; marcadores de ambiente são ignorados."""
    found: dict[str, str] = {}
    for line in lines:
        match = PIN.match(line.split('#', 1)[0])
        if match:
            found[normalize(match[1])] = match[2]
    return found


def differences(declared: Iterable[str], lock: Iterable[str]) -> list[str]:
    locked = pins(lock)
    problems = []
    for requirement in declared:
        wanted = pins([requirement])
        if not wanted:
            problems.append(f'{requirement}: a dependência direta precisa de uma versão fixada com ==')
            continue
        ((name, version),) = wanted.items()
        if name not in locked:
            problems.append(f'{name}: declarado em pyproject.toml e ausente do lock')
        elif locked[name] != version:
            problems.append(f'{name}: pyproject.toml fixa {version} e o lock fixa {locked[name]}')
    return problems


def main() -> int:
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    lock = (ROOT / 'requirements.lock.txt').read_text(encoding='utf-8').splitlines()
    problems = differences(project['dependencies'], lock)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print('requirements.lock.txt confere com pyproject.toml')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
