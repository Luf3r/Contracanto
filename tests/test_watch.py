"""Painel da execução (`contracanto --watch`): desenho, transições e código de saída."""

import json
import unittest
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

import support

from contracanto import cli, maintenance, watch
from contracanto import state as st

NOW = datetime(2026, 11, 17, 12, 10, tzinfo=UTC)
LOG = (
    '12:00:00 Execução abc: 2 tarefa(s) sobre 4fef45e em main\n'
    '12:00:01 A: início de implementation\n'
    '12:03:00 A: fim de implementation após 179.0s\n'
    '12:03:00 A: início de review\n'
    '12:00:01 B: início de implementation\n'
)


def report(run_state='running', statuses=None, alive=True, reservation_state='active', **run):
    data = {
        'state': run_state,
        'run_id': 'abc123def4567890',
        'started_at': '2026-11-17T12:00:00+00:00',
        'statuses': statuses if statuses is not None else {'A': 'running', 'B': 'running', 'C': 'pending'},
        'details': {},
        'timings': {'A': {'started_at': '2026-11-17T12:00:01+00:00', 'phases': {}}},
        'target': {'branch': 'main', 'base': '4fef45e0000000'},
        'duration_seconds': 600.0,
        **run,
    }
    reservation = {'run_id': data['run_id'], 'worker_alive': alive} if reservation_state != 'none' else None
    return {'run': data, 'reservation_state': reservation_state, 'reservation': reservation, 'log': ''}


class RenderTests(unittest.TestCase):
    def test_phases_follow_the_start_and_end_lines(self):
        self.assertEqual(watch.current_phases(LOG), {'A': 'review', 'B': 'implementation'})

    def test_panel_shows_tasks_phases_and_elapsed_time(self):
        text = '\n'.join(watch.render(report(), LOG, NOW))
        self.assertIn('execução abc123def456 · running · 10m00s', text)
        self.assertIn('destino main @ 4fef45e00000', text)
        self.assertRegex(text, r'A\s+running\s+revisão\s+9m59s')
        self.assertRegex(text, r'B\s+running\s+implementação')
        self.assertIn('3 tarefa(s): 2 running · 1 pending', text)
        self.assertIn('Últimos eventos', text)
        self.assertNotIn('\x1b[', text)

    def test_errors_are_listed_on_one_line_each(self):
        run = report(
            'failed', {'A': 'failed'}, details={'A': {'error': 'O executor não alterou arquivos\nmotivo longo'}}
        )
        text = '\n'.join(watch.render(run, '', NOW, width=60))
        self.assertIn('A: O executor não alterou arquivos motivo longo', text)

    def test_color_only_when_asked(self):
        self.assertIn('\x1b[32m', '\n'.join(watch.render(report(statuses={'A': 'approved'}), '', NOW, color=True)))

    def test_waiting_for_the_first_state(self):
        empty: dict = {'run': None, 'reservation_state': 'active', 'reservation': {}, 'log': ''}
        self.assertIn('Aguardando', '\n'.join(watch.render(empty, '', NOW)))


class OutcomeTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(watch.outcome(report('completed', {'A': 'approved'})), watch.EXIT_APPROVED)
        self.assertEqual(watch.outcome(report('completed', {'A': 'approved', 'B': 'failed'})), 1)
        self.assertEqual(watch.outcome(report('cancelled', {'A': 'approved'})), 1)
        self.assertEqual(watch.outcome(report('running', alive=False)), watch.EXIT_WORKER_DEAD)
        self.assertEqual(watch.outcome({'run': None, 'reservation_state': 'none', 'reservation': None}), 1)

    def test_transitions_name_each_change(self):
        before = report(statuses={'A': 'running'})
        after = report(statuses={'A': 'review', 'B': 'pending'})
        lines = watch.transitions(before, after)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith('A: running → review'))
        self.assertTrue(lines[1].endswith('B: — → pending'))
        self.assertEqual(watch.transitions(after, after), [])


class WatchLoopTests(unittest.TestCase):
    def run_watch(self, reports, interactive):
        out = StringIO()
        sleeps: list[float] = []
        with support.temp_dir() as directory:
            ws = support.workspace(directory)
            with patch.object(maintenance, 'status_report', side_effect=reports):
                code = watch.watch(ws, out, interactive=interactive, sleep=sleeps.append)
        return code, out.getvalue(), sleeps

    def test_line_mode_reports_changes_and_ends_with_the_summary(self):
        reports = [
            report(statuses={'A': 'running'}),
            report(statuses={'A': 'running'}),
            report('completed', {'A': 'approved'}, reservation_state='none'),
        ]
        code, output, sleeps = self.run_watch(reports, interactive=False)
        self.assertEqual(code, watch.EXIT_APPROVED)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(output.count('A: running → approved'), 1)
        self.assertIn('todas as tarefas aprovadas', output)
        summary = json.loads(output[output.index('{') :])
        self.assertEqual(summary['statuses'], {'A': 'approved'})

    def test_dead_worker_ends_the_watch(self):
        code, output, _ = self.run_watch([report(alive=False)], interactive=False)
        self.assertEqual(code, watch.EXIT_WORKER_DEAD)
        self.assertIn('wait_for_run', output)

    def test_panel_mode_redraws_in_place_and_restores_the_cursor(self):
        reports = [report(), report('failed', {'A': 'failed'}, reservation_state='none')]
        code, output, _ = self.run_watch(reports, interactive=True)
        self.assertEqual(code, watch.EXIT_NOT_APPROVED)
        self.assertEqual(output.count('\x1b[H'), 2)
        self.assertTrue(output.endswith('\x1b[?25h'))
        self.assertIn('há tarefas não aprovadas', output)

    def test_interrupt_leaves_the_run_alone(self):
        def interrupted(_):
            raise KeyboardInterrupt

        out = StringIO()
        with support.temp_dir() as directory:
            with patch.object(maintenance, 'status_report', return_value=report()):
                code = watch.watch(support.workspace(directory), out, interactive=False, sleep=interrupted)
        self.assertEqual(code, watch.EXIT_INTERRUPTED)
        self.assertIn('a execução continua', out.getvalue())


class WatchCliTests(unittest.TestCase):
    def test_cli_exits_with_the_watch_code(self):
        with support.temp_dir() as directory:
            ws = st.Workspace(directory, directory / '.agent-runs')
            with patch.object(watch, 'watch', return_value=3) as called, self.assertRaises(SystemExit) as exit:
                cli.main(['--watch'], ws)
        self.assertEqual(exit.exception.code, 3)
        called.assert_called_once_with(ws)


if __name__ == '__main__':
    unittest.main()


REJECTED = {
    'attempt': 1,
    'approved': False,
    'feedback': 'REJECTED\nFalta tratar o caso vazio.\n- lista vazia quebra o parser',
}


class ReviewVisibilityTests(unittest.TestCase):
    def test_panel_shows_the_latest_rejection_while_the_task_runs(self):
        run = report(statuses={'A': 'running'}, details={'A': {'reviews': [REJECTED]}})
        text = '\n'.join(watch.render(run, '', NOW))
        self.assertIn('Revisões rejeitadas', text)
        self.assertIn('A: revisão 1 rejeitada: Falta tratar o caso vazio.', text)

    def test_approved_or_finished_tasks_hide_old_rejections(self):
        approved = {**REJECTED, 'attempt': 2, 'approved': True}
        run = report(statuses={'A': 'running'}, details={'A': {'reviews': [REJECTED, approved]}})
        self.assertNotIn('Revisões rejeitadas', '\n'.join(watch.render(run, '', NOW)))
        finished = report('failed', {'A': 'failed'}, details={'A': {'reviews': [REJECTED]}})
        self.assertNotIn('Revisões rejeitadas', '\n'.join(watch.render(finished, '', NOW)))

    def test_line_mode_reports_each_new_rejection_once(self):
        before = report(statuses={'A': 'review'}, details={'A': {}})
        after = report(statuses={'A': 'review'}, details={'A': {'reviews': [REJECTED]}})
        (line,) = watch.transitions(before, after)
        self.assertIn('A: revisão 1 rejeitada: Falta tratar o caso vazio.', line)
        self.assertEqual(watch.transitions(after, after), [])
