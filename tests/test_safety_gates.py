"""Safety regressions with real Git, simulated models and injected Git failures."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o


class SafetyGateTests(unittest.TestCase):
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

            with patch.object(o, 'git', side_effect=interrupt_after_pick):
                try:
                    o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
                except (KeyboardInterrupt, o.RecoveryRequired):
                    pass
            self.assertNotEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertTrue((store / 'run.lock').exists())
            state = json.loads((store / 'status.json').read_text())
            self.assertTrue(state['recovery_required'])
            self.assertNotIn('approved', state['statuses'].values())

    def test_last_worker_failure_checks_target_before_release(self):
        with self.repository() as (root, store, base, task, git):
            def switch_and_fail(*args):
                git('switch', '-c', 'release')
                raise RuntimeError('executor failed')

            with patch.object(o, 'run_agy', side_effect=switch_and_fail):
                try:
                    o.execute({'tasks': [task]}, 1, 0, ['test'])
                except o.RecoveryRequired:
                    pass
            self.assertTrue((store / 'run.lock').exists())
            state = json.loads((store / 'status.json').read_text())
            self.assertTrue(state['recovery_required'])
            self.assertEqual(state['statuses'], {'A': 'failed'})
            self.assertEqual(state['details']['A']['error'], 'executor failed')

    def test_failed_test_diagnostic_reaches_repair_and_final_status(self):
        with self.repository() as (root, store, base, task, git):
            agent = o.run_agy
            repairs = []

            def capture_repair(prompt, wt):
                request = json.loads(prompt.split('\n', 1)[1])
                if 'review' in request:
                    repairs.append(request['review'])
                return agent(prompt, wt)

            command = [sys.executable, '-c', "import sys; sys.exit('missing expected record')"]
            with patch.object(o, 'run_agy', side_effect=capture_repair):
                result = o.execute({'tasks': [task]}, 1, 1, command)
            self.assertEqual(result, {'A': 'failed'})
            self.assertEqual(len(repairs), 1)
            self.assertIn('missing expected record', repairs[0])
            state = json.loads((store / 'status.json').read_text())
            self.assertIn('missing expected record', state['details']['A']['error'])

    @contextlib.contextmanager
    def repository(self, agent_commit=None, mutate_tests=None, test_exit=0):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            root = Path(directory).resolve()
            original_cmd = o.cmd

            def git(*args, cwd=None, check=True):
                return original_cmd(['git', *args], cwd=cwd or root, check=check)

            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@localhost')
            (root / '.gitignore').write_text('.agent-runs/\n')
            (root / 'outside.txt').write_text('original\n')
            git('add', '.')
            git('commit', '-m', 'baseline')
            base = git('rev-parse', 'HEAD').stdout.strip()
            store = root / '.agent-runs'
            store.mkdir()
            calls = {'agent': 0, 'review': 0}

            def agent(prompt, wt):
                calls['agent'] += 1
                if calls['agent'] == agent_commit:
                    (wt / 'outside.txt').write_text('unauthorized\n')
                    git('add', 'outside.txt', cwd=wt)
                    git('commit', '-m', 'unexpected agent commit', cwd=wt)
                (wt / 'allowed.txt').write_text(str(calls['agent']))
                return 'done'

            def command(args, **kwargs):
                kwargs.setdefault('cwd', root)
                if args[0] == 'claude':
                    calls['review'] += 1
                    verdict = 'REJECTED' if agent_commit == 2 and calls['review'] == 1 else 'APPROVED'
                    return subprocess.CompletedProcess(args, 0, json.dumps({'result': verdict}), '')
                if args[0] == sys.executable:
                    in_main = Path(kwargs['cwd']) == root
                    if mutate_tests == ('main' if in_main else 'worker'):
                        (Path(kwargs['cwd']) / 'outside.txt').write_text('changed by tests\n')
                        if test_exit == 'timeout':
                            raise subprocess.TimeoutExpired(args, 600)
                        if test_exit == 'cleanup':
                            raise o.RecoveryRequired('Subprocess termination not confirmed')
                        return subprocess.CompletedProcess(args, test_exit, '', '')
                return original_cmd(args, **kwargs)

            task = dict(id='A', description='Create allowed.txt', deps=[],
                        files=['allowed.txt'], acceptance=['File exists'])
            with contextlib.ExitStack() as stack:
                for name, value in [('ROOT', root), ('STORE', store), ('git', git), ('cmd', command), ('run_agy', agent)]:
                    stack.enter_context(patch.object(o, name, value))
                stack.enter_context(patch.object(o.shutil, 'which', return_value='fake-cli'))
                try:
                    yield root, store, base, task, git
                finally:
                    for wt in (store / 'runs').glob('*/wt-*'):
                        # Preserve no test worktree registrations; dirty fixtures are disposable.
                        git('worktree', 'remove', '--force', str(wt))

    def test_executor_commit_outside_scope_is_not_integrated(self):
        with self.repository(agent_commit=1) as (root, store, base, task, git):
            states = o.execute({'tasks': [task]}, 1, 1, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual((root / 'outside.txt').read_text(), 'original\n')

    def test_agent_branch_switch_cannot_commit_to_another_branch(self):
        for stage in ('implementation', 'repair'):
            with self.subTest(stage=stage), self.repository() as (root, store, base, task, git):
                release_before = None

                def agent(prompt, wt):
                    nonlocal release_before
                    repair = '"review":' in prompt
                    if repair or stage == 'implementation':
                        release_before = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                        git('switch', '-c', 'release', cwd=wt)
                    (wt / 'allowed.txt').write_text('repair' if repair else 'first')
                    return 'done'

                with patch.object(o, 'run_agy', side_effect=agent), \
                        patch.object(o, 'review', return_value=(False, 'REJECTED', '')):
                    result = o.execute({'tasks': [task]}, 1, int(stage == 'repair'), ['test'])
                self.assertEqual(result, {'A': 'failed'})
                self.assertEqual(git('rev-parse', 'release').stdout.strip(), release_before)
                self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)

    def test_pending_merge_after_integration_requires_recovery(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '-c', 'side')
            git('commit', '--allow-empty', '-m', 'parallel history')
            git('switch', 'main')
            code = ("from pathlib import Path\nimport subprocess\n"
                    "if Path('.agent-runs').exists():\n"
                    " subprocess.run(['git','merge','--no-ff','--no-commit','side'],check=True)")
            result = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(result, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
            self.assertEqual(git('rev-parse', 'MERGE_HEAD').stdout, git('rev-parse', 'side').stdout)

    def test_reviewer_receives_applicable_binary_patch(self):
        with self.repository() as (root, store, base, task, git):
            data = bytes(range(256))
            (root / 'allowed.txt').write_bytes(data)
            git('add', 'allowed.txt')
            git('commit', '-m', 'binary asset')
            original_cmd = o.cmd
            patches = []

            def command(args, **kwargs):
                if args[0] == 'claude':
                    patches.append(json.loads(kwargs['input_text'].split('\n', 1)[1])['patch'])
                return original_cmd(args, **kwargs)

            with patch.object(o, 'cmd', side_effect=command):
                o.review(task, dict(worktree=str(root), report='done'), [sys.executable, '-c', 'pass'])
            git('switch', '--detach', base)
            applied = subprocess.run(['git', 'apply', '-'], input=patches[0].encode(), cwd=root,
                                     capture_output=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual((root / 'allowed.txt').read_bytes(), data)

    def test_repair_commit_outside_scope_is_not_squashed_and_integrated(self):
        with self.repository(agent_commit=2) as (root, store, base, task, git):
            states = o.execute({'tasks': [task]}, 1, 1, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual((root / 'outside.txt').read_text(), 'original\n')

    def test_worker_tests_cannot_approve_a_dirty_worktree(self):
        with self.repository(mutate_tests='worker') as (root, store, base, task, git):
            states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertFalse((store / 'run.lock').exists())

    def test_dirty_worker_cannot_hide_uncertain_subprocess_cleanup(self):
        with self.repository(mutate_tests='worker', test_exit='cleanup') as (root, store, base, task, git):
            states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])

    def test_full_candidate_diff_is_checked_before_review(self):
        with self.repository() as (root, store, base, task, git):
            original_executor = o.run_executor

            # Remove the destination first so Git can rename into the authorized path.
            def forged_executor(*args):
                artifact = original_executor(*args)
                wt = Path(artifact['worktree'])
                (wt / 'allowed.txt').unlink()
                git('mv', 'outside.txt', 'allowed.txt', cwd=wt)
                git('add', '.', cwd=wt)
                git('commit', '-m', 'rename outside scope', cwd=wt)
                git('reset', '--soft', base, cwd=wt)
                git('commit', '-m', 'squashed candidate', cwd=wt)
                artifact['commit'] = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                return artifact

            with patch.object(o, 'run_executor', side_effect=forged_executor):
                states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertIn('fora do escopo', json.loads((store / 'status.json').read_text())['details']['A']['error'])

    def test_tests_cannot_replace_head_even_with_clean_working_tree(self):
        with self.repository() as (root, store, base, task, git):
            command = [sys.executable, '-c', "import subprocess; subprocess.run(['git','commit','--allow-empty','-m','unexpected'],check=True)"]
            with self.assertRaises(RuntimeError):
                o.run_tests(command, root, base)
            self.assertEqual(git('status', '--porcelain').stdout, '')

    def test_integration_tests_that_modify_files_require_manual_recovery(self):
        for exit_code in (0, 1, 'timeout'):
            with self.subTest(exit_code=exit_code), self.repository(
                    mutate_tests='main', test_exit=exit_code) as (root, store, base, task, git):
                states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertEqual(states, {'A': 'failed'})
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                self.assertEqual((root / 'outside.txt').read_text(), 'changed by tests\n')
                with self.assertRaises(RuntimeError):
                    o.reserve_run()

    def test_git_inspection_failure_after_tests_preserves_history_and_reservation(self):
        for inspection in ('rev-parse', 'status'):
            for exit_code in (0, 1):
                with self.subTest(inspection=inspection, exit_code=exit_code), self.repository() as (
                        root, store, base, task, git):
                    marker = store / 'test-mutated-repository'
                    code = (
                        "from pathlib import Path\nimport subprocess, sys\n"
                        "if Path('.agent-runs').exists():\n"
                        " Path('outside.txt').write_text('changed by integration tests\\n')\n"
                        + (" subprocess.run(['git','add','outside.txt'],check=True)\n"
                           " subprocess.run(['git','commit','-m','test commit'],check=True)\n"
                           if inspection == 'rev-parse' else '')
                        + " Path('.agent-runs/test-mutated-repository').write_text('ready')\n"
                        + f" sys.exit({exit_code})\n"
                    )
                    failed = False

                    def unreliable_git(*args, **kwargs):
                        nonlocal failed
                        if args[0] == inspection and marker.exists() and not failed:
                            failed = True
                            raise RuntimeError('Git inspection unavailable')
                        return git(*args, **kwargs)

                    second = dict(task, id='B', files=['another.txt'])
                    with patch.object(o, 'git', side_effect=unreliable_git):
                        states = o.execute({'tasks': [task, second]}, 1, 0,
                                           [sys.executable, '-c', code])
                    self.assertTrue(failed, 'The intended Git read must have failed')
                    self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                    self.assertTrue((store / 'run.lock').exists())
                    self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                    self.assertEqual((root / 'outside.txt').read_text(), 'changed by integration tests\n')
                    self.assertNotIn('Revert', git('log', '--format=%s').stdout)
                    with self.assertRaises(RuntimeError):
                        o.reserve_run()

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

            with patch.object(o, 'git', side_effect=unreliable_git):
                states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
            self.assertTrue((root / 'allowed.txt').exists())
            self.assertNotIn('Revert', git('log', '--format=%s').stdout)

    def test_branch_change_during_review_blocks_integration(self):
        for switch in (('switch', '-c', 'unrelated'), ('switch', '--detach')):
            with self.subTest(switch=switch), self.repository() as (root, store, base, task, git):
                original_review = o.review

                def switched_review(*args):
                    result = original_review(*args)
                    git(*switch)
                    return result

                second = dict(task, id='B', files=['another.txt'])
                with patch.object(o, 'review', side_effect=switched_review):
                    states = o.execute({'tasks': [task, second]}, 1, 0, [sys.executable, '-c', 'pass'])
                self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
                self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])

    def test_tests_cannot_switch_branch_at_the_same_commit(self):
        with self.repository() as (root, store, base, task, git):
            command = [sys.executable, '-c',
                       "import subprocess; subprocess.run(['git','switch','-c','changed-by-tests'],check=True)"]
            with self.assertRaises(RuntimeError):
                o.run_tests(command, root, base)
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertEqual(git('branch', '--show-current').stdout.strip(), 'changed-by-tests')

    def test_integration_branch_change_never_reverts_or_releases_reservation(self):
        for outcome in (0, 1, 'timeout', 'interrupt'):
            with self.subTest(outcome=outcome), self.repository() as (root, store, base, task, git):
                original_cmd = o.cmd
                code = ("from pathlib import Path\nimport subprocess,sys\n"
                        "if Path('.agent-runs').exists():\n"
                        " subprocess.run(['git','switch','-c','changed-by-tests'],check=True)\n"
                        f" sys.exit({outcome if isinstance(outcome, int) else 0})\n")

                def command(args, **kwargs):
                    result = original_cmd(args, **kwargs)
                    if args[0] == sys.executable and Path(kwargs['cwd']) == root:
                        if outcome == 'timeout':
                            raise subprocess.TimeoutExpired(args, 600)
                        if outcome == 'interrupt':
                            raise KeyboardInterrupt
                    return result

                second = dict(task, id='B', files=['another.txt'])
                with patch.object(o, 'cmd', side_effect=command):
                    try:
                        states = o.execute({'tasks': [task, second]}, 1, 0, [sys.executable, '-c', code])
                    except KeyboardInterrupt:
                        self.fail('Branch change escaped without requiring recovery')
                self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
                self.assertNotEqual(git('rev-parse', 'main').stdout.strip(), base)
                self.assertEqual(git('rev-parse', 'HEAD').stdout, git('rev-parse', 'main').stdout)
                self.assertNotIn('Revert', git('log', '--all', '--format=%s').stdout)
                with self.assertRaises(RuntimeError):
                    o.reserve_run()

    def test_unreadable_branch_after_tests_requires_manual_recovery(self):
        with self.repository() as (root, store, base, task, git):
            marker = store / 'tests-finished'
            code = ("from pathlib import Path\n"
                    "if Path('.agent-runs').exists(): Path('.agent-runs/tests-finished').touch()\n")

            def unreliable_git(*args, **kwargs):
                if args[0] == 'symbolic-ref' and marker.exists():
                    return subprocess.CompletedProcess(args, 128, '', 'branch read failed')
                return git(*args, **kwargs)

            with patch.object(o, 'git', side_effect=unreliable_git):
                states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', code])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
            self.assertNotIn('Revert', git('log', '--format=%s').stdout)

    def test_branch_is_rechecked_before_rollback(self):
        with self.repository() as (root, store, base, task, git):
            original_tests = o.run_tests

            def switched_tests(command, wt, *args, **kwargs):
                result = original_tests(command, wt, *args, **kwargs)
                if wt == root:
                    git('switch', '-c', 'changed-before-revert')
                    return subprocess.CompletedProcess(command, 1, '', 'failed')
                return result

            with patch.object(o, 'run_tests', side_effect=switched_tests):
                states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed'})
            self.assertTrue((store / 'run.lock').exists())
            self.assertTrue(json.loads((store / 'status.json').read_text())['recovery_required'])
            self.assertNotIn('Revert', git('log', '--all', '--format=%s').stdout)

    def test_unchanged_detached_head_can_integrate(self):
        with self.repository() as (root, store, base, task, git):
            git('switch', '--detach')
            states = o.execute({'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(git('branch', '--show-current').stdout.strip(), '')
            self.assertEqual(git('rev-parse', 'main').stdout.strip(), base)
            self.assertTrue((root / 'allowed.txt').exists())
            self.assertFalse((store / 'run.lock').exists())
