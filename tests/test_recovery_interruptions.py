"""Recovery reservations survive interruptions and partial worker submission."""

import concurrent.futures
import contextlib
import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, errors, gitops, runner
from contracanto import process as proc
from contracanto import state as st


class RecoveryInterruptionTests(unittest.TestCase):
    def test_interrupted_cleanup_preserves_reservation(self):
        for stage in ('terminate', 'wait', 'kill'):
            with self.subTest(stage=stage), support.temp_dir() as directory:
                store = Path(directory)
                ws = support.workspace(store)
                with contextlib.ExitStack() as stack:
                    launch = stack.enter_context(patch.object(proc.subprocess, 'Popen'))
                    process = launch.return_value
                    process.communicate.side_effect = [
                        subprocess.TimeoutExpired(['test'], 1),
                        KeyboardInterrupt('cleanup interrupted'),
                    ]
                    if stage == 'kill':
                        process.kill.side_effect = KeyboardInterrupt('kill interrupted')
                    if os.name == 'nt':
                        job = stack.enter_context(patch.object(proc, 'WindowsJob'))
                        job.creationflags = 4
                        terminate = job.return_value.__enter__.return_value.terminate
                    else:
                        terminate = stack.enter_context(patch.object(proc.os, 'killpg'))
                    if stage == 'wait':
                        terminate.return_value = subprocess.CompletedProcess([], 0, b'', b'')
                    else:
                        terminate.side_effect = KeyboardInterrupt('termination interrupted')
                    stack.enter_context(patch.object(runner, '_execute', side_effect=lambda *args: proc.run(['test'])))
                    try:
                        with self.assertRaises(errors.RecoveryRequired):
                            runner.execute(ws, {}, 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Cleanup interruption escaped without preserving recovery')
                    state = json.loads((store / 'status.json').read_text())
                    self.assertEqual(state['state'], 'failed')
                    self.assertTrue(state['recovery_required'])
                    self.assertTrue((store / 'run.lock').exists())
                    with self.assertRaises(RuntimeError):
                        st.reserve_run(ws)

    def test_partial_submission_preserves_worker_recovery(self):
        for queued in (False, True):
            with self.subTest(queued=queued), support.temp_dir() as directory:
                store = Path(directory)
                ws = support.workspace(store)
                release = threading.Event()
                finished = threading.Event()

                class FailingPool(concurrent.futures.ThreadPoolExecutor):
                    submissions = 0

                    def submit(self, fn, *args, **kwargs):
                        self.submissions += 1
                        if self.submissions == 2:
                            if queued:
                                super().submit(fn, *args, **kwargs)
                            release.set()
                            raise RuntimeError("can't start new thread")
                        return super().submit(fn, *args, **kwargs)

                def worker(*args):
                    if not release.wait(timeout=5):
                        raise RuntimeError('Test did not release worker')
                    finished.set()
                    raise errors.RecoveryRequired('descendant termination unconfirmed')

                def git(*args, **kwargs):
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')

                tasks = [
                    dict(id=tid, description='Example', deps=[], files=[tid + '.txt'], acceptance=['Pass'])
                    for tid in ('A', 'B')
                ]
                with (
                    patch.object(gitops, 'git', side_effect=git),
                    patch.object(runner.shutil, 'which', return_value='tool'),
                    patch.object(agents, 'run_executor', side_effect=worker),
                    patch.object(runner.concurrent.futures, 'ThreadPoolExecutor', FailingPool),
                ):
                    with self.assertRaises(RuntimeError):
                        runner.execute(ws, {'tasks': tasks}, 2, 0, ['test'])
                    self.assertTrue(finished.is_set())
                    state = json.loads((store / 'status.json').read_text())
                    self.assertEqual(state['state'], 'failed')
                    self.assertTrue(state['recovery_required'])
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertNotIn('approved', state['statuses'].values())
                    with self.assertRaises(RuntimeError):
                        st.reserve_run(ws)
