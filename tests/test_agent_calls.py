"""Chamadas ao executor: novas tentativas, preparação do worktree, conferência dos comandos e commit."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, config, runner, server
from contracanto import process as proc
from contracanto import state as st

PASS = [sys.executable, '-c', 'pass']


class ImplementationRetryTests(support.RepositoryTestCase):
    def flaky_agent(self, failures):
        calls = []

        def agent(prompt, wt, stop=None):
            calls.append(wt)
            if len(calls) <= failures:
                raise RuntimeError('agy exited 1: falha passageira')
            (wt / 'allowed.txt').write_text('ok')
            return 'done'

        return agent, calls

    def test_transient_failure_is_retried_in_the_same_worktree(self):
        with self.repository() as (root, store, base, task, git):
            agent, calls = self.flaky_agent(1)
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], calls[1])

    def test_failure_beyond_the_limit_fails_the_task(self):
        with self.repository() as (root, store, base, task, git):
            agent, calls = self.flaky_agent(5)
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(len(calls), config.AGENT_RETRIES + 1)
            self.assertIn('falha passageira', support.read_status(self.ws)['details']['A']['error'])

    def test_uncertain_state_is_never_retried(self):
        with self.repository() as (root, store, base, task, git):
            calls = []

            def agent(prompt, wt, stop=None):
                calls.append(wt)
                raise support.errors.RecoveryRequired('processo ainda ativo')

            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(len(calls), 1)
            self.assertTrue(support.read_status(self.ws)['recovery_required'])


class RepairRetryTests(support.RepositoryTestCase):
    def test_failed_repair_call_is_retried(self):
        with self.repository() as (root, store, base, task, git):
            calls = []

            def agent(prompt, wt, stop=None):
                calls.append(prompt.split('\n', 1)[0][:9])
                if len(calls) == 2:
                    raise RuntimeError('agy exited 1: falha passageira')
                (wt / 'allowed.txt').write_text(str(len(calls)))
                return 'done'

            verdicts = iter([(False, 'REJECTED\n- falta algo'), (True, 'APPROVED')])
            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(agents, 'review', side_effect=lambda *args: next(verdicts)),
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, PASS)
            self.assertEqual(states, {'A': 'approved'})
            # Implementação, correção que falhou e correção repetida.
            self.assertEqual(len(calls), 3)
            self.assertEqual((root / 'allowed.txt').read_text(), '3')


class SetupCommandTests(support.RepositoryTestCase):
    def test_setup_runs_in_the_worktree_before_the_implementation(self):
        with self.repository() as (root, store, base, task, git):
            seen = []
            original = agents.run_agy

            def agent(prompt, wt, stop=None):
                seen.append((wt / '.agent-runs' / 'dependency').read_text())
                return original(prompt, wt, stop)

            code = "import os; os.makedirs('.agent-runs'); open('.agent-runs/dependency', 'w').write('installed')"
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, setup=[sys.executable, '-c', code])
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(seen, ['installed'])

    def test_setup_that_fails_or_dirties_the_worktree_fails_the_task(self):
        cases = {
            'import sys; sys.exit(4)': 'código 4',
            "open('stray.txt', 'w').write('x')": 'stray.txt',
        }
        for code, expected in cases.items():
            with self.subTest(code=code), self.repository() as (root, store, base, task, git):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, setup=[sys.executable, '-c', code])
                self.assertEqual(states, {'A': 'failed'})
                self.assertIn(expected, support.read_status(self.ws)['details']['A']['error'])

    def test_mcp_forwards_the_setup_command(self):
        plan = json.dumps({'tasks': [support.task('A')]})
        _, test, setup = server._parse_submission(plan, 1, 0, '["pytest"]', '["npm", "ci"]')
        self.assertEqual((test, setup), (['pytest'], ['npm', 'ci']))
        self.assertIsNone(server._parse_submission(plan, 1, 0, '["pytest"]')[2])
        with self.assertRaisesRegex(ValueError, 'setup_command_json'):
            server._parse_submission(plan, 1, 0, '["pytest"]', '"npm ci"')
        with support.temp_dir() as directory:
            ws = st.Workspace.for_root(directory)
            argv = server.worker_argv(ws, 'a' * 64, 'token', 1, 0, False, ['pytest'], setup)
            self.assertEqual(json.loads(argv[argv.index('--setup-json') + 1]), ['npm', 'ci'])
            self.assertLess(argv.index('--setup-json'), argv.index('--test'))


class CommandPreflightTests(support.RepositoryTestCase):
    def test_missing_test_or_setup_command_fails_before_any_agent_call(self):
        cases = {
            'Comando de testes': dict(tests=['comando-que-nao-existe']),
            'Comando de preparação': dict(setup=['x-nao-existe']),
        }
        for label, options in cases.items():
            with self.subTest(label=label), self.repository() as (root, store, base, task, git):
                called: list[str] = []
                arguments = dict(tests=PASS, setup=None) | options
                with (
                    patch.object(
                        runner.shutil, 'which', side_effect=lambda name: name if name in ('agy', 'claude') else None
                    ),
                    patch.object(agents, 'run_agy', side_effect=called.append),
                    self.assertRaisesRegex(RuntimeError, label + ' não encontrado'),
                ):
                    runner.execute(self.ws, {'tasks': [task]}, 1, 0, arguments['tests'], setup=arguments['setup'])
                self.assertEqual(called, [])
                self.assertFalse((store / 'run.lock').exists())

    def test_command_inside_the_repository_is_accepted(self):
        with self.repository() as (root, store, base, task, git):
            (root / 'check.cmd').write_text('')
            with patch.object(runner.shutil, 'which', return_value=None):
                self.assertEqual(runner.resolve_command(['check.cmd', '-x'], root, 'Comando'), ['check.cmd', '-x'])

    @unittest.skipUnless(os.name == 'nt', 'Só o Windows precisa do caminho completo de um .cmd')
    def test_windows_script_found_in_path_is_replaced_by_its_full_path(self):
        with support.temp_dir() as directory:
            script = directory / 'ferramenta.cmd'
            script.write_text('@exit /b 0\n')
            with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
                resolved = runner.resolve_command(['ferramenta', 'ci'], directory, 'Comando')
            self.assertEqual(Path(resolved[0]).resolve(), script.resolve())
            self.assertEqual(resolved[1:], ['ci'])
            self.assertEqual(proc.run(resolved, cwd=directory).returncode, 0)


class CommitMessageTests(unittest.TestCase):
    def test_subject_carries_the_description_within_the_limit(self):
        with support.temp_workspace() as ws:
            ctx = support.context(ws, 'a' * 64)
            long_task = support.task('T1')
            long_task['description'] = 'Criar   o endpoint\nde saúde ' + 'x' * 200
            subject, _, body = agents.commit_message(ctx, long_task).partition('\n\n')
            self.assertTrue(subject.startswith('T1: Criar o endpoint de saúde x'))
            self.assertEqual(len(subject), agents.COMMIT_SUBJECT_LENGTH)
            self.assertTrue(subject.endswith('…'))
            self.assertIn('aaaaaaaaaaaa', body)
