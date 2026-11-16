"""Roda a suíte em paralelo, um processo por teste, e resume o resultado.

Uso: python scripts/run_tests.py [-j N] [-v] [--timeout S] [FILTRO ...]

Cada teste roda em um interpretador próprio, então nada é compartilhado entre
eles além do sistema de arquivos. FILTRO seleciona testes cujo identificador
contém o texto (por exemplo `test_scope_gates` ou `worktree`). Um teste que
passa do tempo limite é encerrado com todos os seus subprocessos e conta como
falha, com o nome dele no resumo.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / 'tests'
sys.path.insert(0, str(ROOT))

# A contenção do próprio pacote encerra a árvore inteira de um teste travado.
from contracanto import process  # noqa: E402

DEFAULT_TIMEOUT = 300.0
SKIPPED = re.compile(r'skipped=(\d+)')


def discover() -> list[str]:
    sys.path[:0] = [str(TESTS), str(ROOT)]
    suite = unittest.TestLoader().discover(str(TESTS), top_level_dir=str(TESTS))
    ids: list[str] = []

    def walk(node: unittest.TestSuite | unittest.TestCase) -> None:
        if isinstance(node, unittest.TestSuite):
            for child in node:
                walk(child)
        elif type(node).__name__ == '_FailedTest':
            # Erro de importação: roda o módulo inteiro para que a falha apareça.
            ids.append(node.id().removeprefix('unittest.loader._FailedTest.'))
        else:
            ids.append(node.id())

    walk(suite)
    return ids


def run_one(test_id: str, timeout: float) -> tuple[str, int, str, float]:
    path = os.pathsep.join(filter(None, [str(TESTS), str(ROOT), os.environ.get('PYTHONPATH')]))
    started = time.monotonic()
    try:
        result = process.run(
            [sys.executable, '-m', 'unittest', test_id],
            cwd=ROOT,
            timeout=timeout,
            check=False,
            env_overrides={'PYTHONPATH': path},
        )
        code, output = result.returncode, result.stderr + result.stdout
    except subprocess.TimeoutExpired:
        code = 1
        output = f'Tempo limite de {timeout:g}s excedido; o teste e seus subprocessos foram encerrados.'
    return test_id, code, output, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 4, help='Processos simultâneos')
    parser.add_argument('-v', '--verbose', action='store_true', help='Mostra cada teste e o tempo')
    parser.add_argument(
        '--timeout', type=float, default=DEFAULT_TIMEOUT, help='Segundos por teste (padrão: %(default)g)'
    )
    parser.add_argument('filters', nargs='*', help='Seleciona testes cujo identificador contém o texto')
    args = parser.parse_args()
    ids = [test_id for test_id in discover() if not args.filters or any(f in test_id for f in args.filters)]
    if not ids:
        print('Nenhum teste selecionado.')
        return 1
    started = time.monotonic()
    failures: list[tuple[str, str]] = []
    skipped = 0
    slowest: list[tuple[float, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for test_id, code, output, elapsed in pool.map(lambda test_id: run_one(test_id, args.timeout), ids):
            slowest.append((elapsed, test_id))
            if match := SKIPPED.search(output.rsplit('\n----', 1)[-1]):
                skipped += int(match.group(1))
            if code:
                failures.append((test_id, output))
            if args.verbose:
                print(f'{"FALHOU" if code else "ok":>6}  {elapsed:6.1f}s  {test_id}', flush=True)
            elif not args.verbose:
                print('F' if code else '.', end='', flush=True)
    print()
    for test_id, output in failures:
        print('=' * 70)
        print(f'FALHOU: {test_id}')
        print(output.strip())
    print('-' * 70)
    elapsed = time.monotonic() - started
    print(f'{len(ids)} testes em {elapsed:.1f}s com {args.jobs} processos; {skipped} pulados; {len(failures)} falhas')
    if args.verbose:
        print('Mais lentos: ' + ', '.join(f'{name} ({t:.0f}s)' for t, name in sorted(slowest, reverse=True)[:5]))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
