"""Limites, tempos e parâmetros dos CLIs externos.

Os tempos e os modelos do executor e do revisor podem ser ajustados por variáveis de ambiente
`CONTRACANTO_<NOME>` (por exemplo `CONTRACANTO_TEST_TIMEOUT=1200`), lidas ao
importar o pacote. O worker lançado pelo MCP herda o ambiente do servidor.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from . import pricing
from .errors import ConfigError

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
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser um número de segundos, recebido {raw!r}') from None
    if value <= 0:
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser maior que zero, recebido {raw!r}')
    if value < minimum:
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser pelo menos {minimum:g}, recebido {raw!r}')
    return value


def _seconds_or_off(name: str, default: float) -> float:
    """Segundos, ou 0 para desligar."""
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is not None and raw.strip() in ('0', '0.0'):
        return 0.0
    return _seconds(name, default)


def _count(name: str, default: int, allowed: range) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value not in allowed:
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser um inteiro de {allowed[0]} a {allowed[-1]}, recebido {raw!r}')
    return value


def _text(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, '').strip() or default


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _text(name, default)
    if value not in allowed:
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser um de {", ".join(allowed)}, recebido {value!r}')
    return value


def _command(name: str, allow_empty: bool = False) -> list[str] | None:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if (
        not isinstance(value, list)
        or any(not isinstance(argument, str) for argument in value)
        or not (value or allow_empty)
    ):
        expected = 'uma lista JSON de strings' if allow_empty else 'uma lista JSON não vazia de strings'
        raise ConfigError(f'{ENV_PREFIX}{name} deve ser {expected}, recebido {raw!r}')
    return value


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
# Segundos sem saída nova do executor nem alteração no worktree até ele ser encerrado como
# parado; 0 desliga. A conferência roda a cada IDLE_POLL_INTERVAL segundos.
AGENT_IDLE_TIMEOUT = _seconds_or_off('AGENT_IDLE_TIMEOUT', 600)
IDLE_POLL_INTERVAL = 30.0
# Limite de `agy models`, consultado antes de cada execução quando AGENT_MODEL está definido.
MODELS_TIMEOUT = 60
PROCESS_EXIT_TIMEOUT = 30
STOP_POLL_INTERVAL = 0.25

# O limite interno do agy fica abaixo de AGENT_TIMEOUT para que ele reporte o próprio timeout.
AGENT_PRINT_TIMEOUT = f'{int(AGENT_TIMEOUT - 100) // 60}m'
REVIEW_MODEL = _text('REVIEW_MODEL', 'sonnet')
# CLI que implementa e corrige: `agy` (Antigravity) ou `codex` (Codex CLI da OpenAI).
EXECUTORS = ('agy', 'codex')
EXECUTOR = _choice('EXECUTOR', 'agy', EXECUTORS)
# Executável do Codex; um caminho completo evita depender do PATH herdado pelo servidor MCP.
CODEX_PATH = _text('CODEX_PATH', 'codex')
# Modelo do executor, passado em --model; vazio deixa o padrão do CLI (veja `agy models`).
AGENT_MODEL = _text('AGENT_MODEL', '')
# Novas chamadas ao agy quando uma implementação ou correção falha sem deixar o worktree
# em estado incerto.
AGENT_RETRIES = _count('AGENT_RETRIES', 1, range(4))
# Vezes que wait_for_run relança sozinho, com as tarefas que faltam, uma execução cujo
# worker morreu sem registrar o estado final; 0 desliga.
AUTO_RESUME = _count('AUTO_RESUME', 1, range(4))
# Caracteres do patch aceitos em um pedido de revisão; acima disso a tarefa falha em vez de
# ser revisada pela metade.
REVIEW_PATCH_LIMIT = _count('REVIEW_PATCH_LIMIT', 400_000, range(1, 10_000_001))
# Entradas em `files` acima das quais o submit_plan avisa que a tarefa pode ser grande demais
# para uma revisão; o aviso não bloqueia.
TASK_FILES_WARNING = _count('TASK_FILES_WARNING', 15, range(1, 1001))
# Diretório onde ficam os worktrees das tarefas, fora do repositório orquestrado para que
# ferramentas que varrem a árvore a partir da raiz não enxerguem as cópias. Vazio: a pasta
# Contracanto/worktrees em ~/.cache (ou $XDG_CACHE_HOME), também no Windows; veja state.worktrees_parent.
WORKTREES_DIR = _text('WORKTREES_DIR', '')

# Comandos fixados pelo operador. Com TEST_COMMAND definido, só esse comando de testes é
# aceito. Com SETUP_COMMAND definido, a preparação informada precisa ser igual a ele e,
# sem nenhuma informada, ele é usado; a lista vazia proíbe qualquer preparação. Sem as variáveis, vale o que quem submete escolher.
TEST_COMMAND = _command('TEST_COMMAND')
SETUP_COMMAND = _command('SETUP_COMMAND', allow_empty=True)

# Preço por modelo para estimar o custo das chamadas (veja pricing.py); vazio deixa só os tokens.
PRICES = pricing.load(os.environ.get(ENV_PREFIX + 'PRICES_JSON'), ENV_PREFIX + 'PRICES_JSON')
# Como cada papel é cobrado: `api` cobra por token; `plan` sai do limite de uma assinatura, e o
# custo fica só como equivalente informativo.
BILLING_MODES = ('plan', 'api')
EXECUTOR_BILLING = _choice('EXECUTOR_BILLING', 'plan', BILLING_MODES)
REVIEWER_BILLING = _choice('REVIEWER_BILLING', 'plan', BILLING_MODES)

# Caudas preservadas em mensagens e no pedido de revisão.
ERROR_TAIL = 2000
TEST_OUTPUT_TAIL = 10000

TUNABLE = (
    'GIT_TIMEOUT',
    'TEST_TIMEOUT',
    'SETUP_TIMEOUT',
    'AGENT_TIMEOUT',
    'AGENT_IDLE_TIMEOUT',
    'REVIEW_TIMEOUT',
    'REVIEW_MODEL',
    'AGENT_MODEL',
    'EXECUTOR',
    'CODEX_PATH',
    'AGENT_RETRIES',
    'AUTO_RESUME',
    'REVIEW_PATCH_LIMIT',
    'TASK_FILES_WARNING',
    'WORKTREES_DIR',
    'TEST_COMMAND',
    'SETUP_COMMAND',
    'PRICES_JSON',
    'EXECUTOR_BILLING',
    'REVIEWER_BILLING',
)


def check_limits(parallel: int, retries: int) -> None:
    if parallel not in PARALLEL_RANGE or retries not in RETRY_RANGE:
        raise ValueError('parallel deve ser 1..8, retries 0..3')


def check_pinned(test: Sequence[str] | None, setup: Sequence[str] | None) -> None:
    """Recusa comandos diferentes dos fixados pelo operador nas variáveis de ambiente."""
    if TEST_COMMAND is not None and list(test or []) != TEST_COMMAND:
        raise ValueError(
            f'O operador fixou o comando de testes em {ENV_PREFIX}TEST_COMMAND; '
            f'use exatamente {json.dumps(TEST_COMMAND, ensure_ascii=False)}'
        )
    if SETUP_COMMAND is not None and setup is not None and list(setup) != SETUP_COMMAND:
        if not SETUP_COMMAND:
            raise ValueError(f'O operador não permite comando de preparação ({ENV_PREFIX}SETUP_COMMAND vazio)')
        raise ValueError(
            f'O operador fixou o comando de preparação em {ENV_PREFIX}SETUP_COMMAND; '
            f'omita-o ou use exatamente {json.dumps(SETUP_COMMAND, ensure_ascii=False)}'
        )


def pinned_setup(setup: Sequence[str] | None) -> Sequence[str] | None:
    """A preparação fixada pelo operador vale também quando quem submete não informa nenhuma."""
    return SETUP_COMMAND if setup is None and SETUP_COMMAND else setup
