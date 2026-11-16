"""State persistence must not discard recovery or fail on brief Windows readers."""

import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import errors, runner
from contracanto import state as st


class StatePersistenceTests(unittest.TestCase):
    def test_failed_starting_write_cannot_relabel_previous_run(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            path = store / 'status.json'
            write = st.write_json
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

            with patch.object(st, 'write_json', side_effect=fail_starting):
                with self.assertRaisesRegex(PermissionError, 'initial status locked'):
                    runner.execute(ws, {}, 1, 0, ['test'])
            state = json.loads(path.read_text())
            self.assertEqual(state['state'], 'failed')
            self.assertNotEqual(state['run_id'], 'old')
            self.assertEqual(state['statuses'], {})
            self.assertEqual(state['details'], {})
            self.assertFalse(state['recovery_required'])
            self.assertFalse((store / 'run.lock').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows blocks unlink while a reader is open')
    def test_release_waits_for_status_reader(self):
        with support.temp_workspace() as ws:
            token = st.reserve_run(ws)
            with ws.lock.open() as reader:
                release = threading.Timer(0.15, reader.close)
                release.start()
                try:
                    st.release_run(ws, token)
                finally:
                    release.join()
            next_token = st.reserve_run(ws)
            st.release_run(ws, next_token)

    def test_unsaved_recovery_state_still_blocks_new_runs(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            original_replace = Path.replace

            def replace(source, target):
                if json.loads(source.read_text()).get('recovery_required'):
                    raise PermissionError('cannot persist recovery')
                return original_replace(source, target)

            def execute(ctx, *args):
                support.recovery_pending(ctx, {'A': 'failed'})

            with (
                patch.object(runner, '_execute', side_effect=execute),
                patch.object(Path, 'replace', replace),
            ):
                with self.assertRaises(errors.RecoveryRequired):
                    runner.execute(ws, {}, 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                with self.assertRaises(RuntimeError):
                    st.reserve_run(ws)

    def test_final_write_failure_preserves_recovery_reservation(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent), support.temp_dir() as directory:
                store = Path(directory)
                ws = support.workspace(store)
                original_write = st.write_json
                failed = False

                def execute(ctx, *args):
                    support.recovery_pending(ctx, {'A': 'failed'})
                    return {'A': 'failed'}

                def write(path, data):
                    nonlocal failed
                    if data.get('state') == 'failed' and (persistent or not failed):
                        failed = True
                        raise PermissionError('status locked')
                    original_write(path, data)

                with (
                    patch.object(runner, '_execute', side_effect=execute),
                    patch.object(st, 'write_json', side_effect=write),
                ):
                    with self.assertRaises(PermissionError):
                        runner.execute(ws, {}, 1, 0, ['test'])
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                    with self.assertRaises(RuntimeError):
                        st.reserve_run(ws)

    def test_failure_after_persisting_recovery_preserves_reservation(self):
        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)

            def execute(ctx, *args):
                support.recovery_pending(ctx, {'A': 'failed'})
                raise OSError('subsequent storage failure')

            with patch.object(runner, '_execute', side_effect=execute):
                with self.assertRaises(OSError):
                    runner.execute(ws, {}, 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])

    @unittest.skipUnless(os.name == 'nt', 'Windows blocks replacement while readers are open')
    def test_status_write_survives_brief_windows_reader(self):
        with support.temp_dir() as directory:
            path = Path(directory) / 'status.json'
            st.write_json(path, {'state': 'starting'})
            reader = path.open('r', encoding='utf-8')
            release = threading.Timer(0.15, reader.close)
            release.start()
            try:
                st.write_json(path, {'state': 'completed'})
            finally:
                release.join()
                reader.close()
            self.assertEqual(json.loads(path.read_text()), {'state': 'completed'})

    def test_status_write_retries_transient_permission_failure(self):
        with support.temp_dir() as directory:
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
                st.write_json(path, {'state': 'completed'})
            self.assertEqual(json.loads(path.read_text()), {'state': 'completed'})

    def test_permanent_write_failure_preserves_previous_json(self):
        with support.temp_dir() as directory:
            path = Path(directory) / 'status.json'
            st.write_json(path, {'state': 'starting'})
            with patch.object(Path, 'replace', side_effect=PermissionError('permanent denial')):
                with self.assertRaises(PermissionError):
                    st.write_json(path, {'state': 'completed'})
            self.assertEqual(json.loads(path.read_text()), {'state': 'starting'})
