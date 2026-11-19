"""Exceções que decidem se a reserva da execução pode ser liberada, e a categoria de cada falha."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from typing import Literal, get_args

# Por que uma tarefa falhou, gravado em `details.<id>.failure.category`. Separa o que é
# defeito da ferramenta do que é tarefa mal dimensionada ou critério que os testes não comprovam.
FailureCategory = Literal[
    'tooling',  # executável ausente, preparação, Git ou sistema de arquivos
    'executor_error',  # o executor terminou com erro ou com saída que não se entende
    'executor_noop',  # o executor terminou sem alterar arquivos
    'timeout',  # um comando passou do tempo limite
    'scope',  # arquivo alterado fora da lista files
    'patch_too_large',  # o patch passa do limite de revisão
    'inherited_base',  # os testes já falhavam na base e continuam falhando
    'review_rejected',  # rejeitado depois de todas as correções
    'criteria_unverifiable',  # rejeitado só porque o comando de testes não comprova um critério
    'reviewer_error',  # o revisor terminou com erro ou com saída que não se entende
    'integration_conflict',  # conflito ao aplicar o commit na branch de destino
    'integration_tests',  # os testes falharam depois de integrar, e a integração foi revertida
    'recovery',  # Git ou processos em estado incerto; exige recuperação manual
    'cancelled',  # encerramento pedido pelo operador
    'other',
]
FAILURE_CATEGORIES: tuple[FailureCategory, ...] = get_args(FailureCategory)
CATEGORY_ATTRIBUTE = 'contracanto_category'


class RecoveryRequired(RuntimeError):
    """Estado Git/processos incerto; nenhuma outra tarefa deve integrar."""


class IntegrationStateError(RecoveryRequired):
    """O estado posterior a uma integração não pôde ser gravado."""

    def __init__(self, error: BaseException):
        super().__init__(f'Falha ao registrar a integração: {error}')


class ConfigError(ValueError):
    """Valor inválido em uma variável de ambiente CONTRACANTO_*."""


class RunCancelled(RuntimeError):
    """O operador solicitou o encerramento desta execução."""


def tag[E: BaseException](error: E, category: FailureCategory) -> E:
    """Marca a categoria da falha na própria exceção, sem trocar o tipo nem a mensagem.

    A primeira marca vale: quem relança uma exceção já marcada não a reclassifica.
    """
    if getattr(error, CATEGORY_ATTRIBUTE, None) is None:
        setattr(error, CATEGORY_ATTRIBUTE, category)
    return error


def failure_category(error: BaseException) -> FailureCategory:
    """Categoria de uma falha: a marcada com `tag` ou, sem marca, a que o tipo indica."""
    if isinstance(error, RecoveryRequired):
        return 'recovery'
    if isinstance(error, RunCancelled):
        return 'cancelled'
    category = getattr(error, CATEGORY_ATTRIBUTE, None)
    if category in FAILURE_CATEGORIES:
        return category
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout'
    if isinstance(error, OSError | ConfigError):
        return 'tooling'
    return 'other'


def describe(error: BaseException) -> str:
    return str(error) or type(error).__name__


@contextlib.contextmanager
def recovery_on_failure(message: str) -> Iterator[None]:
    """Qualquer falha ou interrupção no bloco deixa o resultado desconhecido."""
    try:
        yield
    except (Exception, KeyboardInterrupt) as error:
        raise RecoveryRequired(f'{message}: {describe(error)}') from error
