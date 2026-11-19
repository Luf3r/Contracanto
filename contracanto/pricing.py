"""Preço por modelo e custo estimado de cada chamada.

A tabela vem de `CONTRACANTO_PRICES_JSON`, um objeto JSON com um preço por modelo, em dólares
por milhão de tokens:

    {"gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50,
                    "long_context": {"threshold": 272000, "input": 0.20, "cached_input": 0.02,
                                     "cache_write": 0.25, "output": 0.75}}}

`long_context` é opcional. Quando o maior pedido de uma chamada passa de `threshold` tokens de
entrada, a chamada inteira vale pelas tarifas de contexto longo: o provedor cobra assim cada
pedido acima do limite, e os CLIs só informam a soma da chamada, então o custo fica pelo lado
de cima. Sem a variável ou sem o modelo na tabela, vale o custo que o próprio CLI informa
(o `claude -p` informa o de tabela); sem nenhum dos dois, a chamada fica sem preço.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError

RATE_FIELDS = ('input', 'cached_input', 'cache_write', 'output')
PER_TOKENS = 1_000_000


@dataclass(frozen=True)
class Rates:
    input: float
    cached_input: float
    cache_write: float
    output: float


@dataclass(frozen=True)
class Price:
    rates: Rates
    long_context: Rates | None = None
    threshold: int | None = None


@dataclass(frozen=True)
class CallCost:
    """Custo de uma chamada; `usd` vazio quando não há preço para o modelo."""

    usd: float | None
    long_context: bool = False


def _rates(name: str, model: str, data: Any, where: str) -> Rates:
    if not isinstance(data, dict):
        raise ConfigError(f'{name}: {where} de {model!r} deve ser um objeto')
    values = {}
    for field in RATE_FIELDS:
        value = data.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
            raise ConfigError(
                f'{name}: {where}.{field} de {model!r} deve ser um número não negativo, recebido {value!r}'
            )
        values[field] = float(value)
    return Rates(**values)


def load(raw: str | None, name: str = 'CONTRACANTO_PRICES_JSON') -> dict[str, Price]:
    """Interpreta a tabela de preços; um valor inválido falha ao importar, com o nome da variável."""
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigError(f'{name} deve ser um objeto JSON: {error}') from None
    if not isinstance(data, dict):
        raise ConfigError(f'{name} deve ser um objeto JSON com um preço por modelo')
    prices = {}
    for model, entry in data.items():
        rates = _rates(name, model, entry, 'o preço')
        long_data = entry.get('long_context')
        if long_data is None:
            prices[model] = Price(rates)
            continue
        threshold = long_data.get('threshold') if isinstance(long_data, dict) else None
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise ConfigError(f'{name}: long_context.threshold de {model!r} deve ser um inteiro positivo')
        prices[model] = Price(rates, _rates(name, model, long_data, 'long_context'), threshold)
    return prices


def call_cost(
    prices: Mapping[str, Price],
    model: str,
    tokens: Mapping[str, int],
    peak_input: int = 0,
    reported_usd: float | None = None,
) -> CallCost:
    """Custo em dólares de uma chamada com os tokens normalizados de `usage.Usage`."""
    price = prices.get(model)
    if price is None:
        return CallCost(reported_usd)
    long_context = price.long_context is not None and price.threshold is not None and peak_input > price.threshold
    rates = price.long_context if long_context and price.long_context is not None else price.rates
    usd = sum(tokens.get(field, 0) * getattr(rates, field) for field in RATE_FIELDS) / PER_TOKENS
    return CallCost(usd, long_context)
