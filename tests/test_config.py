"""Ajustes lidos das variáveis de ambiente CONTRACANTO_*."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import config, runner, server

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def read_config(self, **env):
        code = (
            'import json; from contracanto import config as c; '
            'print(json.dumps([c.TEST_TIMEOUT, c.AGENT_TIMEOUT, c.AGENT_PRINT_TIMEOUT, c.REVIEW_MODEL, c.AGENT_MODEL]))'
        )
        environment = {k: v for k, v in os.environ.items() if not k.startswith('CONTRACANTO_')}
        environment.update(env, PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True)
        return result

    def test_defaults(self):
        result = self.read_config()
        self.assertEqual(json.loads(result.stdout), [600, 1600, '25m', 'sonnet', ''])

    def test_environment_overrides(self):
        result = self.read_config(
            CONTRACANTO_TEST_TIMEOUT='1200',
            CONTRACANTO_AGENT_TIMEOUT='3700',
            CONTRACANTO_REVIEW_MODEL='opus',
            CONTRACANTO_AGENT_MODEL='gemini-4-hydrogen-high',
        )
        self.assertEqual(json.loads(result.stdout), [1200, 3700, '60m', 'opus', 'gemini-4-hydrogen-high'])

    def test_invalid_value_names_the_variable(self):
        for value in ('dez', '0', '-5'):
            with self.subTest(value=value):
                result = self.read_config(CONTRACANTO_TEST_TIMEOUT=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('CONTRACANTO_TEST_TIMEOUT', result.stderr)


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


class PinnedCommandTests(unittest.TestCase):
    PLAN = json.dumps({'tasks': [support.task('A')]})

    def test_without_pins_any_command_is_accepted(self):
        config.check_pinned(['qualquer'], ['coisa'])

    def test_pinned_test_command_refuses_a_different_one(self):
        with patch.object(config, 'TEST_COMMAND', ['python', '-m', 'pytest']):
            config.check_pinned(['python', '-m', 'pytest'], None)
            for supplied in (['python', '-m', 'pytest', '-x'], ['rm'], None):
                with (
                    self.subTest(supplied=supplied),
                    self.assertRaisesRegex(ValueError, r'\["python", "-m", "pytest"\]'),
                ):
                    config.check_pinned(supplied, None)

    def test_pinned_setup_allows_itself_or_nothing_and_empty_forbids(self):
        with patch.object(config, 'SETUP_COMMAND', ['npm', 'ci']):
            config.check_pinned(['t'], None)
            config.check_pinned(['t'], ['npm', 'ci'])
            with self.assertRaisesRegex(ValueError, 'SETUP_COMMAND'):
                config.check_pinned(['t'], ['npm', 'install'])
        with patch.object(config, 'SETUP_COMMAND', []):
            config.check_pinned(['t'], None)
            with self.assertRaisesRegex(ValueError, 'não permite'):
                config.check_pinned(['t'], ['npm', 'ci'])

    def test_pinned_setup_is_used_when_none_is_supplied(self):
        self.assertIsNone(config.pinned_setup(None))
        with patch.object(config, 'SETUP_COMMAND', ['npm', 'ci']):
            self.assertEqual(config.pinned_setup(None), ['npm', 'ci'])
            self.assertEqual(config.pinned_setup(['npm', 'ci']), ['npm', 'ci'])
        with patch.object(config, 'SETUP_COMMAND', []):
            self.assertIsNone(config.pinned_setup(None))

    def test_mcp_submission_is_refused_without_reserving(self):
        with (
            support.temp_workspace(server) as ws,
            patch.object(config, 'TEST_COMMAND', ['pytest']),
            patch.object(server.process, 'launch_detached', return_value=(1, True)) as launch,
            # Só o comando fixado está em jogo aqui; pytest pode nem estar instalado.
            patch.object(server, 'preflight'),
        ):
            result = server.submit_plan(self.PLAN, test_command_json='["curl", "x"]')
            self.assertTrue(result.startswith('Erro: '), result)
            self.assertIn('["pytest"]', result)
            launch.assert_not_called()
            self.assertFalse(ws.lock.exists())
            launch.return_value = (5, True)
            self.assertIn('PID 5', server.submit_plan(self.PLAN, test_command_json='["pytest"]'))

    def test_worker_refuses_before_resolving_the_executable(self):
        with (
            support.temp_workspace() as ws,
            patch.object(config, 'TEST_COMMAND', ['pytest']),
            patch.object(runner, 'resolve_command') as resolve,
            patch.object(runner.shutil, 'which', return_value='tool'),
            self.assertRaisesRegex(ValueError, 'TEST_COMMAND'),
        ):
            runner.execute(ws, json.loads(self.PLAN), 1, 0, ['outro'])
        resolve.assert_not_called()
        self.assertFalse(ws.lock.exists())

    def test_environment_values_are_validated(self):
        code = 'import json; from contracanto import config as c; print(json.dumps([c.TEST_COMMAND, c.SETUP_COMMAND]))'
        base = {k: v for k, v in os.environ.items() if not k.startswith('CONTRACANTO_')}
        base['PYTHONPATH'] = str(ROOT)

        def read(**env):
            return subprocess.run([sys.executable, '-c', code], env={**base, **env}, capture_output=True, text=True)

        self.assertEqual(json.loads(read().stdout), [None, None])
        result = read(CONTRACANTO_TEST_COMMAND='["py", "-m", "pytest"]', CONTRACANTO_SETUP_COMMAND='[]')
        self.assertEqual(json.loads(result.stdout), [['py', '-m', 'pytest'], []])
        for name, value in (
            ('CONTRACANTO_TEST_COMMAND', '[]'),
            ('CONTRACANTO_TEST_COMMAND', 'py -m pytest'),
            ('CONTRACANTO_SETUP_COMMAND', '["npm", 1]'),
        ):
            with self.subTest(name=name, value=value):
                result = read(**{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
