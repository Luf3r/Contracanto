"""Git real; somente os CLIs pagos e o resultado dos testes são substituídos."""
import contextlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o


class GitIntegrationTests(unittest.TestCase):
    def test_timeout_restores_tree_with_revert_commit(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            root = Path(directory)
            def git(*args, cwd=None, check=True):
                return subprocess.run(['git', *args], cwd=cwd or root, check=check,
                                      capture_output=True, text=True, encoding='utf-8')
            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@localhost')
            (root / '.gitignore').write_text('.agent-runs/\n')
            git('add', '.')
            git('commit', '-m', 'baseline')
            store = root / '.agent-runs'
            store.mkdir()
            before = git('rev-parse', 'HEAD^{tree}').stdout.strip()
            def executor(task, base, context):
                wt = root / '.agent-runs' / 'worker'
                git('worktree', 'add', '-b', 'worker', str(wt), base)
                (wt / 'feature.txt').write_text('change')
                git('add', 'feature.txt', cwd=wt)
                git('commit', '-m', 'feature', cwd=wt)
                return dict(task=task, commit=git('rev-parse', 'HEAD', cwd=wt).stdout.strip(), worktree=str(wt), report='done')
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(o, 'STORE', store))
                stack.enter_context(patch.object(o, 'git', side_effect=git))
                stack.enter_context(patch.object(o.shutil, 'which', return_value='tool'))
                stack.enter_context(patch.object(o, 'run_executor', side_effect=executor))
                stack.enter_context(patch.object(o, 'review', return_value=(True, 'APPROVED', '')))
                stack.enter_context(patch.object(o, 'cmd', side_effect=subprocess.TimeoutExpired(['test'], 600)))
                statuses = o.execute({'tasks': [dict(id='A', description='Change', deps=[], files=['feature.txt'], acceptance=['Pass'])]}, 1, 0, ['test'])
            self.assertEqual(statuses, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD^{tree}').stdout.strip(), before)
            self.assertEqual(git('status', '--porcelain').stdout, '')
            self.assertEqual(git('rev-list', '--count', 'HEAD').stdout.strip(), '3')
            self.assertFalse((store / 'run.lock').exists())
            # Desregistra o worktree antes de TemporaryDirectory remover a fixture.
            git('worktree', 'remove', str(root / '.agent-runs' / 'worker'))
