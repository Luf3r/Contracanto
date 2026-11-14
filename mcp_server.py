"""Conector MCP para o Claude Desktop/Code. Executa um projeto Git local confiável."""
from pathlib import Path
import json
import os
import subprocess
import sys
from mcp.server.fastmcp import FastMCP
from orchestrator import ROOT, STORE, validate, reserve_run, release_run, request_stop, reservation_id, write_json

mcp = FastMCP('Contracanto')

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
        write_json(plan_file, plan)
        argv = [sys.executable, str(ROOT / 'orchestrator.py'), str(plan_file), '--parallel', str(parallel), '--retries', str(retries), '--reservation', token, '--test', *test]
        kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        # Estado anterior não deve ser exibido como resultado da nova execução.
        write_json(STORE / 'status.json', {'state': 'starting', 'run_id': reservation_id(token), 'statuses': {}})
        with (STORE / 'run.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(argv, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, **kw)
    except Exception as error:
        try:
            write_json(STORE / 'status.json', {'state': 'failed', 'run_id': reservation_id(token),
                                             'statuses': {}, 'error': str(error)})
        finally:
            # Falhar ao registrar o erro não deve deixar uma reserva sem worker.
            release_run(token)
        raise
    message = f'Execução iniciada com PID {proc.pid}. Consulte get_run_status. O início não representa aprovação das tarefas.'
    try:
        (STORE/'pid.txt').write_text(str(proc.pid), encoding='utf-8')
    except OSError as error:
        # The worker already owns the run; a diagnostic write must not undo it.
        message += (f'\nAviso: não foi possível salvar pid.txt: {error}. '
                    f'O arquivo pode estar ausente ou desatualizado; use o PID {proc.pid} desta resposta para inspeção.')
    return message

@mcp.tool()
def stop_run(run_id: str | None = None) -> str:
    """Solicita o encerramento da execução ativa. Passe o run_id do status para selecionar uma execução específica."""
    return request_stop(run_id)

@mcp.tool()
def get_run_status() -> str:
    """Retorna status persistido das tasks e as últimas linhas dos logs da execução."""
    path = STORE / 'status.json'
    status = path.read_text(encoding='utf-8') if path.exists() else 'Nenhum estado registrado'
    logfile = STORE / 'run.log'
    log = logfile.read_text(encoding='utf-8')[-4000:] if logfile.exists() else ''
    reservation = STORE / 'run.lock'
    try:
        record = json.loads(reservation.read_text(encoding='utf-8'))
        public = {key: record[key] for key in ('run_id', 'pid') if key in record}
        if record.get('supports_stop') and (STORE / f"stop-{record['run_id']}").is_file():
            public['stop_requested'] = True
        lock = json.dumps(public)
        # Older workers used their credential as their public run_id too.
        if legacy_token := record.get('token'):
            public_id = reservation_id(legacy_token)
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
    path = STORE / 'submitted-plan.json'
    return path.read_text(encoding='utf-8') if path.exists() else 'Nenhum plano recebido'

if __name__ == '__main__':
    mcp.run(transport='stdio')
