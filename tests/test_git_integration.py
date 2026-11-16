"""Git real; somente os CLIs pagos e o resultado dos testes são substituídos."""

import contextlib
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, errors, gitops, runner
from contracanto import process as proc


class GitIntegrationTests(unittest.TestCase):
    def test_timeout_restores_tree_with_revert_commit(self):
        self.assert_integration_failure_restores_tree(subprocess.TimeoutExpired(['test'], 600))

    def test_keyboard_interrupt_restores_tree_with_revert_commit(self):
        self.assert_integration_failure_restores_tree(KeyboardInterrupt())

    def test_stop_during_integration_restores_tree_with_revert_commit(self):
        self.assert_integration_failure_restores_tree(errors.RunCancelled('Encerramento solicitado'))

    def assert_integration_failure_restores_tree(self, error):
        with support.temp_dir() as directory:
            root = Path(directory)

            def git(*args, cwd=None, check=True, **options):
                return subprocess.run(
                    ['git', *args], cwd=cwd or root, check=check, capture_output=True, text=True, encoding='utf-8'
                )

            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@localhost')
            (root / '.gitignore').write_text('.agent-runs/\n')
            git('add', '.')
            git('commit', '-m', 'baseline')
            store = root / '.agent-runs'
            store.mkdir()
            before = git('rev-parse', 'HEAD^{tree}').stdout.strip()

            def executor(ctx, task, base, context):
                wt = root / '.agent-runs' / 'worker'
                git('worktree', 'add', '-b', 'worker', str(wt), base)
                (wt / 'feature.txt').write_text('change')
                git('add', 'feature.txt', cwd=wt)
                git('commit', '-m', 'feature', cwd=wt)
                commit = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                return agents.Artifact(task=task, commit=commit, worktree=str(wt), report='done')

            with contextlib.ExitStack() as stack:
                ws = support.workspace(store, root)
                stack.enter_context(patch.object(gitops, 'git', side_effect=git))
                stack.enter_context(patch.object(runner.shutil, 'which', return_value='tool'))
                stack.enter_context(patch.object(agents, 'run_executor', side_effect=executor))
                stack.enter_context(patch.object(agents, 'review', return_value=(True, 'APPROVED')))
                stack.enter_context(patch.object(proc, 'run', side_effect=error))
                plan = {
                    'tasks': [dict(id='A', description='Change', deps=[], files=['feature.txt'], acceptance=['Pass'])]
                }
                if isinstance(error, (KeyboardInterrupt, errors.RunCancelled)):
                    with self.assertRaises(type(error)):
                        runner.execute(ws, plan, 1, 0, ['test'])
                else:
                    self.assertEqual(runner.execute(ws, plan, 1, 0, ['test']), {'A': 'failed'})
            state = support.read_status(ws)
            self.assertEqual(state['state'], 'cancelled' if isinstance(error, errors.RunCancelled) else 'failed')
            self.assertFalse(state.get('recovery_required', False))
            details = state['details']['A']
            self.assertEqual(details['revert_commit'], git('rev-parse', 'HEAD').stdout.strip())
            self.assertEqual(details['integrated_commit'], git('rev-parse', 'HEAD^').stdout.strip())
            self.assertEqual(details['target_before'], git('rev-parse', 'HEAD^^').stdout.strip())
            self.assertEqual(git('rev-parse', 'HEAD^{tree}').stdout.strip(), before)
            self.assertEqual(git('status', '--porcelain').stdout, '')
            self.assertEqual(git('rev-list', '--count', 'HEAD').stdout.strip(), '3')
            self.assertFalse((store / 'run.lock').exists())
            # Desregistra o worktree antes de TemporaryDirectory remover a fixture.
            git('worktree', 'remove', str(root / '.agent-runs' / 'worker'))
