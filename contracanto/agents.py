"""Chamadas aos CLIs externos: Antigravity implementa e corrige, Claude revisa."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, gitops, process
from .plan import Task
from .state import RunContext, StopSignal

IMPLEMENT_PROMPT = (
    'Você está implementando UMA tarefa no Git worktree atual. '
    'Leia GEMINI.md quando presente. Preserve a branch atual. '
    'Não altere arquitetura, não faça commit/push/deploy. '
    'Não acesse segredos. NÃO altere arquivos fora da lista files. '
    'Se houver conflito de escopo, reporte sem implementar.\n'
)
REPAIR_PROMPT = (
    'Corrija a tarefa abaixo considerando a revisão. Preserve a branch atual. '
    'Não faça commit/push/deploy. Não modifique arquivos fora de files.\n'
)
REVIEW_PROMPT = (
    'Você é revisor de código. Verifique a task e o diff contra critérios de aceitação. '
    'Responda EXATAMENTE com uma palavra na primeira linha: APPROVED ou REJECTED. '
    'Nas linhas seguintes explique problemas encontrados. '
    'Testes não executados NÃO equivalem a testes aprovados. '
    'Não modifique arquivos.\n'
)
# Latin-1 is a reversible mapping even for a patch with mixed encodings.
PATCH_ENCODINGS = ('utf-8', 'cp1252', 'latin-1')


@dataclass
class Artifact:
    """Commit candidato de uma tarefa e o worktree onde ele foi produzido."""

    task: Task
    worktree: str
    report: str
    commit: str | None = None
    branch: str | None = None
    context: str = ''


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
    # NDJSON stdin avoids Windows command-line limits, including during repairs.
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
        ],
        cwd=wt,
        timeout=config.AGENT_TIMEOUT,
        input_data=payload,
        stop=stop,
    )
    return parse_agy_output(result.stdout)


def make_worktree(ctx: RunContext, task: Task, base: str) -> Path:
    location = ctx.run_dir / f'wt-{task["id"]}'
    if location.exists():
        raise RuntimeError(f'Worktree já existe: {location}; verifique antes de limpar')
    branch = f'agent/{task["id"]}-{uuid.uuid4().hex[:12]}'
    gitops.git('worktree', 'add', '-b', branch, str(location), base, cwd=ctx.root)
    return location


def run_executor(ctx: RunContext, task: Task, base: str, context: str) -> Artifact:
    ctx.stop.check()
    wt = make_worktree(ctx, task, base)
    branch = gitops.branch_ref(wt)
    report = run_agy(_with_payload(IMPLEMENT_PROMPT, {'task': task, 'context': context}), wt, ctx.stop)
    gitops.check_checkout(wt, base, branch)
    gitops.check_scope(task, wt, base)
    if not gitops.changed_files(wt):
        raise RuntimeError(f'Gemini não alterou arquivos em {task["id"]}')
    gitops.commit_all(wt, f'agent: {task["id"]}')
    return Artifact(task=task, worktree=str(wt), report=report, commit=gitops.head(wt), branch=branch, context=context)


def repair(ctx: RunContext, artifact: Artifact, base: str, context: str, feedback: str) -> None:
    """Pede a correção e substitui o candidato por um único commit sobre a base."""
    task = artifact.task
    wt = Path(artifact.worktree)
    payload = {'task': task, 'context': context, 'review': feedback}
    correction = run_agy(_with_payload(REPAIR_PROMPT, payload), wt, ctx.stop)
    artifact.report += '\nCorreção: ' + correction
    gitops.check_checkout(wt, artifact.commit, artifact.branch)
    gitops.check_scope(task, wt, base)
    if gitops.changed_files(wt):
        gitops.commit_all(wt, f'agent: repair {task["id"]}')
        # A integração aplica um commit só: a sequência inteira vira um squash sobre a base.
        gitops.git('reset', '--soft', base, cwd=wt)
        gitops.commit_staged(wt, f'agent: {task["id"]} revised')
        artifact.commit = gitops.head(wt)


def _decode_patch(patch: bytes) -> tuple[str, str]:
    for encoding in PATCH_ENCODINGS:
        try:
            return patch.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise AssertionError('latin-1 decodifica qualquer sequência de bytes')


def review(
    task: Task, artifact: Artifact, test_command: Sequence[str], stop: StopSignal | None = None
) -> tuple[bool, str, str]:
    """Roda os testes no worktree e pede o veredito; aprova apenas com ambos positivos."""
    wt = Path(artifact.worktree)
    head = gitops.head(wt)
    if artifact.commit is not None and artifact.commit != head:
        raise RuntimeError('HEAD do worktree difere do commit candidato')
    test = gitops.run_tests(test_command, wt, head, expected_branch=artifact.branch, stop=stop)
    test_output = (test.stdout + '\n' + test.stderr)[-config.TEST_OUTPUT_TAIL :]
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
    prompt = _with_payload(
        REVIEW_PROMPT,
        {
            'task': task,
            'context': artifact.context,
            'diff_summary': summary,
            'patch': patch,
            'patch_encoding': encoding,
            'executor_report': artifact.report,
            'test_exit_code': test.returncode,
            'test_output': test_output,
        },
    )
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
        cwd=wt,
        timeout=config.REVIEW_TIMEOUT,
        input_data=prompt,
        stop=stop,
    )
    verdict = parse_claude_output(response.stdout).strip()
    approved = verdict.splitlines()[0:1] == ['APPROVED'] and test.returncode == 0
    if test.returncode:
        verdict += f'\nTestes do worktree falharam (código {test.returncode}):\n{test_output}'
    return approved, verdict, test_output
