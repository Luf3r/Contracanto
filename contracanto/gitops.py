"""Consultas e verificações Git; todo comando recebe o diretório explicitamente."""

from __future__ import annotations

import itertools
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, plan, process
from .errors import RecoveryRequired
from .state import StopSignal

AGENT_IDENTITY = ('-c', 'user.name=Agent Executor', '-c', 'user.email=agent@localhost')
PENDING_OPERATION_MARKERS = (
    'MERGE_HEAD',
    'CHERRY_PICK_HEAD',
    'REVERT_HEAD',
    'rebase-apply',
    'rebase-merge',
    'sequencer',
    'BISECT_START',
)
# Compara submódulos mesmo quando configurados com ignore = all.
ALL_SUBMODULES = '--ignore-submodules=none'


def git(
    *args: str,
    cwd: str | Path,
    check: bool = True,
    env_overrides: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    output: process.Output = 'text',
) -> subprocess.CompletedProcess:
    return process.run(
        ['git', *args],
        cwd=cwd,
        timeout=config.GIT_TIMEOUT,
        check=check,
        input_data=input_bytes,
        output=output,
        env_overrides=env_overrides,
    )


def git_records(*args: str, cwd: str | Path, **options) -> list[str]:
    """Registros de um comando chamado com -z, sem tradução de quebras de linha
    nem decodificação com perdas antes das verificações de escopo."""
    return [record for record in git(*args, cwd=cwd, output='nul', **options).stdout.split('\0') if record]


def head(wt: str | Path) -> str:
    return git('rev-parse', 'HEAD', cwd=wt).stdout.strip()


def commit_staged(wt: str | Path, message: str) -> None:
    git(*AGENT_IDENTITY, 'commit', '-m', message, cwd=wt)


def commit_all(wt: str | Path, message: str) -> None:
    git('add', '--all', '--', '.', cwd=wt)
    commit_staged(wt, message)


def _sparse_checkout_enabled(wt: str | Path) -> bool:
    result = git('config', '--bool', '--get', 'core.sparseCheckout', cwd=wt, check=False)
    return result.stdout.strip() == 'true'


def hidden_index_changes(wt: str | Path) -> list[str]:
    """Compara com o HEAD as entradas mascaradas, sem mudar as marcas do índice do usuário."""
    masked = set()
    skipped = set()
    changes: list[str] = []
    for entry in git_records('ls-files', '-v', '--stage', '-z', cwd=wt):
        metadata, name = entry.split('\t', 1)
        tag, mode, *_ = metadata.split()
        if tag.islower() or tag == 'S':
            masked.add(name)
        if tag.upper() == 'S':
            skipped.add(name)
        if mode == '160000':
            module = Path(wt) / name
            if (module / '.git').exists():
                changes.extend(f'{name}/{path}' for path in hidden_index_changes(module))
    if masked:
        # Um índice novo não tem dados de stat em cache nem as marcas assume-unchanged e
        # skip-worktree. O próprio Git compara modos, links simbólicos e conteúdo filtrado.
        with tempfile.TemporaryDirectory() as directory:
            env = {'GIT_INDEX_FILE': str(Path(directory).resolve() / 'index')}
            options = (
                '-c',
                'core.sparseCheckout=false',
                '-c',
                'core.ignoreStat=false',
                '-c',
                'core.fsmonitor=false',
                '-c',
                'core.splitIndex=false',
            )
            git(*options, 'read-tree', 'HEAD', cwd=wt, env_overrides=env)
            paths = git_records(
                *options,
                'diff',
                '--name-only',
                '--no-renames',
                ALL_SUBMODULES,
                '-z',
                'HEAD',
                '--',
                cwd=wt,
                env_overrides=env,
            )
            different = masked.intersection(paths)
            missing = {name for name in skipped & different if not os.path.lexists(Path(wt) / name)}
            if missing and _sparse_checkout_enabled(wt):
                # Só as ausências fora da seleção esparsa configurada são esperadas.
                names = ('\0'.join(sorted(missing)) + '\0').encode('utf-8')
                included = git_records('sparse-checkout', 'check-rules', '-z', cwd=wt, input_bytes=names)
                different -= missing.difference(included)
            changes.extend(sorted(different))
    return changes


@dataclass(frozen=True)
class CheckoutState:
    """Branch, commit e arquivos alterados de um checkout, lidos em uma única chamada ao Git."""

    branch: str
    head: str
    paths: tuple[str, ...]


# Campos separados por espaço que precedem o caminho em cada tipo de registro do porcelain v2.
_STATUS_FIELDS = {'1': 8, '2': 9, 'u': 10}


def checkout_state(wt: str | Path) -> CheckoutState:
    """Lê `git status --porcelain=v2 --branch -z`: nomes crus e, nas renomeações, destino E origem.

    `branch` é a ref completa, ou HEAD em um checkout destacado. Uma branch chamada
    literalmente `(detached)` também é lida como HEAD e nunca confere com a esperada.
    """
    records = iter(git_records('status', '--porcelain=v2', '--branch', ALL_SUBMODULES, '-z', '-uall', cwd=wt))
    branch = head = None
    paths: list[str] = []
    for record in records:
        kind, _, rest = record.partition(' ')
        if kind == '#':
            key, _, value = rest.partition(' ')
            if key == 'branch.oid':
                head = value
            elif key == 'branch.head':
                branch = 'HEAD' if value == '(detached)' else 'refs/heads/' + value
        elif kind in _STATUS_FIELDS:
            paths.append(record.split(' ', _STATUS_FIELDS[kind])[-1])
            if kind == '2':
                paths.append(next(records))
        elif kind in ('?', '!'):
            paths.append(rest)
        else:
            raise RuntimeError(f'Saída inesperada do git status: {record[:60]!r}')
    if branch is None or head is None or head == '(initial)':
        raise RuntimeError('git status não informou a branch e o commit atuais')
    return CheckoutState(branch, head, tuple(paths))


def changed_files(wt: str | Path) -> list[str]:
    """Arquivos alterados, inclusive os mascarados por marcas do índice."""
    return list(dict.fromkeys([*checkout_state(wt).paths, *hidden_index_changes(wt)]))


def check_paths_in_scope(task: plan.Task, paths: Iterable[str]) -> None:
    for name in paths:
        if not plan.in_scope(task['files'], name):
            raise RuntimeError(f'Arquivo fora do escopo em {task["id"]}: {name}')


def check_tree_scope(task: plan.Task, wt: str | Path, before: str, after: str) -> None:
    paths = git_records('diff', '--name-only', '--no-renames', ALL_SUBMODULES, '-z', before, after, cwd=wt)
    check_paths_in_scope(task, paths)


def check_scope(task: plan.Task, wt: str | Path, base: str) -> None:
    """Cobre alterações não commitadas, inclusive não rastreadas, e commits desde a base."""
    check_paths_in_scope(task, changed_files(wt))
    check_tree_scope(task, wt, base, 'HEAD')


def protect_untracked_files(root: Path, before: str, after: str) -> None:
    """Confere destinos adicionados e trocas entre arquivo e diretório, inclusive dados ignorados."""
    added = git_records(
        'diff', '--name-only', '--diff-filter=AT', '--no-renames', ALL_SUBMODULES, '-z', before, after, cwd=root
    )
    for name in added:
        path = root
        exists = True
        for part in name.split('/'):
            path = path / part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                exists = False
                break
            if path.is_junction() or (stat.S_ISDIR(mode) and (path / '.git').exists()):
                raise RuntimeError(f'Integração atingiria uma junção ou repositório local: {path}')
            if not stat.S_ISDIR(mode):
                break
        if not exists:
            continue
        # Sem opções de exclusão: o Git inclui arquivos ignorados e repositórios aninhados.
        # Casa a grafia em disco mesmo em sistemas de arquivos que não distinguem maiúsculas.
        relative = path.relative_to(root).as_posix()
        collision = git_records(
            'ls-files', '--others', '--directory', '-z', '--', f':(icase,literal){relative}', cwd=root
        )
        if collision:
            raise RuntimeError(f'Arquivo local seria sobrescrito: {collision[0]}')


def branch_ref(wt: str | Path) -> str:
    """Ref completa da branch, ou HEAD em um checkout destacado."""
    result = git('symbolic-ref', '--quiet', 'HEAD', cwd=wt, check=False)
    if result.returncode == 1:
        return 'HEAD'
    if result.returncode:
        raise RuntimeError(f'Não foi possível ler a branch: {result.stderr[-config.ERROR_TAIL :]}')
    return result.stdout.strip()


# O diretório Git de um checkout não muda enquanto ele existe.
_GIT_DIRS: dict[str, Path] = {}


def git_dir(wt: str | Path) -> Path:
    key = os.path.abspath(wt)
    found = _GIT_DIRS.get(key)
    if found is None:
        found = Path(git('rev-parse', '--absolute-git-dir', cwd=wt).stdout.rstrip('\r\n'))
        # Só um diretório real é lembrado; uma resposta inesperada é consultada de novo.
        if found.is_absolute() and found.is_dir():
            _GIT_DIRS[key] = found
    return found


def check_git_operation(wt: str | Path) -> None:
    directory = git_dir(wt)
    for marker in PENDING_OPERATION_MARKERS:
        try:
            (directory / marker).stat()
        except FileNotFoundError:
            continue
        raise RuntimeError(f'Operação Git pendente no worktree: {marker}')


def check_revision(
    wt: str | Path, expected_head: str, expected_branch: str | None = None, recovery: bool = False
) -> str:
    """Confere o commit e a identidade do checkout antes de confiar no estado do repositório."""
    error_type = RecoveryRequired if recovery else RuntimeError
    try:
        check_git_operation(wt)
        current = checkout_state(wt)
        branch = current.branch
        changed = (
            (expected_branch is not None and branch != expected_branch)
            or current.head != expected_head
            or bool(current.paths)
            or bool(hidden_index_changes(wt))
        )
    except (Exception, KeyboardInterrupt) as error:
        if recovery:
            raise RecoveryRequired(f'Não foi possível verificar branch, HEAD e arquivos: {error}') from error
        raise
    if changed:
        raise error_type('Branch, HEAD ou arquivos do repositório alterados; inspecione as alterações preservadas')
    return branch


def is_top_level(root: Path) -> bool:
    """Se `root` é a raiz do repositório, e não um subdiretório dele."""
    return not git('rev-parse', '--show-prefix', cwd=root).stdout.strip()


def create_branch(root: Path, name: str) -> None:
    """Cria `name` no commit atual e passa o checkout para ela."""
    if git('check-ref-format', '--branch', name, cwd=root, check=False).returncode:
        raise RuntimeError(f'Nome de branch inválido: {name}')
    created = git('switch', '-c', name, cwd=root, check=False)
    if created.returncode:
        raise RuntimeError(f'Não foi possível criar a branch {name}: {created.stderr.strip()[-config.ERROR_TAIL :]}')


def drop_branch(root: Path, name: str, origin: CheckoutState) -> None:
    """Volta o checkout para onde estava e apaga `name`, que não recebeu commits."""
    if origin.branch == 'HEAD':
        git('switch', '--detach', origin.head, cwd=root)
    else:
        git('switch', origin.branch.removeprefix('refs/heads/'), cwd=root)
    # -d recusa apagar uma branch com commits que o checkout atual não tem.
    git('branch', '-d', name, cwd=root)


def reverted(root: Path, commit: str) -> bool:
    """Se algum commit posterior, no histórico do HEAD, é o `git revert` de `commit`."""
    found = git(
        'log', '--format=%H', '--fixed-strings', f'--grep=This reverts commit {commit}', f'{commit}..HEAD', cwd=root
    )
    return bool(found.stdout.strip())


def contains_commit(root: Path, commit: str) -> bool:
    """Se `commit` faz parte do histórico do HEAD atual."""
    return git('merge-base', '--is-ancestor', commit, 'HEAD', cwd=root, check=False).returncode == 0


def merge_tree(root: Path, ours: str, theirs: str) -> str:
    """Árvore resultante do merge de dois commits, sem tocar no checkout.

    Um conflito vira um erro que nomeia os arquivos: o Git os lista na saída
    padrão e sai com código 1, sem nada na saída de erro.
    """
    result = git('merge-tree', '--write-tree', '--name-only', ours, theirs, cwd=root, check=False)
    lines = result.stdout.splitlines()
    if result.returncode == 0 and lines:
        return lines[0].strip()
    if result.returncode == 1 and lines:
        # Depois da árvore vêm os arquivos em conflito, até uma linha em branco.
        files = list(itertools.takewhile(str.strip, lines[1:]))
        raise RuntimeError('Conflito com alterações já integradas neste lote: ' + (', '.join(files) or 'sem detalhes'))
    raise RuntimeError(f'git merge-tree falhou ({result.returncode}): {result.stderr[-config.ERROR_TAIL :]}')


def remove_worktree(root: Path, worktree: str | Path, branch: str | None, force: bool = False) -> None:
    """Remove um worktree e a branch dele.

    Sem `force`, o Git recusa worktrees com alterações; com ele, elas são descartadas.
    """
    git('worktree', 'remove', *(['--force'] if force else []), str(worktree), cwd=root)
    if branch and branch != 'HEAD':
        git('branch', '-D', branch.removeprefix('refs/heads/'), cwd=root)


def check_checkout(wt: str | Path, expected_head: str, expected_branch: str | None) -> None:
    """O executor não pode trocar de branch nem criar commits por conta própria."""
    check_git_operation(wt)
    current = checkout_state(wt)
    if current.branch != expected_branch or current.head != expected_head:
        raise RuntimeError('Executor alterou a branch ou HEAD do worktree')


@dataclass
class TargetCheckout:
    """Branch e commit de destino conforme a última verificação."""

    root: Path
    branch: str
    head: str

    @classmethod
    def current(cls, root: Path) -> TargetCheckout:
        current = checkout_state(root)
        return cls(root, current.branch, current.head)

    def verify(self) -> None:
        check_revision(self.root, self.head, self.branch, recovery=True)


def run_tests(
    command: Sequence[str],
    wt: str | Path,
    expected_head: str,
    recovery: bool = False,
    expected_branch: str | None = None,
    stop: StopSignal | None = None,
) -> subprocess.CompletedProcess:
    """Roda os testes, que devem preservar o commit, a branch (ou o HEAD destacado) e os arquivos."""
    expected_branch = check_revision(wt, expected_head, expected_branch, recovery)
    try:
        result = process.run(command, cwd=wt, timeout=config.TEST_TIMEOUT, check=False, stop=stop)
    except RecoveryRequired:
        # Um worktree sujo não pode esconder um encerramento de processos não confirmado.
        raise
    except (Exception, KeyboardInterrupt):
        check_revision(wt, expected_head, expected_branch, recovery)
        raise
    check_revision(wt, expected_head, expected_branch, recovery)
    return result
