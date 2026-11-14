"""Regressões operacionais; nenhum teste chama modelos pagos."""
import json
import concurrent.futures
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import orchestrator as o


def task(files):
    return dict(id='A', description='Example', deps=[], files=files, acceptance=['Pass'])


class OperationalTests(unittest.TestCase):
    def test_interrupted_worker_collection_preserves_uncertain_recovery(self):
        for failure, collected in ((o.RecoveryRequired('process alive'), False),
                                   (o.RecoveryRequired('process alive'), True),
                                   (RuntimeError('known failure'), False),
                                   (None, False)):
            with self.subTest(failure=failure, collected=collected), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                current = task(['a'])
                artifact = dict(task=current, commit='commit', worktree='.', report='done')

                def interrupted(jobs):
                    concurrent.futures.wait(jobs)
                    if collected:
                        yield next(iter(jobs))
                    raise KeyboardInterrupt

                def git(*args, **kwargs):
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')

                with patch.object(o, 'STORE', store), patch.object(o, 'git', side_effect=git), \
                        patch.object(o.shutil, 'which', return_value='tool'), \
                        patch.object(o, 'run_executor', side_effect=failure, return_value=artifact), \
                        patch.object(o.concurrent.futures, 'as_completed', side_effect=interrupted), \
                        patch.object(o, 'review') as review:
                    uncertain = isinstance(failure, o.RecoveryRequired)
                    try:
                        with self.assertRaises(o.RecoveryRequired if uncertain else KeyboardInterrupt):
                            o.execute(dict(tasks=[current]), 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Interrupção descartou a recuperação exigida pelo worker')
                    state = json.loads((store / 'status.json').read_text())
                    self.assertEqual(state['state'], 'failed')
                    self.assertEqual(state['recovery_required'], uncertain)
                    self.assertEqual((store / 'run.lock').exists(), uncertain)
                    self.assertNotIn('approved', state['statuses'].values())
                    review.assert_not_called()
                    if uncertain:
                        self.assertIn('process alive', state['error'])
                        with self.assertRaises(RuntimeError):
                            o.reserve_run()

    def test_uncertain_process_cleanup_preserves_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(o, '_execute', side_effect=o.RecoveryRequired('process alive')):
                with self.assertRaises(o.RecoveryRequired):
                    o.execute(dict(tasks=[task(['a'])]), 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                with self.assertRaises(RuntimeError):
                    o.reserve_run()

    def test_invalid_reservation_cannot_overwrite_active_status(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store):
                token = o.reserve_run()
                original = '{"state":"starting","statuses":{}}'
                (store / 'status.json').write_text(original)
                with self.assertRaises(RuntimeError):
                    o.execute(dict(tasks=[task(['a'])]), 1, 0, ['test'], reservation='invalid')
                self.assertEqual((store / 'status.json').read_text(), original)
                self.assertTrue((store / 'run.lock').exists())
                o.release_run(token)

    def test_interruption_persists_terminal_state(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(o, '_execute', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    o.execute(dict(tasks=[task(['a'])]), 1, 0, ['test'])
            self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'failed')
            self.assertFalse((store / 'run.lock').exists())

    def test_large_unicode_input_roundtrip(self):
        payload = 'ação ' * 14000
        result = o.cmd([sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read())'],
                       input_text=payload)
        self.assertEqual(result.stdout, payload)

    def test_review_sends_full_patch_through_stdin(self):
        patch_text = '+hello\n' * 12000
        def command(args, **kw):
            if args[0] == 'claude':
                self.assertLess(len(subprocess.list2cmdline(args)), 2000)
                self.assertIn(patch_text, json.loads(kw['input_text'].split('\n', 1)[1])['patch'])
                return subprocess.CompletedProcess(args, 0, json.dumps(dict(result='APPROVED')), '')
            return subprocess.CompletedProcess(args, 0, '', '')
        def git(*args, **kwargs):
            output = patch_text if args[0] == 'show' else ('commit' if args[0] == 'rev-parse' else '')
            return subprocess.CompletedProcess(args, 0, output, '')
        with patch.object(o, 'cmd', side_effect=command), patch.object(o, 'git', side_effect=git):
            self.assertTrue(o.review(task(['a']), dict(worktree='.', report='done'), ['test'])[0])

    def test_preflight_failure_persists_terminal_state(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(o.shutil, 'which', return_value=None):
                with self.assertRaises(RuntimeError):
                    o.execute(dict(tasks=[task(['a'])]), 1, 0, ['test'])
            state = json.loads((store / 'status.json').read_text())
            self.assertEqual(state['state'], 'failed')
            self.assertIn('PATH', state['error'])
            self.assertFalse((store / 'run.lock').exists())

    def test_preflight_failure_discards_previous_run_results(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            previous = dict(state='completed', run_id='old', statuses={'OLD': 'approved'},
                            details={'OLD': {'commit': 'old-commit'}}, recovery_required=True)
            (store / 'status.json').write_text(json.dumps(previous))
            with patch.object(o, 'STORE', store), patch.object(o.shutil, 'which', return_value=None):
                with self.assertRaises(RuntimeError):
                    o.execute(dict(tasks=[task(['a'])]), 1, 0, ['test'])
            state = json.loads((store / 'status.json').read_text())
            self.assertNotEqual(state['run_id'], 'old')
            self.assertEqual(state['state'], 'failed')
            self.assertEqual(state['statuses'], {})
            self.assertEqual(state['details'], {})
            self.assertFalse(state['recovery_required'])
            self.assertFalse((store / 'run.lock').exists())

    def test_interrupted_git_integration_preserves_reservation(self):
        for operation in ('cherry-pick', 'revert'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                current = task(['a'])
                def git(*args, **kwargs):
                    if args[0] == operation:
                        raise KeyboardInterrupt
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')
                artifact = dict(task=current, commit='commit', worktree='.', report='done')
                with patch.object(o, 'STORE', store), patch.object(o, 'git', side_effect=git), \
                        patch.object(o.shutil, 'which', return_value='tool'), \
                        patch.object(o, 'run_executor', return_value=artifact), \
                        patch.object(o, 'review', return_value=(True, 'APPROVED', '')), \
                        patch.object(o, 'cmd', side_effect=RuntimeError('integration failed')):
                    try:
                        o.execute(dict(tasks=[current]), 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Interrupção Git escapou sem exigir recuperação')
                    self.assertTrue((store / 'run.lock').exists())
                    state = json.loads((store / 'status.json').read_text())
                    self.assertTrue(state['recovery_required'])
                    self.assertEqual(state['statuses'], {'A': 'failed'})
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()

    def test_executor_accepts_spaces_unicode_and_rename(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            root = Path(directory)
            def git(*args):
                return o.git(*args, cwd=root)
            git('init', '-b', 'main')
            (root / 'old.txt').write_text('baseline')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'baseline')
            git('mv', 'old.txt', 'ação com espaço.txt')
            original = o.cmd
            def command(args, **kw):
                if args[0] == 'agy':
                    self.assertIn('--sandbox', args)
                    self.assertEqual(args[args.index('--mode') + 1], 'auto')
                    result = dict(status='SUCCESS', response='done')
                    return subprocess.CompletedProcess(args, 0, json.dumps(dict(event='result', result=result)), '')
                return original(args, **kw)
            with patch.object(o, 'make_worktree', return_value=root), patch.object(o, 'cmd', side_effect=command):
                artifact = o.run_executor(task(['old.txt', 'ação com espaço.txt']), git('rev-parse', 'HEAD').stdout.strip(), '')
            self.assertEqual(git('status', '--porcelain').stdout, '')
            self.assertEqual(artifact['commit'], git('rev-parse', 'HEAD').stdout.strip())

    def test_timeout_stops_descendant_before_it_writes(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            root = Path(directory).resolve()
            marker = root / 'late.txt'
            child = 'import time; from pathlib import Path; time.sleep(2); Path(' + repr(str(marker)) + ').write_text("late")'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + '],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(20)'
            with self.assertRaises(subprocess.TimeoutExpired):
                o.cmd([sys.executable, '-c', parent], timeout=0.6)
            time.sleep(2.1)
            self.assertFalse(marker.exists())
