"""The executor guide and the test command go with every implementation and repair request."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, runner

ROOT = Path(__file__).resolve().parent.parent
PASS = [sys.executable, '-c', 'pass']


def payloads(calls, prompt):
    return [call.args[1] for call in calls.call_args_list if call.args[0] == prompt]


class GuideContentTests(unittest.TestCase):
    def test_both_requests_start_with_the_packaged_guide(self):
        guide = (ROOT / 'contracanto' / 'executor.md').read_text(encoding='utf-8')
        self.assertEqual(agents.EXECUTOR_GUIDE, guide)
        for prompt in (agents.IMPLEMENT_PROMPT, agents.REPAIR_PROMPT):
            self.assertTrue(prompt.startswith(guide))
        self.assertIn('Correção depois da revisão', agents.REPAIR_PROMPT)

    def test_guide_covers_the_fallbacks(self):
        for text in ('fora de `files`', 'Bloqueio', 'base_tests', 'test_command', 'Não apague', 'dependências'):
            self.assertIn(text, agents.EXECUTOR_GUIDE)

    def test_the_package_ships_the_guide(self):
        self.assertIn('"executor.md"', (ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


class RequestTests(support.RepositoryTestCase):
    def test_implementation_and_repair_carry_the_test_command(self):
        reviews = iter([agents.ReviewResult(False, 'falta um teste'), agents.ReviewResult(True, 'ok')])

        def agent(prompt, wt, stop=None):
            (wt / 'allowed.txt').write_text(str(len(prompt)))
            return 'feito'

        with self.repository() as (root, store, base, task, git):
            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(agents, 'review', side_effect=lambda *args, **kwargs: next(reviews)),
                patch.object(agents, '_with_payload', wraps=agents._with_payload) as prompts,
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, PASS)
        self.assertEqual(states, {'A': 'approved'})
        (implementation,) = payloads(prompts, agents.IMPLEMENT_PROMPT)
        (repair,) = payloads(prompts, agents.REPAIR_PROMPT)
        self.assertEqual(implementation['test_command'][1:], PASS[1:])
        self.assertEqual(repair['test_command'][1:], PASS[1:])
        self.assertEqual(repair['review'], 'falta um teste')


if __name__ == '__main__':
    unittest.main()
