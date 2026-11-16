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


def _report(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False, indent=2)


def _parse_submission(plan_json: str, parallel: int, retries: int, test_command_json: str):
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


@mcp.tool()
def submit_plan(plan_json: str, parallel: int = 3, retries: int = 2, *, test_command_json: str) -> str:
    """Recebe um plano criado pelo Claude e INICIA sua execução local em segundo plano.
    plan_json: objeto JSON com goal e tasks [{id,description,deps,files,acceptance}].
    test_command_json: lista JSON de argumentos do comando de teste, ex. ["python","-m","pytest"].
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
        argv = [
            sys.executable,
            str(WORKER_SCRIPT),
            str(ws.plan(run_id)),
            '--parallel',
            str(parallel),
            '--retries',
            str(retries),
            '--reservation',
            token,
            '--test',
            *test,
        ]
        run.save()
        with ws.log(run_id).open('w', encoding='utf-8') as log:
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


def _reservation(ws: state.Workspace) -> tuple[str, dict | None]:
    try:
        record = json.loads(ws.lock.read_text(encoding='utf-8'))
    except FileNotFoundError:
        # Normal completion may release the reservation while status is polled.
        return 'none', None
    except json.JSONDecodeError:
        # Polling may observe an exclusive file before its creator finishes writing.
        return 'incomplete', None
    # Only the public identifier leaves this process, never the worker's credential.
    public = {key: record[key] for key in ('run_id', 'pid') if key in record}
    run_id = public.get('run_id')
    if isinstance(run_id, str) and state.RUN_ID.fullmatch(run_id):
        public['stop_requested'] = bool(record.get('supports_stop')) and ws.stop_file(run_id).is_file()
    else:
        public.pop('run_id', None)
    return 'active', public


def _selected_run(ws: state.Workspace, reservation: dict | None) -> str | None:
    """A execução reservada tem precedência sobre a última publicada."""
    return (reservation or {}).get('run_id') or ws.latest_run_id()


def _read_log(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ''
    try:
        return data.decode('utf-8')[-LOG_TAIL:]
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')[-LOG_TAIL:]


@mcp.tool()
def get_run_status() -> str:
    """Retorna um objeto JSON com o estado da execução reservada ou, sem reserva, da mais recente.
    run: status persistido (state, run_id, statuses, details, recovery_required, error) ou null.
    reservation_state: "active", "none" ou "incomplete"; reservation traz run_id, pid e stop_requested.
    log: últimas linhas do log do worker lançado pelo MCP.
    """
    ws = WORKSPACE
    reservation_state, reservation = _reservation(ws)
    run_id = _selected_run(ws, reservation)
    run = None
    log = ''
    if run_id is not None:
        try:
            run = json.loads(ws.status(run_id).read_text(encoding='utf-8'))
        except FileNotFoundError:
            run = None
        log = _read_log(ws.log(run_id))
    return _report(run=run, reservation_state=reservation_state, reservation=reservation, log=log)


@mcp.tool()
def get_submitted_plan() -> str:
    """Exibe o plano da execução reservada ou, sem reserva, da mais recente."""
    ws = WORKSPACE
    run_id = _selected_run(ws, _reservation(ws)[1])
    if run_id is None or not ws.plan(run_id).exists():
        return 'Nenhum plano recebido'
    return ws.plan(run_id).read_text(encoding='utf-8')


if __name__ == '__main__':
    mcp.run(transport='stdio')
