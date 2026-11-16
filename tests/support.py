"""Fixtures compartilhadas: diretórios temporários, workspaces e repositórios Git descartáveis."""

import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracanto import agents, errors, gitops, runner, state
from contracanto import process as proc

# Dentro do projeto e ignorado pelo Git: mesmo volume e sem caminhos curtos do Windows.
TEMP_PARENT = Path(__file__).resolve().parent.parent / '.agent-runs'


@contextlib.contextmanager
def temp_dir():
    TEMP_PARENT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as directory:
        yield Path(directory)


def workspace(store, root=None):
    """Workspace cujo registro de execuções fica em `store`."""
    return state.Workspace(Path(root or store), Path(store))


def context(ws, run_id='test-run'):
    return state.RunContext.open(ws, run_id)


@contextlib.contextmanager
def temp_workspace(server=None):
    """Workspace isolado; com `server`, as ferramentas MCP passam a usá-lo."""
    with temp_dir() as directory:
        ws = workspace(directory)
        if server is None:
            yield ws
        else:
            with patch.object(server, 'WORKSPACE', ws):
                yield ws


def recovery_pending(ctx, statuses):
    """Simula um worker que registrou recuperação pendente antes de terminar."""
    ctx.state.statuses.update(statuses)
    ctx.state.require_recovery()


def init_repository(root, ignore_store=True):
    """Cria um repositório com um commit inicial e devolve (git, store)."""
    original_git = gitops.git

    def git(*args, cwd=None, **kwargs):
        return original_git(*args, cwd=cwd or root, **kwargs)

    git('init', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@localhost')
    if ignore_store:
        (root / '.gitignore').write_text('.agent-runs/\n')
    return git


def read_status(ws):
    """Status persistido da execução mais recente."""
    return json.loads(ws.status(ws.latest_run_id()).read_text(encoding='utf-8'))


def publish_previous_run(ws, status, run_id='a' * 64):
    """Registra uma execução anterior já encerrada."""
    ws.publish_run(run_id)
    ws.status(run_id).write_text(json.dumps(dict(status, run_id=run_id)), encoding='utf-8')
    return run_id


class RepositoryTestCase(unittest.TestCase):
    """Repositório Git real com agente, revisor e testes simulados."""

    @contextlib.contextmanager
    def repository(self, agent_commit=None, mutate_tests=None, test_exit=0):
        with temp_dir() as directory:
            root = Path(directory).resolve()
            original_cmd = proc.run

            git = init_repository(root)
            (root / 'outside.txt').write_text('original\n')
            git('add', '.')
            git('commit', '-m', 'baseline')
            base = git('rev-parse', 'HEAD').stdout.strip()
            store = root / '.agent-runs'
            store.mkdir()
            calls = {'agent': 0, 'review': 0}

            def agent(prompt, wt, stop=None):
                calls['agent'] += 1
                if calls['agent'] == agent_commit:
                    (wt / 'outside.txt').write_text('unauthorized\n')
                    git('add', 'outside.txt', cwd=wt)
                    git('commit', '-m', 'unexpected agent commit', cwd=wt)
                (wt / 'allowed.txt').write_text(str(calls['agent']))
                return 'done'

            def command(args, **kwargs):
                kwargs.setdefault('cwd', root)
                if args[0] == 'claude':
                    calls['review'] += 1
                    verdict = 'REJECTED' if agent_commit == 2 and calls['review'] == 1 else 'APPROVED'
                    return subprocess.CompletedProcess(args, 0, json.dumps({'result': verdict}), '')
                if args[0] == sys.executable:
                    in_main = Path(kwargs['cwd']) == root
                    if mutate_tests == ('main' if in_main else 'worker'):
                        (Path(kwargs['cwd']) / 'outside.txt').write_text('changed by tests\n')
                        if test_exit == 'timeout':
                            raise subprocess.TimeoutExpired(args, 600)
                        if test_exit == 'cleanup':
                            raise errors.RecoveryRequired('Subprocess termination not confirmed')
                        return subprocess.CompletedProcess(args, test_exit, '', '')
                return original_cmd(args, **kwargs)

            task = dict(
                id='A', description='Create allowed.txt', deps=[], files=['allowed.txt'], acceptance=['File exists']
            )
            with contextlib.ExitStack() as stack:
                self.ws = workspace(store, root)
                for module, name, value in [(gitops, 'git', git), (proc, 'run', command), (agents, 'run_agy', agent)]:
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch.object(runner.shutil, 'which', return_value='fake-cli'))
                try:
                    yield root, store, base, task, git
                finally:
                    for wt in (store / 'runs').glob('*/wt-*'):
                        # Preserve no test worktree registrations; dirty fixtures are disposable.
                        git('worktree', 'remove', '--force', str(wt))


def fake_git_output(args, head='base'):
    """Saída de um Git simulado: checkout limpo na branch main, no commit `head`."""
    if args[0] == 'status':
        return f'# branch.oid {head}\0# branch.head main\0'
    if args[0] == 'symbolic-ref':
        return 'refs/heads/main'
    if args[0] == 'rev-parse':
        return head
    return ''


def reserved_repository(test, worker_pid, recovery_required=False):
    """Repositório limpo com uma reserva assumida por um worker de PID conhecido.

    Devolve (workspace, run_id, git).
    """
    directory = test.enterContext(temp_dir())
    git = init_repository(directory)
    git('add', '.')
    git('commit', '-m', 'baseline')
    ws = state.Workspace.for_root(directory)
    run_id = state.reservation_id(state.reserve_run(ws))
    ws.publish_run(run_id)
    ws.claim(run_id).mkdir()
    ws.worker_pid(run_id).write_text(str(worker_pid))
    run = state.RunState(ws.status(run_id), run_id)
    run.state = 'running'
    run.statuses = {'A': 'running', 'B': 'pending', 'C': 'approved'}
    run.recovery_required = recovery_required
    run.save()
    return ws, run_id, git


_FINISHED: list[subprocess.Popen] = []


def finished_process_pid():
    """PID de um processo que já terminou.

    O objeto do processo fica guardado: no Windows o PID só é reaproveitado depois
    que o último handle é fechado, e a suíte paralela cria processos o tempo todo.
    """
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    _FINISHED.append(child)
    return child.pid


def task(tid, files=None, deps=()):
    return dict(id=tid, description='Example', deps=list(deps), files=files or [tid + '.txt'], acceptance=['Pass'])
