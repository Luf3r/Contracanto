import concurrent.futures
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import support

from contracanto import agents, cli, errors, gitops, runner, server
from contracanto import process as proc
from contracanto import state as st

PLAN = json.dumps({'tasks': [dict(id='A', description='Example', deps=[], files=['A.txt'], acceptance=['Tests pass'])]})


def fake_git(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, support.fake_git_output(args, ''), '')


def status_report():
    return json.loads(server.get_run_status())


class SubmissionTests(unittest.TestCase):
    def test_submit_schema_requires_test_command(self):
        tool = server.mcp._tool_manager.get_tool('submit_plan')
        assert tool is not None
        self.assertIn('test_command_json', tool.parameters['required'])

    def test_invalid_submission_is_reported_without_reserving(self):
        cases = {
            'plan_json': dict(plan_json='{not json', test_command_json='["test"]'),
            'tarefas': dict(plan_json='{"tasks": []}', test_command_json='["test"]'),
            'parallel': dict(plan_json=PLAN, parallel=9, test_command_json='["test"]'),
            'retries': dict(plan_json=PLAN, retries=4, test_command_json='["test"]'),
            'test_command_json': dict(plan_json=PLAN, test_command_json='[]'),
            'JSON válido': dict(plan_json=PLAN, test_command_json='python -m pytest'),
        }
        for expected, arguments in cases.items():
            with self.subTest(expected=expected), support.temp_workspace(server) as ws:
                with patch.object(server.subprocess, 'Popen') as launch:
                    result = server.submit_plan(**arguments)  # type: ignore[arg-type]
                self.assertTrue(result.startswith('Erro: '), result)
                self.assertIn(expected, result)
                launch.assert_not_called()
                self.assertFalse(ws.lock.exists())
                self.assertEqual(status_report(), dict(run=None, reservation_state='none', reservation=None, log=''))

    def test_status_is_empty_before_any_run(self):
        with support.temp_workspace(server):
            self.assertEqual(status_report(), dict(run=None, reservation_state='none', reservation=None, log=''))
            self.assertEqual(server.get_submitted_plan(), 'Nenhum plano recebido')

    def test_status_with_legacy_log_encoding(self):
        with support.temp_workspace(server) as ws:
            run_id = support.publish_previous_run(ws, {'state': 'failed'})
            ws.log(run_id).write_bytes('falha de execução'.encode('cp1252'))
            report = status_report()
            self.assertEqual(report['run']['state'], 'failed')
            self.assertIn('falha de execução', report['log'])

    def test_stop_before_worker_claim_and_stale_request(self):
        with support.temp_workspace(server) as ws:
            self.assertIn('Nenhuma execução', server.stop_run())
            token = st.reserve_run(ws)
            run_id = st.reservation_id(token)
            cli.main(['--stop'], ws)
            self.assertIn('solicitado', server.stop_run(run_id))
            self.assertTrue(status_report()['reservation']['stop_requested'])
            with patch.object(runner, '_execute') as start, self.assertRaises(errors.RunCancelled):
                runner.execute(ws, json.loads(PLAN), 1, 0, ['test'], token)
            start.assert_not_called()
            self.assertEqual(support.read_status(ws)['state'], 'cancelled')
            next_token = st.reserve_run(ws)
            self.assertIn('outra execução', server.stop_run(run_id))
            report = status_report()
            self.assertEqual(report['reservation']['run_id'], st.reservation_id(next_token))
            self.assertFalse(report['reservation']['stop_requested'])
            # A reserva nova ainda não publicou estado; o da execução anterior não é reaproveitado.
            self.assertIsNone(report['run'])
            st.release_run(ws, next_token)
            self.assertEqual(status_report()['run']['run_id'], run_id)

    def test_stop_during_worker_command(self):
        with support.temp_workspace(server) as ws:
            started = ws.store / 'started'

            def worker(ctx, *args):
                program = 'import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)'
                return proc.run([sys.executable, '-c', program, str(started)], timeout=15, stop=ctx.stop)

            with (
                patch.object(runner.shutil, 'which', return_value='tool'),
                patch.object(gitops, 'git', side_effect=fake_git),
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
                state = support.read_status(ws)
                self.assertEqual(state['state'], 'cancelled')
                self.assertEqual(state['statuses'], {'A': 'cancelled'})
                self.assertFalse(ws.lock.exists())

    def test_status_keeps_run_identity_while_tasks_run(self):
        with support.temp_workspace(server) as ws:
            seen = []

            def worker(ctx, *args):
                seen.append(status_report())
                raise RuntimeError('executor failed')

            with (
                patch.object(runner.shutil, 'which', return_value='tool'),
                patch.object(gitops, 'git', side_effect=fake_git),
                patch.object(agents, 'run_executor', side_effect=worker),
            ):
                runner.execute(ws, json.loads(PLAN), 1, 0, ['test'])
            running = seen[0]
            self.assertEqual(running['run']['state'], 'running')
            self.assertEqual(running['run']['statuses'], {'A': 'running'})
            self.assertEqual(running['reservation_state'], 'active')
            self.assertEqual(running['run']['run_id'], running['reservation']['run_id'])
            finished = status_report()
            self.assertEqual(finished['run']['run_id'], running['run']['run_id'])
            self.assertEqual(finished['run']['state'], 'failed')
            self.assertEqual(finished['reservation_state'], 'none')

    def test_status_never_exposes_unknown_reservation_fields(self):
        with support.temp_workspace(server) as ws:
            ws.store.mkdir(exist_ok=True)
            ws.lock.write_text(json.dumps({'pid': 123, 'token': 'worker-credential'}))
            status = server.get_run_status()
            self.assertNotIn('worker-credential', status)
            report = json.loads(status)
            self.assertEqual(report['reservation_state'], 'active')
            self.assertEqual(report['reservation'], {'pid': 123, 'worker_alive': ANY})

    def test_only_first_mcp_worker_can_claim_or_release_reservation(self):
        with support.temp_workspace(server) as ws, patch.object(server.subprocess, 'Popen') as launch:
            server.submit_plan(PLAN, test_command_json='["test"]')
            argv = launch.call_args.args[0][3:]
            credential = argv[argv.index('--reservation') + 1]
            public_id = support.read_status(ws)['run_id']
            source = ws.plan(public_id)
            missing = ws.store / 'missing.json'
            self.assertEqual(argv[0], str(source))
            # O worker recebe o repositório explicitamente, sem depender de onde foi instalado.
            self.assertEqual(argv[argv.index('--repo') + 1], str(ws.root))
            self.assertNotIn('--keep-worktrees', argv)

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
                state = ws.status(public_id).read_bytes()
                for path in (source, missing):
                    attempt(credential, path)
                    assert_still_reserved()
                    self.assertEqual(ws.status(public_id).read_bytes(), state)
                return {'A': 'approved'}

            with patch.object(runner, '_execute', side_effect=running):
                cli.main(argv, ws)
            self.assertFalse(ws.lock.exists())
            self.assertEqual(support.read_status(ws)['state'], 'completed')

    def test_status_survives_reservation_release_during_read(self):
        with support.temp_workspace(server) as ws:
            run_id = support.publish_previous_run(ws, {'state': 'completed', 'statuses': {'A': 'approved'}})
            ws.log(run_id).write_text('worker finished', encoding='utf-8')
            reservation = ws.lock
            reservation.write_text('{}', encoding='utf-8')
            original_read = Path.read_text

            def read_after_release(path, *args, **kwargs):
                if path == reservation:
                    path.unlink()
                return original_read(path, *args, **kwargs)

            with patch.object(Path, 'read_text', read_after_release):
                try:
                    result = status_report()
                except FileNotFoundError as error:
                    self.fail(f'Normal reservation release broke status polling: {error}')
            self.assertEqual(result['run']['statuses'], {'A': 'approved'})
            self.assertEqual(result['reservation_state'], 'none')
            self.assertEqual(result['log'], 'worker finished')

    def test_status_reports_incomplete_reservation(self):
        with support.temp_workspace(server) as ws:
            ws.store.mkdir(exist_ok=True)
            ws.lock.write_text('', encoding='utf-8')
            self.assertEqual(status_report()['reservation_state'], 'incomplete')

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
        with support.temp_workspace(server) as ws:
            original_write = Path.write_text

            def denied_pid_write(path, *args, **kwargs):
                if path.name == 'pid.txt':
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
                self.assertIn('iniciada com PID 123', result)
                self.assertIn('Aviso:', result)
                self.assertIn('pid.txt', result)
                self.assertIn('pid file locked', result)
                self.assertTrue(ws.lock.exists())
                state = support.read_status(ws)
                self.assertEqual(state['state'], 'starting')
                self.assertIn(state['run_id'], result)
                with self.assertRaisesRegex(RuntimeError, 'reservada'):
                    st.reserve_run(ws)

    def test_cli_publishes_current_plan_even_when_preflight_fails(self):
        for previous in (None, {'goal': 'Previous MCP plan', 'tasks': []}):
            with self.subTest(previous=previous), support.temp_workspace(server) as ws:
                if previous is not None:
                    old = support.publish_previous_run(ws, {'state': 'completed'})
                    ws.plan(old).write_text(json.dumps(previous), encoding='utf-8')
                plan = dict(json.loads(PLAN), goal='Novo objetivo: documentação')
                source = ws.store / 'cli-plan.json'
                ws.store.mkdir(exist_ok=True)
                source.write_text(json.dumps(plan), encoding='utf-8')
                with patch.object(runner.shutil, 'which', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'PATH'):
                        cli.main([str(source), '--test', 'test'], ws)
                    self.assertEqual(json.loads(server.get_submitted_plan()), plan)
                    self.assertEqual(support.read_status(ws)['state'], 'failed')
                    self.assertFalse(ws.lock.exists())

    def test_cli_cannot_replace_plan_of_reserved_run(self):
        with support.temp_workspace(server) as ws:
            run_id = st.reservation_id(st.reserve_run(ws))
            ws.publish_run(run_id)
            ws.plan(run_id).write_text(PLAN, encoding='utf-8')
            source = ws.store / 'cli-plan.json'
            source.write_text(PLAN.replace('Example', 'New plan'), encoding='utf-8')
            reservation = ws.lock.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'reservada'):
                cli.main([str(source), '--test', 'test'], ws)
            self.assertEqual(json.loads(server.get_submitted_plan()), json.loads(PLAN))
            self.assertEqual(ws.lock.read_bytes(), reservation)

    def test_second_submission_cannot_overwrite_first(self):
        with support.temp_workspace(server), patch.object(server.subprocess, 'Popen') as launch:
            launch.return_value.pid = 123
            first = server.submit_plan(PLAN, test_command_json='["python", "-m", "unittest"]')
            second = server.submit_plan(
                PLAN.replace('Example', 'Second'), test_command_json='["python", "-m", "unittest"]'
            )
            self.assertIn('PID', first)
            self.assertTrue(second.startswith('Erro: '), second)
            self.assertIn('execução', second.lower())
            self.assertEqual(launch.call_count, 1)
            self.assertIn('Example', server.get_submitted_plan())

    def test_failed_launch_releases_reservation(self):
        with (
            support.temp_workspace(server) as ws,
            patch.object(server.subprocess, 'Popen', side_effect=OSError('launch failed')),
        ):
            with self.assertRaises(OSError):
                server.submit_plan(PLAN, test_command_json='["test"]')
            self.assertFalse(ws.lock.exists())
            state = support.read_status(ws)
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
                    self.assertEqual(support.read_status(ws)['state'], 'starting')
