"""Diagnóstico e operação: worker morto, conflitos, erros da CLI, comandos órfãos,
nova tentativa de implementação, preparação do worktree, poda forçada e limite do patch."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, cli, config, gitops, maintenance, runner, server
from contracanto import process as proc
from contracanto import state as st

ROOT = Path(__file__).resolve().parent.parent
PASS = [sys.executable, '-c', 'pass']


class WorkerLivenessTests(unittest.TestCase):
    def test_status_tells_a_live_worker_from_a_dead_one(self):
        ws, _, _ = support.reserved_repository(self, os.getpid())
        self.assertTrue(maintenance.status_report(ws)['reservation']['worker_alive'])
        ws, _, _ = support.reserved_repository(self, support.finished_process_pid())
        report = maintenance.status_report(ws)
        self.assertFalse(report['reservation']['worker_alive'])
        # O estado gravado continua `running`: só a reserva revela que ninguém o atualiza mais.
        self.assertEqual(report['run']['state'], 'running')

    def test_before_the_claim_the_reservation_creator_counts(self):
        with support.temp_workspace() as ws:
            st.reserve_run(ws)
            self.assertTrue(maintenance.status_report(ws)['reservation']['worker_alive'])


class OrphanCommandTests(unittest.TestCase):
    def test_interruptible_command_is_registered_only_while_it_runs(self):
        with support.temp_dir() as directory:
            commands = directory / 'commands'
            stop = st.StopSignal(directory / 'stop', commands)
            code = 'import os, sys; print(",".join(os.listdir(sys.argv[1])))'
            result = proc.run([sys.executable, '-c', code, str(commands)], cwd=directory, stop=stop)
            self.assertTrue(result.stdout.strip().isdecimal(), result.stdout)
            self.assertEqual(list(commands.iterdir()), [])

    def test_unlock_refuses_while_a_command_of_the_dead_worker_survives(self):
        ws, run_id, _ = support.reserved_repository(self, support.finished_process_pid())
        # O filho avisa quando está pronto e termina sozinho ao fim da entrada. Matar o
        # lançador do venv no Windows deixaria o Python real suspenso, segurando a saída.
        code = "import sys; print('ready', flush=True); sys.stdin.read()"
        child = subprocess.Popen(
            [sys.executable, '-c', code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            # No POSIX o comando registrado é líder do próprio grupo, como os do worker.
            start_new_session=os.name == 'posix',
        )
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            ws.commands(run_id).mkdir()
            (ws.commands(run_id) / str(child.pid)).touch()
            message = maintenance.unlock(ws)
            self.assertIn('Recusado', message)
            self.assertIn(str(child.pid), message)
            self.assertTrue(ws.lock.exists())
        finally:
            child.communicate(timeout=30)
        self.assertIn('removida', maintenance.unlock(ws))


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


class CliErrorTests(unittest.TestCase):
    def run_cli(self, argv, **patches):
        with (
            patch.object(sys, 'argv', ['contracanto', *argv]),
            patch.object(logging, 'basicConfig'),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit,
        ):
            cli.run()
        return exit.exception.code, stderr.getvalue()

    def test_missing_plan_is_a_usage_error(self):
        with support.temp_dir() as directory:
            code, stderr = self.run_cli([str(directory / 'missing.json'), '--repo', str(directory), '--test', 'x'])
        self.assertEqual(code, 2)
        self.assertIn('Não foi possível ler o plano', stderr)

    def test_runtime_error_becomes_a_one_line_message(self):
        with support.temp_dir() as directory:
            (directory / 'plan.json').write_text('{}')
            argv = [str(directory / 'plan.json'), '--repo', str(directory), '--test', 'x']
            with patch.object(runner, 'execute', side_effect=RuntimeError('Repositório precisa estar limpo')):
                code, _ = self.run_cli(argv)
        self.assertEqual(code, 'Erro: Repositório precisa estar limpo')

    def test_mcp_worker_keeps_the_traceback_for_the_log(self):
        with support.temp_dir() as directory:
            argv = ['plan.json', '--repo', str(directory), '--reservation', 'token', '--test', 'x']
            with (
                patch.object(sys, 'argv', ['contracanto', *argv]),
                patch.object(logging, 'basicConfig'),
                patch.object(runner, 'execute', side_effect=RuntimeError('falha')),
                self.assertRaises(RuntimeError),
            ):
                cli.run()

    def test_force_requires_prune_and_setup_must_be_a_json_list(self):
        with support.temp_dir() as directory:
            code, stderr = self.run_cli(['--status', '--force', '--repo', str(directory)])
            self.assertEqual(code, 2)
            self.assertIn('--prune-runs', stderr)
            (directory / 'plan.json').write_text('{}')
            argv = [str(directory / 'plan.json'), '--repo', str(directory), '--setup-json', 'npm ci', '--test', 'x']
            code, stderr = self.run_cli(argv)
            self.assertEqual(code, 2)
            self.assertIn('--setup-json', stderr)


class ConfigLimitTests(unittest.TestCase):
    def read_config(self, **env):
        code = 'import json; from contracanto import config as c; print(json.dumps([c.AGENT_RETRIES, c.REVIEW_PATCH_LIMIT]))'
        environment = {k: v for k, v in os.environ.items() if not k.startswith('CONTRACANTO_')}
        environment.update(env, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True)

    def test_counts_default_and_override(self):
        self.assertEqual(json.loads(self.read_config().stdout), [1, 400_000])
        result = self.read_config(CONTRACANTO_AGENT_RETRIES='0', CONTRACANTO_REVIEW_PATCH_LIMIT='1000')
        self.assertEqual(json.loads(result.stdout), [0, 1000])

    def test_out_of_range_values_name_the_variable(self):
        cases = {
            'CONTRACANTO_AGENT_RETRIES': '9',
            'CONTRACANTO_REVIEW_PATCH_LIMIT': 'muito',
            # Abaixo disso o limite interno do agy passaria do próprio tempo total.
            'CONTRACANTO_AGENT_TIMEOUT': '30',
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                result = self.read_config(**{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)

    def test_command_timeout_is_no_longer_advertised(self):
        self.assertNotIn('COMMAND_TIMEOUT', config.TUNABLE)


class ImplementationRetryTests(support.RepositoryTestCase):
    def flaky_agent(self, failures):
        calls = []

        def agent(prompt, wt, stop=None):
            calls.append(wt)
            if len(calls) <= failures:
                raise RuntimeError('agy exited 1: falha passageira')
            (wt / 'allowed.txt').write_text('ok')
            return 'done'

        return agent, calls

    def test_transient_failure_is_retried_in_the_same_worktree(self):
        with self.repository() as (root, store, base, task, git):
            agent, calls = self.flaky_agent(1)
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], calls[1])

    def test_failure_beyond_the_limit_fails_the_task(self):
        with self.repository() as (root, store, base, task, git):
            agent, calls = self.flaky_agent(5)
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(len(calls), config.AGENT_RETRIES + 1)
            self.assertIn('falha passageira', support.read_status(self.ws)['details']['A']['error'])

    def test_uncertain_state_is_never_retried(self):
        with self.repository() as (root, store, base, task, git):
            calls = []

            def agent(prompt, wt, stop=None):
                calls.append(wt)
                raise support.errors.RecoveryRequired('processo ainda ativo')

            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'failed'})
            self.assertEqual(len(calls), 1)
            self.assertTrue(support.read_status(self.ws)['recovery_required'])


class SetupCommandTests(support.RepositoryTestCase):
    def test_setup_runs_in_the_worktree_before_the_implementation(self):
        with self.repository() as (root, store, base, task, git):
            seen = []
            original = agents.run_agy

            def agent(prompt, wt, stop=None):
                seen.append((wt / '.agent-runs' / 'dependency').read_text())
                return original(prompt, wt, stop)

            code = "import os; os.makedirs('.agent-runs'); open('.agent-runs/dependency', 'w').write('installed')"
            with patch.object(agents, 'run_agy', side_effect=agent):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, setup=[sys.executable, '-c', code])
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(seen, ['installed'])

    def test_setup_that_fails_or_dirties_the_worktree_fails_the_task(self):
        cases = {
            'import sys; sys.exit(4)': 'código 4',
            "open('stray.txt', 'w').write('x')": 'stray.txt',
        }
        for code, expected in cases.items():
            with self.subTest(code=code), self.repository() as (root, store, base, task, git):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS, setup=[sys.executable, '-c', code])
                self.assertEqual(states, {'A': 'failed'})
                self.assertIn(expected, support.read_status(self.ws)['details']['A']['error'])

    def test_mcp_forwards_the_setup_command(self):
        plan = json.dumps({'tasks': [support.task('A')]})
        _, test, setup = server._parse_submission(plan, 1, 0, '["pytest"]', '["npm", "ci"]')
        self.assertEqual((test, setup), (['pytest'], ['npm', 'ci']))
        self.assertIsNone(server._parse_submission(plan, 1, 0, '["pytest"]')[2])
        with self.assertRaisesRegex(ValueError, 'setup_command_json'):
            server._parse_submission(plan, 1, 0, '["pytest"]', '"npm ci"')
        with support.temp_dir() as directory:
            ws = st.Workspace.for_root(directory)
            argv = server.worker_argv(ws, 'a' * 64, 'token', 1, 0, False, ['pytest'], setup)
            self.assertEqual(json.loads(argv[argv.index('--setup-json') + 1]), ['npm', 'ci'])
            self.assertLess(argv.index('--setup-json'), argv.index('--test'))


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


class PatchLimitTests(unittest.TestCase):
    def test_oversized_patch_fails_before_calling_the_reviewer(self):
        artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')
        called = []

        def command(args, **kwargs):
            called.append(args[0])
            return subprocess.CompletedProcess(args, 0, b'x' * 50 if '--binary' in args else '', '')

        passing = subprocess.CompletedProcess([], 0, '', '')
        with (
            patch.object(proc, 'run', side_effect=command),
            patch.object(gitops, 'run_tests', return_value=passing),
            patch.object(gitops, 'head', return_value='commit'),
            patch.object(config, 'REVIEW_PATCH_LIMIT', 10),
            self.assertRaisesRegex(RuntimeError, 'REVIEW_PATCH_LIMIT'),
        ):
            agents.review(artifact, ['test'])
        self.assertNotIn('claude', called)


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


class WaitForRunTests(unittest.TestCase):
    def wait(self, ws, timeout=5):
        with patch.object(server, 'WORKSPACE', ws), patch.object(server, 'WAIT_POLL_INTERVAL', 0.05):
            return json.loads(asyncio.run(server.wait_for_run(timeout)))

    def test_returns_at_once_without_a_run_or_with_a_dead_worker(self):
        with support.temp_workspace() as ws:
            summary = self.wait(ws)
            self.assertEqual((summary['reservation_state'], summary['timed_out']), ('none', False))
        ws, run_id, _ = support.reserved_repository(self, support.finished_process_pid())
        summary = self.wait(ws)
        self.assertEqual(summary['run_id'], run_id)
        self.assertEqual((summary['state'], summary['worker_alive'], summary['timed_out']), ('running', False, False))

    def test_times_out_while_the_worker_is_still_running(self):
        ws, _, _ = support.reserved_repository(self, os.getpid())
        started = time.monotonic()
        summary = self.wait(ws, timeout=1)
        self.assertTrue(summary['timed_out'])
        self.assertTrue(summary['worker_alive'])
        self.assertGreaterEqual(time.monotonic() - started, 1)

    def test_returns_when_the_run_finishes_during_the_wait(self):
        ws, run_id, _ = support.reserved_repository(self, os.getpid())

        def finish():
            time.sleep(0.3)
            run = st.RunState(ws.status(run_id), run_id)
            run.statuses = {'A': 'failed'}
            run.details = {'A': {'error': 'x' * 2000, 'review': 'parecer longo'}}
            run.finish('failed')
            run.save()

        worker = threading.Thread(target=finish)
        worker.start()
        summary = self.wait(ws, timeout=20)
        worker.join()
        self.assertEqual((summary['state'], summary['timed_out']), ('failed', False))
        self.assertEqual(summary['statuses'], {'A': 'failed'})
        # O resumo corta os erros e deixa pareceres e log para get_run_status.
        self.assertEqual(len(summary['task_errors']['A']), maintenance.SUMMARY_ERROR_TAIL)
        self.assertNotIn('parecer longo', json.dumps(summary))


class CommandPreflightTests(support.RepositoryTestCase):
    def test_missing_test_or_setup_command_fails_before_any_agent_call(self):
        cases = {
            'Comando de testes': dict(tests=['comando-que-nao-existe']),
            'Comando de preparação': dict(setup=['x-nao-existe']),
        }
        for label, options in cases.items():
            with self.subTest(label=label), self.repository() as (root, store, base, task, git):
                called: list[str] = []
                arguments = dict(tests=PASS, setup=None) | options
                with (
                    patch.object(
                        runner.shutil, 'which', side_effect=lambda name: name if name in ('agy', 'claude') else None
                    ),
                    patch.object(agents, 'run_agy', side_effect=called.append),
                    self.assertRaisesRegex(RuntimeError, label + ' não encontrado'),
                ):
                    runner.execute(self.ws, {'tasks': [task]}, 1, 0, arguments['tests'], setup=arguments['setup'])
                self.assertEqual(called, [])
                self.assertFalse((store / 'run.lock').exists())

    def test_command_inside_the_repository_is_accepted(self):
        with self.repository() as (root, store, base, task, git):
            (root / 'check.cmd').write_text('')
            with patch.object(runner.shutil, 'which', return_value=None):
                self.assertEqual(runner.resolve_command(['check.cmd', '-x'], root, 'Comando'), ['check.cmd', '-x'])

    @unittest.skipUnless(os.name == 'nt', 'Só o Windows precisa do caminho completo de um .cmd')
    def test_windows_script_found_in_path_is_replaced_by_its_full_path(self):
        with support.temp_dir() as directory:
            script = directory / 'ferramenta.cmd'
            script.write_text('@exit /b 0\n')
            with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
                resolved = runner.resolve_command(['ferramenta', 'ci'], directory, 'Comando')
            self.assertEqual(Path(resolved[0]).resolve(), script.resolve())
            self.assertEqual(resolved[1:], ['ci'])
            self.assertEqual(proc.run(resolved, cwd=directory).returncode, 0)


class RepairRetryTests(support.RepositoryTestCase):
    def test_failed_repair_call_is_retried(self):
        with self.repository() as (root, store, base, task, git):
            calls = []

            def agent(prompt, wt, stop=None):
                calls.append(prompt.split('\n', 1)[0][:9])
                if len(calls) == 2:
                    raise RuntimeError('agy exited 1: falha passageira')
                (wt / 'allowed.txt').write_text(str(len(calls)))
                return 'done'

            verdicts = iter([(False, 'REJECTED\n- falta algo'), (True, 'APPROVED')])
            with (
                patch.object(agents, 'run_agy', side_effect=agent),
                patch.object(agents, 'review', side_effect=lambda *args: next(verdicts)),
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 1, PASS)
            self.assertEqual(states, {'A': 'approved'})
            # Implementação, correção que falhou e correção repetida.
            self.assertEqual(len(calls), 3)
            self.assertEqual((root / 'allowed.txt').read_text(), '3')


class CommitMessageTests(unittest.TestCase):
    def test_subject_carries_the_description_within_the_limit(self):
        with support.temp_workspace() as ws:
            ctx = support.context(ws, 'a' * 64)
            long_task = support.task('T1')
            long_task['description'] = 'Criar   o endpoint\nde saúde ' + 'x' * 200
            subject, _, body = agents.commit_message(ctx, long_task).partition('\n\n')
            self.assertTrue(subject.startswith('T1: Criar o endpoint de saúde x'))
            self.assertEqual(len(subject), agents.COMMIT_SUBJECT_LENGTH)
            self.assertTrue(subject.endswith('…'))
            self.assertIn('aaaaaaaaaaaa', body)


class VersionTests(unittest.TestCase):
    def test_cli_reports_the_package_version(self):
        import contracanto

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as exit:
            cli.main(['--version'])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f'contracanto {contracanto.__version__}')


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
