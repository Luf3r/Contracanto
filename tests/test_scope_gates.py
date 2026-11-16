"""Escopo de arquivos e identidade do checkout, com Git real e modelos simulados."""

import sys
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, gitops, runner


class ScopeGateTests(support.RepositoryTestCase):
    def test_parallel_directory_rename_cannot_redirect_an_authorized_addition(self):
        with self.repository() as (root, store, base, task, git):
            git('config', 'merge.directoryRenames', 'true')
            (root / 'old').mkdir()
            (root / 'old/a.txt').write_text('seed')
            git('add', 'old')
            git('commit', '-m', 'directory to rename')
            tasks = [dict(task, files=['old/a.txt', 'new/a.txt']), dict(task, id='B', files=['old/b.txt'])]

            def agent(prompt, wt, stop=None):
                if wt.name == 'wt-A':
                    (wt / 'old').rename(wt / 'new')
                else:
                    (wt / 'old/b.txt').write_text('added by B')
                return 'done'

            # Both workers start at the same commit. Integrate A first.
            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(runner.concurrent.futures, 'as_completed', side_effect=iter),
            ):
                states = runner.execute(self.ws, {'tasks': tasks}, 2, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved', 'B': 'failed'})
            details = support.read_status(self.ws)['details']
            self.assertIn('new/b.txt', details['B']['error'])
            self.assertEqual(details['A']['integrated_commit'], git('rev-parse', 'HEAD').stdout.strip())
            self.assertNotEqual(details['A']['integrated_commit'], details['A']['commit'])
            # A tarefa integrada não deixa worktree nem branch; a que falhou fica para inspeção.
            self.assertTrue(details['A']['worktree_removed'])
            self.assertFalse(Path(details['A']['worktree']).exists())
            self.assertTrue(Path(details['B']['worktree']).exists())
            self.assertEqual(len(git('branch', '--format=%(refname)', '--list', 'agent/*').stdout.split()), 1)
            self.assertFalse((root / 'new/b.txt').exists())
            self.assertTrue((root / 'new/a.txt').is_file())

    def test_tests_cannot_hide_tracked_changes_with_index_flags(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag), self.repository() as (root, store, base, task, git):
                git('update-index', flag, 'outside.txt')
                code = (
                    'from pathlib import Path\n'
                    "if Path('.agent-runs').is_dir(): Path('outside.txt').write_text('modified')"
                )
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
                self.assertEqual(states, {'A': 'failed'})
                self.assertEqual((root / 'outside.txt').read_text(), 'modified')
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(self.ws)['recovery_required'])
                self.assertEqual(
                    git('ls-files', '-v', 'outside.txt').stdout[0], 'h' if flag == '--assume-unchanged' else 'S'
                )

    def test_sparse_checkout_accepts_excluded_files_and_detects_missing_selected_file(self):
        with self.repository() as (root, store, base, task, git):
            for directory in ('src', 'docs'):
                (root / directory).mkdir()
                (root / directory / 'entry').write_text(directory)
            git('add', 'src', 'docs')
            git('commit', '-m', 'sparse fixture')
            git('sparse-checkout', 'set', '--cone', 'src')
            self.assertFalse((root / 'docs/entry').exists())
            self.assertEqual(gitops.changed_files(root), [])
            git('update-index', '--skip-worktree', 'src/entry')
            (root / 'src/entry').unlink()
            self.assertIn('src/entry', gitops.changed_files(root))

    def test_ignored_local_data_survives_integration(self):
        for layout in ('file', 'directory', 'parent', 'nested_repo'):
            with self.subTest(layout=layout), self.repository() as (root, store, base, task, git):
                (root / '.gitignore').write_text('.agent-runs/\ncache.local\n')
                git('add', '.gitignore')
                git('commit', '-m', 'Ignore local cache')
                before = git('rev-parse', 'HEAD').stdout
                local = root / 'cache.local'
                if layout in ('directory', 'nested_repo'):
                    local.mkdir()
                    if layout == 'nested_repo':
                        git('init', cwd=local)
                    local = local / 'private'
                local.write_bytes(b'local data')
                task['files'] = ['.gitignore', 'cache.local']

                def agent(prompt, wt, stop=None):
                    (wt / '.gitignore').write_text('.agent-runs/\n')
                    destination = wt / 'cache.local'
                    if layout in ('parent', 'nested_repo'):
                        destination.mkdir()
                        destination = destination / ('private' if layout == 'nested_repo' else 'new')
                    destination.write_text('candidate')
                    return 'done'

                with patch.object(agents, 'run_agy', side_effect=agent):
                    result = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertEqual(result, {'A': 'failed'})
                self.assertEqual(local.read_bytes(), b'local data')
                self.assertEqual(git('rev-parse', 'HEAD').stdout, before)
                self.assertFalse((store / 'run.lock').exists())

    def test_integration_detects_dirty_submodule_with_ignore_all(self):
        with self.repository() as (root, store, base, task, git):
            vendor = root / 'vendor'
            vendor.mkdir()
            git('init', cwd=vendor)
            (vendor / 'outside.txt').write_text('original')
            git('add', '.', cwd=vendor)
            git('-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'initial', cwd=vendor)
            git('config', '-f', '.gitmodules', 'submodule.vendor.path', 'vendor')
            git('config', '-f', '.gitmodules', 'submodule.vendor.url', './vendor')
            git('config', '-f', '.gitmodules', 'submodule.vendor.ignore', 'all')
            git('add', '.gitmodules', 'vendor')
            git('commit', '-m', 'Register submodule')
            code = "from pathlib import Path; p = Path('vendor/outside.txt'); p.exists() and p.write_text('dirty')"
            result = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(result, {'A': 'failed'})
            self.assertEqual((vendor / 'outside.txt').read_text(), 'dirty')
            self.assertTrue(support.read_status(self.ws)['recovery_required'])
            self.assertTrue((store / 'run.lock').exists())

    def test_executor_commit_outside_scope_is_not_integrated(self):
        with self.repository(agent_commit=1) as (root, store, base, task, git):
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual((root / 'outside.txt').read_text(), 'original\n')

    def test_agent_branch_switch_cannot_commit_to_another_branch(self):
        for stage in ('implementation', 'repair'):
            with self.subTest(stage=stage), self.repository() as (root, store, base, task, git):
                release_before = None

                def agent(prompt, wt, stop=None):
                    nonlocal release_before
                    repair = '"review":' in prompt
                    if repair or stage == 'implementation':
                        release_before = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                        git('switch', '-c', 'release', cwd=wt)
                    (wt / 'allowed.txt').write_text('repair' if repair else 'first')
                    return 'done'

                with (
                    patch.object(agents, 'run_agy', side_effect=agent),
                    patch.object(agents, 'review', return_value=(False, 'REJECTED', '')),
                ):
                    result = runner.execute(self.ws, {'tasks': [task]}, 1, int(stage == 'repair'), ['test'])
                self.assertEqual(result, {'A': 'failed'})
                self.assertEqual(git('rev-parse', 'release').stdout.strip(), release_before)
                self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)

    def test_repair_commit_outside_scope_is_not_squashed_and_integrated(self):
        with self.repository(agent_commit=2) as (root, store, base, task, git):
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual((root / 'outside.txt').read_text(), 'original\n')

    def test_worker_tests_cannot_approve_a_dirty_worktree(self):
        with self.repository(mutate_tests='worker') as (root, store, base, task, git):
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertFalse((store / 'run.lock').exists())

    def test_dirty_worker_cannot_hide_uncertain_subprocess_cleanup(self):
        with self.repository(mutate_tests='worker', test_exit='cleanup') as (root, store, base, task, git):
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])

    def test_full_candidate_diff_is_checked_before_review(self):
        with self.repository() as (root, store, base, task, git):
            original_executor = agents.run_executor

            # Remove the destination first so Git can rename into the authorized path.
            def forged_executor(*args):
                artifact = original_executor(*args)
                wt = Path(artifact.worktree)
                (wt / 'allowed.txt').unlink()
                git('mv', 'outside.txt', 'allowed.txt', cwd=wt)
                git('add', '.', cwd=wt)
                git('commit', '-m', 'rename outside scope', cwd=wt)
                git('reset', '--soft', base, cwd=wt)
                git('commit', '-m', 'squashed candidate', cwd=wt)
                artifact.commit = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                return artifact

            with patch.object(agents, 'run_executor', side_effect=forged_executor):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertIn('fora do escopo', support.read_status(self.ws)['details']['A']['error'])
