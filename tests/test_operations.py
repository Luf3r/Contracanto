"""Tempos no status, revisão em paralelo, veredito JSON e operações do operador."""

import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, cli, maintenance, runner, server
from contracanto import process as proc
from contracanto import state as st

ROOT = Path(__file__).resolve().parent.parent


def finished_process_pid():
    """PID de um processo que já terminou."""
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    return child.pid


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
            verdicts = iter([(False, 'REJECTED\n- falta algo', ''), (True, 'APPROVED', '')])
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

            def review(task, *args):
                # Uma revisão serial nunca teria duas chamadas simultâneas aqui.
                both_reviewing.wait()
                return original_review(task, *args)

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

            def review(task, *args):
                if task['id'] == 'A':
                    raise support.errors.RecoveryRequired('test process still alive')
                return True, 'APPROVED', ''

            with patch.object(agents, 'run_agy', side_effect=agent), patch.object(agents, 'review', side_effect=review):
                states = runner.execute(self.ws, {'tasks': tasks}, 2, 0, [sys.executable, '-c', 'pass'])
            self.assertEqual(states, {'A': 'failed', 'B': 'blocked'})
            status = support.read_status(self.ws)
            self.assertTrue(status['recovery_required'])
            self.assertIn('still alive', status['details']['A']['error'])
            self.assertEqual(git('rev-parse', 'HEAD').stdout.strip(), base)
            self.assertTrue((store / 'run.lock').exists())


class VerdictTests(unittest.TestCase):
    def test_json_verdict_variants(self):
        cases = [
            ('{"verdict": "APPROVED", "summary": "ok", "problems": []}', True),
            ('{"verdict": "approved"}', True),
            ('Segue a análise:\n```json\n{"verdict": "APPROVED", "summary": "ok"}\n```', True),
            ('{"verdict": "REJECTED", "problems": ["falta teste"]}', False),
            ('{"verdict": "TALVEZ"}', False),
            ('{"outro": 1} e depois {"verdict": "APPROVED"}', True),
            ('APPROVED\nTudo certo', True),
            ('REJECTED\nFalta teste', False),
            ('Aprovado, parece bom', False),
            ('', False),
        ]
        for response, approved in cases:
            with self.subTest(response=response):
                self.assertIs(agents.parse_verdict(response).approved, approved)

    def test_rejection_text_lists_problems_for_the_repair(self):
        verdict = agents.parse_verdict('{"verdict": "REJECTED", "summary": "Incompleto", "problems": ["a", "b"]}')
        self.assertEqual(verdict.text, 'REJECTED\nIncompleto\n- a\n- b')

    def test_unrecognized_answer_is_rejected_with_the_raw_answer(self):
        verdict = agents.parse_verdict('Parece ótimo!')
        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.text.startswith('REJECTED\n'))
        self.assertIn('Parece ótimo!', verdict.text)

    def test_review_requires_passing_tests_even_with_json_approval(self):
        artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')

        def command(args, **kwargs):
            if args[0] == 'claude':
                return subprocess.CompletedProcess(args, 0, json.dumps({'result': '{"verdict": "APPROVED"}'}), '')
            return subprocess.CompletedProcess(args, 0, b'' if '--binary' in args else '', '')

        failing = subprocess.CompletedProcess([], 3, 'out', 'err')
        with (
            patch.object(proc, 'run', side_effect=command),
            patch.object(support.gitops, 'run_tests', return_value=failing),
            patch.object(support.gitops, 'head', return_value='commit'),
        ):
            approved, feedback, _ = agents.review(artifact.task, artifact, ['test'])
        self.assertFalse(approved)
        self.assertIn('código 3', feedback)


class ConfigTests(unittest.TestCase):
    def read_config(self, **env):
        code = (
            'import json; from contracanto import config as c; '
            'print(json.dumps([c.TEST_TIMEOUT, c.AGENT_TIMEOUT, c.AGENT_PRINT_TIMEOUT, c.REVIEW_MODEL]))'
        )
        environment = {k: v for k, v in os.environ.items() if not k.startswith('CONTRACANTO_')}
        environment.update(env, PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True)
        return result

    def test_defaults(self):
        result = self.read_config()
        self.assertEqual(json.loads(result.stdout), [600, 1600, '25m', 'sonnet'])

    def test_environment_overrides(self):
        result = self.read_config(
            CONTRACANTO_TEST_TIMEOUT='1200', CONTRACANTO_AGENT_TIMEOUT='3700', CONTRACANTO_REVIEW_MODEL='opus'
        )
        self.assertEqual(json.loads(result.stdout), [1200, 3700, '60m', 'opus'])

    def test_invalid_value_names_the_variable(self):
        for value in ('dez', '0', '-5'):
            with self.subTest(value=value):
                result = self.read_config(CONTRACANTO_TEST_TIMEOUT=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('CONTRACANTO_TEST_TIMEOUT', result.stderr)


class ProcessTests(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(proc.pid_alive(os.getpid()))
        self.assertFalse(proc.pid_alive(finished_process_pid()))


class UnlockTests(unittest.TestCase):
    def reserved_repository(self, worker_pid):
        directory = self.enterContext(support.temp_dir())
        git = support.init_repository(directory)
        git('add', '.')
        git('commit', '-m', 'baseline')
        ws = st.Workspace.for_root(directory)
        run_id = st.reservation_id(st.reserve_run(ws))
        ws.publish_run(run_id)
        ws.claim(run_id).mkdir()
        ws.worker_pid(run_id).write_text(str(worker_pid))
        run = st.RunState(ws.status(run_id), run_id)
        run.state = 'running'
        run.recovery_required = True
        run.save()
        return ws, run_id, git

    def test_refuses_while_worker_is_alive(self):
        ws, run_id, git = self.reserved_repository(os.getpid())
        message = maintenance.unlock(ws)
        self.assertIn('Recusado', message)
        self.assertIn(str(os.getpid()), message)
        self.assertTrue(ws.lock.exists())

    def test_refuses_with_dirty_repository_or_pending_operation(self):
        ws, run_id, git = self.reserved_repository(finished_process_pid())
        (ws.root / 'dirty.txt').write_text('x')
        self.assertIn('alterado', maintenance.unlock(ws))
        (ws.root / 'dirty.txt').unlink()
        (Path(git('rev-parse', '--absolute-git-dir').stdout.strip()) / 'MERGE_HEAD').write_text('x')
        self.assertIn('MERGE_HEAD', maintenance.unlock(ws))
        self.assertTrue(ws.lock.exists())

    def test_releases_dead_worker_and_records_it(self):
        ws, run_id, git = self.reserved_repository(finished_process_pid())
        message = maintenance.unlock(ws)
        self.assertIn('removida', message)
        self.assertFalse(ws.lock.exists())
        status = json.loads(ws.status(run_id).read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['recovery_required'])
        self.assertTrue(status['recovery_required_at_unlock'])
        self.assertIn('unlocked_at', status)
        st.release_run(ws, st.reserve_run(ws))
        self.assertEqual(maintenance.unlock(ws), 'Nenhuma reserva ativa.')

    def test_cli_exit_code_reflects_refusal(self):
        ws, run_id, git = self.reserved_repository(os.getpid())
        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as exit:
            cli.main(['--unlock'], ws)
        self.assertEqual(exit.exception.code, 1)
        self.assertIn('Recusado', output.getvalue())


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


class EntryPointTests(unittest.TestCase):
    def test_cli_status_prints_json_report(self):
        with support.temp_workspace() as ws:
            run_id = support.publish_previous_run(ws, {'state': 'completed'})
            with redirect_stdout(StringIO()) as output:
                cli.main(['--status'], ws)
            report = json.loads(output.getvalue())
            self.assertEqual(report['run']['run_id'], run_id)
            self.assertEqual(report['reservation_state'], 'none')

    def test_worker_command_runs_outside_the_installation(self):
        with support.temp_dir() as directory:
            ws = st.Workspace.for_root(directory)
            argv = server.worker_argv(ws, 'a' * 64, 'token', 1, 0, True, ['python'])
            self.assertEqual(argv[1:3], ['-m', 'contracanto'])
            self.assertIn('--keep-worktrees', argv)
            self.assertEqual(argv[argv.index('--repo') + 1], str(ws.root))
            # O mesmo interpretador e ambiente do worker, a partir de outro diretório.
            result = subprocess.run(
                [sys.executable, '-m', 'contracanto', '--status', '--repo', str(directory)],
                cwd=directory,
                env=server.worker_env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['reservation_state'], 'none')
