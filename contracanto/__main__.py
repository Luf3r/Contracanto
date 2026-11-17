"""Pontos de entrada: `contracanto`, `contracanto-mcp` e `python -m contracanto`.

Os ajustes das variáveis CONTRACANTO_* são lidos ao importar o pacote; um valor
inválido vira uma linha de erro em vez de uma pilha de chamadas.
"""

from __future__ import annotations

import sys


def _exit_on_bad_setting(error: ValueError) -> None:
    from .errors import ConfigError

    if not isinstance(error, ConfigError):
        raise error
    sys.exit(f'Erro: {error}')


def main() -> None:
    try:
        from .cli import run
    except ValueError as error:
        _exit_on_bad_setting(error)
    else:
        run()


def serve() -> None:
    try:
        from .server import main as run_server
    except ValueError as error:
        _exit_on_bad_setting(error)
    else:
        run_server()


if __name__ == '__main__':
    main()
