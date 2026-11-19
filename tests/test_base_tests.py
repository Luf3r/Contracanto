"""Testes da base antes da implementação e o que o revisor recebe sobre a verificação."""

import json
import sys
from unittest.mock import patch

import support

from contracanto import agents, gitops, runner

# Falha enquanto allowed.txt não existe: a base chega quebrada e só uma tarefa que cria o
# arquivo a conserta.
FAILS_WITHOUT_ALLOWED = [
    sys.executable,
    '-c',
    "import os, sys; sys.exit(0 if os.path.exists('allowed.txt') else 'falha antiga da base')",
]
ALWAYS_FAILS = [sys.executable, '-c', "import sys; sys.exit('falha antiga da base')"]
PASS = [sys.executable, '-c', 'pass']


def payloads(calls, prompt):
    return [call.args[1] for call in calls.call_args_list if call.args[0] == prompt]


class BrokenBaseTests(support.RepositoryTestCase):
    def test_task_that_does_not_fix_a_broken_base_fails_without_review_or_repair(self):
        with self.repository() as (root, store, base, task, git):
            with (
                patch.object(agents, 'repair') as repair,
                patch.object(agents, '_with_payload', wraps=agents._with_payload) as prompts,
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 2, ALWAYS_FAILS)
            self.assertEqual(states, {'A': 'failed'})
            repair.assert_not_called()
            self.assertEqual(payloads(prompts, agents.REVIEW_PROMPT), [])
            (implementation,) = payloads(prompts, agents.IMPLEMENT_PROMPT)
            self.assertEqual(implementation['base_tests']['exit_code'], 1)
            self.assertIn('falha antiga da base', implementation['base_tests']['output'])
            detail = support.read_status(self.ws)['details']['A']
            self.assertIn('já falhavam na base', detail['error'])
            self.assertIn('falha antiga da base', detail['error'])

    def test_task_that_fixes_the_base_is_reviewed_with_the_base_result(self):
        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, '_with_payload', wraps=agents._with_payload) as prompts:
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, FAILS_WITHOUT_ALLOWED)
            self.assertEqual(states, {'A': 'approved'})
            (review,) = payloads(prompts, agents.REVIEW_PROMPT)
            self.assertEqual(review['test_exit_code'], 0)
            self.assertEqual(review['base_test_exit_code'], 1)
            self.assertIn('falha antiga da base', review['base_test_output'])

    def test_healthy_base_adds_nothing_to_the_requests(self):
        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, '_with_payload', wraps=agents._with_payload) as prompts:
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'approved'})
            (implementation,) = payloads(prompts, agents.IMPLEMENT_PROMPT)
            (review,) = payloads(prompts, agents.REVIEW_PROMPT)
            self.assertNotIn('base_tests', implementation)
            self.assertNotIn('base_test_exit_code', review)
            self.assertEqual(review['test_command'], PASS)

    def test_base_is_tested_once_per_batch(self):
        with self.repository() as (root, store, base, task, git):

            def agent(prompt, wt, stop=None):
                task_id = json.loads(prompt.rsplit('\n', 1)[1])['task']['id']
                (wt / f'{task_id}.txt').write_text('ok')
                return 'done'

            tasks = [
                dict(task, id=task_id, files=[f'{task_id}.txt'], description=f'Create {task_id}.txt')
                for task_id in ('A', 'B', 'C')
            ]
            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(gitops, 'run_tests', wraps=gitops.run_tests) as tests,
            ):
                states = runner.execute(self.ws, {'tasks': tasks}, 3, 0, PASS)
            self.assertEqual(states, {'A': 'approved', 'B': 'approved', 'C': 'approved'})
            on_base = [call for call in tests.call_args_list if call.args[2] == base]
            self.assertEqual(len(on_base), 1)


class VerdictTests(support.RepositoryTestCase):
    def test_inherited_points_are_kept_apart_from_the_problems(self):
        verdict = agents.parse_verdict(
            json.dumps(
                {
                    'verdict': 'APPROVED',
                    'summary': 'Implementa o pedido.',
                    'problems': [],
                    'inherited': ['lint já falhava em src/old.ts'],
                }
            )
        )
        self.assertTrue(verdict.approved)
        self.assertIn('- herdado da base: lint já falhava em src/old.ts', verdict.text)

    def test_reviewer_is_told_which_command_ran(self):
        self.assertIn('test_command', agents.REVIEW_PROMPT)
        self.assertIn('inherited', agents.REVIEW_PROMPT)


class ResolvedCommandTests(support.RepositoryTestCase):
    def test_base_tests_use_the_resolved_command(self):
        # No Windows, `npm` só roda pelo caminho do npm.cmd que resolve_command encontra;
        # aqui um nome que só existe depois da resolução faz o mesmo papel.
        raw = ['verificacao-sem-caminho']
        original = runner.resolve_command

        def resolve(command, root, label):
            return list(PASS) if list(command) == raw else original(command, root, label)

        with self.repository() as (root, store, base, task, git):
            with (
                patch.object(runner, 'resolve_command', side_effect=resolve),
                patch.object(gitops, 'run_tests', wraps=gitops.run_tests) as tests,
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, raw)
            self.assertEqual(states, {'A': 'approved'}, support.read_status(self.ws)['details'])
            commands = [list(call.args[0]) for call in tests.call_args_list]
            self.assertEqual(len(commands), 3)  # base, worktree e integração
            self.assertTrue(all(command == PASS for command in commands), commands)
