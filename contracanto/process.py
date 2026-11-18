"""Execução de comandos com contenção de toda a árvore de subprocessos."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from . import config
from .errors import RecoveryRequired, describe
from .state import StopSignal

# text: stdout e stderr decodificados com substituição.
# nul: caminhos Git delimitados por NUL; stdout precisa ser UTF-8 exato.
# binary: stdout em bytes, sem tradução de quebras de linha.
Output = Literal['text', 'nul', 'binary']

if sys.platform == 'win32':
    from .windows_job import WindowsJob

    class _Containment:
        """Um Job Object por comando; fechar o job encerra os descendentes."""

        def __enter__(self) -> _Containment:
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
            # A associação ao job pode ter falhado com a raiz ainda suspensa.
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=config.PROCESS_EXIT_TIMEOUT)

    def detached_options() -> dict[str, Any]:
        """Opções de Popen para um worker que sobrevive ao processo que o lançou."""
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}

    def command_alive(pid: int) -> bool:
        """Se o comando cuja raiz tinha este PID ainda pode estar rodando.

        O Job Object encerra os descendentes quando o worker termina; basta a raiz.
        """
        return pid_alive(pid)

    def pid_alive(pid: int) -> bool:
        """Se existe processo com este PID. Na dúvida, responde que sim."""
        # os.kill(pid, 0) encerraria o processo no Windows; consulta o código de saída.
        import pywintypes
        import win32api
        import win32con
        import win32process

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except pywintypes.error as error:
            # ERROR_INVALID_PARAMETER: nenhum processo com esse PID.
            return error.winerror != 87
        try:
            return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
        finally:
            handle.Close()

else:

    def kill_posix_group(pid: int) -> None:
        # communicate() pode já ter recolhido o líder do grupo.
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

    class _Containment:
        """Um grupo de processos por comando."""

        def __enter__(self) -> _Containment:
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

    def detached_options() -> dict[str, Any]:
        """Opções de Popen para um worker que sobrevive ao processo que o lançou."""
        return {'start_new_session': True}

    def command_alive(pid: int) -> bool:
        """Se o comando cuja raiz tinha este PID ainda pode estar rodando.

        O grupo de processos sobrevive a um worker encerrado à força.
        """
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def pid_alive(pid: int) -> bool:
        """Se existe processo com este PID. Na dúvida, responde que sim."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def launch_detached(argv: Sequence[str], popen: Any = None, **options: Any) -> tuple[subprocess.Popen, bool]:
    """Lança um worker que sobrevive ao processo que o lançou.

    Devolve o processo e se ele ficou fora do Job Object do lançador. No Windows, o cliente
    MCP costuma pôr o servidor num job que encerra os descendentes quando o servidor é
    substituído; o worker pede para sair dele com CREATE_BREAKAWAY_FROM_JOB. Um job que não
    permite a saída recusa a criação com acesso negado, e então o worker é lançado dentro
    dele, como antes, com o retorno False para que quem lançou possa avisar. `popen` substitui
    subprocess.Popen, para quem lança simular o worker.
    """
    popen = popen or subprocess.Popen
    flags = detached_options()
    if sys.platform != 'win32':
        return popen(argv, **flags, **options), True
    breakaway = flags['creationflags'] | subprocess.CREATE_BREAKAWAY_FROM_JOB
    try:
        return popen(argv, creationflags=breakaway, **options), True
    except PermissionError:
        process = popen(argv, **flags, **options)
        return process, not _in_job(process)


def _in_job(process: subprocess.Popen) -> bool:
    """Se o processo pertence a algum Job Object; na dúvida, responde que sim."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    result = wintypes.BOOL()
    handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
    if not kernel.IsProcessInJob(handle, None, ctypes.byref(result)):
        return True
    return bool(result.value)


def _register(stop: StopSignal | None, pid: int) -> Path | None:
    """Registra um comando interrompível em andamento; é diagnóstico, nunca impede o comando."""
    if stop is None or stop.commands is None:
        return None
    marker = stop.commands / str(pid)
    try:
        stop.commands.mkdir(exist_ok=True)
        marker.touch()
    except OSError:
        return None
    return marker


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
    # A contenção fecha antes da limpeza dos pipes do Popen: filhos sobreviventes
    # ainda podem segurar esses pipes se o encerramento explícito falhou.
    with contextlib.ExitStack() as resources, _Containment() as containment:
        process = None
        marker: Path | None = None
        stdin: Any = subprocess.PIPE
        if stop is not None:
            stop.check()
            # Pythons antigos no Windows escrevem a entrada do pipe de forma síncrona em
            # communicate. Um arquivo também permite repetir a espera enquanto o filho não a consumiu.
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
            marker = _register(stop, process.pid)
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
            # O encerramento foi confirmado; sem isso o registro fica para o --unlock.
            if marker is not None:
                marker.unlink(missing_ok=True)
            raise
        if marker is not None:
            marker.unlink(missing_ok=True)
        if output == 'nul':
            stdout = stdout.decode('utf-8')
        if raw:
            stderr = stderr.decode('utf-8', errors='replace')
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr[-config.ERROR_TAIL :]}')
    return result
