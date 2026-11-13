"""Conector MCP para o Claude Desktop/Code. Executa um projeto Git local confiável."""
from pathlib import Path
import json
import os
import subprocess
import sys
from mcp.server.fastmcp import FastMCP
from orchestrator import ROOT, STORE, validate, reserve_run, release_run

mcp = FastMCP('Claude Antigravity Orchestrator')

@mcp.tool()
def submit_plan(plan_json: str, parallel: int = 3, retries: int = 2, test_command_json: str = '[]') -> str:
    """Recebe um plano criado pelo Claude e INICIA sua execução local em segundo plano.
    plan_json: objeto JSON com goal e tasks [{id,description,deps,files,acceptance}].
    test_command_json: lista JSON de argumentos do comando de teste, ex. ["python","-m","pytest"].
    ATENÇÃO: Apenas projetos Git locais confiáveis e sem segredos; não executa deploy.
    """
    plan = json.loads(plan_json)
    validate(plan)
    if parallel not in range(1, 9) or retries not in range(4):
        return 'Erro: parallel 1..8; retries 0..3'
    test = json.loads(test_command_json)
    if not isinstance(test, list) or not test or any(not isinstance(v, str) for v in test):
        return 'Erro: test_command_json deve ser uma lista de strings'
    try:
        token = reserve_run()
    except RuntimeError as error:
        return str(error)
    try:
        plan_file = STORE / 'submitted-plan.json'
        plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
        argv = [sys.executable, str(ROOT / 'orchestrator.py'), str(plan_file), '--parallel', str(parallel), '--retries', str(retries), '--reservation', token, '--test', *test]
        kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        # Estado anterior não deve ser exibido como resultado da nova execução.
        (STORE / 'status.json').write_text(json.dumps({'state': 'starting', 'statuses': {}}), encoding='utf-8')
        with (STORE / 'run.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(argv, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, **kw)
    except Exception:
        release_run(token)
        raise
    (STORE/'pid.txt').write_text(str(proc.pid), encoding='utf-8')
    return f'Execução iniciada com PID {proc.pid}. Consulte get_run_status. O início não representa aprovação das tarefas.'

@mcp.tool()
def get_run_status() -> str:
    """Retorna status persistido das tasks e as últimas linhas dos logs da execução."""
    path = STORE / 'status.json'
    status = path.read_text(encoding='utf-8') if path.exists() else 'Nenhum estado registrado'
    logfile = STORE / 'run.log'
    log = logfile.read_text(encoding='utf-8')[-4000:] if logfile.exists() else ''
    reservation = STORE / 'run.lock'
    lock = reservation.read_text(encoding='utf-8') if reservation.exists() else 'Sem reserva ativa'
    return status + '\n--- RESERVA ---\n' + lock + '\n--- LOG ---\n' + log

@mcp.tool()
def get_submitted_plan() -> str:
    """Exibe o plano de execução salvo atualmente."""
    path = STORE / 'submitted-plan.json'
    return path.read_text(encoding='utf-8') if path.exists() else 'Nenhum plano recebido'

if __name__ == '__main__':
    mcp.run(transport='stdio')
