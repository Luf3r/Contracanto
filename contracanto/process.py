"""Execução de comandos com contenção de toda a árvore de subprocessos."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import config
from .errors import RecoveryRequired, describe
from .state import StopSignal

# text: stdout e stderr decodificados com substituição.
# nul: caminhos Git delimitados por NUL; stdout precisa ser UTF-8 exato.
# binary: stdout em bytes, sem tradução de quebras de linha.
Output = Literal['text', 'nul', 'binary']


@dataclass(frozen=True)
class IdleWatch:
    """Encerra o comando que passa `timeout` segundos sem progresso.

    Progresso é a saída padrão crescer ou `probe` devolver outro valor, por exemplo o estado do
    worktree que o comando altera. A conferência roda a cada `interval` segundos.
    """

    timeout: float
    probe: Callable[[], Hashable] | None = None
    interval: float = 30.0


class Stalled(RuntimeError):
    """O comando ficou sem progresso pelo tempo de IdleWatch e foi encerrado."""

    contracanto_category = 'stalled'


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


DETACH_TIMEOUT = 60
DETACH_POLL_INTERVAL = 0.05
DETACH_EXIT_TIMEOUT = 10


def launch_detached(
    argv: Sequence[str], log: str | Path, cwd: str | Path, env: Mapping[str, str], use_wmi: bool = True
) -> tuple[int, bool]:
    """Lança um worker que sobrevive ao processo que o lançou e ao app que lançou esse processo.

    Devolve o PID do worker e se ele ficou fora de qualquer Job Object. A saída e os erros do
    worker vão para o arquivo `log`. No Windows, o worker é lançado por um intermediário (veja
    detach) que a WMI cria fora do job e da árvore de processos do app do Claude; sem a WMI, o
    intermediário é criado aqui, o que ainda tira o worker da árvore. O pedido ao intermediário
    passa por um arquivo temporário do usuário, com o ambiente do worker, removido em seguida.
    Nos demais sistemas, uma sessão própria já separa o worker do grupo de quem lançou.
    """
    if sys.platform != 'win32':
        with open(log, 'a', encoding='utf-8') as output:
            worker = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                **detached_options(),
            )
        return worker.pid, True
    from . import detach

    with tempfile.TemporaryDirectory(prefix='contracanto-launch-', ignore_cleanup_errors=True) as scratch:
        spec, result = Path(scratch) / 'spec.json', Path(scratch) / 'result.json'
        data = {'argv': list(argv), 'env': dict(env), 'cwd': str(cwd), 'log': str(log), 'result': str(result)}
        spec.write_text(json.dumps(data), encoding='utf-8')
        # pythonw não tem console: nem o intermediário nem o worker abrem uma janela.
        windowless = Path(sys.executable).with_name('pythonw.exe')
        python = str(windowless) if windowless.is_file() else sys.executable
        command = [python, '-I', detach.__file__, '--spec', str(spec)]
        intermediate = None
        if use_wmi:
            try:
                intermediate = _create_with_wmi(command, cwd)
            except Exception as error:  # A WMI pode estar desativada ou negada por política.
                log_line = f'WMI indisponível para lançar o worker ({describe(error)}); lançando diretamente\n'
                with contextlib.suppress(OSError), open(log, 'a', encoding='utf-8') as output:
                    output.write(log_line)
        if intermediate is None:
            process, _ = detach.spawn(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
            )
            intermediate = process.pid
        return _detach_result(result, intermediate)


def _wmi_process_create(command_line: str, cwd: str) -> int:
    import win32com.client

    service = win32com.client.GetObject('winmgmts:{impersonationLevel=impersonate}!//./root/cimv2')
    startup = service.Get('Win32_ProcessStartup').SpawnInstance_()
    startup.ShowWindow = 0  # SW_HIDE
    process_class = service.Get('Win32_Process')
    inputs = process_class.Methods_('Create').InParameters.SpawnInstance_()
    inputs.CommandLine = command_line
    inputs.CurrentDirectory = cwd
    inputs.ProcessStartupInformation = startup
    outputs = process_class.ExecMethod_('Create', inputs)
    if outputs.ReturnValue != 0:
        raise OSError(f'Win32_Process.Create devolveu {outputs.ReturnValue}')
    return int(outputs.ProcessId)


def _create_with_wmi(command: Sequence[str], cwd: str | Path) -> int:
    """Cria o processo pela WMI (Win32_Process.Create) e devolve o PID.

    O processo nasce do provedor da WMI, fora do Job Object e da árvore de quem chama.
    """
    import pythoncom

    pythoncom.CoInitialize()
    try:
        # Os objetos COM vivem só dentro da chamada e são liberados antes do CoUninitialize.
        return _wmi_process_create(subprocess.list2cmdline(command), str(cwd))
    finally:
        pythoncom.CoUninitialize()


def _detach_result(result: Path, intermediate: int) -> tuple[int, bool]:
    """Espera o intermediário informar o worker que lançou."""
    deadline = time.monotonic() + DETACH_TIMEOUT
    while not result.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('O intermediário que lança o worker não respondeu a tempo')
        if not pid_alive(intermediate) and not result.exists():
            raise RuntimeError('O intermediário que lança o worker terminou sem lançá-lo')
        time.sleep(DETACH_POLL_INTERVAL)
    report = json.loads(result.read_text(encoding='utf-8'))
    # O intermediário sai logo depois de responder. Esperá-lo libera o arquivo temporário e o
    # diretório de trabalho que ele ainda segura; um atraso aqui não impede o worker.
    exit_deadline = time.monotonic() + DETACH_EXIT_TIMEOUT
    while pid_alive(intermediate) and time.monotonic() < exit_deadline:
        time.sleep(DETACH_POLL_INTERVAL)
    if 'error' in report:
        raise RuntimeError(f'O intermediário não lançou o worker: {report["error"]}')
    return int(report['pid']), bool(report['outside_job'])


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


def _communicate(
    process: subprocess.Popen,
    input_data,
    timeout: float,
    stop: StopSignal | None,
    args,
    idle: IdleWatch | None = None,
    output_size: Callable[[], int] | None = None,
):
    if stop is None and idle is None:
        return process.communicate(input_data, timeout=timeout)
    now = time.monotonic()
    deadline = now + timeout
    last_progress = next_check = now
    signature: tuple[Any, ...] | None = None
    while True:
        if stop is not None:
            stop.check()
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        if idle is not None and now >= next_check:
            current = (output_size() if output_size else None, idle.probe() if idle.probe else None)
            if current != signature:
                signature, last_progress = current, now
            elif now - last_progress >= idle.timeout:
                raise Stalled(
                    f'{args[0]} ficou {int(now - last_progress)}s sem progresso (sem saída nova nem alteração '
                    f'de arquivos) e foi encerrado; ajuste {config.ENV_PREFIX}AGENT_IDLE_TIMEOUT se for esperado'
                )
            next_check = now + idle.interval
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
    idle: IdleWatch | None = None,
) -> subprocess.CompletedProcess:
    """Executa o comando e só retorna depois de encerrar todos os seus descendentes.

    Com `stop`, o comando é interrompido quando o operador pede o encerramento. Com `idle`,
    quando fica sem progresso; a saída padrão vai então para um arquivo temporário, para que
    o crescimento dela possa ser medido enquanto o comando roda (só com `output='text'`).
    """
    if idle is not None and output != 'text':
        raise ValueError('IdleWatch exige output text')
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
        stdout_target: Any = subprocess.PIPE
        stdout_file: Any = None
        if idle is not None:
            stdout_file = resources.enter_context(tempfile.TemporaryFile(mode='w+b'))
            stdout_target = stdout_file
        if stop is not None:
            stop.check()
        if stop is not None or idle is not None:
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
                stdout=stdout_target,
                stderr=subprocess.PIPE,
                encoding=None if raw else 'utf-8',
                errors=None if raw else 'replace',
                env=env,
                **containment.popen_options(),
            )
            resources.enter_context(process)
            marker = _register(stop, process.pid)
            containment.start(process)
            size = (lambda: os.fstat(stdout_file.fileno()).st_size) if stdout_file is not None else None
            stdout, stderr = _communicate(process, input_data, timeout, stop, args, idle, size)
            containment.terminate()
            if stdout_file is not None:
                stdout_file.seek(0)
                stdout = stdout_file.read().decode('utf-8', errors='replace')
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
