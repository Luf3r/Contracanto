"""Retomada automática de uma execução cujo worker morreu sem registrar o estado final."""

import asyncio
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

import support

from contracanto import agents, cli, config, maintenance, runner, server
from contracanto import state as st

PASS = [sys.executable, '-c', 'pass']


class AutoResumeTests(support.RepositoryTestCase):
    def crashed_run(self, task):
        """Execução com A aprovada e B em andamento, reservada por um worker que já morreu."""
        tasks = [dict(task, id='A', files=['a.txt']), dict(task, id='B', files=['b.txt'], deps=['A'])]
        failed: list[str] = []

        def agent(prompt, wt, stop=None):
            name = json.loads(prompt.rsplit('\n', 1)[1])['task']['files'][0]
            if name == 'b.txt' and not failed:
                failed.append(name)
                raise RuntimeError('worker interrompido')
            (wt / name).write_text(name)
            return 'done'

        self.enterContext(patch.object(agents, 'run_agy', side_effect=agent))
        with patch.object(config, 'AGENT_RETRIES', 0):
            runner.execute(self.ws, {'tasks': tasks}, 1, 0, PASS)
        run_id = self.ws.latest_run_id()
        self.simulate_crash(run_id, {'A': 'approved', 'B': 'running'})
        return run_id

    def simulate_crash(self, run_id, statuses=None):
        status = json.loads(self.ws.status(run_id).read_text(encoding='utf-8'))
        status.update(state='running', finished_at=None, statuses=statuses or status['statuses'])
        status.pop('error', None)
        st.write_json(self.ws.status(run_id), status)
        dead = support.finished_process_pid()
        self.ws.lock.write_text(json.dumps({'run_id': run_id, 'pid': dead}), encoding='utf-8')
        for path in (self.ws.worker_pid(run_id), self.ws.pid(run_id)):
            path.parent.mkdir(exist_ok=True)
            path.write_text(str(dead), encoding='utf-8')

    def wait(self, timeout=1):
        with (
            patch.object(server, 'WORKSPACE', self.ws),
            patch.object(server, 'WAIT_POLL_INTERVAL', 0.05),
            patch.object(server.process, 'launch_detached', return_value=(1, True)) as launch,
        ):
            # O worker relançado é simulado por um processo que continua vivo: o do teste.
            launch.return_value = (os.getpid(), True)
            summary = json.loads(asyncio.run(server.wait_for_run(timeout)))
        return summary, launch

    def test_dead_worker_is_relaunched_once_with_the_remaining_tasks(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            summary, launch = self.wait()
            self.assertEqual(summary['auto_resume'], {'resumed_from': first, 'attempt': 1})
            self.assertEqual(launch.call_count, 1)
            second = summary['run_id']
            self.assertNotEqual(second, first)
            self.assertTrue(summary['timed_out'])
            plan = json.loads(self.ws.plan(second).read_text(encoding='utf-8'))
            self.assertEqual([(item['id'], item['deps']) for item in plan['tasks']], [('B', [])])
            options = maintenance.read_options(self.ws, second)
            assert options is not None
            self.assertEqual((options['test'], options['parallel'], options['retries']), (PASS, 1, 0))
            self.assertEqual((options['auto_resumes'], options['resumed_from']), (1, first))
            argv = launch.call_args.args[0]
            self.assertEqual(argv[argv.index('--test') + 1 :], PASS)
            # A execução interrompida fica registrada como falha, sem tarefas em andamento.
            old = json.loads(self.ws.status(first).read_text(encoding='utf-8'))
            self.assertEqual((old['state'], old['statuses']), ('failed', {'A': 'approved', 'B': 'blocked'}))

            # Se a execução relançada também morrer, o limite padrão impede outra tentativa.
            self.simulate_crash(second)
            summary, launch = self.wait()
            self.assertIn('Limite', summary['auto_resume']['error'])
            launch.assert_not_called()
            self.assertEqual(summary['run_id'], second)
            self.assertFalse(summary['timed_out'])
            self.assertTrue(self.ws.lock.exists())

    def test_dirty_repository_is_never_resumed_automatically(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            (root / 'pela-metade.txt').write_text('x')
            summary, launch = self.wait()
            launch.assert_not_called()
            self.assertIn('alterado', summary['auto_resume']['error'])
            self.assertEqual(summary['run_id'], first)
            self.assertFalse(summary['worker_alive'])
            self.assertTrue(self.ws.lock.exists())

    def test_disabled_or_missing_options_leave_the_run_for_the_operator(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            with patch.object(config, 'AUTO_RESUME', 0):
                summary, launch = self.wait()
            launch.assert_not_called()
            self.assertIn('Limite', summary['auto_resume']['error'])
            self.ws.options(first).unlink()
            summary, launch = self.wait()
            launch.assert_not_called()
            self.assertIn('opções', summary['auto_resume']['error'])
            self.assertTrue(self.ws.lock.exists())

    def test_run_that_asked_for_manual_recovery_is_not_resumed(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            status = json.loads(self.ws.status(first).read_text(encoding='utf-8'))
            st.write_json(self.ws.status(first), {**status, 'state': 'failed', 'recovery_required': True})
            summary, launch = self.wait()
            launch.assert_not_called()
            self.assertNotIn('auto_resume', summary)
            with self.assertRaisesRegex(RuntimeError, 'recuperação manual'):
                maintenance.release_dead_reservation(self.ws)
            self.assertTrue(self.ws.lock.exists())

    def test_integration_branch_is_not_created_twice(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            options = maintenance.read_options(self.ws, first)
            assert options is not None
            st.write_json(self.ws.options(first), {**options, 'branch': 'lote'})
            # O worker morreu antes de criar a branch: a retomada ainda precisa criá-la.
            summary, launch = self.wait()
            argv = launch.call_args.args[0]
            self.assertEqual(argv[argv.index('--branch') + 1], 'lote')
            # Já no checkout da branch, ela não é pedida de novo.
            git('switch', '-c', 'lote')
            self.simulate_crash(summary['run_id'])
            with patch.object(config, 'AUTO_RESUME', 2):
                summary, launch = self.wait()
            self.assertEqual(summary['auto_resume']['attempt'], 2)
            self.assertNotIn('--branch', launch.call_args.args[0])

    def test_cli_resume_releases_the_dead_reservation_by_itself(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                cli.main(['--resume', '--parallel', '1', '--retries', '0', '--test', *PASS], self.ws)
            self.assertIn('removida', stderr.getvalue())
            self.assertNotEqual(self.ws.latest_run_id(), first)
            self.assertEqual(support.read_status(self.ws)['statuses'], {'B': 'approved'})
            self.assertTrue((root / 'b.txt').exists())
            self.assertFalse(self.ws.lock.exists())

    def test_live_run_is_never_released(self):
        with self.repository() as (root, store, base, task, git):
            first = self.crashed_run(task)
            self.ws.worker_pid(first).write_text(str(os.getpid()), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'em andamento'):
                maintenance.release_dead_reservation(self.ws)
            self.assertTrue(self.ws.lock.exists())
