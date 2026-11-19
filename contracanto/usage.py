"""Contagem de tokens das chamadas aos CLIs, normalizada entre executor e revisor.

Cada CLI informa os tokens de um jeito. Aqui todos viram um `Usage` com o mesmo significado:

- `input`: entrada sem cache;
- `cached_input`: entrada lida do cache;
- `cache_write`: entrada gravada no cache;
- `output`: tudo o que o modelo gerou, raciocínio incluído;
- `reasoning`: a parte de `output` que foi raciocínio;
- `calls`: chamadas somadas.

A chamada que lê o uso o entrega a `record`, e a fase aberta em `RunState.phase` acumula o que
chegar na mesma thread. Fora de uma fase, ou com a saída sem contagem, nada é registrado: a
medição nunca faz uma chamada falhar.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

Role = Literal['executor', 'reviewer']
FIELDS = ('input', 'cached_input', 'cache_write', 'output', 'reasoning', 'calls')


@dataclass(frozen=True)
class Usage:
    input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    calls: int = 1

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def add(total: Mapping[str, int] | None, usage: Usage) -> dict[str, int]:
    """Soma `usage` a um total já gravado no status, campo a campo."""
    current = dict(total or {})
    return {name: int(current.get(name, 0)) + getattr(usage, name) for name in FIELDS}


def _count(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key) or 0
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f'contagem inválida em {key}: {value!r}')
    return int(value)


def from_agy(stdout: str) -> Usage | None:
    """Uso do evento `result` do `agy --output-format stream-json`.

    O agy informa `cache_read_tokens` fora de `input_tokens` e `thinking_tokens` fora de
    `output_tokens`; o raciocínio entra em `output` aqui.
    """
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict) and event.get('event') == 'result':
                data = (event.get('result') or {}).get('usage')
                if not isinstance(data, dict):
                    return None
                thinking = _count(data, 'thinking_tokens')
                return Usage(
                    input=_count(data, 'input_tokens'),
                    cached_input=_count(data, 'cache_read_tokens'),
                    output=_count(data, 'output_tokens') + thinking,
                    reasoning=thinking,
                )
    except (ValueError, AttributeError, TypeError):
        return None
    return None


def from_claude(stdout: str) -> Usage | None:
    """Uso da saída de `claude -p --output-format json`, que já separa leitura e gravação de cache."""
    try:
        data = json.loads(stdout).get('usage')
        if not isinstance(data, dict):
            return None
        details = data.get('output_tokens_details') or {}
        return Usage(
            input=_count(data, 'input_tokens'),
            cached_input=_count(data, 'cache_read_input_tokens'),
            cache_write=_count(data, 'cache_creation_input_tokens'),
            output=_count(data, 'output_tokens'),
            reasoning=_count(details, 'thinking_tokens') if isinstance(details, dict) else 0,
        )
    except (ValueError, AttributeError, TypeError):
        return None


Sink = Callable[[Role, Usage], None]
_sink: contextvars.ContextVar[Sink | None] = contextvars.ContextVar('contracanto_usage_sink', default=None)


@contextlib.contextmanager
def collecting(sink: Sink) -> Iterator[None]:
    """Entrega a `sink` o uso registrado nesta thread enquanto o bloco roda."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def record(role: Role, usage: Usage | None) -> None:
    sink = _sink.get()
    if sink is not None and usage is not None:
        sink(role, usage)
