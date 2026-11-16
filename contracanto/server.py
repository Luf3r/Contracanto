"""Servidor MCP para o Claude Desktop/Code. Executa um projeto Git local confiável."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config, maintenance, process, state
from .plan import validate

mcp = FastMCP('Contracanto')
WORKSPACE = state.default_workspace()
# Diretório que contém o pacote; o worker o recebe no PYTHONPATH para rodar com
# `python -m contracanto` em qualquer repositório, instalado ou não.
PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def _parse_submission(plan_json: str, parallel: int, retries: int, test_command_json: str) -> tuple[Any, list[str]]:
    """Valida a entrada; ValueError descreve o problema para quem submeteu."""
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as error:
        raise ValueError(f'plan_json não é JSON válido: {error}') from error
    validate(plan)
    config.check_limits(parallel, retries)
    try:
        test = json.loads(test_command_json)
    except json.JSONDecodeError as error:
        raise ValueError(f'test_command_json não é JSON válido: {error}') from error
    if not isinstance(test, list) or not test or any(not isinstance(value, str) for value in test):
        raise ValueError('test_command_json deve ser uma lista não vazia de strings')
    return plan, test


def worker_argv(
    ws: state.Workspace, run_id: str, token: str, parallel: int, retries: int, keep_worktrees: bool, test: list[str]
) -> list[str]:
    return [
        sys.executable,
        '-m',
        'contracanto',
        str(ws.plan(run_id)),
        '--repo',
        str(ws.root),
        *(['--keep-worktrees'] if keep_worktrees else []),
        '--parallel',
        str(parallel),
        '--retries',
        str(retries),
        '--reservation',
        token,
        '--test',
        *test,
    ]


def worker_env() -> dict[str, str]:
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get('PYTHONPATH')]))
    return env


@mcp.tool()
def submit_plan(
    plan_json: str, parallel: int = 3, retries: int = 2, keep_worktrees: bool = False, *, test_command_json: str
) -> str:
    """Recebe um plano criado pelo Claude e INICIA sua execução local em segundo plano.
    plan_json: objeto JSON com goal e tasks [{id,description,deps,files,acceptance}].
    test_command_json: lista JSON de argumentos do comando de teste, ex. ["python","-m","pytest"].
    keep_worktrees: preserva worktree e branch das tarefas aprovadas; por padrão são removidos
    após a integração. Os de tarefas que falharam sempre ficam para inspeção.
    Forneça somente projetos, planos e comandos de teste confiáveis. O comando e o código
    testado rodam com as permissões locais do worker antes da revisão e após a integração.
    Podem acessar arquivos fora do worktree, credenciais e rede, inclusive para push ou deploy.
    Entrada inválida ou execução já reservada retornam um texto iniciado por "Erro:" sem
    iniciar nada. Falhas ao lançar o worker são reportadas como erro da ferramenta.
    """
    ws = WORKSPACE
    try:
        plan, test = _parse_submission(plan_json, parallel, retries, test_command_json)
        token = state.reserve_run(ws)
    except (ValueError, RuntimeError) as error:
        return f'Erro: {error}'
    run_id = state.reservation_id(token)
    run = state.RunState(ws.status(run_id), run_id)
    try:
        # A partir daqui as consultas mostram esta execução, não a anterior.
        ws.publish_run(run_id)
        state.write_json(ws.plan(run_id), plan)
        argv = worker_argv(ws, run_id, token, parallel, retries, keep_worktrees, test)
        run.save()
        with ws.log(run_id).open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(
                argv,
                cwd=str(ws.root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=worker_env(),
                **process.detached_options(),
            )
    except Exception as error:
        try:
            run.finish('failed')
            run.error = str(error)
            run.save()
        finally:
            # Falhar ao registrar o erro não deve deixar uma reserva sem worker.
            state.release_run(ws, token)
        raise
    message = (
        f'Execução {run_id} iniciada com PID {proc.pid}. Consulte get_run_status. '
        'O início não representa aprovação das tarefas.'
    )
    try:
        ws.pid(run_id).write_text(str(proc.pid), encoding='utf-8')
    except OSError as error:
        # The worker already owns the run; a diagnostic write must not undo it.
        message += (
            f'\nAviso: não foi possível salvar pid.txt: {error}. '
            f'O arquivo pode estar ausente ou desatualizado; use o PID {proc.pid} desta resposta para inspeção.'
        )
    return message


@mcp.tool()
def stop_run(run_id: str | None = None) -> str:
    """Solicita o encerramento da execução ativa. Passe o run_id do status para selecionar uma execução específica."""
    return state.request_stop(WORKSPACE, run_id)


@mcp.tool()
def get_run_status() -> str:
    """Retorna um objeto JSON com o estado da execução reservada ou, sem reserva, da mais recente.
    run: status persistido (state, run_id, started_at, finished_at, duration_seconds, statuses,
    details, timings por tarefa e fase, recovery_required, error) ou null.
    reservation_state: "active", "none" ou "incomplete"; reservation traz run_id, pid e stop_requested.
    log: últimas linhas do log do worker lançado pelo MCP.
    """
    return json.dumps(maintenance.status_report(WORKSPACE), ensure_ascii=False, indent=2)


@mcp.tool()
def get_submitted_plan() -> str:
    """Exibe o plano da execução reservada ou, sem reserva, da mais recente."""
    ws = WORKSPACE
    run_id = maintenance.selected_run(ws, maintenance.read_reservation(ws)[1])
    if run_id is None or not ws.plan(run_id).exists():
        return 'Nenhum plano recebido'
    return ws.plan(run_id).read_text(encoding='utf-8')


def main() -> None:
    mcp.run(transport='stdio')
