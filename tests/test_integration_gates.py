"""Integração, rollback e recuperação, com Git real, modelos simulados e falhas Git injetadas."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, errors, gitops, runner
from contracanto import process as proc
from contracanto import state as st


class IntegrationGateTests(support.RepositoryTestCase):
    def test_failed_approval_write_preserves_integrated_sha_and_lock(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent), self.repository() as (root, store, base, task, git):
                original_write = st.write_json
                failed = False

                def write(path, value):
                    nonlocal failed
                    if value.get('statuses', {}).get('A') == 'approved' and (persistent or not failed):
                        failed = True
                        raise OSError('approval write failed')
                    original_write(path, value)

                second = dict(task, id='B', files=['b.txt'])
                with (
                    patch.object(st, 'write_json', side_effect=write),
                    self.assertRaises((errors.RecoveryRequired, OSError)),
                ):
                    runner.execute(self.ws, {'tasks': [task, second]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertTrue(failed)
                self.assertTrue((store / 'run.lock').exists())
                state = support.read_status(self.ws)
                self.assertEqual(state['details']['A']['integrated_commit'], git('rev-parse', 'HEAD').stdout.strip())
                if not persistent:
                    self.assertTrue(state['recovery_required'])
                    self.assertEqual(state['statuses'], {'A': 'approved', 'B': 'blocked'})
                self.assertFalse((root / 'b.txt').exists())

    def test_revert_preserves_ignored_file_created_by_integration_tests(self):
        with self.repository() as (root, store, base, task, git):
            (root / '.gitignore').write_text('.agent-runs/\noutside.txt\n')
            git('add', '.gitignore')
            git('commit', '-m', 'Ignore untracked copies')
            task['files'] = ['outside.txt']

            def agent(prompt, wt, stop=None):
                (wt / 'outside.txt').unlink()
                return 'deleted tracked file'

            code = (
                'from pathlib import Path\n'
                "if Path('.agent-runs').is_dir():\n"
                " Path('outside.txt').write_bytes(b'local diagnostic')\n"
                ' raise SystemExit(1)'
            )
            with patch.object(agents, 'run_agy', side_effect=agent):
                result = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(result, {'A': 'failed'})
            self.assertEqual((root / 'outside.txt').read_bytes(), b'local diagnostic')
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])

    def test_interrupt_after_successful_pick_keeps_unverified_commit_reserved(self):
        with self.repository() as (root, store, base, task, git):

            class InterruptedResult:
                @property
                def returncode(self):
                    raise KeyboardInterrupt

            def interrupt_after_pick(*args, **kwargs):
                result = git(*args, **kwargs)
                if args[0] == 'cherry-pick' and result.returncode == 0:
                    return InterruptedResult()
                return result

            with patch.object(gitops, 'git', side_effect=interrupt_after_pick):
                try:
                    runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
                except (KeyboardInterrupt, errors.RecoveryRequired):
                    pass
            self.assertNotEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertTrue((store / 'run.lock').exists())
            state = support.read_status(self.ws)
            self.assertTrue(state['recovery_required'])
            self.assertNotIn('approved', state['statuses'].values())

    def test_last_worker_failure_checks_target_before_release(self):
        with self.repository() as (root, store, base, task, git):

            def switch_and_fail(*args):
                git('switch', '-c', 'release')
                raise RuntimeError('executor failed')

            with (
                patch.object(agents, 'run_agy', side_effect=switch_and_fail),
                patch.object(agents.config, 'AGENT_RETRIES', 0),
            ):
                try:
                    runner.execute(self.ws, {'tasks': [task]}, 1, 0, ['test'])
                except errors.RecoveryRequired:
                    pass
            self.assertTrue((store / 'run.lock').exists())
            state = support.read_status(self.ws)
            self.assertTrue(state['recovery_required'])
            self.assertEqual(state['statuses'], {'A': 'failed'})
            self.assertEqual(state['details']['A']['error'], 'executor failed')

    def test_pending_merge_after_integration_requires_recovery(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '-c', 'side')
            git('commit', '--allow-empty', '-m', 'parallel history')
            git('switch', 'main')
            code = (
                'from pathlib import Path\nimport subprocess\n'
                "if Path('.agent-runs').exists():\n"
                " subprocess.run(['git','merge','--no-ff','--no-commit','side'],check=True)"
            )
            result = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(result, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])
            self.assertEqual(git('rev-parse', 'MERGE_HEAD').stdout, git('rev-parse', 'side').stdout)

    def test_tests_cannot_replace_head_even_with_clean_working_tree(self):
        with self.repository() as (root, store, base, task, git):
            command = [
                sys.executable,
                '-c',
                "import subprocess; subprocess.run(['git','commit','--allow-empty','-m','unexpected'],check=True)",
            ]
            with self.assertRaises(RuntimeError):
                gitops.run_tests(command, root, base)
            self.assertEqual(git('status', '--porcelain').stdout, '')

    def test_integration_tests_that_modify_files_require_manual_recovery(self):
        for exit_code in (0, 1, 'timeout'):
            with (
                self.subTest(exit_code=exit_code),
                self.repository(mutate_tests='main', test_exit=exit_code) as (root, store, base, task, git),
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertEqual(states, {'A': 'failed'})
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(self.ws)['recovery_required'])
                self.assertEqual((root / 'outside.txt').read_text(), 'changed by tests\n')
                with self.assertRaises(RuntimeError):
                    st.reserve_run(self.ws)

    def test_git_inspection_failure_after_tests_preserves_history_and_reservation(self):
        for inspection in ('rev-parse', 'status'):
            for exit_code in (0, 1):
                with (
                    self.subTest(inspection=inspection, exit_code=exit_code),
                    self.repository() as (root, store, base, task, git),
                ):
                    marker = store / 'test-mutated-repository'
                    code = (
                        'from pathlib import Path\nimport subprocess, sys\n'
                        "if Path('.agent-runs').exists():\n"
                        " Path('outside.txt').write_text('changed by integration tests\\n')\n"
                        + (
                            " subprocess.run(['git','add','outside.txt'],check=True)\n"
                            " subprocess.run(['git','commit','-m','test commit'],check=True)\n"
                            if inspection == 'rev-parse'
                            else ''
                        )
                        + " Path('.agent-runs/test-mutated-repository').write_text('ready')\n"
                        + f' sys.exit({exit_code})\n'
                    )
                    failed = False

                    def unreliable_git(*args, **kwargs):
                        nonlocal failed
                        if args[0] == inspection and marker.exists() and not failed:
                            failed = True
                            raise RuntimeError('Git inspection unavailable')
                        return git(*args, **kwargs)

                    second = dict(task, id='B', files=['another.txt'])
                    with patch.object(gitops, 'git', side_effect=unreliable_git):
                        states = runner.execute(self.ws, {'tasks': [task, second]}, 1, 0, [sys.executable, '-c', code])
                    self.assertTrue(failed, 'The intended Git read must have failed')
                    self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertTrue(support.read_status(self.ws)['recovery_required'])
                    self.assertEqual((root / 'outside.txt').read_text(), 'changed by integration tests\n')
                    self.assertNotIn('Revert', git('log', '--format=%s').stdout)
                    with self.assertRaises(RuntimeError):
                        st.reserve_run(self.ws)

    def test_unreadable_head_after_cherry_pick_requires_manual_recovery(self):
        with self.repository() as (root, store, base, task, git):
            applied = False

            def unreliable_git(*args, **kwargs):
                nonlocal applied
                if applied and args == ('rev-parse', 'HEAD'):
                    raise RuntimeError('Integrated HEAD unavailable')
                result = git(*args, **kwargs)
                if args[0] == 'cherry-pick' and result.returncode == 0:
                    applied = True
                return result

            with patch.object(gitops, 'git', side_effect=unreliable_git):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])
            self.assertTrue((root / 'allowed.txt').exists())
            self.assertNotIn('Revert', git('log', '--format=%s').stdout)

    def test_branch_change_during_review_blocks_integration(self):
        for switch in (('switch', '-c', 'unrelated'), ('switch', '--detach')):
            with self.subTest(switch=switch), self.repository() as (root, store, base, task, git):
                original_review = agents.review

                def switched_review(*args):
                    result = original_review(*args)
                    git(*switch)
                    return result

                second = dict(task, id='B', files=['another.txt'])
                with patch.object(agents, 'review', side_effect=switched_review):
                    states = runner.execute(self.ws, {'tasks': [task, second]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
                self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(self.ws)['recovery_required'])

    def test_tests_cannot_switch_branch_at_the_same_commit(self):
        with self.repository() as (root, store, base, task, git):
            command = [
                sys.executable,
                '-c',
                "import subprocess; subprocess.run(['git','switch','-c','changed-by-tests'],check=True)",
            ]
            with self.assertRaises(RuntimeError):
                gitops.run_tests(command, root, base)
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual(git('branch', '--show-current').stdout.strip(), 'changed-by-tests')

    def test_integration_branch_change_never_reverts_or_releases_reservation(self):
        for outcome in (0, 1, 'timeout', 'interrupt'):
            with self.subTest(outcome=outcome), self.repository() as (root, store, base, task, git):
                original_cmd = proc.run
                code = (
                    'from pathlib import Path\nimport subprocess,sys\n'
                    "if Path('.agent-runs').exists():\n"
                    " subprocess.run(['git','switch','-c','changed-by-tests'],check=True)\n"
                    f' sys.exit({outcome if isinstance(outcome, int) else 0})\n'
                )

                def command(args, **kwargs):
                    result = original_cmd(args, **kwargs)
                    if args[0] == sys.executable and Path(kwargs['cwd']) == root:
                        if outcome == 'timeout':
                            raise subprocess.TimeoutExpired(args, 600)
                        if outcome == 'interrupt':
                            raise KeyboardInterrupt
                    return result

                second = dict(task, id='B', files=['another.txt'])
                with patch.object(proc, 'run', side_effect=command):
                    try:
                        states = runner.execute(self.ws, {'tasks': [task, second]}, 1, 0, [sys.executable, '-c', code])
                    except KeyboardInterrupt:
                        self.fail('Branch change escaped without requiring recovery')
                self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(self.ws)['recovery_required'])
                self.assertNotEqual(git('rev-parse', 'main').stdout.strip(), base)
                self.assertEqual(git('rev-parse', 'HEAD').stdout, git('rev-parse', 'main').stdout)
                self.assertNotIn('Revert', git('log', '--all', '--format=%s').stdout)
                with self.assertRaises(RuntimeError):
                    st.reserve_run(self.ws)

    def test_unreadable_branch_after_tests_requires_manual_recovery(self):
        with self.repository() as (root, store, base, task, git):
            marker = store / 'tests-finished'
            code = (
                'from pathlib import Path\n'
                "if Path('.agent-runs').exists(): Path('.agent-runs/tests-finished').touch()\n"
            )

            def unreliable_git(*args, **kwargs):
                if args[0] == 'status' and marker.exists():
                    return subprocess.CompletedProcess(args, 128, '', 'branch read failed')
                return git(*args, **kwargs)

            with patch.object(gitops, 'git', side_effect=unreliable_git):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])
            self.assertNotIn('Revert', git('log', '--format=%s').stdout)

    def test_branch_is_rechecked_before_rollback(self):
        with self.repository() as (root, store, base, task, git):
            original_tests = gitops.run_tests

            def switched_tests(command, wt, *args, **kwargs):
                result = original_tests(command, wt, *args, **kwargs)
                if wt == root:
                    git('switch', '-c', 'changed-before-revert')
                    return subprocess.CompletedProcess(command, 1, '', 'failed')
                return result

            with patch.object(gitops, 'run_tests', side_effect=switched_tests):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(support.read_status(self.ws)['recovery_required'])
            self.assertNotIn('Revert', git('log', '--all', '--format=%s').stdout)

    def test_approved_worktree_is_removed_unless_kept(self):
        for keep in (False, True):
            with self.subTest(keep=keep), self.repository() as (root, store, base, task, git):
                states = runner.execute(
                    self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'], keep_worktrees=keep
                )
                self.assertEqual(states, {'A': 'approved'})
                details = support.read_status(self.ws)['details']['A']
                self.assertEqual(Path(details['worktree']).exists(), keep)
                self.assertEqual(bool(git('branch', '--format=%(refname)', '--list', 'agent/*').stdout.strip()), keep)
                self.assertEqual(details.get('worktree_removed', False), not keep)
                self.assertNotIn('cleanup_error', details)
                self.assertEqual(git('status', '--porcelain').stdout, '')

    def test_failed_worktree_cleanup_does_not_fail_an_integrated_task(self):
        with self.repository() as (root, store, base, task, git):

            def locked_worktree(*args, **kwargs):
                if args[:2] == ('worktree', 'remove') and '--force' not in args:
                    raise RuntimeError('worktree in use')
                return git(*args, **kwargs)

            with patch.object(gitops, 'git', side_effect=locked_worktree):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved'})
            state = support.read_status(self.ws)
            self.assertEqual(state['state'], 'completed')
            self.assertIn('worktree in use', state['details']['A']['cleanup_error'])
            self.assertTrue(Path(state['details']['A']['worktree']).exists())
            self.assertFalse((store / 'run.lock').exists())

    def test_unchanged_detached_head_can_integrate(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '--detach')
            states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(git('branch', '--show-current').stdout.strip(), '')
            self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)
            self.assertTrue((root / 'allowed.txt').exists())
            self.assertFalse((store / 'run.lock').exists())
