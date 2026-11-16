"""Execução de comandos com contenção de toda a árvore de subprocessos."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from . import config
from .errors import RecoveryRequired, describe
from .state import StopSignal

if os.name == 'nt':
    from .windows_job import WindowsJob

# text: stdout e stderr decodificados com substituição.
# nul: caminhos Git delimitados por NUL; stdout precisa ser UTF-8 exato.
# binary: stdout em bytes, sem tradução de quebras de linha.
Output = Literal['text', 'nul', 'binary']


def kill_posix_group(pid: int) -> None:
    # communicate() may already have reaped the leader.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def wait_posix_group(pid: int) -> None:
    deadline = time.monotonic() + config.PROCESS_EXIT_TIMEOUT
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RecoveryRequired('Encerramento do grupo de processos não confirmado')
        time.sleep(0.02)


class _PosixGroup:
    """Um grupo de processos por comando."""

    def __enter__(self) -> _PosixGroup:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def popen_options(self) -> dict[str, Any]:
        return {'start_new_session': True}

    def start(self, process: subprocess.Popen) -> None:
        self._pid = process.pid

    def terminate(self) -> None:
        kill_posix_group(self._pid)
        wait_posix_group(self._pid)

    def abort(self, process: subprocess.Popen) -> None:
        kill_posix_group(process.pid)
        process.communicate(timeout=config.PROCESS_EXIT_TIMEOUT)
        wait_posix_group(process.pid)


class _WindowsContainment:
    """Um Job Object por comando; fechar o job encerra os descendentes."""

    def __enter__(self) -> _WindowsContainment:
        self._manager = WindowsJob()
        self._job = self._manager.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        return self._manager.__exit__(*exc)

    def popen_options(self) -> dict[str, Any]:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | WindowsJob.creationflags
        return {'creationflags': flags}

    def start(self, process: subprocess.Popen) -> None:
        self._job.start(process.pid)

    def terminate(self) -> None:
        self._job.terminate()

    def abort(self, process: subprocess.Popen) -> None:
        self._job.terminate()
        # Assignment may have failed while the root was suspended.
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=config.PROCESS_EXIT_TIMEOUT)


def _containment() -> _PosixGroup | _WindowsContainment:
    return _WindowsContainment() if os.name == 'nt' else _PosixGroup()


def detached_options() -> dict[str, Any]:
    """Opções de Popen para um worker que sobrevive ao processo que o lançou."""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {'start_new_session': True}


def _communicate(process: subprocess.Popen, input_data, timeout: float, stop: StopSignal | None, args):
    if stop is None:
        return process.communicate(input_data, timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        stop.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        try:
            return process.communicate(input_data, timeout=min(config.STOP_POLL_INTERVAL, remaining))
        except subprocess.TimeoutExpired:
            continue


def run(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = config.COMMAND_TIMEOUT,
    check: bool = True,
    input_data: str | bytes | None = None,
    stop: StopSignal | None = None,
    output: Output = 'text',
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Executa o comando e só retorna depois de encerrar todos os seus descendentes.

    Com `stop`, o comando é interrompido quando o operador pede o encerramento.
    """
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    if env_overrides:
        env.update(env_overrides)
    raw = output != 'text'
    # Close the containment before Popen's pipe cleanup: surviving children may
    # still hold those pipes open if explicit termination failed.
    with contextlib.ExitStack() as resources, _containment() as containment:
        process = None
        stdin: Any = subprocess.PIPE
        if stop is not None:
            stop.check()
            # Older Windows Pythons write pipe input synchronously in communicate.
            # A file also allows retries while the child has not consumed its input.
            stdin = subprocess.DEVNULL
            if input_data is not None:
                stdin = resources.enter_context(tempfile.TemporaryFile(mode='w+', encoding='utf-8'))
                stdin.write(input_data)
                stdin.seek(0)
                input_data = None
        try:
            process = subprocess.Popen(
                args,
                cwd=None if cwd is None else str(cwd),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding=None if raw else 'utf-8',
                errors=None if raw else 'replace',
                env=env,
                **containment.popen_options(),
            )
            resources.enter_context(process)
            containment.start(process)
            stdout, stderr = _communicate(process, input_data, timeout, stop, args)
            containment.terminate()
        except (Exception, KeyboardInterrupt) as error:
            if process is None:
                if isinstance(error, KeyboardInterrupt):
                    raise RecoveryRequired('Criação do subprocesso interrompida; resultado desconhecido') from error
                raise
            try:
                containment.abort(process)
            except (Exception, KeyboardInterrupt) as cleanup_error:
                try:
                    process.kill()
                finally:
                    raise RecoveryRequired(
                        f'Encerramento dos subprocessos não confirmado: {describe(cleanup_error)}'
                    ) from cleanup_error
            raise
        if output == 'nul':
            stdout = stdout.decode('utf-8')
        if raw:
            stderr = stderr.decode('utf-8', errors='replace')
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr[-config.ERROR_TAIL :]}')
    return result
