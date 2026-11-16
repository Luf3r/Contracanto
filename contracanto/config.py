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


def _seconds(name: str, default: float, minimum: float = 0) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser um número de segundos, recebido {raw!r}') from None
    if value <= 0:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser maior que zero, recebido {raw!r}')
    if value < minimum:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser pelo menos {minimum:g}, recebido {raw!r}')
    return value


def _count(name: str, default: int, allowed: range) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value not in allowed:
        raise ValueError(f'{ENV_PREFIX}{name} deve ser um inteiro de {allowed[0]} a {allowed[-1]}, recebido {raw!r}')
    return value


def _text(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, '').strip() or default


# Segundos.
# Limite de um comando chamado sem tempo próprio; todos os do pacote informam o seu.
COMMAND_TIMEOUT = 1800
# O agy recebe o próprio limite em minutos inteiros, 100 s abaixo deste (veja AGENT_PRINT_TIMEOUT).
AGENT_TIMEOUT_MINIMUM = 160
GIT_TIMEOUT = _seconds('GIT_TIMEOUT', 180)
TEST_TIMEOUT = _seconds('TEST_TIMEOUT', 600)
SETUP_TIMEOUT = _seconds('SETUP_TIMEOUT', 600)
AGENT_TIMEOUT = _seconds('AGENT_TIMEOUT', 1600, AGENT_TIMEOUT_MINIMUM)
REVIEW_TIMEOUT = _seconds('REVIEW_TIMEOUT', 900)
PROCESS_EXIT_TIMEOUT = 30
STOP_POLL_INTERVAL = 0.25

# O limite interno do agy fica abaixo de AGENT_TIMEOUT para que ele reporte o próprio timeout.
AGENT_PRINT_TIMEOUT = f'{int(AGENT_TIMEOUT - 100) // 60}m'
REVIEW_MODEL = _text('REVIEW_MODEL', 'sonnet')
# Novas chamadas ao agy quando a implementação falha sem deixar o worktree em estado incerto.
IMPLEMENT_RETRIES = _count('IMPLEMENT_RETRIES', 1, range(4))
# Caracteres do patch aceitos em um pedido de revisão; acima disso a tarefa falha em vez de
# ser revisada pela metade.
REVIEW_PATCH_LIMIT = _count('REVIEW_PATCH_LIMIT', 400_000, range(1, 10_000_001))

# Caudas preservadas em mensagens e no pedido de revisão.
ERROR_TAIL = 2000
TEST_OUTPUT_TAIL = 10000

TUNABLE = (
    'GIT_TIMEOUT',
    'TEST_TIMEOUT',
    'SETUP_TIMEOUT',
    'AGENT_TIMEOUT',
    'REVIEW_TIMEOUT',
    'REVIEW_MODEL',
    'IMPLEMENT_RETRIES',
    'REVIEW_PATCH_LIMIT',
)


def check_limits(parallel: int, retries: int) -> None:
    if parallel not in PARALLEL_RANGE or retries not in RETRY_RANGE:
        raise ValueError('parallel deve ser 1..8, retries 0..3')
