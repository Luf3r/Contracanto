"""O que a execução registra: tempos por fase no status e eventos no log."""

import sys
from unittest.mock import patch

import support

from contracanto import agents, runner

PASS = [sys.executable, '-c', 'pass']


class TimingTests(support.RepositoryTestCase):
    def test_status_records_run_and_phase_times(self):
        with self.repository() as (root, store, base, task, git):
            runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            status = support.read_status(self.ws)
            self.assertEqual(status['state'], 'completed')
            self.assertLessEqual(status['started_at'], status['finished_at'])
            self.assertGreaterEqual(status['duration_seconds'], 0)
            timing = status['timings']['A']
            self.assertLessEqual(timing['started_at'], timing['finished_at'])
            self.assertEqual(set(timing['phases']), {'implementation', 'review', 'integration'})
            for phase in timing['phases'].values():
                self.assertEqual(phase['count'], 1)
                self.assertGreaterEqual(phase['seconds'], 0)

    def test_repairs_accumulate_review_and_repair_time(self):
        with self.repository() as (root, store, base, task, git):
            verdicts = iter([(False, 'REJECTED\n- falta algo'), (True, 'APPROVED')])
            with patch.object(agents, 'review', side_effect=lambda *args: next(verdicts)):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved'})
            phases = support.read_status(self.ws)['timings']['A']['phases']
            self.assertEqual(phases['review']['count'], 2)
            self.assertEqual(phases['repair']['count'], 1)

    def test_blocked_task_that_never_ran_has_no_start_time(self):
        with self.repository() as (root, store, base, task, git):
            child = dict(task, id='B', deps=['A'], files=['b.txt'])
            with patch.object(agents, 'run_agy', side_effect=RuntimeError('executor failed')):
                runner.execute(self.ws, {'tasks': [task, child]}, 1, 0, [sys.executable, '-c', 'pass'])
            status = support.read_status(self.ws)
            self.assertEqual(status['statuses'], {'A': 'failed', 'B': 'blocked'})
            self.assertIn('finished_at', status['timings']['A'])
            self.assertNotIn('started_at', status['timings']['B'])


class EventLogTests(support.RepositoryTestCase):
    def test_run_events_are_logged(self):
        with self.repository() as (root, store, base, task, git), self.assertLogs('contracanto', 'INFO') as logs:
            runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
        text = '\n'.join(logs.output)
        for expected in (
            '1 tarefa(s)',
            'Lote sobre',
            'A: fim de implementation',
            'A: revisão 1 aprovada',
            'A: approved',
        ):
            self.assertIn(expected, text)
        self.assertIn('Execução encerrada: completed', text)
