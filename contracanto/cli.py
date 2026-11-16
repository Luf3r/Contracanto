"""Linha de comando: executa um plano ou opera a execução ativa e os registros."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import config, maintenance, runner, state
from .errors import RunCancelled


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='contracanto',
        description='Orquestra a implementação de um plano pelo Antigravity e a revisão pelo Claude.',
        epilog='Tempos e modelo do revisor: variáveis ' + ', '.join(config.ENV_PREFIX + n for n in config.TUNABLE),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('plan', type=Path, nargs='?', help='Arquivo JSON do plano a executar')
    action.add_argument('--stop', action='store_true', help='Solicita o encerramento da execução ativa')
    action.add_argument('--status', action='store_true', help='Mostra o estado da execução ativa ou da mais recente')
    action.add_argument(
        '--unlock',
        action='store_true',
        help='Remove a reserva de um worker que não está mais em execução, com o repositório limpo',
    )
    action.add_argument(
        '--prune-runs',
        type=int,
        metavar='N',
        help='Remove os registros das execuções encerradas, mantendo as N mais recentes',
    )
    parser.add_argument('--parallel', type=int, default=config.DEFAULT_PARALLEL)
    parser.add_argument('--retries', type=int, default=config.DEFAULT_RETRIES)
    parser.add_argument('--reservation', help=argparse.SUPPRESS)
    parser.add_argument(
        '--repo',
        type=Path,
        help=f'Repositório Git a orquestrar. Padrão: variável {state.REPO_ENV}, o projeto desta cópia ou o diretório atual.',
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
    return parser


def main(argv: Sequence[str] | None = None, ws: state.Workspace | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        ws = ws or state.default_workspace(args.repo)
    except ValueError as error:
        parser.error(str(error))
    if args.stop:
        print(state.request_stop(ws))
        return
    if args.status:
        print(json.dumps(maintenance.status_report(ws), ensure_ascii=False, indent=2))
        return
    if args.unlock:
        message = maintenance.unlock(ws)
        print(message)
        if message.startswith('Recusado'):
            sys.exit(1)
        return
    if args.prune_runs is not None:
        try:
            result = maintenance.prune_runs(ws, args.prune_runs)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
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
