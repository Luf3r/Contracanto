"""Poda dos registros de execuções encerradas."""

import json
import sys
from pathlib import Path

import support

from contracanto import maintenance, runner
from contracanto import state as st

PASS = [sys.executable, '-c', 'pass']


class PruneTests(support.RepositoryTestCase):
    def test_prune_keeps_recent_reserved_and_recovery_runs(self):
        with self.repository() as (root, store, base, task, git):
            command = [sys.executable, '-c', 'pass']
            for _ in range(3):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, command, keep_worktrees=True)
                self.assertEqual(states, {'A': 'approved'})
            old, middle, recent = self.ws.run_ids()
            middle_status = json.loads(self.ws.status(middle).read_text())
            st.write_json(self.ws.status(middle), {**middle_status, 'recovery_required': True})
            self.assertEqual(len(git('worktree', 'list').stdout.splitlines()), 4)
            result = maintenance.prune_runs(self.ws, keep=1)
            self.assertEqual(result['removed'], [old], result)
            self.assertEqual(result['kept'], {middle: 'recuperação pendente'})
            self.assertEqual(self.ws.run_ids(), [middle, recent])
            self.assertEqual(len(git('worktree', 'list').stdout.splitlines()), 3)
            self.assertEqual(len(git('branch', '--format=%(refname)', '--list', 'agent/*').stdout.split()), 2)

    def test_prune_keeps_run_with_dirty_worktree(self):
        with self.repository() as (root, store, base, task, git):
            runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'], keep_worktrees=True)
            (run_id,) = self.ws.run_ids()
            worktree = Path(support.read_status(self.ws)['details']['A']['worktree'])
            (worktree / 'allowed.txt').write_text('local edit')
            result = maintenance.prune_runs(self.ws, keep=0)
            self.assertEqual(result['removed'], [])
            self.assertIn('worktree não removido', result['kept'][run_id])
            self.assertTrue(worktree.exists())


class ForcedPruneTests(support.RepositoryTestCase):
    def test_force_discards_a_dirty_worktree(self):
        with self.repository() as (root, store, base, task, git):
            runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, keep_worktrees=True)
            (run_id,) = self.ws.run_ids()
            worktree = Path(support.read_status(self.ws)['details']['A']['worktree'])
            (worktree / 'allowed.txt').write_text('local edit')
            self.assertEqual(maintenance.prune_runs(self.ws, keep=0)['removed'], [])
            result = maintenance.prune_runs(self.ws, keep=0, force=True)
            self.assertEqual(result, {'removed': [run_id], 'kept': {}})
            self.assertFalse(worktree.exists())
            self.assertEqual(git('branch', '--format=%(refname)', '--list', 'agent/*').stdout.split(), [])
