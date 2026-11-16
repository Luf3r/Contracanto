"""Conector MCP para o Claude Desktop/Code. Executa um projeto Git local confiável."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from contracanto import config, process, state
from contracanto.plan import validate

mcp = FastMCP('Contracanto')
WORKSPACE = state.default_workspace()
WORKER_SCRIPT = Path(__file__).resolve().parent / 'orchestrator.py'
LOG_TAIL = 4000


@mcp.tool()
def submit_plan(plan_json: str, parallel: int = 3, retries: int = 2, *, test_command_json: str) -> str:
    """Recebe um plano criado pelo Claude e INICIA sua execução local em segundo plano.
    plan_json: objeto JSON com goal e tasks [{id,description,deps,files,acceptance}].
    test_command_json: lista JSON de argumentos do comando de teste, ex. ["python","-m","pytest"].
    Forneça somente projetos, planos e comandos de teste confiáveis. O comando e o código
    testado rodam com as permissões locais do worker antes da revisão e após a integração.
    Podem acessar arquivos fora do worktree, credenciais e rede, inclusive para push ou deploy.
    """
    ws = WORKSPACE
    plan = json.loads(plan_json)
    validate(plan)
    try:
        config.check_limits(parallel, retries)
    except ValueError as error:
        return f'Erro: {error}'
    test = json.loads(test_command_json)
    if not isinstance(test, list) or not test or any(not isinstance(v, str) for v in test):
        return 'Erro: test_command_json deve ser uma lista não vazia de strings'
    try:
        token = state.reserve_run(ws)
    except RuntimeError as error:
        return str(error)
    run = state.RunState(ws.status, state.reservation_id(token))
    try:
        state.write_json(ws.plan, plan)
        argv = [
            sys.executable,
            str(WORKER_SCRIPT),
            str(ws.plan),
            '--parallel',
            str(parallel),
            '--retries',
            str(retries),
            '--reservation',
            token,
            '--test',
            *test,
        ]
        # Estado anterior não deve ser exibido como resultado da nova execução.
        run.save()
        with ws.log.open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(
                argv,
                cwd=str(ws.root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                **process.detached_options(),
            )
    except Exception as error:
        try:
            run.state = 'failed'
            run.error = str(error)
            run.save()
        finally:
            # Falhar ao registrar o erro não deve deixar uma reserva sem worker.
            state.release_run(ws, token)
        raise
    message = (
        f'Execução iniciada com PID {proc.pid}. Consulte get_run_status. O início não representa aprovação das tarefas.'
    )
    try:
        ws.pid.write_text(str(proc.pid), encoding='utf-8')
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
    """Retorna status persistido das tasks e as últimas linhas dos logs da execução."""
    ws = WORKSPACE
    status = ws.status.read_text(encoding='utf-8') if ws.status.exists() else 'Nenhum estado registrado'
    try:
        data = ws.log.read_bytes()
    except FileNotFoundError:
        data = b''
    try:
        log = data.decode('utf-8')[-LOG_TAIL:]
    except UnicodeDecodeError:
        log = data.decode('cp1252', errors='replace')[-LOG_TAIL:]
    try:
        record = json.loads(ws.lock.read_text(encoding='utf-8'))
        public = {key: record[key] for key in ('run_id', 'pid') if key in record}
        if record.get('supports_stop') and ws.stop_file(record['run_id']).is_file():
            public['stop_requested'] = True
        lock = json.dumps(public)
        # Older workers used their credential as their public run_id too.
        if legacy_token := record.get('token'):
            public_id = state.reservation_id(legacy_token)
            status = status.replace(legacy_token, public_id)
            log = log.replace(legacy_token, public_id)
    except FileNotFoundError:
        # Normal completion may release the reservation while status is polled.
        lock = 'Sem reserva ativa'
    except json.JSONDecodeError:
        # Polling may observe an exclusive file before its creator finishes writing.
        lock = 'Reserva presente; registro incompleto'
    return status + '\n--- RESERVA ---\n' + lock + '\n--- LOG ---\n' + log


@mcp.tool()
def get_submitted_plan() -> str:
    """Exibe o plano de execução salvo atualmente."""
    return WORKSPACE.plan.read_text(encoding='utf-8') if WORKSPACE.plan.exists() else 'Nenhum plano recebido'


if __name__ == '__main__':
    mcp.run(transport='stdio')
