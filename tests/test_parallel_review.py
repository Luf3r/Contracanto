"""Revisão do lote em paralelo e integração na ordem das implementações."""

import json
import sys
import threading
from unittest.mock import patch

import support

from contracanto import agents, runner


class ParallelReviewTests(support.RepositoryTestCase):
    def test_reviews_run_together_and_integration_follows_implementation_order(self):
        with self.repository() as (root, store, base, task, git):
            tasks = [dict(task, id=tid, files=[f'{tid}.txt']) for tid in ('A', 'B')]
            both_reviewing = threading.Barrier(2, timeout=10)
            original_review = agents.review
            integrated = []
            original_integrate = runner.integrate

            def agent(prompt, wt, stop=None):
                data = json.loads(prompt.split('\n', 1)[1])
                (wt / data['task']['files'][0]).write_text(data['task']['id'])
                return 'done'

            def review(*args):
                # Uma revisão serial nunca teria duas chamadas simultâneas aqui.
                both_reviewing.wait()
                return original_review(*args)

            def record(ctx, artifact, *args):
                integrated.append(artifact.task['id'])
                return original_integrate(ctx, artifact, *args)

            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(agents, 'review', side_effect=review),
                patch.object(runner, 'integrate', side_effect=record),
                patch.object(runner.concurrent.futures, 'as_completed', side_effect=iter),
            ):
                states = runner.execute(self.ws, {'tasks': tasks}, 2, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved', 'B': 'approved'})
            self.assertEqual(integrated, ['A', 'B'])
            self.assertEqual((root / 'A.txt').read_text(), 'A')
            self.assertEqual((root / 'B.txt').read_text(), 'B')

    def test_recovery_during_one_review_blocks_every_integration(self):
        with self.repository() as (root, store, base, task, git):
            tasks = [dict(task, id=tid, files=[f'{tid}.txt']) for tid in ('A', 'B')]

            def agent(prompt, wt, stop=None):
                data = json.loads(prompt.split('\n', 1)[1])
                (wt / data['task']['files'][0]).write_text('x')
                return 'done'

            def review(artifact, *args):
                if artifact.task['id'] == 'A':
                    raise support.errors.RecoveryRequired('test process still alive')
                return True, 'APPROVED'

            with patch.object(agents, 'run_agy', side_effect=agent), patch.object(agents, 'review', side_effect=review):
                states = runner.execute(self.ws, {'tasks': tasks}, 2, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
            status = support.read_status(self.ws)
            self.assertTrue(status['recovery_required'])
            self.assertIn('still alive', status['details']['A']['error'])
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertTrue((store / 'run.lock').exists())
