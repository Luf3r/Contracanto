"""Limites, tempos e parâmetros dos CLIs externos.

Os tempos e o modelo do revisor podem ser ajustados por variáveis de ambiente
`CONTRACANTO_<NOME>` (por exemplo `CONTRACANTO_TEST_TIMEOUT=1200`), lidas ao
importar o pacote. O worker lançado pelo MCP herda o ambiente do servidor.
"""

from __future__ import annotations

import os

ENV_PREFIX = 'CONTRACANTO_'

MAX_TASKS = 100
PARALLEL_RANGE = range(1, 9)
RETRY_RANGE = range(4)
DEFAULT_PARALLEL = 3
DEFAULT_RETRIES = 2


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser um número de segundos, recebido {raw!r}') from None
    if value <= 0:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser maior que zero, recebido {raw!r}')
    return value


def _text(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, '').strip() or default


# Segundos.
COMMAND_TIMEOUT = _seconds('COMMAND_TIMEOUT', 1800)
GIT_TIMEOUT = _seconds('GIT_TIMEOUT', 180)
TEST_TIMEOUT = _seconds('TEST_TIMEOUT', 600)
AGENT_TIMEOUT = _seconds('AGENT_TIMEOUT', 1600)
REVIEW_TIMEOUT = _seconds('REVIEW_TIMEOUT', 900)
PROCESS_EXIT_TIMEOUT = 30
STOP_POLL_INTERVAL = 0.25

# O limite interno do agy fica abaixo de AGENT_TIMEOUT para que ele reporte o próprio timeout.
AGENT_PRINT_TIMEOUT = f'{max(1, int(AGENT_TIMEOUT - 100) // 60)}m'
REVIEW_MODEL = _text('REVIEW_MODEL', 'sonnet')

# Caudas preservadas em mensagens e no pedido de revisão.
ERROR_TAIL = 2000
TEST_OUTPUT_TAIL = 10000

TUNABLE = ('COMMAND_TIMEOUT', 'GIT_TIMEOUT', 'TEST_TIMEOUT', 'AGENT_TIMEOUT', 'REVIEW_TIMEOUT', 'REVIEW_MODEL')


def check_limits(parallel: int, retries: int) -> None:
    if parallel not in PARALLEL_RANGE or retries not in RETRY_RANGE:
        raise ValueError('parallel deve ser 1..8, retries 0..3')
