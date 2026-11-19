"""Preparação na raiz antes dos testes de integração e resumo de erros que preserva a causa."""

import json
import sys
from unittest.mock import patch

import support

from contracanto import agents, maintenance, runner

PASS = [sys.executable, '-c', 'pass']


def task(task_id, path, deps=()):
    return dict(id=task_id, description=f'Create {path}', deps=list(deps), files=[path], acceptance=['File exists'])


class RootSetupTests(support.RepositoryTestCase):
    def run_plan(self, store, tasks, files, setup_code='pass', tests=PASS):
        """Executa o plano com um agente que escreve o arquivo de cada tarefa.

        A preparação anota em store/setup.log onde rodou, além de executar `setup_code`.
        """
        record = store / 'setup.log'
        setup = [
            sys.executable,
            '-c',
            f'import os; open({str(record)!r}, "a").write(os.getcwd() + "\\n"); {setup_code}',
        ]

        def agent(prompt, wt, stop=None):
            task_id = json.loads(prompt.rsplit('\n', 1)[1])['task']['id']
            (wt / files[task_id]).write_text(f'{task_id}\n')
            return 'done'

        with patch.object(agents, 'run_agy', side_effect=agent):
            states = runner.execute(self.ws, {'tasks': tasks}, 1, 0, tests, setup=setup)
        lines = record.read_text().splitlines() if record.exists() else []
        return states, lines

    def test_root_is_prepared_on_the_first_integration_and_when_dependencies_change(self):
        with self.repository() as (root, store, base, _, git):
            files = {'A': 'a.txt', 'B': 'package.json', 'C': 'c.txt'}
            tasks = [task('A', 'a.txt'), task('B', 'package.json', ['A']), task('C', 'c.txt', ['B'])]
            states, lines = self.run_plan(store, tasks, files)
            self.assertEqual(states, {'A': 'approved', 'B': 'approved', 'C': 'approved'})
            at_root = [line for line in lines if line == str(root)]
            # Primeira integração (A) e a mudança em package.json (B); C não muda dependências.
            self.assertEqual(len(at_root), 2, lines)

    def test_failed_root_setup_reverts_the_integration_with_its_output(self):
        with self.repository() as (root, store, base, _, git):
            fails_at_root = 'import sys; sys.exit("npm ERR! falta o pacote" if os.path.isdir(".git") else 0)'
            states, _ = self.run_plan(store, [task('A', 'a.txt')], {'A': 'a.txt'}, setup_code=fails_at_root)
            self.assertEqual(states, {'A': 'failed'})
            error = support.read_status(self.ws)['details']['A']['error']
            self.assertIn('Integração revertida', error)
            self.assertIn('Preparação na raiz falhou', error)
            self.assertIn('npm ERR! falta o pacote', error)
            self.assertFalse((root / 'a.txt').exists())

    def test_runs_without_setup_never_prepare_the_root(self):
        with self.repository() as (root, store, base, task_a, git):
            with patch.object(runner.gitops, 'run_tests', wraps=runner.gitops.run_tests) as runs:
                states = runner.execute(self.ws, {'tasks': [task_a]}, 1, 0, PASS)
            self.assertEqual(states, {'A': 'approved'})
            self.assertEqual(len(runs.call_args_list), 3)  # base, worktree e integração

    def test_dependency_files_are_recognized_in_any_directory(self):
        changed = runner.dependency_changes(
            ['web/package.json', 'src/app.ts', 'requirements-dev.txt', 'api/poetry.lock', 'README.md']
        )
        self.assertEqual(changed, ['web/package.json', 'requirements-dev.txt', 'api/poetry.lock'])


class ErrorTailTests(support.RepositoryTestCase):
    def test_integration_error_keeps_the_cause_at_the_end(self):
        with self.repository() as (root, store, base, task_a, git):
            noisy = [
                sys.executable,
                '-c',
                'import os, sys\n'
                'if not os.path.isdir(".git"): sys.exit(0)\n'
                'print("\\n".join(f"ok teste {n}" for n in range(400)))\n'
                'sys.exit("CAUSA REAL: tsc encontrou 3 erros")',
            ]
            states = runner.execute(self.ws, {'tasks': [task_a]}, 1, 0, noisy)
            self.assertEqual(states, {'A': 'failed'})
            error = support.read_status(self.ws)['details']['A']['error']
            self.assertIn('CAUSA REAL', error)
            summary = maintenance.summarize(maintenance.status_report(self.ws))['task_errors']['A']
            self.assertLessEqual(len(summary), maintenance.SUMMARY_ERROR_LIMIT)
            self.assertTrue(summary.startswith('Integração revertida'), summary)
            self.assertIn('CAUSA REAL: tsc encontrou 3 erros', summary)

    def test_abbreviate_keeps_short_text_and_marks_the_cut(self):
        self.assertEqual(maintenance.abbreviate('curto', 50), 'curto')
        text = 'Falhou\n' + 'meio\n' * 100 + 'fim da causa'
        short = maintenance.abbreviate(text, 60)
        self.assertLessEqual(len(short), 60)
        self.assertTrue(short.startswith('Falhou'))
        self.assertIn('[…]', short)
        self.assertTrue(short.endswith('fim da causa'))
