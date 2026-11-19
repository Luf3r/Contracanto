"""Each kind of task failure lands in its own category, and failed work is summed as waste."""

import json
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import support

from contracanto import agents, errors, maintenance, runner, watch
from contracanto import process as proc

PASS = [sys.executable, '-c', 'pass']
FAILS_AT_ROOT = [sys.executable, '-c', 'import os, sys; sys.exit(1 if os.path.isdir(".git") else 0)']
UNVERIFIABLE = {'verdict': 'REJECTED', 'summary': 'x', 'unverifiable': ['npm run check passa: não rodou']}


class CategoryTests(unittest.TestCase):
    def test_first_tag_wins_and_keeps_type_and_message(self):
        error = errors.tag(errors.tag(RuntimeError('boom'), 'scope'), 'other')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'boom')
        self.assertEqual(errors.failure_category(error), 'scope')

    def test_untagged_errors_follow_their_type(self):
        cases = [
            (errors.RecoveryRequired('x'), 'recovery'),
            (errors.tag(errors.RecoveryRequired('x'), 'scope'), 'recovery'),
            (errors.RunCancelled('x'), 'cancelled'),
            (subprocess.TimeoutExpired(['agy'], 10), 'timeout'),
            (FileNotFoundError(2, 'The system cannot find the file specified'), 'tooling'),
            (errors.ConfigError('x'), 'tooling'),
            (agents.InheritedFailure('x'), 'inherited_base'),
            (RuntimeError('x'), 'other'),
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                self.assertEqual(errors.failure_category(error), expected)


class VerdictTests(unittest.TestCase):
    def test_unverifiable_criteria_are_kept_apart_from_problems(self):
        verdict = agents.parse_verdict(json.dumps({**UNVERIFIABLE, 'problems': []}))
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.problems, ())
        self.assertEqual(verdict.unverifiable, ('npm run check passa: não rodou',))
        self.assertIn('- não comprovado pelos testes: npm run check passa', verdict.text)

    def review(self, answer, test_exit=0):
        def command(args, **kwargs):
            if args[0] == 'claude':
                return subprocess.CompletedProcess(args, 0, json.dumps({'result': json.dumps(answer)}), '')
            output = b'' if '--binary' in args else support.fake_git_output(args[1:], 'commit')
            return subprocess.CompletedProcess(args, 0, output, '')

        def tests(command, wt, head, **kwargs):
            return subprocess.CompletedProcess(command, test_exit, '', '')

        artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')
        with (
            patch.object(proc, 'run', side_effect=command),
            patch.object(agents.gitops, 'run_tests', side_effect=tests),
        ):
            return agents.review(artifact, ['test'])

    def test_rejection_only_for_unverifiable_criteria_is_flagged(self):
        approved, feedback = result = self.review(UNVERIFIABLE)
        self.assertFalse(approved)
        self.assertTrue(result.unverifiable_only)

    def test_real_problems_or_failing_tests_are_not_only_unverifiable(self):
        self.assertFalse(self.review({**UNVERIFIABLE, 'problems': ['falta o teste']}).unverifiable_only)
        self.assertFalse(self.review(UNVERIFIABLE, test_exit=1).unverifiable_only)
        self.assertFalse(self.review({'verdict': 'APPROVED'}).unverifiable_only)


class RecordedFailureTests(support.RepositoryTestCase):
    def run_plan(self, task, tests=PASS, retries=0):
        runner.execute(self.ws, {'tasks': [task]}, 1, retries, tests)
        return support.read_status(self.ws)

    def assert_failure(self, status, category, phase):
        self.assertEqual(status['statuses'], {'A': 'failed'})
        self.assertEqual(status['details']['A']['failure'], {'category': category, 'phase': phase})
        self.assertEqual(status['waste']['tasks'], 1)
        self.assertEqual(status['waste']['by_category'], {category: 1})
        self.assertGreaterEqual(status['waste']['seconds'], 0)

    def test_executor_that_changes_nothing(self):
        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, 'run_agy', return_value='nada a fazer'):
                status = self.run_plan(task)
            self.assert_failure(status, 'executor_noop', 'implementation')

    def test_executor_error_after_the_retries(self):
        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, 'run_agy', side_effect=RuntimeError('agy exited 1: quota')):
                status = self.run_plan(task)
            self.assert_failure(status, 'executor_error', 'implementation')

    def test_missing_executable(self):
        with self.repository() as (root, store, base, task, git):
            missing = FileNotFoundError(2, 'The system cannot find the file specified')
            with patch.object(agents, 'run_agy', side_effect=missing):
                status = self.run_plan(task)
            self.assert_failure(status, 'tooling', 'implementation')

    def test_file_outside_the_task_scope(self):
        def agent(prompt, wt, stop=None):
            (wt / 'allowed.txt').write_text('ok')
            (wt / 'outside.txt').write_text('changed')
            return 'done'

        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, 'run_agy', side_effect=agent):
                status = self.run_plan(task)
            self.assert_failure(status, 'scope', 'implementation')

    def test_rejection_for_unverifiable_criteria(self):
        with self.repository() as (root, store, base, task, git):
            rejection = agents.ReviewResult(False, 'REJECTED', unverifiable_only=True)
            with patch.object(agents, 'review', return_value=rejection):
                status = self.run_plan(task)
            self.assert_failure(status, 'criteria_unverifiable', 'review')

    def test_rejection_after_every_repair(self):
        with self.repository() as (root, store, base, task, git):
            with patch.object(agents, 'review', return_value=(False, 'REJECTED\n- falta algo')):
                status = self.run_plan(task, retries=1)
            self.assert_failure(status, 'review_rejected', 'review')
            self.assertEqual(status['timings']['A']['phases']['repair']['count'], 1)

    def test_integration_tests_that_fail_and_revert(self):
        with self.repository() as (root, store, base, task, git):
            status = self.run_plan(task, tests=FAILS_AT_ROOT)
            self.assertIn('Integração revertida', status['details']['A']['error'])
            self.assert_failure(status, 'integration_tests', 'integration')

    def test_approved_run_has_no_waste(self):
        with self.repository() as (root, store, base, task, git):
            status = self.run_plan(task)
            self.assertEqual(status['statuses'], {'A': 'approved'})
            self.assertNotIn('waste', status)


class ReportTests(unittest.TestCase):
    def report(self):
        run = {
            'state': 'failed',
            'run_id': 'abc',
            'statuses': {'A': 'failed', 'B': 'approved'},
            'details': {
                'A': {
                    'error': 'O executor não alterou arquivos',
                    'failure': {'category': 'executor_noop', 'phase': 'x'},
                },
                'B': {},
            },
            'timings': {},
            'waste': {'tasks': 1, 'seconds': 928.2, 'by_category': {'executor_noop': 1}},
        }
        return {'run': run, 'reservation_state': 'none', 'reservation': None, 'log': ''}

    def test_summary_lists_categories_and_waste(self):
        summary = maintenance.summarize(self.report())
        self.assertEqual(summary['task_failures'], {'A': 'executor_noop'})
        self.assertEqual(summary['waste']['seconds'], 928.2)

    def test_panel_labels_errors_and_shows_waste(self):
        text = '\n'.join(watch.render(self.report(), '', datetime.now(UTC)))
        self.assertIn('[executor_noop] O executor não alterou arquivos', text)
        self.assertIn('Desperdício', text)
        self.assertIn('15m28s', text)
        self.assertIn('executor_noop 1', text)


if __name__ == '__main__':
    unittest.main()
