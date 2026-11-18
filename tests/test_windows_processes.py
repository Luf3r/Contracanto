"""Windows process cleanup must not depend on taskkill's racy exit status."""

import concurrent.futures
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
DETACHED_PARENT = r"""
import site, subprocess, sys
for directory in {sites!r}:
    site.addsitedir(directory)
sys.path.insert(0, {root!r})
from contracanto import process
child, outside = process.launch_detached(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(child.pid, outside, flush=True)
"""


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class DetachedWorkerJobTests(unittest.TestCase):
    """O worker do MCP precisa sobreviver ao job que o cliente aplica ao servidor."""

    def launch_from_job(self, allow_breakaway):
        """Lança um worker de dentro de um job que encerra tudo ao fechar; devolve PID e saída do job."""
        from contracanto import windows_job as w

        root = str(Path(__file__).resolve().parent.parent)
        code = DETACHED_PARENT.format(root=root, sites=site.getsitepackages())
        with w.WindowsJob() as job:
            if allow_breakaway:
                kind = w.win32job.JobObjectExtendedLimitInformation
                limits = w.win32job.QueryInformationJobObject(job.handle, kind)
                limits['BasicLimitInformation']['LimitFlags'] |= w.win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
                w.win32job.SetInformationJobObject(job.handle, kind, limits)
            parent = subprocess.Popen(
                [getattr(sys, '_base_executable', sys.executable), '-I', '-c', code],
                stdout=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW | w.WindowsJob.creationflags,
            )
            job.start(parent.pid)
            output, _ = parent.communicate(timeout=60)
            self.assertEqual(parent.returncode, 0, output)
        pid, outside = output.split()
        self.addCleanup(self.kill, int(pid))
        return int(pid), outside == 'True'

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

    def test_worker_leaves_a_job_that_allows_it(self):
        pid, outside = self.launch_from_job(allow_breakaway=True)
        if not outside:
            self.skipTest('o ambiente que roda os testes está num job que não permite a saída')
        self.assertTrue(self.alive_after_job_closes(pid))

    def test_job_that_forbids_leaving_still_gets_a_worker_and_a_warning(self):
        pid, outside = self.launch_from_job(allow_breakaway=False)
        self.assertFalse(outside)
        self.assertFalse(self.alive_after_job_closes(pid))
