"""Linha de comando do worker: executa um plano ou pede o encerramento da execução ativa."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import config, runner, state
from .errors import RunCancelled


def main(argv: Sequence[str] | None = None, ws: state.Workspace | None = None) -> None:
    parser = argparse.ArgumentParser(prog='orchestrator.py')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('plan', type=Path, nargs='?')
    action.add_argument('--stop', action='store_true', help='Solicita o encerramento da execução ativa')
    parser.add_argument('--parallel', type=int, default=config.DEFAULT_PARALLEL)
    parser.add_argument('--retries', type=int, default=config.DEFAULT_RETRIES)
    parser.add_argument('--reservation', help=argparse.SUPPRESS)
    parser.add_argument(
        '--repo',
        type=Path,
        help=f'Repositório Git a orquestrar. Padrão: variável {state.REPO_ENV} ou o diretório desta instalação.',
    )
    parser.add_argument(
        '--keep-worktrees',
        action='store_true',
        help='Preserva worktree e branch das tarefas aprovadas em vez de removê-los após a integração.',
    )
    parser.add_argument(
        '--test',
        nargs=argparse.REMAINDER,
        default=None,
        help='Última opção. Comando confiável executado com as permissões do worker antes da revisão e na integração.',
    )
    args = parser.parse_args(argv)
    try:
        ws = ws or state.default_workspace(args.repo)
    except ValueError as error:
        parser.error(str(error))
    if args.stop:
        print(state.request_stop(ws))
        return
    try:
        config.check_limits(args.parallel, args.retries)
    except ValueError as error:
        parser.error(str(error))
    # Um worker lançado pelo MCP só lê o plano depois de assumir a reserva.
    plan = args.plan if args.reservation else json.loads(args.plan.read_text(encoding='utf-8'))
    result = runner.execute(
        ws,
        plan,
        args.parallel,
        args.retries,
        args.test,
        reservation=args.reservation,
        keep_worktrees=args.keep_worktrees,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(status != 'approved' for status in result.values()):
        sys.exit(1)


def run() -> None:
    try:
        main()
    except RunCancelled as error:
        sys.exit(str(error))
