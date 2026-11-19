"""The Codex CLI as executor: command line, output parsing, tokens and the choice by configuration."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import support

from contracanto import agents, config, errors, runner, usage
from contracanto import process as proc
from contracanto import state as st

FIXTURE = (Path(__file__).parent / 'fixtures' / 'agents' / 'codex-exec.jsonl').read_text(encoding='utf-8')
PASS = [sys.executable, '-c', 'pass']


def events(*items):
    return ''.join(json.dumps(item) + '\n' for item in items)


def message(text):
    return {'type': 'item.completed', 'item': {'id': 'x', 'type': 'agent_message', 'text': text}}


def completed(**counts):
    return {'type': 'turn.completed', 'usage': counts}


class ParsingTests(unittest.TestCase):
    def test_report_is_the_last_agent_message(self):
        self.assertIn('returns `a + b`', agents.parse_codex_output(FIXTURE))
        self.assertEqual(agents.parse_codex_output(events(message('a'), message('b'), completed())), 'b')
        self.assertEqual(agents.parse_codex_output(events(completed())), '')

    def test_failures_are_executor_errors(self):
        cases = {
            'turn failed': (events({'type': 'turn.failed', 'error': {'message': 'quota'}}), 'quota'),
            'no completed turn': (events({'type': 'error', 'message': 'stream closed'}), 'stream closed'),
            'nothing at all': ('', 'sem turn.completed'),
            'not json': ('nope\n', 'não é JSON'),
        }
        for name, (stdout, text) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(RuntimeError, text) as caught:
                agents.parse_codex_output(stdout)
            self.assertEqual(errors.failure_category(caught.exception), 'executor_error')

    def test_error_event_before_a_completed_turn_is_a_warning(self):
        stdout = events({'type': 'error', 'message': 'reconnecting'}, message('feito'), completed())
        self.assertEqual(agents.parse_codex_output(stdout), 'feito')

    def test_tokens_split_the_cache_out_of_the_input(self):
        self.assertEqual(
            usage.from_codex(FIXTURE, 'gpt-6-luna'),
            usage.Usage(input=64286 - 55040, cached_input=55040, output=314, reasoning=8, model='gpt-6-luna'),
        )

    def test_tokens_of_every_turn_are_summed_in_one_call(self):
        stdout = events(
            completed(input_tokens=100, cached_input_tokens=40, output_tokens=10),
            completed(input_tokens=50, cached_input_tokens=50, cache_write_input_tokens=5, output_tokens=2),
        )
        self.assertEqual(
            usage.from_codex(stdout),
            usage.Usage(input=60, cached_input=90, cache_write=5, output=12, calls=1),
        )

    def test_output_without_counts_yields_nothing(self):
        for stdout in ('', events(message('x')), 'nope', events(completed(input_tokens=-1))):
            with self.subTest(stdout=stdout):
                self.assertIsNone(usage.from_codex(stdout))


class CommandTests(unittest.TestCase):
    def command(self, model='', platform='win32'):
        with (
            patch.object(config, 'CODEX_PATH', 'C:/codex/codex.exe'),
            patch.object(config, 'AGENT_MODEL', model),
            patch.object(agents.sys, 'platform', platform),
        ):
            return agents.codex_command('wt')

    def test_command_ignores_user_config_and_keeps_the_sandbox(self):
        args = self.command('gpt-6-luna')
        self.assertEqual(args[:2], ['C:/codex/codex.exe', 'exec'])
        for flag in ('--json', '--ephemeral', '--ignore-user-config'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'workspace-write')
        self.assertIn('approval_policy="never"', args)
        self.assertIn('windows.sandbox="elevated"', args)
        self.assertEqual(args[args.index('--model') + 1], 'gpt-6-luna')
        self.assertEqual(args[-3:], ['-C', 'wt', '-'])

    def test_without_model_or_outside_windows_the_flags_are_left_out(self):
        args = self.command(platform='linux')
        self.assertNotIn('--model', args)
        self.assertNotIn('windows.sandbox="elevated"', args)


class CallTests(unittest.TestCase):
    def call(self, stdout, returncode=0):
        seen: dict[str, Any] = {}

        def command(args, **kwargs):
            seen.update(args=args, **kwargs)
            return subprocess.CompletedProcess(args, returncode, stdout, 'aviso do codex')

        with support.temp_dir() as directory:
            run = st.RunState(Path(directory) / 'status.json', 'a' * 64)
            with (
                patch.object(proc, 'run', side_effect=command),
                patch.object(config, 'AGENT_MODEL', 'gpt-6-luna'),
                run.phase('A', 'implementation'),
            ):
                report: str | RuntimeError
                try:
                    report = agents.run_codex('o pedido', 'wt')
                except RuntimeError as error:
                    report = error
            return report, seen, run.snapshot()

    def test_prompt_goes_by_stdin_under_the_watchdog_and_tokens_are_recorded(self):
        report, seen, snapshot = self.call(FIXTURE)
        self.assertIn('returns `a + b`', report)
        self.assertEqual(seen['input_data'], 'o pedido')
        self.assertEqual(seen['cwd'], 'wt')
        self.assertFalse(seen['check'])
        self.assertIsInstance(seen['idle'], proc.IdleWatch)
        executor = snapshot['usage']['executor']
        self.assertEqual(
            (executor['input'], executor['cached_input'], executor['models']), (9246, 55040, ['gpt-6-luna'])
        )

    def test_failed_exit_reports_the_turn_error_and_still_counts_tokens(self):
        stdout = events(
            completed(input_tokens=7, output_tokens=1), {'type': 'turn.failed', 'error': {'message': 'limite'}}
        )
        report, _, snapshot = self.call(stdout, returncode=1)
        self.assertIsInstance(report, RuntimeError)
        self.assertIn('codex terminou com código 1: limite', str(report))
        self.assertEqual(errors.failure_category(report), 'executor_error')
        self.assertEqual(snapshot['usage']['executor']['input'], 7)


class ChoiceTests(unittest.TestCase):
    def test_configured_executor_is_called(self):
        with (
            patch.object(agents, 'run_agy', return_value='agy') as agy,
            patch.object(agents, 'run_codex', return_value='codex') as codex,
        ):
            for name in ('agy', 'codex'):
                with self.subTest(name), patch.object(config, 'EXECUTOR', name):
                    self.assertEqual(agents.run_agent('p', 'wt'), name)
        self.assertEqual((agy.call_count, codex.call_count), (1, 1))

    def test_preflight_looks_for_the_configured_executor(self):
        with patch.object(config, 'EXECUTOR', 'codex'), patch.object(config, 'CODEX_PATH', 'C:/x/codex.exe'):
            with (
                patch.object(runner.shutil, 'which', side_effect=lambda name: None if 'codex' in name else name),
                self.assertRaisesRegex(RuntimeError, 'Não encontrado no PATH: C:/x/codex.exe'),
            ):
                runner.preflight(Path('.'), PASS, None)

    def test_codex_skips_the_agy_model_list(self):
        with (
            patch.object(config, 'EXECUTOR', 'codex'),
            patch.object(config, 'AGENT_MODEL', 'gpt-6-luna'),
            patch.object(proc, 'run') as run,
        ):
            agents.check_model()
        run.assert_not_called()


class RunTests(support.RepositoryTestCase):
    def test_task_runs_end_to_end_with_codex(self):
        calls = []

        def agent(prompt, wt, stop=None):
            calls.append(prompt)
            (wt / 'allowed.txt').write_text('ok')
            return 'done'

        with self.repository() as (root, store, base, task, git):
            with (
                patch.object(config, 'EXECUTOR', 'codex'),
                patch.object(agents, 'run_codex', side_effect=agent),
                patch.object(agents, 'run_agy', side_effect=AssertionError('agy não deve ser chamado')),
            ):
                states = runner.execute(self.ws, {'tasks': [task]}, 1, 0, PASS)
        self.assertEqual(states, {'A': 'approved'})
        self.assertEqual(len(calls), 1)
        self.assertIn('AGENTS.md', calls[0])


if __name__ == '__main__':
    unittest.main()
