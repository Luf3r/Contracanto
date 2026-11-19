"""Non-blocking plan warnings: tasks too wide for one review and criteria the test command cannot prove."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import cli, config, plan, runner, server

NPM_CHECK = ['C:/node/npm.cmd', 'run', 'check']


def single(files=None, acceptance=None):
    task = support.task('T1', files)
    if acceptance is not None:
        task['acceptance'] = acceptance
    return {'tasks': [task]}


class WarningRuleTests(unittest.TestCase):
    def setUp(self):
        directory = support.temp_dir()
        self.root = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        for name in ('src', 'test', 'docs'):
            (self.root / name).mkdir()
        (self.root / 'README.md').write_text('x', encoding='utf-8')

    def warn(self, candidate, test=NPM_CHECK):
        return plan.warnings(candidate, test, self.root)

    def test_two_whole_root_directories_are_flagged(self):
        (found,) = self.warn(single(['src', 'test/', 'README.md']))
        self.assertIn('T1: files inclui diretórios raiz inteiros (src, test)', found)

    def test_one_root_directory_files_or_subdirectories_are_not(self):
        for files in (['src'], ['README.md', 'src'], ['src/mcp', 'test/mcp'], ['missing', 'other']):
            with self.subTest(files=files):
                self.assertEqual(self.warn(single(files)), [])

    def test_too_many_entries_are_flagged(self):
        with patch.object(config, 'TASK_FILES_WARNING', 3):
            self.assertEqual(self.warn(single(['a', 'b', 'c'])), [])
            (found,) = self.warn(single(['a', 'b', 'c', 'd']))
        self.assertIn('files tem 4 entradas, acima de 3', found)

    def test_criteria_citing_the_test_command_pass(self):
        acceptance = ['`npm run check` verde', 'npm check passa', 'Comportamento descrito na tarefa', 'npm é usado']
        self.assertEqual(self.warn(single(acceptance=acceptance)), [])
        self.assertEqual(self.warn(single(acceptance=['py -m pytest passa']), ['python', '-m', 'pytest', '-q']), [])

    def test_criteria_citing_another_command_are_flagged_once_each(self):
        acceptance = ['`npm test` e `tsc` passam', 'vitest estável', 'npm run check verde']
        found = self.warn(single(acceptance=acceptance))
        self.assertEqual(len(found), 2, found)
        self.assertIn('cita `npm test`, fora do comando de testes (`C:/node/npm.cmd run check`)', found[0])
        self.assertIn('cita `vitest`', found[1])

    def test_command_names_inside_words_or_paths_are_ignored(self):
        acceptance = ['src/tsc.ts exporta a função', 'pytest-like helper removido', 'jestaria']
        self.assertEqual(self.warn(single(acceptance=acceptance)), [])


class SubmissionWarningTests(unittest.TestCase):
    PASS = (sys.executable, '-c', 'pass')

    def submit(self, candidate):
        with (
            support.temp_workspace(server) as ws,
            patch.object(config, 'TEST_COMMAND', None),
            patch.object(server, 'preflight'),
            patch.object(server.process, 'launch_detached', return_value=(5, True)),
        ):
            for name in ('src', 'test'):
                (ws.root / name).mkdir()
            return server.submit_plan(json.dumps(candidate), test_command_json=json.dumps(list(self.PASS)))

    def test_warnings_follow_the_launch_message(self):
        result = self.submit(single(['src', 'test'], ['npm run check verde']))
        self.assertIn('PID 5', result)
        head, warnings = result.split('\nAvisos do plano (não bloqueiam; replaneje se fizer sentido):\n')
        lines = warnings.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(line.startswith('- T1: ') for line in lines))

    def test_clean_plan_has_no_warning_section(self):
        self.assertNotIn('Avisos do plano', self.submit(single()))


class CliWarningTests(support.RepositoryTestCase):
    def test_cli_prints_warnings_and_still_runs(self):
        with self.repository() as (root, store, base, task, git):
            task['acceptance'] = ['tsc sem erros']
            plan_file = Path(store) / 'plan.json'
            plan_file.write_text(json.dumps({'tasks': [task]}), encoding='utf-8')
            stderr = io.StringIO()
            with (
                patch.object(runner, 'execute', return_value={'A': 'approved'}) as execute,
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                cli.main([str(plan_file), '--test', sys.executable, '-c', 'pass'], self.ws)
        self.assertIn('Aviso do plano: A: o critério "tsc sem erros" cita `tsc`', stderr.getvalue())
        execute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
