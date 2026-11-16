import json
import concurrent.futures
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

import orchestrator as o
import mcp_server as server


PLAN = json.dumps({'tasks': [dict(id='A', description='Example', deps=[],
                                 files=['A.txt'], acceptance=['Tests pass'])]})


class SubmissionTests(unittest.TestCase):
    def test_submit_schema_requires_test_command(self):
        tool = server.mcp._tool_manager.get_tool('submit_plan')
        self.assertIn('test_command_json', tool.parameters['required'])

    def test_status_with_legacy_log_encoding(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            (store / 'status.json').write_text('{"state":"failed"}', encoding='utf-8')
            (store / 'run.log').write_bytes('falha de execução'.encode('cp1252'))
            with patch.object(server, 'STORE', store):
                report = server.get_run_status()
            self.assertIn('"state":"failed"', report)
            self.assertIn('falha de execução', report)

    def test_stop_before_worker_claim_and_stale_request(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory, \
                patch.object(o, 'STORE', Path(directory)), patch.object(server, 'STORE', Path(directory)):
            self.assertIn('Nenhuma execução', server.stop_run())
            token = o.reserve_run()
            run_id = o.reservation_id(token)
            with patch.object(sys, 'argv', ['orchestrator.py', '--stop']):
                o.main()
            self.assertIn('solicitado', server.stop_run(run_id))
            with patch.object(o, '_execute') as start, self.assertRaises(o.RunCancelled):
                o.execute(json.loads(PLAN), 1, 0, ['test'], token)
            start.assert_not_called()
            self.assertEqual(json.loads((o.STORE / 'status.json').read_text())['state'], 'cancelled')
            next_token = o.reserve_run()
            self.assertIn('outra execução', server.stop_run(run_id))
            self.assertNotIn('stop_requested', server.get_run_status())
            o.release_run(next_token)

    def test_stop_during_worker_command(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            started = store / 'started'
            def worker(*args):
                program = 'import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)'
                return o.cmd([sys.executable, '-c', program, str(started)], timeout=15, cancellable=True)

            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                    patch.object(o.shutil, 'which', return_value='tool'), \
                    patch.object(o, 'git', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                    patch.object(o, 'run_executor', side_effect=worker), \
                    concurrent.futures.ThreadPoolExecutor(1) as pool:
                run = pool.submit(o.execute, json.loads(PLAN), 1, 0, ['test'])
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline and not run.done():
                    time.sleep(0.02)
                self.assertTrue(started.exists())
                self.assertIn('solicitado', server.stop_run())
                with self.assertRaises(o.RunCancelled):
                    run.result(timeout=5)
                state = json.loads((store / 'status.json').read_text())
                self.assertEqual(state['state'], 'cancelled')
                self.assertEqual(state['statuses'], {'A': 'cancelled'})
                self.assertFalse((store / 'run.lock').exists())

    def test_status_reports_pre_upgrade_reservation_without_legacy_credential(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            token = 'previous-worker-credential'
            (store / 'run.lock').write_text(json.dumps({'pid': 123, 'token': token}))
            (store / 'status.json').write_text(json.dumps({'state': 'failed', 'run_id': token,
                                                          'recovery_required': True}))
            with patch.object(server, 'STORE', store):
                status = server.get_run_status()
            self.assertNotIn(token, status)
            self.assertIn('123', status)
            self.assertTrue(json.loads(status.split('\n---')[0])['recovery_required'])

    def test_only_first_mcp_worker_can_claim_or_release_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            source = store / 'submitted-plan.json'
            missing = store / 'missing.json'
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                    patch.object(server.subprocess, 'Popen') as launch:
                server.submit_plan(PLAN, test_command_json='["test"]')
                argv = launch.call_args.args[0][1:]
                credential = argv[argv.index('--reservation') + 1]
                public_id = json.loads((store / 'status.json').read_text())['run_id']

                def attempt(token, path):
                    with patch.object(sys, 'argv', [argv[0], str(path), '--reservation', token, '--test', 'test']):
                        with self.assertRaises(RuntimeError):
                            o.main()

                def assert_still_reserved():
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertEqual(json.loads(source.read_text()), json.loads(PLAN))
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()

                # A status reader must not be able to take even an unclaimed run.
                self.assertNotIn(credential, server.get_run_status())
                attempt(public_id, source)
                attempt(public_id, missing)
                assert_still_reserved()

                def running(*args):
                    state = (store / 'status.json').read_bytes()
                    for path in (source, missing):
                        attempt(credential, path)
                        assert_still_reserved()
                        self.assertEqual((store / 'status.json').read_bytes(), state)
                    return {'A': 'approved'}

                with patch.object(sys, 'argv', argv), patch.object(o, '_execute', side_effect=running):
                    o.main()
                self.assertFalse((store / 'run.lock').exists())
                self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'completed')

    def test_status_survives_reservation_release_during_read(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            status = '{"state":"completed","statuses":{"A":"approved"}}'
            (store / 'status.json').write_text(status, encoding='utf-8')
            (store / 'run.log').write_text('worker finished', encoding='utf-8')
            reservation = store / 'run.lock'
            reservation.write_text('{}', encoding='utf-8')
            original_read = Path.read_text

            def read_after_release(path, *args, **kwargs):
                if path == reservation:
                    path.unlink()
                return original_read(path, *args, **kwargs)

            with patch.object(server, 'STORE', store), patch.object(Path, 'read_text', read_after_release):
                try:
                    result = server.get_run_status()
                except FileNotFoundError as error:
                    self.fail(f'Normal reservation release broke status polling: {error}')
            self.assertIn(status, result)
            self.assertIn('Sem reserva ativa', result)
            self.assertIn('worker finished', result)

    def test_status_does_not_hide_unreadable_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            reservation = store / 'run.lock'
            reservation.write_text('{}', encoding='utf-8')
            original_read = Path.read_text

            def denied_read(path, *args, **kwargs):
                if path == reservation:
                    raise PermissionError('reservation unreadable')
                return original_read(path, *args, **kwargs)

            with patch.object(server, 'STORE', store), patch.object(Path, 'read_text', denied_read):
                with self.assertRaisesRegex(PermissionError, 'reservation unreadable'):
                    server.get_run_status()

    def test_pid_write_failure_reports_started_worker_and_preserves_state(self):
        for previous_pid in (None, '456'):
            with self.subTest(previous_pid=previous_pid), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                pid_file = store / 'pid.txt'
                if previous_pid is not None:
                    pid_file.write_text(previous_pid, encoding='utf-8')
                original_write = Path.write_text

                def denied_pid_write(path, *args, **kwargs):
                    if path == pid_file:
                        raise PermissionError('pid file locked')
                    return original_write(path, *args, **kwargs)

                with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                        patch.object(server.subprocess, 'Popen') as launch, \
                        patch.object(Path, 'write_text', denied_pid_write):
                    launch.return_value.pid = 123
                    try:
                        result = server.submit_plan(PLAN, test_command_json='["test"]')
                    except PermissionError as error:
                        self.fail(f'Launched worker was reported as a failed submission: {error}')
                    self.assertIn('Execução iniciada com PID 123', result)
                    self.assertIn('Aviso:', result)
                    self.assertIn('pid.txt', result)
                    self.assertIn('pid file locked', result)
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'starting')
                    with self.assertRaisesRegex(RuntimeError, 'reservada'):
                        o.reserve_run()

    def test_cli_publishes_current_plan_even_when_preflight_fails(self):
        for previous in (None, {'goal': 'Previous MCP plan', 'tasks': []}):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)
                if previous is not None:
                    (store / 'submitted-plan.json').write_text(json.dumps(previous), encoding='utf-8')
                plan = dict(json.loads(PLAN), goal='Novo objetivo: documentação')
                source = store / 'cli-plan.json'
                source.write_text(json.dumps(plan), encoding='utf-8')
                with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                        patch.object(o.shutil, 'which', return_value=None), \
                        patch.object(sys, 'argv', ['orchestrator.py', str(source), '--test', 'test']):
                    with self.assertRaisesRegex(RuntimeError, 'PATH'):
                        o.main()
                    self.assertTrue((store / 'submitted-plan.json').exists())
                    self.assertEqual(json.loads(server.get_submitted_plan()), plan)
                    self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'failed')
                    self.assertFalse((store / 'run.lock').exists())

    def test_cli_cannot_replace_plan_of_reserved_run(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            (store / 'submitted-plan.json').write_text(PLAN, encoding='utf-8')
            source = store / 'cli-plan.json'
            source.write_text(PLAN.replace('Example', 'New plan'), encoding='utf-8')
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                    patch.object(sys, 'argv', ['orchestrator.py', str(source), '--test', 'test']):
                o.reserve_run()
                reservation = (store / 'run.lock').read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'reservada'):
                    o.main()
                self.assertEqual(json.loads(server.get_submitted_plan()), json.loads(PLAN))
                self.assertEqual((store / 'run.lock').read_bytes(), reservation)

    def test_second_submission_cannot_overwrite_first(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), patch.object(server.subprocess, 'Popen') as launch:
                launch.return_value.pid = 123
                first = server.submit_plan(PLAN, test_command_json='["python", "-m", "unittest"]')
                second = server.submit_plan(PLAN.replace('Example', 'Second'), test_command_json='["python", "-m", "unittest"]')
                self.assertIn('PID', first)
                self.assertIn('execução', second.lower())
                self.assertEqual(launch.call_count, 1)
                self.assertIn('Example', (store / 'submitted-plan.json').read_text())

    def test_failed_launch_releases_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), patch.object(server.subprocess, 'Popen', side_effect=OSError('launch failed')):
                with self.assertRaises(OSError):
                    server.submit_plan(PLAN, test_command_json='["test"]')
                self.assertFalse((store / 'run.lock').exists())
                state = json.loads((store / 'status.json').read_text())
                self.assertEqual(state['state'], 'failed')
                self.assertIn('launch failed', state['error'])

    def test_status_write_failure_before_launch_does_not_block_next_submission(self):
        for failure_stage in ('starting', 'failed'):
            with self.subTest(failure_stage=failure_stage), tempfile.TemporaryDirectory(dir=o.STORE) as directory:
                store = Path(directory)

                def write(path, value):
                    if path.name == 'status.json' and (failure_stage == 'starting' or value['state'] == 'failed'):
                        raise PermissionError('status unavailable')
                    o.write_json(path, value)

                with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), \
                        patch.object(server.subprocess, 'Popen') as launch:
                    launch.side_effect = OSError('launch failed')
                    with patch.object(server, 'write_json', side_effect=write):
                        with self.assertRaisesRegex(PermissionError, 'status unavailable'):
                            server.submit_plan(PLAN, test_command_json='["test"]')
                    self.assertFalse((store / 'run.lock').exists())

                    launch.side_effect = None
                    launch.return_value.pid = 123
                    result = server.submit_plan(PLAN, test_command_json='["test"]')
                    self.assertIn('PID 123', result)
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'starting')
