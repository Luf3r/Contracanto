"""Retomada das tarefas não aprovadas de uma execução encerrada."""

import json
import sys
import unittest
from unittest.mock import patch

import support

from contracanto import agents, cli, maintenance, plan, runner, server
from contracanto import state as st

PASS = [sys.executable, '-c', 'pass']


class RemainingPlanTests(unittest.TestCase):
    def test_approved_tasks_leave_the_plan_and_the_dependencies(self):
        tasks = [support.task('A'), support.task('B', deps=['A']), support.task('C', deps=['A', 'B'])]
        original = {'goal': 'Objetivo', 'tasks': tasks}
        pending = plan.remaining(original, {'A': 'approved', 'B': 'failed', 'C': 'blocked'})  # type: ignore[arg-type]
        self.assertEqual(pending['goal'], 'Objetivo')
        self.assertEqual([(task['id'], task['deps']) for task in pending['tasks']], [('B', []), ('C', ['B'])])
        # O plano de origem não é alterado.
        self.assertEqual(tasks[1]['deps'], ['A'])
        plan.validate(pending)


class ResumeTests(support.RepositoryTestCase):
    def failing_once(self, failing_file):
        """Agente que escreve o arquivo de cada tarefa e falha, na primeira vez, em uma delas."""
        failed: list[str] = []

        def agent(prompt, wt, stop=None):
            name = json.loads(prompt.split('\n', 1)[1])['task']['files'][0]
            if name == failing_file and not failed:
                failed.append(name)
                raise RuntimeError('falha passageira')
            (wt / name).write_text(name)
            return 'done'

        return agent

    def first_run(self, task):
        tasks = [
            dict(task, id='A', files=['a.txt']),
            dict(task, id='B', files=['b.txt'], deps=['A']),
            dict(task, id='C', files=['c.txt'], deps=['B']),
        ]
        with patch.object(agents, 'run_agy', side_effect=self.failing_once('b.txt')):
            with patch.object(agents.config, 'AGENT_RETRIES', 0):
                states = runner.execute(self.ws, {'goal': 'Lote', 'tasks': tasks}, 1, 0, PASS)
                self.assertEqual(states, {'A': 'approved', 'B': 'failed', 'C': 'blocked'})
                yield

    def test_resume_runs_only_what_was_not_approved(self):
        with self.repository() as (root, store, base, task, git):
            for _ in self.first_run(task):
                first = self.ws.latest_run_id()
                pending = maintenance.resume_plan(self.ws)
                self.assertEqual([item['id'] for item in pending['tasks']], ['B', 'C'])
                self.assertEqual(pending['tasks'][0]['deps'], [])
                cli.main(['--resume', '--parallel', '1', '--retries', '0', '--test', *PASS], self.ws)
            self.assertNotEqual(self.ws.latest_run_id(), first)
            self.assertEqual(support.read_status(self.ws)['statuses'], {'B': 'approved', 'C': 'approved'})
            self.assertEqual(json.loads(self.ws.plan(self.ws.latest_run_id()).read_text())['goal'], 'Lote')
            for name in ('a.txt', 'b.txt', 'c.txt'):
                self.assertTrue((root / name).exists(), name)
            with self.assertRaisesRegex(RuntimeError, 'já foram aprovadas'):
                maintenance.resume_plan(self.ws)

    def test_resume_refuses_when_an_approved_commit_left_the_history(self):
        with self.repository() as (root, store, base, task, git):
            for _ in self.first_run(task):
                git('switch', '-c', 'outra', base)
                with self.assertRaisesRegex(RuntimeError, 'A tarefa A .* não está no HEAD'):
                    maintenance.resume_plan(self.ws)

    def test_resume_refuses_an_unapproved_task_whose_commit_is_in_head(self):
        with self.repository() as (root, store, base, task, git):
            for _ in self.first_run(task):
                # Simula o worker encerrado entre o cherry-pick e a aprovação de B.
                (root / 'b.txt').write_text('b.txt')
                git('add', '.')
                git('commit', '-m', 'B: integrado sem registro')
                commit = git('rev-parse', 'HEAD').stdout.strip()
                run_id = self.ws.latest_run_id()
                status = support.read_status(self.ws)
                status['details'].setdefault('B', {})['integrated_commit'] = commit
                st.write_json(self.ws.status(run_id), status)
                with self.assertRaisesRegex(RuntimeError, f'A tarefa B .*{commit[:12]}'):
                    maintenance.resume_plan(self.ws)
                # O operador pode afirmar que já desfez o commit de outra forma.
                forced = maintenance.resume_plan(self.ws, force=True)
                self.assertEqual([item['id'] for item in forced['tasks']], ['B', 'C'])
                with self.assertRaisesRegex(RuntimeError, '--resume --force'):
                    cli.main(['--resume', '--test', *PASS], self.ws)
                # Depois do git revert, a tarefa pode ser refeita.
                git('revert', '--no-edit', commit)
                self.assertEqual([item['id'] for item in maintenance.resume_plan(self.ws)['tasks']], ['B', 'C'])

    def test_resume_by_run_id_and_refusals(self):
        with self.repository() as (root, store, base, task, git):
            for _ in self.first_run(task):
                run_id = self.ws.latest_run_id()
                self.assertEqual(len(maintenance.resume_plan(self.ws, run_id)['tasks']), 2)
                with self.assertRaisesRegex(RuntimeError, 'inválido'):
                    maintenance.resume_plan(self.ws, '../outro')
                with self.assertRaisesRegex(RuntimeError, 'Nenhuma execução'):
                    maintenance.resume_plan(self.ws, 'f' * 64)
                token = st.reserve_run(self.ws)
                with self.assertRaisesRegex(RuntimeError, 'reservada'):
                    maintenance.resume_plan(self.ws)
                st.release_run(self.ws, token)

    def test_mcp_resume_launches_a_worker_with_the_remaining_plan(self):
        with self.repository() as (root, store, base, task, git):
            for _ in self.first_run(task):
                with (
                    patch.object(server, 'WORKSPACE', self.ws),
                    patch.object(server, 'subprocess') as fake,
                ):
                    # Só o lançamento do worker é simulado; o Git da retomada é real.
                    launch = fake.Popen
                    launch.return_value.pid = 123
                    result = server.resume_run(parallel=1, retries=0, branch='lote-2', test_command_json='["test"]')
                    self.assertIn('PID 123', result)
                    argv = launch.call_args.args[0]
                    self.assertEqual(argv[argv.index('--branch') + 1], 'lote-2')
                    submitted = json.loads(server.get_submitted_plan())
                    self.assertEqual([item['id'] for item in submitted['tasks']], ['B', 'C'])
                    st.release_run(self.ws, argv[argv.index('--reservation') + 1])

    def test_mcp_resume_without_a_previous_run_is_an_error(self):
        with support.temp_workspace(server) as ws, patch.object(server.subprocess, 'Popen') as launch:
            result = server.resume_run(test_command_json='["test"]')
            self.assertTrue(result.startswith('Erro: '), result)
            launch.assert_not_called()
            self.assertFalse(ws.lock.exists())
