"""Intermediário que lança o worker do MCP no Windows e sai em seguida.

O servidor MCP roda como descendente do app do Claude e dentro de um Job Object dele, que
costuma proibir a saída. Lançado pelo servidor, o worker morre quando o app encerra a árvore
de processos ou o job, ao fechar ou reiniciar. Por isso o servidor pede à WMI que crie este
intermediário: ele nasce de um processo do sistema, fora do job e da árvore do app, lança o
worker e termina. Sem a WMI, o servidor cria o intermediário diretamente, o que ainda tira o
worker da árvore de processos.

Roda como script, com `python -I`, e usa só a biblioteca padrão: não importa o pacote, para
não depender de PYTHONPATH nem do pywin32. Recebe em `--spec` um arquivo JSON com `argv`,
`env`, `cwd`, `log` e `result`; a WMI não repassa pipes nem o ambiente. O worker escreve no
`log`, e o PID dele e `outside_job` vão para `result`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Sem janela e num grupo próprio, como o worker sempre foi lançado.
BASE_FLAGS = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
BREAKAWAY = getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0)


def in_job(process: subprocess.Popen) -> bool:
    """Se o processo pertence a algum Job Object; na dúvida, responde que sim."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    result = wintypes.BOOL()
    handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
    if not kernel.IsProcessInJob(handle, None, ctypes.byref(result)):
        return True
    return bool(result.value)


def spawn(argv: Sequence[str], popen: Any = None, **options: Any) -> tuple[subprocess.Popen, bool]:
    """Lança fora do Job Object do lançador quando o job permite; devolve o processo e se saiu.

    Um job que não permite a saída recusa CREATE_BREAKAWAY_FROM_JOB com acesso negado; então o
    processo é lançado dentro dele. O retorno diz se ele ficou fora de qualquer job.
    """
    popen = popen or subprocess.Popen
    try:
        process = popen(argv, creationflags=BASE_FLAGS | BREAKAWAY, **options)
    except PermissionError:
        process = popen(argv, creationflags=BASE_FLAGS, **options)
    return process, not in_job(process)


def write_result(path: Path, data: dict[str, Any]) -> None:
    # Quem espera lê o arquivo assim que ele aparece: a troca atômica evita uma leitura parcial.
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    os.replace(temporary, path)


def main() -> None:
    spec_path = Path(sys.argv[sys.argv.index('--spec') + 1])
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    result = Path(spec['result'])
    try:
        with open(spec['log'], 'a', encoding='utf-8') as log:
            worker, outside = spawn(
                spec['argv'],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=spec['cwd'],
                env=spec['env'],
            )
    except Exception as error:
        write_result(result, {'error': f'{type(error).__name__}: {error}'})
        raise
    write_result(result, {'pid': worker.pid, 'outside_job': outside})


if __name__ == '__main__':
    main()
