"""Consultas e verificações Git; todo comando recebe o diretório explicitamente."""

from __future__ import annotations

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
    """Inspect masked entries against HEAD without changing the user's index flags."""
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
        # A fresh index has neither cached stat data nor assume-unchanged/skip-worktree
        # bits. Git itself compares file modes, symlinks and filtered contents.
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
                # Only absences outside the configured sparse selection are expected.
                names = ('\0'.join(sorted(missing)) + '\0').encode('utf-8')
                included = git_records('sparse-checkout', 'check-rules', '-z', cwd=wt, input_bytes=names)
                different -= missing.difference(included)
            changes.extend(sorted(different))
    return changes


def changed_files(wt: str | Path) -> list[str]:
    """Porcelain -z returns raw names; renames contain destination AND source."""
    records = iter(git_records('status', '--porcelain', ALL_SUBMODULES, '-z', '-uall', cwd=wt))
    paths = []
    for record in records:
        paths.append(record[3:])
        if 'R' in record[:2] or 'C' in record[:2]:
            paths.append(next(records))
    return list(dict.fromkeys([*paths, *hidden_index_changes(wt)]))


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
    """Check added destinations and file/directory replacements, including ignored data."""
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
        # No exclusion options: Git includes ignored files and nested repositories.
        # Match the on-disk spelling even on case-insensitive filesystems.
        relative = path.relative_to(root).as_posix()
        collision = git_records(
            'ls-files', '--others', '--directory', '-z', '--', f':(icase,literal){relative}', cwd=root
        )
        if collision:
            raise RuntimeError(f'Arquivo local seria sobrescrito: {collision[0]}')


def branch_ref(wt: str | Path) -> str:
    """Return the full branch ref, or HEAD for a detached checkout."""
    result = git('symbolic-ref', '--quiet', 'HEAD', cwd=wt, check=False)
    if result.returncode == 1:
        return 'HEAD'
    if result.returncode:
        raise RuntimeError(f'Não foi possível ler a branch: {result.stderr[-config.ERROR_TAIL :]}')
    return result.stdout.strip()


def check_git_operation(wt: str | Path) -> None:
    git_dir = Path(git('rev-parse', '--absolute-git-dir', cwd=wt).stdout.rstrip('\r\n'))
    for marker in PENDING_OPERATION_MARKERS:
        try:
            (git_dir / marker).stat()
        except FileNotFoundError:
            continue
        raise RuntimeError(f'Operação Git pendente no worktree: {marker}')


def check_revision(
    wt: str | Path, expected_head: str, expected_branch: str | None = None, recovery: bool = False
) -> str:
    """Verify both commit and checkout identity before trusting repository state."""
    error_type = RecoveryRequired if recovery else RuntimeError
    try:
        check_git_operation(wt)
        branch = branch_ref(wt)
        changed = (
            (expected_branch is not None and branch != expected_branch)
            or head(wt) != expected_head
            or changed_files(wt)
        )
    except (Exception, KeyboardInterrupt) as error:
        if recovery:
            raise RecoveryRequired(f'Não foi possível verificar branch, HEAD e arquivos: {error}') from error
        raise
    if changed:
        raise error_type('Branch, HEAD ou arquivos do repositório alterados; inspecione as alterações preservadas')
    return branch


def remove_worktree(root: Path, worktree: str | Path, branch: str | None) -> None:
    """Remove um worktree limpo e a branch dele; o Git recusa worktrees com alterações."""
    git('worktree', 'remove', str(worktree), cwd=root)
    if branch and branch != 'HEAD':
        git('branch', '-D', branch.removeprefix('refs/heads/'), cwd=root)


def check_checkout(wt: str | Path, expected_head: str, expected_branch: str | None) -> None:
    """O executor não pode trocar de branch nem criar commits por conta própria."""
    check_git_operation(wt)
    if branch_ref(wt) != expected_branch or head(wt) != expected_head:
        raise RuntimeError('Executor alterou a branch ou HEAD do worktree')


@dataclass
class TargetCheckout:
    """Branch e commit de destino conforme a última verificação."""

    root: Path
    branch: str
    head: str

    @classmethod
    def current(cls, root: Path) -> TargetCheckout:
        return cls(root, branch_ref(root), head(root))

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
    """Tests must preserve the commit, branch (including detached HEAD), and files."""
    expected_branch = check_revision(wt, expected_head, expected_branch, recovery)
    try:
        result = process.run(command, cwd=wt, timeout=config.TEST_TIMEOUT, check=False, stop=stop)
    except RecoveryRequired:
        # A dirty worktree must not hide an unconfirmed process termination.
        raise
    except (Exception, KeyboardInterrupt):
        check_revision(wt, expected_head, expected_branch, recovery)
        raise
    check_revision(wt, expected_head, expected_branch, recovery)
    return result
