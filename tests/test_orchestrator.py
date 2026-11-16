import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator as o


def task(tid, deps=()):
    return dict(id=tid, description='Example', deps=list(deps), files=[tid + '.txt'], acceptance=['Tests pass'])


class RegressionTests(unittest.TestCase):
    def test_rejects_ids_that_differ_only_by_case(self):
        for first, second in [('A', 'a'), ('Task_01', 'task_01')]:
            with self.subTest(ids=(first, second)):
                with self.assertRaises(ValueError):
                    o.validate({'tasks': [task(first), task(second)]})

    def test_cli_forwards_test_options(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            plan = Path(directory) / 'plan.json'
            plan.write_text(json.dumps({'tasks': [task('A')]}))
            with (
                patch.object(sys, 'argv', ['orchestrator.py', str(plan), '--test', 'py', '-m', 'pytest']),
                patch.object(o, 'execute', return_value={'A': 'approved'}) as execute,
            ):
                o.main()
            self.assertEqual(execute.call_args.args[3], ['py', '-m', 'pytest'])

    def test_review_selects_sonnet_explicitly(self):
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args[0] == 'claude':
                return subprocess.CompletedProcess(args, 0, json.dumps({'result': 'APPROVED', 'is_error': False}), '')
            return subprocess.CompletedProcess(args, 0, b'' if '--binary' in args else '', '')

        with patch.object(o, 'cmd', side_effect=command):
            o.review(task('A'), {'worktree': '.', 'report': 'done'}, ['python', 'test.py'])
        review_args = next(args for args in calls if args[0] == 'claude')
        self.assertEqual(review_args[review_args.index('--model') + 1], 'sonnet')

    def simulate(
        self, tasks, executor=None, integration_error=None, revert_fails=False, abort_error=False, pick_error=False
    ):
        calls = []

        def git(*args, **kwargs):
            calls.append(args)
            if pick_error and args[0] == 'cherry-pick':
                raise subprocess.TimeoutExpired(['git', *args], 180)
            if abort_error and args[:2] == ('cherry-pick', '--abort'):
                raise subprocess.TimeoutExpired(['git', *args], 180)
            failed = (revert_fails and args[0] == 'revert') or (abort_error and args[0] == 'cherry-pick')
            return subprocess.CompletedProcess(args, int(failed), 'base' if args[0] == 'rev-parse' else '', '')

        def artifact(t, base, context):
            return dict(task=t, commit='commit', worktree='.', report='done')

        with tempfile.TemporaryDirectory(dir=o.STORE) as directory, contextlib.ExitStack() as stack:
            for target, kwargs in [
                ('STORE', {'new': Path(directory)}),
                ('git', {'side_effect': git}),
                ('run_executor', {'side_effect': executor or artifact}),
                ('review', {'return_value': (True, 'APPROVED', '')}),
                (
                    'cmd',
                    {'side_effect': integration_error}
                    if integration_error
                    else {'return_value': subprocess.CompletedProcess([], 0, '', '')},
                ),
            ]:
                stack.enter_context(patch.object(o, target, **kwargs))
            stack.enter_context(patch.object(o.shutil, 'which', return_value='tool'))
            statuses = o.execute({'tasks': tasks}, 1, 0, ['test'])
        return statuses, calls

    def test_timeout_after_cherry_pick_reverts(self):
        statuses, calls = self.simulate([task('A')], integration_error=subprocess.TimeoutExpired(['test'], 600))
        self.assertEqual(statuses['A'], 'failed')
        self.assertTrue(any(call[0] == 'revert' for call in calls), calls)

    def test_failure_blocks_all_descendants(self):
        statuses, _ = self.simulate([task('A'), task('B', ['A']), task('C', ['B'])], executor=RuntimeError('failed'))
        self.assertEqual(statuses, dict(A='failed', B='blocked', C='blocked'))

    def test_failed_rollback_stops_other_independent_tasks(self):
        statuses, calls = self.simulate(
            [task('A'), task('B')], integration_error=OSError('test unavailable'), revert_fails=True
        )
        self.assertEqual(sum(call[0] == 'cherry-pick' for call in calls), 1)
        self.assertEqual(statuses, {'A': 'failed', 'B': 'blocked'})

    def test_timeout_aborting_cherry_pick_stops_run(self):
        statuses, calls = self.simulate([task('A'), task('B')], abort_error=True)
        self.assertEqual(statuses, {'A': 'failed', 'B': 'blocked'})
        self.assertEqual(sum(call[0] == 'cherry-pick' and call[1] != '--abort' for call in calls), 1)

    def test_unknown_cherry_pick_result_stops_run(self):
        statuses, _ = self.simulate([task('A'), task('B')], pick_error=True)
        self.assertEqual(statuses, {'A': 'failed', 'B': 'blocked'})

    def test_rejects_incomplete_or_unsafe_scope(self):
        for files in (None, [], ['../secret'], ['C:/secret'], ['src/../secret'], ['.git/config'], [1]):
            with self.subTest(files=files):
                t = task('A')
                if files is None:
                    del t['files']
                else:
                    t['files'] = files
                with self.assertRaises(ValueError):
                    o.validate({'tasks': [t]})

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            o.validate({'tasks': [task('A', ['B']), task('B', ['A'])]})


if __name__ == '__main__':
    unittest.main()
