"""Worker vivo ou morto, comandos órfãos e liberação da reserva com --unlock."""

import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import support

from contracanto import cli, maintenance
from contracanto import process as proc
from contracanto import state as st


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


class ProcessTests(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(proc.pid_alive(os.getpid()))
        self.assertFalse(proc.pid_alive(support.finished_process_pid()))


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


class UnlockTests(unittest.TestCase):
    def reserved_repository(self, worker_pid):
        return support.reserved_repository(self, worker_pid, recovery_required=True)

    def test_refuses_while_worker_is_alive(self):
        ws, run_id, git = self.reserved_repository(os.getpid())
        message = maintenance.unlock(ws)
        self.assertIn('Recusado', message)
        self.assertIn(str(os.getpid()), message)
        self.assertTrue(ws.lock.exists())

    def test_refuses_with_dirty_repository_or_pending_operation(self):
        ws, run_id, git = self.reserved_repository(support.finished_process_pid())
        (ws.root / 'dirty.txt').write_text('x')
        self.assertIn('alterado', maintenance.unlock(ws))
        (ws.root / 'dirty.txt').unlink()
        (Path(git('rev-parse', '--absolute-git-dir').stdout.strip()) / 'MERGE_HEAD').write_text('x')
        self.assertIn('MERGE_HEAD', maintenance.unlock(ws))
        self.assertTrue(ws.lock.exists())

    def test_releases_dead_worker_and_records_it(self):
        ws, run_id, git = self.reserved_repository(support.finished_process_pid())
        message = maintenance.unlock(ws)
        self.assertIn('removida', message)
        self.assertFalse(ws.lock.exists())
        status = json.loads(ws.status(run_id).read_text())
        self.assertEqual(status['state'], 'failed')
        # Nenhuma tarefa fica em andamento depois que o worker morreu.
        self.assertEqual(status['statuses'], {'A': 'blocked', 'B': 'blocked', 'C': 'approved'})
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
