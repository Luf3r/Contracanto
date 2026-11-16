"""Recovery reservations survive interruptions and partial worker submission."""

import concurrent.futures
import contextlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator as o


class RecoveryInterruptionTests(unittest.TestCase):
    def test_interrupted_cleanup_preserves_reservation(self):
        for stage in ('terminate', 'wait', 'kill'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(o, 'STORE', store))
                    launch = stack.enter_context(patch.object(o.subprocess, 'Popen'))
                    process = launch.return_value
                    process.communicate.side_effect = [
                        subprocess.TimeoutExpired(['test'], 1),
                        KeyboardInterrupt('cleanup interrupted'),
                    ]
                    if stage == 'kill':
                        process.kill.side_effect = KeyboardInterrupt('kill interrupted')
                    if os.name == 'nt':
                        job = stack.enter_context(patch.object(o, 'WindowsJob'))
                        job.creationflags = 4
                        terminate = job.return_value.__enter__.return_value.terminate
                    else:
                        terminate = stack.enter_context(patch.object(o.os, 'killpg'))
                    if stage == 'wait':
                        terminate.return_value = subprocess.CompletedProcess([], 0, b'', b'')
                    else:
                        terminate.side_effect = KeyboardInterrupt('termination interrupted')
                    stack.enter_context(patch.object(o, '_execute', side_effect=lambda *args: o.cmd(['test'])))
                    try:
                        with self.assertRaises(o.RecoveryRequired):
                            o.execute({}, 1, 0, ['test'])
                    except KeyboardInterrupt:
                        self.fail('Cleanup interruption escaped without preserving recovery')
                    state = json.loads((store / 'status.json').read_text())
                    self.assertEqual(state['state'], 'failed')
                    self.assertTrue(state['recovery_required'])
                    self.assertTrue((store / 'run.lock').exists())
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()

    def test_partial_submission_preserves_worker_recovery(self):
        for queued in (False, True):
            with self.subTest(queued=queued), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
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
                    raise o.RecoveryRequired('descendant termination unconfirmed')

                def git(*args, **kwargs):
                    return subprocess.CompletedProcess(args, 0, 'base' if args[0] == 'rev-parse' else '', '')

                tasks = [
                    dict(id=tid, description='Example', deps=[], files=[tid + '.txt'], acceptance=['Pass'])
                    for tid in ('A', 'B')
                ]
                with (
                    patch.object(o, 'STORE', store),
                    patch.object(o, 'git', side_effect=git),
                    patch.object(o.shutil, 'which', return_value='tool'),
                    patch.object(o, 'run_executor', side_effect=worker),
                    patch.object(o.concurrent.futures, 'ThreadPoolExecutor', FailingPool),
                ):
                    with self.assertRaises(RuntimeError):
                        o.execute({'tasks': tasks}, 2, 0, ['test'])
                    self.assertTrue(finished.is_set())
                    state = json.loads((store / 'status.json').read_text())
                    self.assertEqual(state['state'], 'failed')
                    self.assertTrue(state['recovery_required'])
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertNotIn('approved', state['statuses'].values())
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()
