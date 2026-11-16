"""Contain a suspended Windows subprocess and its descendants in a Job Object."""

import ctypes
import sys
import time
from ctypes import wintypes

import win32api
import win32con
import win32job
import win32process

assert sys.platform == 'win32', 'Job Objects existem apenas no Windows'


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ThreadID', wintypes.DWORD),
        ('th32OwnerProcessID', wintypes.DWORD),
        ('tpBasePri', wintypes.LONG),
        ('tpDeltaPri', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
    ]


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
for _name in ('Thread32First', 'Thread32Next'):
    _function = getattr(_kernel, _name)
    _function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _function.restype = wintypes.BOOL


def _resume_suspended_process(pid):
    # Popen closes CreateProcess's primary thread handle. Before any user code
    # runs, recover that thread from a read-only Toolhelp snapshot.
    snapshot = _kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel.Thread32First(snapshot, ctypes.byref(entry))
        threads = []
        while found:
            if entry.th32OwnerProcessID == pid:
                threads.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            found = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
        if len(threads) != 1:
            raise OSError(f'Não foi possível identificar a thread inicial do processo {pid}')
        thread = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, threads[0])
        try:
            # Release our CREATE_SUSPENDED count once. Another owner may hold
            # an additional suspension; the normal command timeout still applies.
            previous_count = win32process.ResumeThread(thread)
            if previous_count < 1:
                raise OSError(f'Thread inicial do processo {pid} sem suspensão esperada: {previous_count}')
        finally:
            thread.Close()
    finally:
        win32api.CloseHandle(snapshot)


class WindowsJob:
    creationflags = win32con.CREATE_SUSPENDED

    def __enter__(self):
        self.handle = win32job.CreateJobObject(None, '')
        try:
            limits = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, limits)
        except BaseException:
            self.handle.Close()
            raise
        return self

    def start(self, pid):
        process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(self.handle, process)
            # No child can be spawned before containment has been established.
            _resume_suspended_process(pid)
        finally:
            process.Close()

    def terminate(self, timeout=30):
        win32job.TerminateJobObject(self.handle, 1)
        deadline = time.monotonic() + timeout
        while win32job.QueryInformationJobObject(self.handle, win32job.JobObjectBasicAccountingInformation)[
            'ActiveProcesses'
        ]:
            if time.monotonic() >= deadline:
                raise TimeoutError('O Job Object ainda contém processos ativos')
            time.sleep(0.01)

    def __exit__(self, *exc):
        # Also contain descendants if cleanup itself was interrupted.
        self.handle.Close()
