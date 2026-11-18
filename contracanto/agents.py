"""Chamadas aos CLIs externos: Antigravity implementa e corrige, Claude revisa."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, gitops, process
from .errors import RecoveryRequired, RunCancelled, describe
from .plan import Task
from .state import RunContext, StopSignal

log = logging.getLogger('contracanto')

IMPLEMENT_PROMPT = (
    'Você está implementando UMA tarefa no Git worktree atual. '
    'Leia GEMINI.md quando presente. Preserve a branch atual. '
    'Não altere a arquitetura além do que a tarefa pede. Não faça commit/push/deploy. '
    'Não acesse segredos. NÃO altere arquivos fora da lista files. '
    'Se houver conflito de escopo, reporte sem implementar. '
    'Se o pedido trouxer base_tests, os testes já falham antes da sua alteração: corrija essas '
    'falhas quando a tarefa pedir ou quando os critérios dependerem delas, e cite-as no relatório; '
    'a tarefa só é aprovada com os testes passando.\n'
)
REPAIR_PROMPT = (
    'Corrija a tarefa abaixo considerando a revisão. Preserve a branch atual. '
    'Não faça commit/push/deploy. Não modifique arquivos fora de files.\n'
)
REVIEW_PROMPT = (
    'Você é revisor de código. Verifique a task e o diff contra critérios de aceitação. '
    'Testes não executados NÃO equivalem a testes aprovados. Não modifique arquivos. '
    'test_command é a única verificação que rodou, no worktree e na integração: um critério que exige '
    'outra verificação (lint, tipos, build, outro script) não está comprovado pela saída dos testes; '
    'nesse caso rejeite e diga qual verificação faltou. '
    'Separe defeito herdado de defeito da tarefa: o que já existia na base, inclusive as falhas de '
    'base_test_output quando presente, não impede a aprovação, salvo quando a tarefa pede para corrigir; '
    'liste esses pontos em inherited, não em problems. '
    'Responda SOMENTE com um objeto JSON, sem texto antes ou depois, no formato '
    '{"verdict": "APPROVED" ou "REJECTED", "summary": "uma frase", '
    '"problems": ["cada problema da tarefa que impede a aprovação"], '
    '"inherited": ["cada problema que já existia na base"]}.\n'
)
VERDICTS = ('APPROVED', 'REJECTED')
COMMIT_SUBJECT_LENGTH = 72
# Latin-1 é um mapeamento reversível mesmo para um patch com codificações misturadas.
PATCH_ENCODINGS = ('utf-8', 'cp1252', 'latin-1')


@dataclass(frozen=True)
class Verdict:
    approved: bool
    text: str


def _verdict_object(response: str) -> dict | None:
    """Primeiro objeto JSON com `verdict` na resposta, mesmo cercado de texto ou de cercas de código."""
    decoder = json.JSONDecoder()
    position = response.find('{')
    while position != -1:
        try:
            value, _ = decoder.raw_decode(response, position)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and 'verdict' in value:
            return value
        position = response.find('{', position + 1)
    return None


def parse_verdict(response: str) -> Verdict:
    """Interpreta a resposta do revisor, um objeto JSON com `verdict`; na dúvida, rejeita."""
    response = response.strip()
    data = _verdict_object(response)
    if data is None:
        return Verdict(False, 'REJECTED\nResposta do revisor sem veredito reconhecível:\n' + response)
    verdict = str(data.get('verdict', '')).strip().upper()
    if verdict not in VERDICTS:
        return Verdict(False, f'REJECTED\nVeredito desconhecido {data.get("verdict")!r}:\n' + response)
    lines = [verdict]
    if summary := str(data.get('summary') or '').strip():
        lines.append(summary)
    problems = data.get('problems') or []
    if isinstance(problems, str):
        problems = [problems]
    lines.extend(f'- {problem}' for problem in problems if str(problem).strip())
    inherited = data.get('inherited') or []
    if isinstance(inherited, str):
        inherited = [inherited]
    lines.extend(f'- herdado da base: {item}' for item in inherited if str(item).strip())
    return Verdict(verdict == 'APPROVED', '\n'.join(lines))


@dataclass(frozen=True)
class BaseTests:
    """Resultado dos testes na base do lote, antes de qualquer alteração."""

    returncode: int
    output: str


class InheritedFailure(RuntimeError):
    """Os testes já falhavam na base e continuam falhando: revisão e correções não ajudam."""


@dataclass
class Artifact:
    """Commit candidato de uma tarefa e o worktree onde ele foi produzido."""

    task: Task
    worktree: str
    report: str
    commit: str | None = None
    branch: str | None = None
    context: str = ''
    # Presente quando os testes já falhavam na base, antes da implementação.
    base_failure: BaseTests | None = None

    @property
    def candidate(self) -> str:
        if self.commit is None:
            raise RuntimeError(f'Tarefa {self.task["id"]} sem commit candidato')
        return self.commit


def _with_payload(instructions: str, payload: dict) -> str:
    return instructions + json.dumps(payload, ensure_ascii=False)


def parse_agy_output(stdout: str) -> str:
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    results = [event['result'] for event in events if event.get('event') == 'result']
    if len(results) != 1:
        raise RuntimeError('Antigravity não retornou exatamente um resultado terminal')
    result = results[0]
    if result.get('status') != 'SUCCESS':
        raise RuntimeError(f'Antigravity: {result.get("status")}: {result.get("error", "")}')
    return str(result.get('response', ''))


def parse_claude_output(stdout: str) -> str:
    data = json.loads(stdout)
    if data.get('is_error'):
        raise RuntimeError('Claude retornou erro')
    result = data.get('result', '')
    if isinstance(result, list):
        return '\n'.join(item.get('text', '') for item in result if isinstance(item, dict))
    return str(result)


def run_agy(prompt: str, wt: str | Path, stop: StopSignal | None = None) -> str:
    # NDJSON pela entrada padrão evita o limite da linha de comando do Windows, inclusive nas correções.
    payload = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n'
    result = process.run(
        [
            'agy',
            '--input-format',
            'stream-json',
            '--output-format',
            'stream-json',
            '--sandbox',
            '--mode',
            'auto',
            '--print-timeout',
            config.AGENT_PRINT_TIMEOUT,
            *(['--model', config.AGENT_MODEL] if config.AGENT_MODEL else []),
        ],
        cwd=wt,
        timeout=config.AGENT_TIMEOUT,
        input_data=payload,
        stop=stop,
    )
    return parse_agy_output(result.stdout)


def make_worktree(ctx: RunContext, task: Task, base: str) -> Path:
    # Fora do repositório: ferramentas que varrem a árvore a partir da raiz, como test runners
    # e linters com globs amplos, não podem enxergar as cópias das outras tarefas.
    location = ctx.ws.worktree_dir(ctx.run_id) / f'wt-{task["id"]}'
    if location.exists():
        raise RuntimeError(f'Worktree já existe: {location}; verifique antes de limpar')
    branch = f'agent/{task["id"]}-{uuid.uuid4().hex[:12]}'
    location.parent.mkdir(parents=True, exist_ok=True)
    gitops.git('worktree', 'add', '-b', branch, str(location), base, cwd=ctx.root)
    return location


def prepare_worktree(ctx: RunContext, wt: Path, base: str, branch: str) -> None:
    """Roda o comando de preparação, que só pode criar arquivos ignorados pelo Git."""
    if not ctx.setup:
        return
    result = process.run(ctx.setup, cwd=wt, timeout=config.SETUP_TIMEOUT, check=False, stop=ctx.stop)
    if result.returncode:
        output = (result.stdout + '\n' + result.stderr)[-config.ERROR_TAIL :]
        raise RuntimeError(f'Preparação do worktree falhou (código {result.returncode}): {output}')
    gitops.check_checkout(wt, base, branch)
    changed = gitops.changed_files(wt)
    if changed:
        raise RuntimeError(f'Preparação do worktree alterou arquivos não ignorados, por exemplo {changed[0]}')


def call_agent(ctx: RunContext, task: Task, prompt: str, wt: Path, expected_head: str, branch: str | None) -> str:
    """Chama o agy e repete a chamada quando ela falha sem deixar o estado incerto."""
    for attempt in range(config.AGENT_RETRIES + 1):
        try:
            return run_agy(prompt, wt, ctx.stop)
        except (RecoveryRequired, RunCancelled):
            raise
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            if attempt == config.AGENT_RETRIES:
                raise
            # O que o agente já escreveu fica para a nova tentativa, desde que a
            # branch e o commit do worktree continuem os esperados.
            gitops.check_checkout(wt, expected_head, branch)
            log.info(
                '%s: chamada ao agy falhou (%s); nova tentativa %d de %d',
                task['id'],
                describe(error).splitlines()[0][:200],
                attempt + 1,
                config.AGENT_RETRIES,
            )
    raise AssertionError('o laço retorna ou propaga o erro')


def commit_message(ctx: RunContext, task: Task) -> str:
    """Mensagem do commit candidato, que a integração leva para a branch de destino."""
    task_id = task['id']
    summary = ' '.join(task['description'].split())
    room = COMMIT_SUBJECT_LENGTH - len(task_id) - 2
    if len(summary) > room:
        summary = summary[: room - 1].rstrip() + '…'
    return f'{task_id}: {summary}\n\nTarefa {task_id} da execução {ctx.run_id[:12]} do Contracanto.'


def base_tests(ctx: RunContext, wt: Path, base: str, branch: str) -> BaseTests | None:
    """Testa a base uma vez por lote, no primeiro worktree preparado sobre ela.

    O resultado separa o que a base já traz de quebrado do que a tarefa introduz.
    """
    tests = ctx.tests
    if not tests:
        return None

    def compute() -> BaseTests:
        result = gitops.run_tests(tests, wt, base, expected_branch=branch, stop=ctx.stop)
        output = (result.stdout + '\n' + result.stderr)[-config.TEST_OUTPUT_TAIL :]
        outcome = f'falharam (código {result.returncode})' if result.returncode else 'passaram'
        log.info('Testes da base %s: %s', base[:12], outcome)
        return BaseTests(result.returncode, output)

    baseline: BaseTests = ctx.baseline(base, compute)
    return baseline


def run_executor(ctx: RunContext, task: Task, base: str, context: str) -> Artifact:
    ctx.stop.check()
    wt = make_worktree(ctx, task, base)
    branch = gitops.branch_ref(wt)
    prepare_worktree(ctx, wt, base, branch)
    baseline = base_tests(ctx, wt, base, branch)
    failure = baseline if baseline is not None and baseline.returncode else None
    payload: dict[str, Any] = {'task': task, 'context': context}
    if failure is not None:
        payload['base_tests'] = {'exit_code': failure.returncode, 'output': failure.output}
    prompt = _with_payload(IMPLEMENT_PROMPT, payload)
    report = call_agent(ctx, task, prompt, wt, base, branch)
    gitops.check_checkout(wt, base, branch)
    gitops.check_scope(task, wt, base)
    if not gitops.changed_files(wt):
        # O relatório costuma explicar a recusa; sem ele, o motivo fica só no log do agy.
        raise RuntimeError(
            f'O executor não alterou arquivos em {task["id"]}. Relatório do executor:\n'
            + (report.strip()[-config.ERROR_TAIL :] or '(vazio)')
        )
    gitops.commit_all(wt, commit_message(ctx, task))
    return Artifact(
        task=task,
        worktree=str(wt),
        report=report,
        commit=gitops.head(wt),
        branch=branch,
        context=context,
        base_failure=failure,
    )


def repair(ctx: RunContext, artifact: Artifact, base: str, context: str, feedback: str) -> None:
    """Pede a correção e substitui o candidato por um único commit sobre a base."""
    task = artifact.task
    wt = Path(artifact.worktree)
    payload = {'task': task, 'context': context, 'review': feedback}
    prompt = _with_payload(REPAIR_PROMPT, payload)
    correction = call_agent(ctx, task, prompt, wt, artifact.candidate, artifact.branch)
    artifact.report += '\nCorreção: ' + correction
    gitops.check_checkout(wt, artifact.candidate, artifact.branch)
    gitops.check_scope(task, wt, base)
    if gitops.changed_files(wt):
        gitops.commit_all(wt, f'correção de {task["id"]}')
        # A integração aplica um commit só: a sequência inteira vira um squash sobre a base.
        gitops.git('reset', '--soft', base, cwd=wt)
        gitops.commit_staged(wt, commit_message(ctx, task))
        artifact.commit = gitops.head(wt)


def _decode_patch(patch: bytes) -> tuple[str, str]:
    for encoding in PATCH_ENCODINGS:
        try:
            return patch.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise AssertionError('latin-1 decodifica qualquer sequência de bytes')


def review(artifact: Artifact, test_command: Sequence[str], stop: StopSignal | None = None) -> tuple[bool, str]:
    """Roda os testes no worktree e pede o veredito; aprova apenas com ambos positivos.

    Devolve a aprovação e o parecer, que inclui a saída dos testes quando eles falham.
    """
    task = artifact.task
    wt = Path(artifact.worktree)
    head = gitops.head(wt)
    if artifact.commit is not None and artifact.commit != head:
        raise RuntimeError('HEAD do worktree difere do commit candidato')
    test = gitops.run_tests(test_command, wt, head, expected_branch=artifact.branch, stop=stop)
    test_output = (test.stdout + '\n' + test.stderr)[-config.TEST_OUTPUT_TAIL :]
    base = artifact.base_failure
    if test.returncode and base is not None:
        # A aprovação exige testes verdes; com a base vermelha, nem revisão nem correção ajudam.
        raise InheritedFailure(
            f'Os testes já falhavam na base (código {base.returncode}) e continuam falhando no worktree '
            f'(código {test.returncode}). Corrija a base antes, com uma tarefa própria, ou ajuste o comando '
            f'de testes.\nSaída na base:\n{base.output[-config.ERROR_TAIL :]}'
            f'\nSaída no worktree:\n{test_output[-config.ERROR_TAIL :]}'
        )
    summary = gitops.git('show', '--format=medium', '--stat', gitops.ALL_SUBMODULES, 'HEAD', cwd=wt).stdout
    patch_bytes = gitops.git(
        'show',
        '--format=',
        '--binary',
        '--no-textconv',
        '--no-ext-diff',
        gitops.ALL_SUBMODULES,
        'HEAD',
        cwd=wt,
        output='binary',
    ).stdout
    patch, encoding = _decode_patch(patch_bytes)
    if len(patch) > config.REVIEW_PATCH_LIMIT:
        raise RuntimeError(
            f'Patch de {len(patch)} caracteres excede o limite de revisão ({config.REVIEW_PATCH_LIMIT}); '
            f'divida a tarefa ou ajuste {config.ENV_PREFIX}REVIEW_PATCH_LIMIT'
        )
    prompt = _with_payload(
        REVIEW_PROMPT,
        {
            'task': task,
            'context': artifact.context,
            'diff_summary': summary,
            'patch': patch,
            'patch_encoding': encoding,
            'executor_report': artifact.report,
            'test_command': list(test_command),
            'test_exit_code': test.returncode,
            'test_output': test_output,
            **({'base_test_exit_code': base.returncode, 'base_test_output': base.output} if base is not None else {}),
        },
    )
    # Fora do worktree: o revisor recebe tudo pelo pedido e não deve carregar as instruções
    # que o projeto revisado guarda para outros papéis, como o CLAUDE.md do arquiteto.
    with tempfile.TemporaryDirectory(prefix='contracanto-review-') as neutral:
        response = process.run(
            [
                'claude',
                '-p',
                '--model',
                config.REVIEW_MODEL,
                '--output-format',
                'json',
                '--max-turns',
                '1',
                '--tools',
                '',
                '--strict-mcp-config',
            ],
            cwd=neutral,
            timeout=config.REVIEW_TIMEOUT,
            input_data=prompt,
            stop=stop,
        )
    verdict = parse_verdict(parse_claude_output(response.stdout))
    feedback = verdict.text
    if test.returncode:
        feedback += f'\nTestes do worktree falharam (código {test.returncode}):\n{test_output}'
    return verdict.approved and test.returncode == 0, feedback
