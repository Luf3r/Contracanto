import concurrent.futures
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import support

import mcp_server as server
from contracanto import agents, cli, errors, gitops, runner
from contracanto import process as proc
from contracanto import state as st

PLAN = json.dumps({'tasks': [dict(id='A', description='Example', deps=[], files=['A.txt'], acceptance=['Tests pass'])]})


class SubmissionTests(unittest.TestCase):
    def test_submit_schema_requires_test_command(self):
        tool = server.mcp._tool_manager.get_tool('submit_plan')
        self.assertIn('test_command_json', tool.parameters['required'])

    def test_status_with_legacy_log_encoding(self):
        with support.temp_workspace(server) as ws:
            ws.status.write_text('{"state":"failed"}', encoding='utf-8')
            ws.log.write_bytes('falha de execução'.encode('cp1252'))
            report = server.get_run_status()
            self.assertIn('"state":"failed"', report)
            self.assertIn('falha de execução', report)

    def test_stop_before_worker_claim_and_stale_request(self):
        with support.temp_workspace(server) as ws:
            self.assertIn('Nenhuma execução', server.stop_run())
            token = st.reserve_run(ws)
            run_id = st.reservation_id(token)
            cli.main(['--stop'], ws)
            self.assertIn('solicitado', server.stop_run(run_id))
            with patch.object(runner, '_execute') as start, self.assertRaises(errors.RunCancelled):
                runner.execute(ws, json.loads(PLAN), 1, 0, ['test'], token)
            start.assert_not_called()
            self.assertEqual(json.loads(ws.status.read_text())['state'], 'cancelled')
            next_token = st.reserve_run(ws)
            self.assertIn('outra execução', server.stop_run(run_id))
            self.assertNotIn('stop_requested', server.get_run_status())
            st.release_run(ws, next_token)

    def test_stop_during_worker_command(self):
        with support.temp_workspace(server) as ws:
            started = ws.store / 'started'

            def worker(ctx, *args):
                program = 'import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)'
                return proc.run([sys.executable, '-c', program, str(started)], timeout=15, stop=ctx.stop)

            with (
                patch.object(runner.shutil, 'which', return_value='tool'),
                patch.object(gitops, 'git', return_value=subprocess.CompletedProcess([], 0, '', '')),
                patch.object(agents, 'run_executor', side_effect=worker),
                concurrent.futures.ThreadPoolExecutor(1) as pool,
            ):
                run = pool.submit(runner.execute, ws, json.loads(PLAN), 1, 0, ['test'])
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline and not run.done():
                    time.sleep(0.02)
                self.assertTrue(started.exists())
                self.assertIn('solicitado', server.stop_run())
                with self.assertRaises(errors.RunCancelled):
                    run.result(timeout=5)
                state = json.loads(ws.status.read_text())
                self.assertEqual(state['state'], 'cancelled')
                self.assertEqual(state['statuses'], {'A': 'cancelled'})
                self.assertFalse(ws.lock.exists())

    def test_status_keeps_run_identity_while_tasks_run(self):
        with support.temp_workspace() as ws:
            seen = []

            def worker(ctx, *args):
                seen.append(json.loads(ws.status.read_text()))
                raise RuntimeError('executor failed')

            with (
                patch.object(runner.shutil, 'which', return_value='tool'),
                patch.object(gitops, 'git', return_value=subprocess.CompletedProcess([], 0, '', '')),
                patch.object(agents, 'run_executor', side_effect=worker),
            ):
                runner.execute(ws, json.loads(PLAN), 1, 0, ['test'])
            self.assertEqual(seen[0]['state'], 'running')
            self.assertEqual(seen[0]['statuses'], {'A': 'running'})
            self.assertEqual(seen[0]['run_id'], json.loads(ws.status.read_text())['run_id'])

    def test_status_reports_pre_upgrade_reservation_without_legacy_credential(self):
        with support.temp_workspace(server) as ws:
            token = 'previous-worker-credential'
            ws.lock.write_text(json.dumps({'pid': 123, 'token': token}))
            ws.status.write_text(json.dumps({'state': 'failed', 'run_id': token, 'recovery_required': True}))
            status = server.get_run_status()
            self.assertNotIn(token, status)
            self.assertIn('123', status)
            self.assertTrue(json.loads(status.split('\n---')[0])['recovery_required'])

    def test_only_first_mcp_worker_can_claim_or_release_reservation(self):
        with support.temp_workspace(server) as ws, patch.object(server.subprocess, 'Popen') as launch:
            source = ws.plan
            missing = ws.store / 'missing.json'
            server.submit_plan(PLAN, test_command_json='["test"]')
            argv = launch.call_args.args[0][2:]
            credential = argv[argv.index('--reservation') + 1]
            public_id = json.loads(ws.status.read_text())['run_id']

            def attempt(token, path):
                with self.assertRaises(RuntimeError):
                    cli.main([str(path), '--reservation', token, '--test', 'test'], ws)

            def assert_still_reserved():
                self.assertTrue(ws.lock.exists())
                self.assertEqual(json.loads(source.read_text()), json.loads(PLAN))
                with self.assertRaises(RuntimeError):
                    st.reserve_run(ws)

            # A status reader must not be able to take even an unclaimed run.
            self.assertNotIn(credential, server.get_run_status())
            attempt(public_id, source)
            attempt(public_id, missing)
            assert_still_reserved()

            def running(*args):
                state = ws.status.read_bytes()
                for path in (source, missing):
                    attempt(credential, path)
                    assert_still_reserved()
                    self.assertEqual(ws.status.read_bytes(), state)
                return {'A': 'approved'}

            with patch.object(runner, '_execute', side_effect=running):
                cli.main(argv, ws)
            self.assertFalse(ws.lock.exists())
            self.assertEqual(json.loads(ws.status.read_text())['state'], 'completed')

    def test_status_survives_reservation_release_during_read(self):
        with support.temp_workspace(server) as ws:
            status = '{"state":"completed","statuses":{"A":"approved"}}'
            ws.status.write_text(status, encoding='utf-8')
            ws.log.write_text('worker finished', encoding='utf-8')
            reservation = ws.lock
            reservation.write_text('{}', encoding='utf-8')
            original_read = Path.read_text

            def read_after_release(path, *args, **kwargs):
                if path == reservation:
                    path.unlink()
                return original_read(path, *args, **kwargs)

            with patch.object(Path, 'read_text', read_after_release):
                try:
                    result = server.get_run_status()
                except FileNotFoundError as error:
                    self.fail(f'Normal reservation release broke status polling: {error}')
            self.assertIn(status, result)
            self.assertIn('Sem reserva ativa', result)
            self.assertIn('worker finished', result)

    def test_status_does_not_hide_unreadable_reservation(self):
        with support.temp_workspace(server) as ws:
            reservation = ws.lock
            reservation.write_text('{}', encoding='utf-8')
            original_read = Path.read_text

            def denied_read(path, *args, **kwargs):
                if path == reservation:
                    raise PermissionError('reservation unreadable')
                return original_read(path, *args, **kwargs)

            with patch.object(Path, 'read_text', denied_read):
                with self.assertRaisesRegex(PermissionError, 'reservation unreadable'):
                    server.get_run_status()

    def test_pid_write_failure_reports_started_worker_and_preserves_state(self):
        for previous_pid in (None, '456'):
            with self.subTest(previous_pid=previous_pid), support.temp_workspace(server) as ws:
                pid_file = ws.pid
                if previous_pid is not None:
                    pid_file.write_text(previous_pid, encoding='utf-8')
                original_write = Path.write_text

                def denied_pid_write(path, *args, **kwargs):
                    if path == pid_file:
                        raise PermissionError('pid file locked')
                    return original_write(path, *args, **kwargs)

                with (
                    patch.object(server.subprocess, 'Popen') as launch,
                    patch.object(Path, 'write_text', denied_pid_write),
                ):
                    launch.return_value.pid = 123
                    try:
                        result = server.submit_plan(PLAN, test_command_json='["test"]')
                    except PermissionError as error:
                        self.fail(f'Launched worker was reported as a failed submission: {error}')
                    self.assertIn('Execução iniciada com PID 123', result)
                    self.assertIn('Aviso:', result)
                    self.assertIn('pid.txt', result)
                    self.assertIn('pid file locked', result)
                    self.assertTrue(ws.lock.exists())
                    self.assertEqual(json.loads(ws.status.read_text())['state'], 'starting')
                    with self.assertRaisesRegex(RuntimeError, 'reservada'):
                        st.reserve_run(ws)

    def test_cli_publishes_current_plan_even_when_preflight_fails(self):
        for previous in (None, {'goal': 'Previous MCP plan', 'tasks': []}):
            with self.subTest(previous=previous), support.temp_workspace(server) as ws:
                if previous is not None:
                    ws.plan.write_text(json.dumps(previous), encoding='utf-8')
                plan = dict(json.loads(PLAN), goal='Novo objetivo: documentação')
                source = ws.store / 'cli-plan.json'
                source.write_text(json.dumps(plan), encoding='utf-8')
                with patch.object(runner.shutil, 'which', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'PATH'):
                        cli.main([str(source), '--test', 'test'], ws)
                    self.assertTrue(ws.plan.exists())
                    self.assertEqual(json.loads(server.get_submitted_plan()), plan)
                    self.assertEqual(json.loads(ws.status.read_text())['state'], 'failed')
                    self.assertFalse(ws.lock.exists())

    def test_cli_cannot_replace_plan_of_reserved_run(self):
        with support.temp_workspace(server) as ws:
            ws.plan.write_text(PLAN, encoding='utf-8')
            source = ws.store / 'cli-plan.json'
            source.write_text(PLAN.replace('Example', 'New plan'), encoding='utf-8')
            st.reserve_run(ws)
            reservation = ws.lock.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'reservada'):
                cli.main([str(source), '--test', 'test'], ws)
            self.assertEqual(json.loads(server.get_submitted_plan()), json.loads(PLAN))
            self.assertEqual(ws.lock.read_bytes(), reservation)

    def test_second_submission_cannot_overwrite_first(self):
        with support.temp_workspace(server) as ws, patch.object(server.subprocess, 'Popen') as launch:
            launch.return_value.pid = 123
            first = server.submit_plan(PLAN, test_command_json='["python", "-m", "unittest"]')
            second = server.submit_plan(
                PLAN.replace('Example', 'Second'), test_command_json='["python", "-m", "unittest"]'
            )
            self.assertIn('PID', first)
            self.assertIn('execução', second.lower())
            self.assertEqual(launch.call_count, 1)
            self.assertIn('Example', ws.plan.read_text())

    def test_failed_launch_releases_reservation(self):
        with (
            support.temp_workspace(server) as ws,
            patch.object(server.subprocess, 'Popen', side_effect=OSError('launch failed')),
        ):
            with self.assertRaises(OSError):
                server.submit_plan(PLAN, test_command_json='["test"]')
            self.assertFalse(ws.lock.exists())
            state = json.loads(ws.status.read_text())
            self.assertEqual(state['state'], 'failed')
            self.assertIn('launch failed', state['error'])

    def test_status_write_failure_before_launch_does_not_block_next_submission(self):
        for failure_stage in ('starting', 'failed'):
            with self.subTest(failure_stage=failure_stage), support.temp_workspace(server) as ws:
                original_write = st.write_json

                def write(path, value):
                    if path.name == 'status.json' and (failure_stage == 'starting' or value['state'] == 'failed'):
                        raise PermissionError('status unavailable')
                    original_write(path, value)

                with patch.object(server.subprocess, 'Popen') as launch:
                    launch.side_effect = OSError('launch failed')
                    with patch.object(st, 'write_json', side_effect=write):
                        with self.assertRaisesRegex(PermissionError, 'status unavailable'):
                            server.submit_plan(PLAN, test_command_json='["test"]')
                    self.assertFalse(ws.lock.exists())

                    launch.side_effect = None
                    launch.return_value.pid = 123
                    result = server.submit_plan(PLAN, test_command_json='["test"]')
                    self.assertIn('PID 123', result)
                    self.assertTrue(ws.lock.exists())
                    self.assertEqual(json.loads(ws.status.read_text())['state'], 'starting')
