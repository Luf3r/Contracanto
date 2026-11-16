"""Regressões operacionais; nenhum teste chama modelos pagos."""

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, errors, gitops, runner
from contracanto import process as proc
from contracanto import state as st


def task(files):
    return dict(id='A', description='Example', deps=[], files=files, acceptance=['Pass'])


class OperationalTests(unittest.TestCase):
    def test_interrupted_worker_collection_preserves_uncertain_recovery(self):
        for failure, collected in (
            (errors.RecoveryRequired('process alive'), False),
            (errors.RecoveryRequired('process alive'), True),
            (RuntimeError('known failure'), False),
            (None, False),
        ):
            with (
                self.subTest(failure=failure, collected=collected),
                support.temp_dir() as directory,
            ):
                store = Path(directory)
                ws = support.workspace(store)
                current = task(['a'])
                artifact = agents.Artifact(task=current, commit='commit', worktree='.', report='done')

                def interrupted(jobs):
                    concurrent.futures.wait(jobs)
                    if collected:
                        yield next(iter(jobs))
                    raise KeyboardInterrupt

                def git(*args, **kwargs):
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')

                with (
                    patch.object(gitops, 'git', side_effect=git),
                    patch.object(runner.shutil, 'which', return_value='tool'),
                    patch.object(agents, 'run_executor', side_effect=failure, return_value=artifact),
                    patch.object(runner.concurrent.futures, 'as_completed', side_effect=interrupted),
                    patch.object(agents, 'review') as review,
                ):
                    uncertain = isinstance(failure, errors.RecoveryRequired)
                    try:
                        with self.assertRaises(errors.RecoveryRequired if uncertain else KeyboardInterrupt):
                            runner.execute(ws, dict(tasks=[current]), 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Interrupção descartou a recuperação exigida pelo worker')
                    state = support.read_status(ws)
                    self.assertEqual(state['state'], 'failed')
                    self.assertEqual(state['recovery_required'], uncertain)
                    self.assertEqual((store / 'run.lock').exists(), uncertain)
                    self.assertNotIn('approved', state['statuses'].values())
                    review.assert_not_called()
                    if uncertain:
                        self.assertIn('process alive', state['error'])
                        with self.assertRaises(RuntimeError):
                            st.reserve_run(ws)

    def test_uncertain_process_cleanup_preserves_reservation(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            with (
                patch.object(runner, '_execute', side_effect=errors.RecoveryRequired('process alive')),
            ):
                with self.assertRaises(errors.RecoveryRequired):
                    runner.execute(ws, dict(tasks=[task(['a'])]), 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(ws)['recovery_required'])
                with self.assertRaises(RuntimeError):
                    st.reserve_run(ws)

    def test_invalid_reservation_cannot_overwrite_active_status(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            token = st.reserve_run(ws)
            run_id = st.reservation_id(token)
            ws.publish_run(run_id)
            original = '{"state":"starting","statuses":{}}'
            ws.status(run_id).write_text(original)
            with self.assertRaises(RuntimeError):
                runner.execute(ws, dict(tasks=[task(['a'])]), 1, 0, ['test'], reservation='invalid')
            self.assertEqual(ws.status(run_id).read_text(), original)
            self.assertEqual(ws.latest_run_id(), run_id)
            self.assertTrue((store / 'run.lock').exists())
            st.release_run(ws, token)

    def test_interruption_persists_terminal_state(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            with patch.object(runner, '_execute', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    runner.execute(ws, dict(tasks=[task(['a'])]), 1, 0, ['test'])
            self.assertEqual(support.read_status(ws)['state'], 'failed')
            self.assertFalse((store / 'run.lock').exists())

    def test_large_unicode_input_roundtrip(self):
        payload = 'ação ' * 14000
        result = proc.run([sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read())'], input_data=payload)
        self.assertEqual(result.stdout, payload)

    def test_review_sends_full_patch_through_stdin(self):
        patch_text = '+hello\n' * 12000

        def command(args, **kw):
            if args[0] == 'claude':
                self.assertLess(len(subprocess.list2cmdline(args)), 2000)
                self.assertIn(patch_text, json.loads(kw['input_data'].split('\n', 1)[1])['patch'])
                return subprocess.CompletedProcess(args, 0, json.dumps(dict(result='APPROVED')), '')
            return subprocess.CompletedProcess(args, 0, '', '')

        def git(*args, **kwargs):
            output = patch_text if args[0] == 'show' else ('commit' if args[0] == 'rev-parse' else '')
            if '--binary' in args:
                output = output.encode()
            return subprocess.CompletedProcess(args, 0, output, '')

        current = task(['a'])
        with patch.object(proc, 'run', side_effect=command), patch.object(gitops, 'git', side_effect=git):
            self.assertTrue(
                agents.review(current, agents.Artifact(task=current, worktree='.', report='done'), ['test'])[0]
            )

    def test_preflight_failure_persists_terminal_state(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            with patch.object(runner.shutil, 'which', return_value=None):
                with self.assertRaises(RuntimeError):
                    runner.execute(ws, dict(tasks=[task(['a'])]), 1, 0, ['test'])
            state = support.read_status(ws)
            self.assertEqual(state['state'], 'failed')
            self.assertIn('PATH', state['error'])
            self.assertFalse((store / 'run.lock').exists())

    def test_preflight_failure_discards_previous_run_results(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            previous = dict(
                state='completed',
                statuses={'OLD': 'approved'},
                details={'OLD': {'commit': 'old-commit'}},
                recovery_required=True,
            )
            old = support.publish_previous_run(ws, previous)
            with patch.object(runner.shutil, 'which', return_value=None):
                with self.assertRaises(RuntimeError):
                    runner.execute(ws, dict(tasks=[task(['a'])]), 1, 0, ['test'])
            state = support.read_status(ws)
            self.assertNotEqual(state['run_id'], old)
            self.assertEqual(state['state'], 'failed')
            self.assertEqual(state['statuses'], {})
            self.assertEqual(state['details'], {})
            self.assertFalse(state['recovery_required'])
            self.assertFalse((store / 'run.lock').exists())
            # O registro da execução anterior continua disponível para inspeção.
            self.assertEqual(json.loads(ws.status(old).read_text())['statuses'], {'OLD': 'approved'})

    def test_interrupted_git_integration_preserves_reservation(self):
        for operation in ('cherry-pick', 'revert'):
            with self.subTest(operation=operation), support.temp_dir() as directory:
                store = Path(directory)
                ws = support.workspace(store)
                current = task(['a'])

                def git(*args, **kwargs):
                    if args[0] == operation:
                        raise KeyboardInterrupt
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')

                artifact = agents.Artifact(task=current, commit='commit', worktree='.', report='done')
                with (
                    patch.object(gitops, 'git', side_effect=git),
                    patch.object(runner.shutil, 'which', return_value='tool'),
                    patch.object(agents, 'run_executor', return_value=artifact),
                    patch.object(agents, 'review', return_value=(True, 'APPROVED', '')),
                    patch.object(proc, 'run', side_effect=RuntimeError('integration failed')),
                ):
                    try:
                        runner.execute(ws, dict(tasks=[current]), 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Interrupção Git escapou sem exigir recuperação')
                    self.assertTrue((store / 'run.lock').exists())
                    state = support.read_status(ws)
                    self.assertTrue(state['recovery_required'])
                    self.assertEqual(state['statuses'], {'A': 'failed'})
                    with self.assertRaises(RuntimeError):
                        st.reserve_run(ws)

    def test_executor_accepts_spaces_unicode_and_rename(self):
        with support.temp_dir() as directory:
            root = Path(directory)

            def git(*args):
                return gitops.git(*args, cwd=root)

            git('init', '-b', 'main')
            (root / 'old.txt').write_text('baseline')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'baseline')
            git('mv', 'old.txt', 'ação com espaço.txt')
            original = proc.run

            def command(args, **kw):
                if args[0] == 'agy':
                    self.assertIn('--sandbox', args)
                    self.assertEqual(args[args.index('--mode') + 1], 'auto')
                    result = dict(status='SUCCESS', response='done')
                    return subprocess.CompletedProcess(args, 0, json.dumps(dict(event='result', result=result)), '')
                return original(args, **kw)

            with (
                patch.object(agents, 'make_worktree', return_value=root),
                patch.object(proc, 'run', side_effect=command),
            ):
                artifact = agents.run_executor(
                    support.context(support.workspace(root)),
                    task(['old.txt', 'ação com espaço.txt']),
                    git('rev-parse', 'HEAD').stdout.strip(),
                    '',
                )
            self.assertEqual(git('status', '--porcelain').stdout, '')
            self.assertEqual(artifact.commit, git('rev-parse', 'HEAD').stdout.strip())

    @unittest.skipUnless(os.name == 'posix', 'POSIX filenames')
    def test_literal_backslash_does_not_grant_directory_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gitops.git('init', cwd=root)
            gitops.git(
                '-c',
                'user.name=Test',
                '-c',
                'user.email=test@localhost',
                'commit',
                '--allow-empty',
                '-m',
                'baseline',
                cwd=root,
            )
            (root / r'src\outside').touch()
            with self.assertRaisesRegex(RuntimeError, 'fora do escopo'):
                gitops.check_scope({'files': ['src'], 'id': 'A'}, root, 'HEAD')

    @unittest.skipUnless(os.name == 'posix', 'POSIX process group')
    def test_success_stops_background_process(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'late'
            child = f'import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()'
            parent = (
                'import subprocess as s, sys; '
                f's.Popen([sys.executable, "-c", {child!r}], stdin=s.DEVNULL, stdout=s.DEVNULL, stderr=s.DEVNULL)'
            )
            self.assertEqual(proc.run([sys.executable, '-c', parent]).returncode, 0)
            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_timeout_stops_descendant_before_it_writes(self):
        with support.temp_dir() as directory:
            root = Path(directory).resolve()
            marker = root / 'late.txt'
            child = (
                'import time; from pathlib import Path; time.sleep(2); Path('
                + repr(str(marker))
                + ').write_text("late")'
            )
            parent = (
                'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
                + repr(child)
                + '],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(20)'
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run([sys.executable, '-c', parent], timeout=0.6)
            time.sleep(2.1)
            self.assertFalse(marker.exists())
