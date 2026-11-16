"""Exceções que decidem se a reserva da execução pode ser liberada."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator


class RecoveryRequired(RuntimeError):
    """Estado Git/processos incerto; nenhuma outra tarefa deve integrar."""


class IntegrationStateError(RecoveryRequired):
    """O estado posterior a uma integração não pôde ser gravado."""

    def __init__(self, error: BaseException):
        super().__init__(f'Falha ao registrar a integração: {error}')


class RunCancelled(RuntimeError):
    """O operador solicitou o encerramento desta execução."""


def describe(error: BaseException) -> str:
    return str(error) or type(error).__name__


@contextlib.contextmanager
def recovery_on_failure(message: str) -> Iterator[None]:
    """Qualquer falha ou interrupção no bloco deixa o resultado desconhecido."""
    try:
        yield
    except (Exception, KeyboardInterrupt) as error:
        raise RecoveryRequired(f'{message}: {describe(error)}') from error
