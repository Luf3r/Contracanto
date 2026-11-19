"""Worktrees fora do repositório e aviso quando a integração enxerga o registro .agent-runs."""

import dataclasses
import os
import sys
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import config, maintenance, runner
from contracanto import state as st

PASS = [sys.executable, '-c', 'pass']
# Passa no worktree, onde .git é um arquivo, e falha na raiz, citando uma cópia antiga em
# .agent-runs como faria um test runner que varre a árvore inteira.
FAILS_AT_ROOT = [
    sys.executable,
    '-c',
    'import os, sys; at_root = os.path.isdir(".git"); '
    'print(".agent-runs/runs/x/wt-A/app.test.js falhou" if at_root else "ok"); sys.exit(1 if at_root else 0)',
]


class WorktreeLocationTests(support.RepositoryTestCase):
    def test_repository_worktrees_live_outside_the_repository(self):
        with support.temp_dir() as directory:
            elsewhere = directory / 'worktrees'
            first, second = directory / 'a' / 'app', directory / 'b' / 'app'
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            with patch.object(config, 'WORKTREES_DIR', str(elsewhere)):
                ws, other = st.Workspace.for_root(first), st.Workspace.for_root(second)
            run_dir = ws.worktree_dir('f' * 64)
            self.assertEqual(run_dir.parent.parent, elsewhere.resolve())
            self.assertTrue(run_dir.parent.name.startswith('app-'))
            self.assertNotEqual(run_dir.parent, other.worktree_dir('f' * 64).parent)
            self.assertFalse(run_dir.is_relative_to(first.resolve()))
            self.assertEqual(run_dir.name, 'f' * 16)

    def test_default_location_is_the_user_cache_outside_localappdata(self):
        with (
            support.temp_dir() as directory,
            patch.object(config, 'WORKTREES_DIR', ''),
            patch.dict(os.environ, {'XDG_CACHE_HOME': str(directory / 'cache'), 'LOCALAPPDATA': str(directory)}),
        ):
            parent = st.worktrees_parent()
            legacy = st.legacy_worktrees_parent()
        self.assertEqual(parent, (directory / 'cache' / 'Contracanto' / 'worktrees').resolve())
        expected = (directory / 'Contracanto' / 'worktrees').resolve() if os.name == 'nt' else None
        self.assertEqual(legacy, expected)

    def test_configured_location_has_no_legacy_folder(self):
        with support.temp_dir() as directory, patch.object(config, 'WORKTREES_DIR', str(directory)):
            self.assertIsNone(st.legacy_worktrees_parent())
            self.assertIsNone(st.Workspace.for_root(directory).legacy_worktrees)

    def test_prune_also_removes_worktrees_left_in_the_legacy_folder(self):
        with self.repository() as (root, store, base, task, git), support.temp_dir() as outside:
            old, new = outside / 'old' / 'repo', outside / 'new' / 'repo'
            self.ws = dataclasses.replace(self.ws, worktrees=old)
            runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, keep_worktrees=True)
            (run_id,) = self.ws.run_ids()
            # The default moved after the run: the worktree stays where the old default put it.
            self.ws = dataclasses.replace(self.ws, worktrees=new, legacy_worktrees=old)
            result = maintenance.prune_runs(self.ws, keep=0)
            self.assertEqual(result['removed'], [run_id], result)
            self.assertFalse((old / run_id[:16]).exists())
            self.assertEqual(len(git('worktree', 'list').stdout.splitlines()), 1)

    def test_execution_and_prune_use_the_outside_directory(self):
        with self.repository() as (root, store, base, task, git), support.temp_dir() as outside:
            self.ws = dataclasses.replace(self.ws, worktrees=outside / 'repo')
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, keep_worktrees=True)
            self.assertEqual(states, {'A': 'approved'})
            (run_id,) = self.ws.run_ids()
            worktree = Path(support.read_status(self.ws)['details']['A']['worktree'])
            self.assertEqual(worktree.parent, self.ws.worktree_dir(run_id))
            self.assertFalse(worktree.is_relative_to(root))
            result = maintenance.prune_runs(self.ws, keep=0)
            self.assertEqual(result['removed'], [run_id], result)
            self.assertFalse(self.ws.worktree_dir(run_id).exists())
            self.assertEqual(len(git('worktree', 'list').stdout.splitlines()), 1)


class RegistryWarningTests(support.RepositoryTestCase):
    def test_hint_recognizes_both_separators(self):
        self.assertIsNotNone(runner.registry_hint('FAIL .agent-runs/runs/x/wt-A/a.test.ts'))
        self.assertIsNotNone(runner.registry_hint('C:\\repo\\.agent-runs\\runs\\x\\wt-A\\a.py'))
        self.assertIsNone(runner.registry_hint('FAIL src/a.test.ts'))

    def test_failed_integration_that_saw_the_registry_says_so(self):
        with self.repository() as (root, store, base, task, git):
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, FAILS_AT_ROOT)
            self.assertEqual(states, {'A': 'failed'})
            detail = support.read_status(self.ws)['details']['A']
            self.assertIn('Integração revertida', detail['error'])
            self.assertIn('.agent-runs', detail['error'])
            self.assertIn('--prune-runs', detail['error'])
            self.assertIn('--prune-runs', detail['warning'])
