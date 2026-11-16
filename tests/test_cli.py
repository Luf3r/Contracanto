"""Linha de comando: erros de uso, mensagens de erro, versão e pontos de entrada."""

import json
import logging
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

import support

from contracanto import cli, runner, server
from contracanto import state as st


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


class VersionTests(unittest.TestCase):
    def test_cli_reports_the_package_version(self):
        import contracanto

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as exit:
            cli.main(['--version'])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f'contracanto {contracanto.__version__}')


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
