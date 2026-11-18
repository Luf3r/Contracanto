"""Windows process cleanup must not depend on taskkill's racy exit status."""

import concurrent.futures
import io
import os
import site
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import errors, runner
from contracanto import process as proc


@unittest.skipUnless(os.name == 'nt', 'Windows process containment')
class WindowsProcessTests(unittest.TestCase):
    def test_extra_suspend_is_left_for_its_owner_to_release(self):
        from contracanto import windows_job as w

        resume = w.win32process.ResumeThread
        counts = []

        def concurrent_suspend(thread):
            w.win32process.SuspendThread(thread)
            try:
                previous = resume(thread)
                counts.append(previous)
                return previous
            finally:
                # Release only the extra suspension introduced by this fixture.
                resume(thread)

        with patch.object(w.win32process, 'ResumeThread', side_effect=concurrent_suspend):
            result = proc.run([sys.executable, '-c', 'print("started")'], timeout=5)
        self.assertEqual(counts, [2])
        self.assertEqual(result.stdout.strip(), 'started')

    def assert_tree_stopped(self, parent_exits=False, taskkill_unavailable=False):
        with support.temp_dir() as directory:
            marker = Path(directory).resolve() / 'late.txt'
            child = f'import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text("late")'
            parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); ' + (
                '' if parent_exits else 'time.sleep(20)'
            )
            original_run = subprocess.run

            def run(args, **kwargs):
                if taskkill_unavailable and args[0] == 'taskkill':
                    return subprocess.CompletedProcess(args, 128, b'', b'No running instance')
                return original_run(args, **kwargs)

            # The base executable exits immediately, unlike the venv launcher.
            outcome = None
            with patch.object(proc.subprocess, 'run', side_effect=run):
                try:
                    proc.run([sys._base_executable, '-c', parent], timeout=0.6)  # type: ignore[attr-defined]
                except Exception as error:
                    outcome = error
            # Also check survivors if the runner returned the wrong exception.
            time.sleep(2.1)
            self.assertFalse(marker.exists(), 'A descendant survived the timeout')
            self.assertIsInstance(outcome, subprocess.TimeoutExpired)

    def test_timeout_without_taskkill(self):
        self.assert_tree_stopped(taskkill_unavailable=True)

    def test_timeout_after_parent_exits_still_stops_descendant(self):
        self.assert_tree_stopped(parent_exits=True)

    def test_assignment_failure_never_runs_command(self):
        from contracanto import windows_job as w

        with support.temp_dir() as directory:
            marker = Path(directory).resolve() / 'should-not-run.txt'
            code = f'from pathlib import Path; Path({str(marker)!r}).touch()'
            with patch.object(w.win32job, 'AssignProcessToJobObject', side_effect=OSError('assignment denied')):
                with self.assertRaisesRegex(OSError, 'assignment denied'):
                    proc.run([sys.executable, '-c', code], timeout=2)
            self.assertFalse(marker.exists())

    def test_startup_interrupt_reaps_suspended_process(self):
        created = []
        previous_trace = sys.gettrace()

        def interrupt_after_creation(frame, event, arg):
            if frame.f_code is proc.run.__code__ and event == 'line':
                process = frame.f_locals.get('process')
                if isinstance(process, subprocess.Popen):
                    created.append(process)
                    sys.settrace(None)
                    raise KeyboardInterrupt
            return interrupt_after_creation

        try:
            sys.settrace(interrupt_after_creation)
            with self.assertRaises(KeyboardInterrupt):
                proc.run([sys.executable, '-c', 'import time; time.sleep(20)'])
        finally:
            sys.settrace(previous_trace)
            alive = [process for process in created if process.poll() is None]
            # Keep a failing regression run from leaking its own suspended child.
            for process in alive:
                process.kill()
                process.communicate(timeout=5)
        self.assertEqual(len(created), 1, 'The startup interruption must actually run')
        self.assertFalse(alive, 'Startup interruption leaked a suspended subprocess')

    def test_unconfirmed_job_termination_preserves_reservation(self):
        from contracanto import windows_job as w

        with support.temp_dir() as directory:
            store = Path(directory)
            ws = support.workspace(store)
            with (
                patch.object(
                    runner,
                    '_execute',
                    side_effect=lambda *args: proc.run(
                        [sys.executable, '-c', 'import time; time.sleep(20)'], timeout=0.3
                    ),
                ),
                patch.object(w.win32job, 'TerminateJobObject', side_effect=OSError('termination denied')),
            ):
                with self.assertRaises(errors.RecoveryRequired):
                    runner.execute(ws, {}, 1, 0, ['test'])
                self.assertTrue((store / 'run.lock').exists())
                self.assertTrue(support.read_status(ws)['recovery_required'])

    def test_failed_termination_closes_job_before_waiting_for_child_pipes(self):
        from contracanto import windows_job as w

        with support.temp_dir() as directory:
            marker = Path(directory).resolve() / 'late.txt'
            child = f'import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()'
            parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(20)'
            with patch.object(w.win32job, 'TerminateJobObject', side_effect=OSError('termination denied')):
                with self.assertRaises(errors.RecoveryRequired):
                    proc.run([sys.executable, '-c', parent], timeout=0.6)
            time.sleep(2.1)
            self.assertFalse(marker.exists(), 'Pipe cleanup waited while a descendant was still running')

    def test_parallel_command_survives_other_commands_timeout(self):
        def timeout():
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=0.3)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            interrupted = pool.submit(timeout)
            other = pool.submit(proc.run, [sys.executable, '-c', 'import time; time.sleep(1); print("survived")'])
            interrupted.result()
            self.assertEqual(other.result().stdout.strip(), 'survived')

    def test_success_cleans_up_detached_descendant(self):
        with support.temp_dir() as directory:
            marker = Path(directory).resolve() / 'late.txt'
            child = f'import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()'
            parent = (
                'import subprocess,sys; '
                f'subprocess.Popen([sys.executable,"-c",{child!r}], '
                'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'
            )
            result = proc.run([sys.executable, '-c', parent])
            time.sleep(2.1)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


# Roda no interpretador base, sem o lançador do venv, que cria um job próprio e confundiria
# o teste; os pacotes do venv entram por addsitedir, que também processa os .pth do pywin32.
BASE_PYTHON = getattr(sys, '_base_executable', sys.executable)
DETACHED_PARENT = r"""
import os, site, sys, time
for directory in {sites!r}:
    site.addsitedir(directory)
sys.path.insert(0, {root!r})
from contracanto import process
pid, outside = process.launch_detached(
    [sys.executable, '-c', 'import time; time.sleep(60)'], log=os.devnull, cwd='.', env=dict(os.environ),
    use_wmi={wmi!r},
)
print(pid, outside, flush=True)
# Com STAY, o lançador continua vivo, como o servidor MCP enquanto o app está aberto.
if {stay!r}:
    time.sleep(60)
"""


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class DetachedWorkerJobTests(unittest.TestCase):
    """O worker do MCP precisa sobreviver ao job que o cliente aplica ao servidor."""

    def launch_from_job(self, allow_breakaway, wmi):
        """Lança um worker de dentro de um job que encerra tudo ao fechar.

        Devolve o PID, o `outside_job` informado e se o worker ficou neste job. O ambiente que
        roda os testes pode ter um job externo próprio, por isso a conferência é por este job.
        """
        if sys.platform != 'win32':  # Para o mypy fora do Windows; a classe já é pulada lá.
            raise unittest.SkipTest('Windows Job Objects')
        from contracanto import windows_job as w

        code = self.parent_code(stay=False, wmi=wmi)
        with w.WindowsJob() as job:
            if allow_breakaway:
                kind = w.win32job.JobObjectExtendedLimitInformation
                limits = w.win32job.QueryInformationJobObject(job.handle, kind)
                limits['BasicLimitInformation']['LimitFlags'] |= w.win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
                w.win32job.SetInformationJobObject(job.handle, kind, limits)
            parent = subprocess.Popen(
                [BASE_PYTHON, '-I', '-c', code],
                stdout=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW | w.WindowsJob.creationflags,
            )
            job.start(parent.pid)
            output, _ = parent.communicate(timeout=60)
            self.assertEqual(parent.returncode, 0, output)
            pid, outside = output.split()
            self.addCleanup(self.kill, int(pid))
            worker = w.win32api.OpenProcess(w.win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            try:
                in_test_job = w.win32job.IsProcessInJob(worker, job.handle)
            finally:
                worker.Close()
        return int(pid), outside == 'True', in_test_job

    def parent_code(self, stay, wmi):
        root = str(Path(__file__).resolve().parent.parent)
        return DETACHED_PARENT.format(root=root, sites=site.getsitepackages(), stay=stay, wmi=wmi)

    def kill(self, pid):
        import pywintypes
        import win32api
        import win32con

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        except pywintypes.error:
            return
        try:
            win32api.TerminateProcess(handle, 1)
        except pywintypes.error:
            pass
        finally:
            handle.Close()

    def alive_after_job_closes(self, pid):
        # O fechamento do job encerra os processos de forma assíncrona.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not proc.pid_alive(pid):
                return False
            time.sleep(0.1)
        return True

    def test_wmi_takes_the_worker_out_of_a_job_that_forbids_leaving(self):
        # O caso do app do Claude: o job do servidor MCP não permite CREATE_BREAKAWAY_FROM_JOB.
        pid, outside, in_test_job = self.launch_from_job(allow_breakaway=False, wmi=True)
        self.assertFalse(in_test_job)
        self.assertTrue(outside)
        self.assertTrue(self.alive_after_job_closes(pid))

    def test_without_wmi_the_worker_leaves_a_job_that_allows_it(self):
        pid, _, in_test_job = self.launch_from_job(allow_breakaway=True, wmi=False)
        self.assertFalse(in_test_job)
        self.assertTrue(self.alive_after_job_closes(pid))

    def test_worker_survives_a_tree_kill_of_its_launcher(self):
        # O app do Claude, ao fechar ou reiniciar, pode encerrar a árvore de processos inteira;
        # mesmo sem a WMI, o intermediário tira o worker dela.
        if sys.platform != 'win32':  # Para o mypy fora do Windows; a classe já é pulada lá.
            raise unittest.SkipTest('Windows Job Objects')
        parent = subprocess.Popen(
            [BASE_PYTHON, '-I', '-c', self.parent_code(stay=True, wmi=False)],
            stdout=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(parent.kill)
        pid, _ = (parent.stdout or io.StringIO()).readline().split()
        self.addCleanup(self.kill, int(pid))
        self.assertIsNone(parent.poll())
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(parent.pid)], capture_output=True, timeout=30)
        parent.wait(timeout=30)
        self.assertTrue(self.alive_after_job_closes(int(pid)))

    def test_without_wmi_a_job_that_forbids_leaving_keeps_the_worker_and_warns(self):
        pid, outside, in_test_job = self.launch_from_job(allow_breakaway=False, wmi=False)
        self.assertTrue(in_test_job)
        self.assertFalse(outside)
        self.assertFalse(self.alive_after_job_closes(pid))


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class DetachedLaunchTests(unittest.TestCase):
    def wait_for_exit(self, pid):
        # O worker segura o log e o diretório até sair; a pasta temporária só sai depois.
        deadline = time.monotonic() + 30
        while proc.pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

    def wait_for_text(self, path, text):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            content = path.read_text(encoding='utf-8', errors='replace')
            if text in content:
                return content
            time.sleep(0.1)
        self.fail(f'{text!r} não apareceu em {path}: {path.read_text(errors="replace")!r}')

    def test_worker_gets_the_environment_directory_and_log_through_wmi(self):
        with support.temp_dir() as directory:
            log = directory / 'run.log'
            log.write_text('', encoding='utf-8')
            code = 'import os; print("ambiente", os.environ["CONTRACANTO_PROBE"], os.getcwd(), flush=True)'
            env = dict(os.environ, CONTRACANTO_PROBE='ação ok')
            pid, outside = proc.launch_detached([sys.executable, '-c', code], log=log, cwd=directory, env=env)
            content = self.wait_for_text(log, 'ambiente')
            self.wait_for_exit(pid)
        self.assertTrue(outside)
        self.assertNotIn('WMI indisponível', content)
        self.assertIn('ação ok', content)
        self.assertIn(str(directory), content)

    def test_wmi_failure_falls_back_to_a_direct_launch_and_says_why(self):
        with support.temp_dir() as directory:
            log = directory / 'run.log'
            log.write_text('', encoding='utf-8')
            with patch.object(proc, '_create_with_wmi', side_effect=OSError('negado por política')):
                pid, _ = proc.launch_detached(
                    [sys.executable, '-c', 'print("fallback ok")'], log=log, cwd=directory, env=os.environ
                )
            content = self.wait_for_text(log, 'fallback ok')
            self.wait_for_exit(pid)
        self.assertIn('WMI indisponível', content)
        self.assertIn('negado por política', content)

    def test_worker_that_cannot_start_is_an_error(self):
        with support.temp_dir() as directory:
            log = directory / 'run.log'
            log.write_text('', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'não lançou o worker'):
                proc.launch_detached([str(directory / 'inexistente.exe')], log=log, cwd=directory, env=os.environ)
