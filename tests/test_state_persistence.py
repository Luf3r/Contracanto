"""State persistence must not discard recovery or fail on brief Windows readers."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator as o


class StatePersistenceTests(unittest.TestCase):
    def test_failed_starting_write_cannot_relabel_previous_run(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            path = store / 'status.json'
            write = o.write_json
            write(
                path,
                dict(
                    state='failed',
                    run_id='old',
                    statuses={'OLD': 'approved'},
                    details={'OLD': {'review': 'old review'}},
                    recovery_required=True,
                ),
            )

            def fail_starting(path, value):
                if value.get('state') == 'starting':
                    raise PermissionError('initial status locked')
                write(path, value)

            with patch.object(o, 'STORE', store), patch.object(o, 'write_json', side_effect=fail_starting):
                with self.assertRaisesRegex(PermissionError, 'initial status locked'):
                    o.execute({}, 1, 0, ['test'])
            state = json.loads(path.read_text())
            self.assertEqual(state['state'], 'failed')
            self.assertNotEqual(state['run_id'], 'old')
            self.assertEqual(state['statuses'], {})
            self.assertEqual(state['details'], {})
            self.assertFalse(state['recovery_required'])
            self.assertFalse((store / 'run.lock').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows blocks unlink while a reader is open')
    def test_release_waits_for_status_reader(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory, patch.object(o, 'STORE', Path(directory)):
            token = o.reserve_run()
            with (o.STORE / 'run.lock').open() as reader:
                release = threading.Timer(0.15, reader.close)
                release.start()
                try:
                    o.release_run(token)
                finally:
                    release.join()
            next_token = o.reserve_run()
            o.release_run(next_token)

    def test_unsaved_recovery_state_still_blocks_new_runs(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            original_replace = Path.replace

            def replace(source, target):
                if json.loads(source.read_text()).get('recovery_required'):
                    raise PermissionError('cannot persist recovery')
                return original_replace(source, target)

            def execute(*args):
                o.write_json(store / 'status.json', {'statuses': {'A': 'failed'}, 'recovery_required': True})

            with (
                patch.object(o, 'STORE', store),
                patch.object(o, '_execute', side_effect=execute),
                patch.object(Path, 'replace', replace),
            ):
                with self.assertRaises(o.RecoveryRequired):
                    o.execute({}, 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                with self.assertRaises(RuntimeError):
                    o.reserve_run()

    def test_final_write_failure_preserves_recovery_reservation(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                original_write = o.write_json
                failed = False

                def execute(*args):
                    original_write(store / 'status.json', {'statuses': {'A': 'failed'}, 'recovery_required': True})
                    return {'A': 'failed'}

                def write(path, data):
                    nonlocal failed
                    if data.get('state') == 'failed' and (persistent or not failed):
                        failed = True
                        raise PermissionError('status locked')
                    original_write(path, data)

                with (
                    patch.object(o, 'STORE', store),
                    patch.object(o, '_execute', side_effect=execute),
                    patch.object(o, 'write_json', side_effect=write),
                ):
                    with self.assertRaises(PermissionError):
                        o.execute({}, 1, 0, ['test'])
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()

    def test_failure_after_persisting_recovery_preserves_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)

            def execute(*args):
                o.write_json(store / 'status.json', {'statuses': {'A': 'failed'}, 'recovery_required': True})
                raise OSError('subsequent storage failure')

            with patch.object(o, 'STORE', store), patch.object(o, '_execute', side_effect=execute):
                with self.assertRaises(OSError):
                    o.execute({}, 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])

    @unittest.skipUnless(os.name == 'nt', 'Windows blocks replacement while readers are open')
    def test_status_write_survives_brief_windows_reader(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            path = Path(directory) / 'status.json'
            o.write_json(path, {'state': 'starting'})
            reader = path.open('r', encoding='utf-8')
            release = threading.Timer(0.15, reader.close)
            release.start()
            try:
                o.write_json(path, {'state': 'completed'})
            finally:
                release.join()
                reader.close()
            self.assertEqual(json.loads(path.read_text()), {'state': 'completed'})

    def test_status_write_retries_transient_permission_failure(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            path = Path(directory) / 'status.json'
            original_replace = Path.replace
            failed = False

            def replace(source, target):
                nonlocal failed
                if not failed:
                    failed = True
                    raise PermissionError('reader still open')
                return original_replace(source, target)

            with patch.object(Path, 'replace', replace):
                o.write_json(path, {'state': 'completed'})
            self.assertEqual(json.loads(path.read_text()), {'state': 'completed'})

    def test_permanent_write_failure_preserves_previous_json(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            path = Path(directory) / 'status.json'
            o.write_json(path, {'state': 'starting'})
            with patch.object(Path, 'replace', side_effect=PermissionError('permanent denial')):
                with self.assertRaises(PermissionError):
                    o.write_json(path, {'state': 'completed'})
            self.assertEqual(json.loads(path.read_text()), {'state': 'starting'})
