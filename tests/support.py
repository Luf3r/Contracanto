"""Fixtures compartilhadas: diretórios temporários, workspaces e repositórios Git descartáveis."""

import contextlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from contracanto import gitops, state

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
