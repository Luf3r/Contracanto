"""Servidor MCP para o Claude Desktop/Code. Executa um projeto Git local confiável."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config, gitops, maintenance, process, state
from .plan import parse_command, validate
from .runner import check_repository

mcp = FastMCP('Contracanto')
WORKSPACE = state.default_workspace()
# Diretório que contém o pacote; o worker o recebe no PYTHONPATH para rodar com
# `python -m contracanto` em qualquer repositório, instalado ou não.
PACKAGE_PARENT = Path(__file__).resolve().parent.parent
# Segundos: teto de uma espera de wait_for_run e intervalo entre as leituras do estado.
WAIT_LIMIT = 600
WAIT_POLL_INTERVAL = 1.0


def _parse_options(
    parallel: int, retries: int, test_command_json: str, setup_command_json: str = ''
) -> tuple[list[str], list[str] | None]:
    """Valida limites e comandos; ValueError descreve o problema para quem submeteu."""
    config.check_limits(parallel, retries)
    test = parse_command(test_command_json, 'test_command_json')
    setup = parse_command(setup_command_json, 'setup_command_json') if setup_command_json.strip() else None
    config.check_pinned(test, setup)
    return test, setup


def _parse_submission(
    plan_json: str, parallel: int, retries: int, test_command_json: str, setup_command_json: str = ''
) -> tuple[Any, list[str], list[str] | None]:
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as error:
        raise ValueError(f'plan_json não é JSON válido: {error}') from error
    validate(plan)
    return plan, *_parse_options(parallel, retries, test_command_json, setup_command_json)


def worker_argv(
    ws: state.Workspace,
    run_id: str,
    token: str,
    parallel: int,
    retries: int,
    keep_worktrees: bool,
    test: list[str],
    setup: list[str] | None = None,
    branch: str | None = None,
) -> list[str]:
    return [
        sys.executable,
        '-m',
        'contracanto',
        str(ws.plan(run_id)),
        '--repo',
        str(ws.root),
        *(['--keep-worktrees'] if keep_worktrees else []),
        *(['--setup-json', json.dumps(setup)] if setup else []),
        *(['--branch', branch] if branch else []),
        '--parallel',
        str(parallel),
        '--retries',
        str(retries),
        '--reservation',
        token,
        '--test',
        *test,
    ]


def watch_command(ws: state.Workspace) -> list[str]:
    """Comando que acompanha a execução e sai quando ela termina (`contracanto --watch`).

    Usa o executável `contracanto` instalado ao lado deste Python, que não depende de
    PYTHONPATH; sem ele, o próprio Python com `-m contracanto`.
    """
    scripts = Path(sys.executable).parent
    for name in ('contracanto.exe', 'contracanto'):
        if (scripts / name).is_file():
            return [(scripts / name).as_posix(), '--watch', '--repo', ws.root.as_posix()]
    return [Path(sys.executable).as_posix(), '-m', 'contracanto', '--watch', '--repo', ws.root.as_posix()]


def watch_command_line(ws: state.Workspace) -> str:
    # Barras normais e aspas duplas valem tanto no Git Bash do Claude Code quanto no cmd.
    return ' '.join(f'"{part}"' if ' ' in part or not part else part for part in watch_command(ws))


def worker_env() -> dict[str, str]:
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get('PYTHONPATH')]))
    return env


@mcp.tool()
def submit_plan(
    plan_json: str,
    parallel: int = 3,
    retries: int = 2,
    keep_worktrees: bool = False,
    setup_command_json: str = '',
    branch: str = '',
    *,
    test_command_json: str,
) -> str:
    """Recebe um plano criado pelo Claude e INICIA sua execução local em segundo plano.
    plan_json: objeto JSON com goal e tasks [{id,description,deps,files,acceptance}].
    id e description são strings; deps é uma lista de ids (pode ser []); files e acceptance
    são listas não vazias de strings, mesmo com um item só, ex. "files": ["src/app.py"],
    "acceptance": ["testes passam"].
    test_command_json: lista JSON de argumentos do comando de teste, ex. ["python","-m","pytest"].
    keep_worktrees: preserva worktree e branch das tarefas aprovadas; por padrão são removidos
    após a integração. Os de tarefas que falharam sempre ficam para inspeção.
    setup_command_json: opcional; lista JSON de argumentos de um comando executado em cada
    worktree antes da implementação, ex. ["npm","ci"], para instalar o que o Git ignora.
    Só pode criar arquivos ignorados pelo Git e é tão confiável quanto o comando de teste.
    branch: opcional; nome de uma branch nova, criada no commit atual, que recebe os commits
    aprovados em vez da branch em uso. O checkout do repositório fica nela ao final.
    O operador pode fixar os comandos aceitos (CONTRACANTO_TEST_COMMAND e
    CONTRACANTO_SETUP_COMMAND); um comando diferente é recusado com o valor esperado.
    A resposta traz o comando `contracanto --watch` desta execução: rodado em segundo plano,
    ele sai quando a execução termina e avisa quem o lançou, sem consultas repetidas.
    Forneça somente projetos, planos e comandos de teste confiáveis. O comando e o código
    testado rodam com as permissões locais do worker antes da revisão e após a integração.
    Podem acessar arquivos fora do worktree, credenciais e rede, inclusive para push ou deploy.
    Entrada inválida, execução já reservada ou repositório que não pode receber a execução
    (alterações pendentes, HEAD destacado sem branch, nome de branch inválido ou já usado)
    retornam um texto iniciado por "Erro:" sem iniciar nada. Falhas ao lançar o worker são reportadas como erro da ferramenta.
    """
    try:
        plan, test, setup = _parse_submission(plan_json, parallel, retries, test_command_json, setup_command_json)
    except ValueError as error:
        return f'Erro: {error}'
    return _launch(plan, parallel, retries, keep_worktrees, test, setup, branch)


def _launch(
    plan: Any,
    parallel: int,
    retries: int,
    keep_worktrees: bool,
    test: list[str],
    setup: list[str] | None,
    branch: str,
    auto_resumes: int = 0,
    resumed_from: str | None = None,
) -> str:
    """Reserva a execução e lança o worker destacado para um plano já validado."""
    ws = WORKSPACE
    try:
        # O worker confere de novo; aqui o erro chega a quem submeteu sem criar uma execução.
        check_repository(ws.root, branch.strip() or None)
        token = state.reserve_run(ws)
    except RuntimeError as error:
        return f'Erro: {error}'
    run_id = state.reservation_id(token)
    run = state.RunState(ws.status(run_id), run_id)
    try:
        # A partir daqui as consultas mostram esta execução, não a anterior.
        ws.publish_run(run_id)
        state.write_json(ws.plan(run_id), plan)
        state.write_json(
            ws.options(run_id),
            {
                'parallel': parallel,
                'retries': retries,
                'keep_worktrees': keep_worktrees,
                'test': test,
                'setup': setup,
                'branch': branch.strip() or None,
                'auto_resumes': auto_resumes,
                'resumed_from': resumed_from,
            },
        )
        argv = worker_argv(ws, run_id, token, parallel, retries, keep_worktrees, test, setup, branch.strip() or None)
        run.save()
        # O log começa vazio; o worker, lançado por outro processo, acrescenta a ele.
        ws.log(run_id).write_text('', encoding='utf-8')
        pid, outside_job = process.launch_detached(argv, log=ws.log(run_id), cwd=str(ws.root), env=worker_env())
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
        f'Execução {run_id} iniciada com PID {pid}. Acompanhe com wait_for_run ou get_run_status. '
        'O início não representa aprovação das tarefas.'
        '\nPara ser avisado do fim sem consultar, rode em segundo plano (no Claude Code, Bash com '
        f'run_in_background): {watch_command_line(ws)}\n'
        'O comando escreve uma linha por mudança de estado e sai quando a execução termina, com o '
        'resumo de wait_for_run: código 0 se todas as tarefas foram aprovadas, 1 se alguma não foi, '
        '3 se o worker morreu. Com 3, chame wait_for_run, que relança as tarefas que faltam.'
    )
    if not outside_job:
        message += (
            '\nAviso: o worker ficou dentro de um Job Object, provavelmente o do app do Claude, porque a '
            'WMI não pôde criá-lo fora dele (veja run.log). Se o app ou o servidor MCP forem encerrados, '
            'o worker e os agentes podem morrer junto; wait_for_run relança as tarefas que faltarem.'
        )
    try:
        ws.pid(run_id).write_text(str(pid), encoding='utf-8')
    except OSError as error:
        # O worker já é dono da execução; uma gravação de diagnóstico não pode desfazer isso.
        message += (
            f'\nAviso: não foi possível salvar pid.txt: {error}. '
            f'O arquivo pode estar ausente ou desatualizado; use o PID {pid} desta resposta para inspeção.'
        )
    return message


@mcp.tool()
def resume_run(
    parallel: int = 3,
    retries: int = 2,
    keep_worktrees: bool = False,
    setup_command_json: str = '',
    branch: str = '',
    run_id: str = '',
    *,
    test_command_json: str,
) -> str:
    """INICIA uma nova execução só com as tarefas não aprovadas de uma execução encerrada.

    run_id: execução de origem; vazio usa a mais recente. As tarefas aprovadas nela saem do
    plano e deixam de ser exigidas como dependência, por isso os commits delas precisam estar
    no HEAD atual do repositório. Os demais parâmetros e avisos são os de submit_plan.
    Se a reserva pertence a um worker que morreu no meio da execução, ela é liberada antes,
    com as mesmas conferências de --unlock.
    Retorna um texto iniciado por "Erro:" quando não há o que retomar.
    """
    try:
        test, setup = _parse_options(parallel, retries, test_command_json, setup_command_json)
        maintenance.release_dead_reservation(WORKSPACE)
        plan = maintenance.resume_plan(WORKSPACE, run_id.strip() or None)
    except (ValueError, RuntimeError) as error:
        return f'Erro: {error}'
    return _launch(plan, parallel, retries, keep_worktrees, test, setup, branch)


@mcp.tool()
def stop_run(run_id: str | None = None) -> str:
    """Solicita o encerramento da execução ativa. Passe o run_id do status para selecionar uma execução específica."""
    return state.request_stop(WORKSPACE, run_id)


@mcp.tool()
def get_run_status() -> str:
    """Retorna um objeto JSON com o estado da execução reservada ou, sem reserva, da mais recente.
    run: status persistido (state, run_id, started_at, finished_at, duration_seconds, statuses,
    details, timings por tarefa e fase, recovery_required, error) ou null.
    reservation_state: "active", "none" ou "incomplete"; reservation traz run_id, pid, stop_requested
    e worker_alive. Com worker_alive false a execução não vai mais avançar sozinha: chame
    wait_for_run, que tenta a retomada automática, e avise o operador se ela não for possível.
    Para apenas esperar o término, prefira wait_for_run, que devolve um resumo curto.
    log: últimas linhas do log do worker lançado pelo MCP.
    """
    return json.dumps(maintenance.status_report(WORKSPACE), ensure_ascii=False, indent=2)


def _auto_resume(report: dict[str, Any]) -> dict[str, Any]:
    """Relança, com as tarefas que faltam, a execução cujo worker morreu.

    Devolve `resumed_from` e `attempt` quando relançou, ou `error` com o motivo
    de não ter relançado. Nada é forçado: valem as conferências de --unlock e
    de --resume, e o limite CONTRACANTO_AUTO_RESUME por cadeia de retomadas.
    """
    ws = WORKSPACE
    run_id = (report['reservation'] or {}).get('run_id')
    options = maintenance.read_options(ws, run_id) if run_id else None
    if run_id is None or options is None:
        return {'error': 'A execução não registrou as opções necessárias para ser retomada'}
    attempts = int(options.get('auto_resumes') or 0)
    if attempts >= config.AUTO_RESUME:
        return {'error': f'Limite de retomadas automáticas atingido ({config.AUTO_RESUME})', 'from': run_id}
    try:
        maintenance.release_dead_reservation(ws)
        plan = maintenance.resume_plan(ws, run_id)
        config.check_pinned(options['test'], options.get('setup'))
        branch = options.get('branch') or ''
        if branch and gitops.branch_ref(ws.root) == f'refs/heads/{branch}':
            # O worker anterior já criou a branch de integração e deixou o checkout nela.
            branch = ''
    except (RuntimeError, ValueError) as error:
        return {'error': str(error), 'from': run_id}
    message = _launch(
        plan,
        options['parallel'],
        options['retries'],
        bool(options.get('keep_worktrees')),
        options['test'],
        options.get('setup'),
        branch,
        auto_resumes=attempts + 1,
        resumed_from=run_id,
    )
    if message.startswith('Erro:'):
        return {'error': message.removeprefix('Erro: '), 'from': run_id}
    return {'resumed_from': run_id, 'attempt': attempts + 1}


@mcp.tool()
async def wait_for_run(timeout_seconds: int = 120) -> str:
    """Espera a execução terminar e retorna um resumo JSON, em vez de consultar repetidamente.

    Retorna assim que a execução chega a um estado final, o worker morre ou não há execução
    reservada; caso contrário, depois de timeout_seconds (1 a 600), com timed_out true. Nesse
    caso chame de novo. Mantenha timeout_seconds abaixo do tempo limite de ferramentas do cliente.
    O resumo traz run_id, state, statuses, target (branch de destino e commit de partida),
    task_errors, recovery_required, error,
    duration_seconds, reservation_state, worker_alive e stop_requested. Para pareceres, tempos
    por fase e log, use get_run_status.
    Se o worker morreu sem registrar o estado final, a ferramenta libera a reserva e relança
    sozinha as tarefas que faltavam, desde que o repositório esteja limpo e sem operação Git
    pendente; a espera segue na execução nova e auto_resume traz resumed_from e attempt. Quando
    não é possível relançar, auto_resume.error diz o motivo e o operador precisa intervir.
    """
    deadline = time.monotonic() + max(1, min(timeout_seconds, WAIT_LIMIT))
    resumed: dict[str, Any] = {}
    while True:
        report = maintenance.status_report(WORKSPACE)
        if not resumed and maintenance.crashed(report):
            resumed = _auto_resume(report)
            if 'error' not in resumed:
                # A espera continua na execução nova.
                continue
        done = maintenance.settled(report)
        if done or time.monotonic() >= deadline:
            break
        await asyncio.sleep(WAIT_POLL_INTERVAL)
    summary = {**maintenance.summarize(report), 'timed_out': not done}
    if resumed:
        summary['auto_resume'] = resumed
    return json.dumps(summary, ensure_ascii=False, indent=2)


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


if __name__ == '__main__':
    main()
