"""Consultas Git: estado do checkout e merge de três pontas."""

import subprocess
from unittest.mock import patch

import support

from contracanto import gitops


class CheckoutStateTests(support.RepositoryTestCase):
    def test_clean_branch_and_detached_head(self):
        with self.repository() as (root, store, base, task, git):
            self.assertEqual(gitops.checkout_state(root), gitops.CheckoutState('refs/heads/main', base, ()))
            git('switch', '--detach')
            self.assertEqual(gitops.checkout_state(root), gitops.CheckoutState('HEAD', base, ()))

    def test_paths_cover_edits_renames_untracked_and_conflicts(self):
        with self.repository() as (root, store, base, task, git):
            (root / 'kept.txt').write_text('kept\n')
            git('add', '.')
            git('commit', '-m', 'more')
            (root / 'outside.txt').write_text('edited\n')
            git('mv', 'kept.txt', 'ação nova.txt')
            (root / 'sem rastreio.txt').write_text('x')
            current = gitops.checkout_state(root)
            self.assertEqual(set(current.paths), {'outside.txt', 'ação nova.txt', 'kept.txt', 'sem rastreio.txt'})
            self.assertEqual(set(gitops.changed_files(root)), set(current.paths))

    def test_unmerged_entry_is_reported(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '-c', 'other')
            (root / 'outside.txt').write_text('other\n')
            git('commit', '-am', 'other')
            git('switch', 'main')
            (root / 'outside.txt').write_text('main\n')
            git('commit', '-am', 'main')
            git('merge', 'other', check=False)
            self.assertEqual(gitops.checkout_state(root).paths, ('outside.txt',))
            git('merge', '--abort')

    def test_one_status_call_replaces_three_commands(self):
        with self.repository() as (root, store, base, task, git):
            calls = []

            def counting(*args, **kwargs):
                calls.append(args[0])
                return git(*args, **kwargs)

            with patch.object(gitops, 'git', side_effect=counting):
                gitops.check_revision(root, base, 'refs/heads/main')
            self.assertEqual(calls, ['rev-parse', 'status', 'ls-files'])
            # O diretório Git do checkout é lembrado: as conferências seguintes fazem duas consultas.
            calls.clear()
            with patch.object(gitops, 'git', side_effect=counting):
                gitops.check_revision(root, base, 'refs/heads/main')
            self.assertEqual(calls, ['status', 'ls-files'])

    def test_unexpected_git_dir_answer_is_not_remembered(self):
        with support.temp_dir() as directory:
            answer = subprocess.CompletedProcess([], 0, 'não é um diretório', '')
            with patch.object(gitops, 'git', return_value=answer) as git:
                gitops.git_dir(directory)
                gitops.git_dir(directory)
            self.assertEqual(git.call_count, 2)


class ConflictTests(support.RepositoryTestCase):
    def test_merge_tree_conflict_names_the_files(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '-c', 'other')
            (root / 'outside.txt').write_text('other\n')
            git('commit', '-am', 'other')
            other = git('rev-parse', 'HEAD').stdout.strip()
            git('switch', 'main')
            (root / 'outside.txt').write_text('main\n')
            git('commit', '-am', 'main')
            with self.assertRaisesRegex(RuntimeError, r'Conflito.*outside\.txt'):
                gitops.merge_tree(root, 'HEAD', other)

    def test_merge_tree_returns_the_merged_tree(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '-c', 'other')
            (root / 'new.txt').write_text('new\n')
            git('add', '.')
            git('commit', '-m', 'other')
            tree = gitops.merge_tree(root, base, 'HEAD')
            self.assertEqual(tree, git('rev-parse', 'HEAD^{tree}').stdout.strip())
