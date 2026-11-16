"""Limites, tempos e parâmetros dos CLIs externos."""

from __future__ import annotations

MAX_TASKS = 100
PARALLEL_RANGE = range(1, 9)
RETRY_RANGE = range(4)
DEFAULT_PARALLEL = 3
DEFAULT_RETRIES = 2

# Segundos.
COMMAND_TIMEOUT = 1800
GIT_TIMEOUT = 180
TEST_TIMEOUT = 600
AGENT_TIMEOUT = 1600
REVIEW_TIMEOUT = 900
PROCESS_EXIT_TIMEOUT = 30
STOP_POLL_INTERVAL = 0.25

# O limite interno do agy fica abaixo de AGENT_TIMEOUT para que ele reporte o próprio timeout.
AGENT_PRINT_TIMEOUT = '25m'
REVIEW_MODEL = 'sonnet'

# Caudas preservadas em mensagens e no pedido de revisão.
ERROR_TAIL = 2000
TEST_OUTPUT_TAIL = 10000


def check_limits(parallel: int, retries: int) -> None:
    if parallel not in PARALLEL_RANGE or retries not in RETRY_RANGE:
        raise ValueError('parallel deve ser 1..8, retries 0..3')
