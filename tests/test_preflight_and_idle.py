"""Failing early on a broken environment and stopping an executor that makes no progress."""

import itertools
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import support

from contracanto import agents, config, errors, maintenance, runner, server
from contracanto import process as proc

ONLY_CLIS = staticmethod(lambda name: name if name in ('agy', 'claude') else None)
MODELS = (
    'Fetching available models...\ngemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\ngpt-oss-120b-medium\tGPT-OSS\n'
)


class SubmissionPreflightTests(unittest.TestCase):
    PLAN = json.dumps({'tasks': [support.task('A')]})

    def submit(self, which, test='["comando-que-nao-existe"]'):
        with (
            support.temp_workspace(server) as ws,
            patch.object(config, 'TEST_COMMAND', None),
            patch.object(config, 'AGENT_MODEL', ''),
            patch.object(runner.shutil, 'which', side_effect=which),
            patch.object(server.process, 'launch_detached', return_value=(5, True)) as launch,
        ):
            result = server.submit_plan(self.PLAN, test_command_json=test)
            reserved = ws.lock.exists()
        return result, reserved, launch

    def test_missing_test_command_is_refused_before_reserving(self):
        result, reserved, launch = self.submit(ONLY_CLIS)
        self.assertTrue(result.startswith('Erro: Comando de testes não encontrado: comando-que-nao-existe'), result)
        self.assertFalse(reserved)
        launch.assert_not_called()

    def test_missing_cli_is_named(self):
        result, reserved, launch = self.submit(lambda name: None if name == 'agy' else name)
        self.assertIn('Não encontrado no PATH: agy', result)
        self.assertFalse(reserved)
        launch.assert_not_called()

    def test_ready_environment_launches(self):
        which = lambda name: name if name in ('agy', 'claude') else sys.executable  # noqa: E731
        result, _, launch = self.submit(which, json.dumps([sys.executable, '-c', 'pass']))
        self.assertIn('PID 5', result)
        launch.assert_called_once()

    def test_missing_failure_is_tooling(self):
        with patch.object(runner.shutil, 'which', return_value=None), self.assertRaises(RuntimeError) as caught:
            runner.preflight(support.Path('.'), ['nada'], None)
        self.assertEqual(errors.failure_category(caught.exception), 'tooling')


class ModelCheckTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(agents, '_models', None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, model, returncode=0, stdout=MODELS):
        listing = subprocess.CompletedProcess(['agy', 'models'], returncode, stdout, '')
        with patch.object(config, 'AGENT_MODEL', model), patch.object(proc, 'run', return_value=listing) as run:
            agents.check_model()
        return run

    def test_listed_model_passes_and_the_list_is_reused(self):
        self.check('gemini-4-hydrogen-high')
        self.assertEqual(self.check('gpt-oss-120b-medium').call_count, 0)

    def test_unlisted_model_is_refused_with_the_options(self):
        with self.assertRaises(RuntimeError) as caught:
            self.check('gpt-6-luna')
        self.assertIn("'gpt-6-luna'", str(caught.exception))
        self.assertIn('gemini-4-hydrogen-high', str(caught.exception))
        self.assertEqual(errors.failure_category(caught.exception), 'tooling')

    def test_no_model_or_no_list_does_not_block(self):
        self.assertEqual(self.check('').call_count, 0)
        self.check('gpt-6-luna', returncode=1)
        self.check('gpt-6-luna', stdout='Fetching available models...\n')


class IdleWatchTests(unittest.TestCase):
    def run_python(self, code, idle, **kwargs):
        return proc.run([sys.executable, '-c', code], timeout=30, idle=idle, **kwargs)

    def test_silent_command_is_stopped_as_stalled(self):
        started = time.monotonic()
        with self.assertRaises(proc.Stalled) as caught:
            self.run_python('import time; time.sleep(20)', proc.IdleWatch(0.6, interval=0.2))
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn('AGENT_IDLE_TIMEOUT', str(caught.exception))
        self.assertEqual(errors.failure_category(caught.exception), 'stalled')

    def test_growing_output_keeps_the_command_alive(self):
        code = 'import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.25)'
        result = self.run_python(code, proc.IdleWatch(0.8, interval=0.1))
        self.assertEqual(result.stdout.split(), [str(i) for i in range(8)])

    def test_changing_probe_keeps_a_silent_command_alive(self):
        ticks = itertools.count()
        result = self.run_python(
            'import time; time.sleep(2)', proc.IdleWatch(0.6, probe=lambda: next(ticks), interval=0.1)
        )
        self.assertEqual(result.returncode, 0)

    def test_input_and_output_still_flow(self):
        code = 'import sys; print(sys.stdin.read().upper())'
        result = self.run_python(code, proc.IdleWatch(5, interval=0.1), input_data='olá')
        self.assertEqual(result.stdout.strip(), 'OLÁ')

    def test_watch_requires_text_output(self):
        with self.assertRaises(ValueError):
            self.run_python('pass', proc.IdleWatch(5), output='binary')

    def test_executor_watch_follows_the_setting(self):
        with patch.object(config, 'AGENT_IDLE_TIMEOUT', 0.0):
            self.assertIsNone(agents.idle_watch('.'))
        with patch.object(config, 'AGENT_IDLE_TIMEOUT', 600.0):
            watch = agents.idle_watch('.')
        assert watch is not None
        self.assertEqual((watch.timeout, watch.interval), (600.0, config.IDLE_POLL_INTERVAL))


class StalledExecutorTests(support.RepositoryTestCase):
    def test_stalled_executor_is_recorded_after_the_retry(self):
        calls = []

        def agent(prompt, wt, stop=None):
            calls.append(wt)
            raise proc.Stalled('agy ficou 600s sem progresso')

        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, 'run_agy', side_effect=agent), patch.object(config, 'AGENT_RETRIES', 1):
                runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            status = support.read_status(self.ws)
        self.assertEqual(len(calls), 2)
        self.assertEqual(status['details']['A']['failure'], {'category': 'stalled', 'phase': 'implementation'})


class DeadWorkerTests(unittest.TestCase):
    def test_released_run_names_the_last_log_event(self):
        ws, run_id, _ = support.reserved_repository(self, support.finished_process_pid())
        ws.log(run_id).write_text('20:38:16 T01: running\n20:38:16 T01: início de implementation\n\n', encoding='utf-8')
        maintenance.release_dead_reservation(ws)
        status = json.loads(ws.status(run_id).read_text(encoding='utf-8'))
        self.assertEqual(
            status['error'],
            'Worker encerrado sem registrar o estado final; último evento do log: 20:38:16 T01: início de implementation',
        )

    def test_without_a_log_the_message_stays_short(self):
        ws, run_id, _ = support.reserved_repository(self, support.finished_process_pid())
        maintenance.release_dead_reservation(ws)
        status = json.loads(ws.status(run_id).read_text(encoding='utf-8'))
        self.assertEqual(status['error'], 'Worker encerrado sem registrar o estado final')


if __name__ == '__main__':
    unittest.main()
