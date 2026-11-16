"""MVP local de orquestração Claude -> Antigravity, para um repositório Git confiável.
Executa o comando de testes com as permissões locais do worker. Requer CLIs autenticados.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

if os.name == 'nt':
    from windows_job import WindowsJob

ROOT = Path(__file__).resolve().parent
STORE = ROOT / '.agent-runs'
STORE.mkdir(exist_ok=True)
SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
RUN_DIR = STORE
STOP_FILE = None


class RecoveryRequired(RuntimeError):
    """Estado Git/processos incerto; nenhuma outra tarefa deve integrar."""


class IntegrationStateError(RecoveryRequired):
    def __init__(self, state, error):
        super().__init__(f'Falha ao registrar a integração: {error}')
        self.state = state


class RunCancelled(RuntimeError):
    """O operador solicitou o encerramento desta execução."""


def check_stop():
    if STOP_FILE is not None and STOP_FILE.is_file():
        raise RunCancelled('Execução encerrada a pedido do operador')


def request_stop(run_id=None):
    try:
        record = json.loads((STORE / 'run.lock').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 'Nenhuma execução ativa.'
    except json.JSONDecodeError:
        return 'Reserva em criação ou incompleta. Consulte o status e tente novamente.'
    active_id = record.get('run_id', '')
    if run_id is not None and run_id != active_id:
        return 'A reserva atual pertence a outra execução.'
    if not record.get('supports_stop') or not re.fullmatch('[0-9a-f]{64}', active_id):
        return 'Esta reserva não oferece encerramento cooperativo. Consulte o procedimento de recuperação.'
    try:
        state = json.loads((STORE / 'status.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        state = {}
    if state.get('run_id') == active_id and state.get('state') in ('completed', 'failed', 'cancelled'):
        if state.get('recovery_required'):
            return 'Execução finalizada com recuperação manual pendente.'
        return 'Execução já finalizada.'
    # The request may precede the worker's single-use mkdir or race a new run.
    (STORE / f'stop-{active_id}').touch()
    return f'Encerramento solicitado para {active_id}. Consulte get_run_status para confirmar o término.'


def reserve_run():
    """Reserva atômica; após crash, exige inspeção e recuperação manual."""
    token = uuid.uuid4().hex
    try:
        with (STORE / 'run.lock').open('x', encoding='utf-8') as handle:
            json.dump({'run_id': reservation_id(token), 'pid': os.getpid(), 'supports_stop': True}, handle)
    except FileExistsError:
        raise RuntimeError(
            'Já existe execução reservada; consulte get_run_status e o procedimento de recuperação'
        ) from None
    return token


def reservation_id(token):
    """Only this public identifier is persisted or returned by status queries."""
    return hashlib.sha256(token.encode()).hexdigest()


def release_run(token):
    path = STORE / 'run.lock'
    run_id = reservation_id(token)

    def remove_owned_lock():
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('run_id') == run_id:
                path.unlink()
        except FileNotFoundError:
            pass

    retry_permission_error(remove_owned_lock)


def retry_permission_error(operation, *args):
    # A Windows reader may briefly prevent replacing or removing an open file.
    for attempt in range(21):
        try:
            return operation(*args)
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(0.05)


def kill_posix_group(pid):
    # communicate() may already have reaped the leader.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def wait_posix_group(pid):
    deadline = time.monotonic() + 30
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RecoveryRequired('Encerramento do grupo de processos não confirmado')
        time.sleep(0.02)


def cmd(
    args,
    cwd=ROOT,
    timeout=1800,
    check=True,
    input_text=None,
    cancellable=False,
    raw_paths=False,
    binary_output=False,
    env_overrides=None,
):
    options = (
        {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | WindowsJob.creationflags}
        if os.name == 'nt'
        else {'start_new_session': True}
    )
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    if env_overrides:
        env.update(env_overrides)
    # Close the job before Popen's pipe cleanup: surviving children may still
    # hold those pipes open if explicit termination failed.
    with contextlib.ExitStack() as processes, WindowsJob() if os.name == 'nt' else contextlib.nullcontext() as job:
        process = None
        poll_stop = cancellable and STOP_FILE is not None
        stdin = subprocess.PIPE
        if poll_stop:
            check_stop()
            # Older Windows Pythons write pipe input synchronously in communicate.
            # A file also allows retries while the child has not consumed its input.
            stdin = subprocess.DEVNULL
            if input_text is not None:
                stdin = processes.enter_context(tempfile.TemporaryFile(mode='w+', encoding='utf-8'))
                stdin.write(input_text)
                stdin.seek(0)
                input_text = None
        try:
            # NUL-delimited Git paths must not undergo universal newline translation
            # or lossy decoding before the ownership and checkout checks.
            raw = raw_paths or binary_output
            process = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding=None if raw else 'utf-8',
                errors=None if raw else 'replace',
                env=env,
                **options,
            )
            processes.enter_context(process)
            if job is not None:
                job.start(process.pid)
            if poll_stop:
                deadline = time.monotonic() + timeout
                while True:
                    check_stop()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        stdout, stderr = process.communicate(input_text, timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            else:
                stdout, stderr = process.communicate(input_text, timeout=timeout)
            if job is not None:
                job.terminate()
            else:
                kill_posix_group(process.pid)
                wait_posix_group(process.pid)
        except (Exception, KeyboardInterrupt) as error:
            if process is None:
                if isinstance(error, KeyboardInterrupt):
                    raise RecoveryRequired('Criação do subprocesso interrompida; resultado desconhecido') from error
                raise
            try:
                if job is not None:
                    job.terminate()
                    # Assignment may have failed while the root was suspended.
                    if process.poll() is None:
                        process.kill()
                else:
                    kill_posix_group(process.pid)
                process.communicate(timeout=30)
                if job is None:
                    wait_posix_group(process.pid)
            except (Exception, KeyboardInterrupt) as cleanup_error:
                try:
                    process.kill()
                finally:
                    reason = str(cleanup_error) or type(cleanup_error).__name__
                    raise RecoveryRequired(f'Encerramento dos subprocessos não confirmado: {reason}') from cleanup_error
            raise
        if raw_paths:
            stdout = stdout.decode('utf-8')
        if raw:
            stderr = stderr.decode('utf-8', errors='replace')
        p = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} exited {p.returncode}: {p.stderr[-2000:]}')
    return p


def git(*args, cwd=ROOT, check=True, env_overrides=None, input_bytes=None):
    return cmd(
        ['git', *args],
        cwd=cwd,
        timeout=180,
        check=check,
        raw_paths='-z' in args,
        binary_output='--binary' in args,
        env_overrides=env_overrides,
        input_text=input_bytes,
    )


def hidden_index_changes(wt):
    """Inspect masked entries against HEAD without changing the user's index flags."""
    entries = git('ls-files', '-v', '--stage', '-z', cwd=wt).stdout.split('\0')
    masked = set()
    skipped = set()
    changes = []
    for entry in filter(None, entries):
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
        with tempfile.TemporaryDirectory(dir=STORE) as directory:
            env = {'GIT_INDEX_FILE': str(Path(directory).resolve() / 'index')}
            config = (
                '-c',
                'core.sparseCheckout=false',
                '-c',
                'core.ignoreStat=false',
                '-c',
                'core.fsmonitor=false',
                '-c',
                'core.splitIndex=false',
            )
            git(*config, 'read-tree', 'HEAD', cwd=wt, env_overrides=env)
            paths = git(
                *config,
                'diff',
                '--name-only',
                '--no-renames',
                '--ignore-submodules=none',
                '-z',
                'HEAD',
                '--',
                cwd=wt,
                env_overrides=env,
            ).stdout
            different = masked.intersection(paths.split('\0'))
            missing = {name for name in skipped & different if not os.path.lexists(Path(wt) / name)}
            if (
                missing
                and git('config', '--bool', '--get', 'core.sparseCheckout', cwd=wt, check=False).stdout.strip()
                == 'true'
            ):
                # Only absences outside the configured sparse selection are expected.
                names = ('\0'.join(sorted(missing)) + '\0').encode('utf-8')
                included = git('sparse-checkout', 'check-rules', '-z', cwd=wt, input_bytes=names)
                different -= missing.difference(included.stdout.split('\0'))
            changes.extend(sorted(different))
    return changes


def changed_files(wt):
    """Porcelain -z returns raw names; renames contain destination AND source."""
    records = iter(git('status', '--porcelain', '--ignore-submodules=none', '-z', '-uall', cwd=wt).stdout.split('\0'))
    paths = []
    for record in records:
        if not record:
            continue
        paths.append(record[3:])
        if 'R' in record[:2] or 'C' in record[:2]:
            paths.append(next(records))
    return list(dict.fromkeys([*paths, *hidden_index_changes(wt)]))


def check_paths_in_scope(task, paths):
    for name in paths:
        if not any(name == item.rstrip('/') or name.startswith(item.rstrip('/') + '/') for item in task['files']):
            raise RuntimeError(f'Arquivo fora do escopo em {task["id"]}: {name}')


def check_tree_scope(task, wt, before, after):
    paths = git(
        'diff', '--name-only', '--no-renames', '--ignore-submodules=none', '-z', before, after, cwd=wt
    ).stdout.split('\0')
    check_paths_in_scope(task, filter(None, paths))


def check_scope(task, wt, base):
    check_paths_in_scope(task, changed_files(wt))
    check_tree_scope(task, wt, base, 'HEAD')


def protect_untracked_files(before, after):
    """Check added destinations and file/directory replacements, including ignored data."""
    added = git(
        'diff', '--name-only', '--diff-filter=AT', '--no-renames', '--ignore-submodules=none', '-z', before, after
    ).stdout
    for name in filter(None, added.split('\0')):
        path = ROOT
        for part in name.split('/'):
            path = path / part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                path = None
                break
            if path.is_junction() or (stat.S_ISDIR(mode) and (path / '.git').exists()):
                raise RuntimeError(f'Integração atingiria uma junção ou repositório local: {path}')
            if not stat.S_ISDIR(mode):
                break
        if path is None:
            continue
        # No exclusion options: Git includes ignored files and nested repositories.
        # Match the on-disk spelling even on case-insensitive filesystems.
        relative = path.relative_to(ROOT).as_posix()
        collision = git('ls-files', '--others', '--directory', '-z', '--', f':(icase,literal){relative}').stdout
        if collision:
            first_path = collision.partition('\0')[0]
            raise RuntimeError(f'Arquivo local seria sobrescrito: {first_path}')


def branch_ref(wt):
    """Return the full branch ref, or HEAD for a detached checkout."""
    result = git('symbolic-ref', '--quiet', 'HEAD', cwd=wt, check=False)
    if result.returncode == 1:
        return 'HEAD'
    if result.returncode:
        raise RuntimeError(f'Não foi possível ler a branch: {result.stderr[-2000:]}')
    return result.stdout.strip()


def check_git_operation(wt):
    git_dir = Path(git('rev-parse', '--absolute-git-dir', cwd=wt).stdout.rstrip('\r\n'))
    for marker in (
        'MERGE_HEAD',
        'CHERRY_PICK_HEAD',
        'REVERT_HEAD',
        'rebase-apply',
        'rebase-merge',
        'sequencer',
        'BISECT_START',
    ):
        try:
            (git_dir / marker).stat()
        except FileNotFoundError:
            continue
        raise RuntimeError(f'Operação Git pendente no worktree: {marker}')


def check_revision(wt, expected_head, expected_branch=None, recovery=False):
    """Verify both commit and checkout identity before trusting repository state."""
    error_type = RecoveryRequired if recovery else RuntimeError
    try:
        check_git_operation(wt)
        branch = branch_ref(wt)
        changed = (
            (expected_branch is not None and branch != expected_branch)
            or git('rev-parse', 'HEAD', cwd=wt).stdout.strip() != expected_head
            or changed_files(wt)
        )
    except (Exception, KeyboardInterrupt) as error:
        if recovery:
            raise RecoveryRequired(f'Não foi possível verificar branch, HEAD e arquivos: {error}') from error
        raise
    if changed:
        raise error_type('Branch, HEAD ou arquivos do repositório alterados; inspecione as alterações preservadas')
    return branch


@dataclass
class TargetCheckout:
    branch: str
    head: str

    def verify(self):
        check_revision(ROOT, self.head, self.branch, recovery=True)


def run_tests(command, wt, expected_head, recovery=False, expected_branch=None):
    """Tests must preserve the commit, branch (including detached HEAD), and files."""
    expected_branch = check_revision(wt, expected_head, expected_branch, recovery)
    try:
        result = cmd(command, cwd=wt, timeout=600, check=False, cancellable=True)
    except RecoveryRequired:
        # A dirty worktree must not hide an unconfirmed process termination.
        raise
    except (Exception, KeyboardInterrupt):
        check_revision(wt, expected_head, expected_branch, recovery)
        raise
    check_revision(wt, expected_head, expected_branch, recovery)
    return result


def run_agy(prompt, wt):
    # NDJSON stdin avoids Windows command-line limits, including during repairs.
    payload = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n'
    result = cmd(
        [
            'agy',
            '--input-format',
            'stream-json',
            '--output-format',
            'stream-json',
            '--sandbox',
            '--mode',
            'auto',
            '--print-timeout',
            '25m',
        ],
        cwd=wt,
        timeout=1600,
        input_text=payload,
        cancellable=True,
    )
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    results = [event['result'] for event in events if event.get('event') == 'result']
    if len(results) != 1:
        raise RuntimeError('Antigravity não retornou exatamente um resultado terminal')
    return parse_json_output(json.dumps(results[0]), 'agy')


def write_json(path, obj):
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')
        retry_permission_error(tmp.replace, path)
    except (Exception, KeyboardInterrupt) as error:
        # Recovery is required even if its first durable record cannot be saved.
        if obj.get('recovery_required'):
            raise RecoveryRequired(f'Falha ao persistir recuperação pendente: {error}') from error
        raise


def validate(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('tasks'), list):
        raise ValueError('Plano deve ser objeto com lista tasks')
    tasks = plan['tasks']
    if not isinstance(tasks, list) or not tasks or len(tasks) > 100:
        raise ValueError('O plano deve ter entre 1 e 100 tarefas')
    if any(not isinstance(t, dict) or not isinstance(t.get('id'), str) for t in tasks):
        raise ValueError('Cada tarefa precisa de id textual')
    ids = [t['id'] for t in tasks]
    if len({v.casefold() for v in ids}) != len(ids) or not all(SAFE_ID.fullmatch(v) for v in ids):
        raise ValueError('IDs duplicados (sem distinguir maiúsculas/minúsculas) ou inválidos')
    lookup = set(ids)
    for t in tasks:
        if (
            not isinstance(t.get('description'), str)
            or not t['description'].strip()
            or not isinstance(t.get('deps'), list)
        ):
            raise ValueError(f'Tarefa incompleta: {t}')
        if any(not isinstance(d, str) or d not in lookup or d == t['id'] for d in t['deps']):
            raise ValueError(f'Dependência inválida: {t["id"]}')
        for key in ('files', 'acceptance'):
            values = t.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
            ):
                raise ValueError(f'{key} deve ser lista não vazia de strings')
        for name in t['files']:
            parts = name.rstrip('/').split('/')
            if (
                '\\' in name
                or ':' in name
                or any(p in ('', '.', '..') for p in parts)
                or any(p.lower() in ('.git', '.agent-runs') for p in parts)
            ):
                raise ValueError(f'Caminho de escopo inválido: {name}')
    # Detectar ciclos.
    done = set()
    while len(done) < len(tasks):
        possible = {t['id'] for t in tasks if t['id'] not in done and set(t['deps']) <= done}
        if not possible:
            raise ValueError('Ciclo de dependências')
        done |= possible


def parse_json_output(stdout, engine):
    data = json.loads(stdout)
    if engine == 'agy':
        if data.get('status') != 'SUCCESS':
            raise RuntimeError(f'Antigravity: {data.get("status")}: {data.get("error", "")}')
        return str(data.get('response', ''))
    if data.get('is_error'):
        raise RuntimeError('Claude retornou erro')
    return (
        '\n'.join(x.get('text', '') for x in data.get('result', []) if isinstance(x, dict))
        if isinstance(data.get('result'), list)
        else str(data.get('result', ''))
    )


def make_worktree(task, base):
    location = RUN_DIR / f'wt-{task["id"]}'
    if location.exists():
        raise RuntimeError(f'Worktree já existe: {location}; verifique antes de limpar')
    branch = f'agent/{task["id"]}-{uuid.uuid4().hex[:12]}'
    git('worktree', 'add', '-b', branch, str(location), base)
    return location


def run_executor(task, base, context):
    check_stop()
    wt = make_worktree(task, base)
    branch = branch_ref(wt)
    prompt = (
        'Você está implementando UMA tarefa no Git worktree atual. '
        'Leia GEMINI.md quando presente. Preserve a branch atual. Não altere arquitetura, não faça commit/push/deploy. '
        'Não acesse segredos. NÃO altere arquivos fora da lista files. '
        'Se houver conflito de escopo, reporte sem implementar.\n'
        + json.dumps({'task': task, 'context': context}, ensure_ascii=False)
    )
    report = run_agy(prompt, wt)
    check_executor_checkout(wt, base, branch)
    # Verifica modificações fora do escopo, incluindo arquivos ainda não rastreados.
    check_scope(task, wt, base)
    changed = changed_files(wt)
    if not changed:
        raise RuntimeError(f'Gemini não alterou arquivos em {task["id"]}')
    git('add', '--all', '--', '.', cwd=wt)
    git(
        '-c',
        'user.name=Agent Executor',
        '-c',
        'user.email=agent@localhost',
        'commit',
        '-m',
        f'agent: {task["id"]}',
        cwd=wt,
    )
    commit = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
    return {'task': task, 'commit': commit, 'branch': branch, 'worktree': str(wt), 'report': report, 'context': context}


def check_executor_checkout(wt, head, branch):
    check_git_operation(wt)
    if branch_ref(wt) != branch or git('rev-parse', 'HEAD', cwd=wt).stdout.strip() != head:
        raise RuntimeError('Executor alterou a branch ou HEAD do worktree')


def review(task, artifact, test_command):
    wt = Path(artifact['worktree'])
    head = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
    if artifact.get('commit', head) != head:
        raise RuntimeError('HEAD do worktree difere do commit candidato')
    test = run_tests(test_command, wt, head, expected_branch=artifact.get('branch')) if test_command else None
    test_output = (test.stdout + '\n' + test.stderr)[-10000:] if test else 'Nenhum comando de teste configurado'
    diff = git('show', '--format=medium', '--stat', '--ignore-submodules=none', 'HEAD', cwd=wt).stdout
    patch_bytes = git(
        'show', '--format=', '--binary', '--no-textconv', '--no-ext-diff', '--ignore-submodules=none', 'HEAD', cwd=wt
    ).stdout
    # Latin-1 is a reversible mapping even for a patch with mixed encodings.
    for encoding in ('utf-8', 'cp1252', 'latin-1'):
        try:
            patch = patch_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    prompt = (
        'Você é revisor de código. Verifique a task e o diff contra critérios de aceitação. '
        'Responda EXATAMENTE com uma palavra na primeira linha: APPROVED ou REJECTED. '
        'Nas linhas seguintes explique problemas encontrados. Testes não executados NÃO equivalem a testes aprovados. '
        'Não modifique arquivos.\n'
        + json.dumps(
            {
                'task': task,
                'context': artifact.get('context', ''),
                'diff_summary': diff,
                'patch': patch,
                'patch_encoding': encoding,
                'executor_report': artifact['report'],
                'test_exit_code': test.returncode if test else None,
                'test_output': test_output,
            },
            ensure_ascii=False,
        )
    )
    response = cmd(
        [
            'claude',
            '-p',
            '--model',
            'sonnet',
            '--output-format',
            'json',
            '--max-turns',
            '1',
            '--tools',
            '',
            '--strict-mcp-config',
        ],
        cwd=wt,
        timeout=900,
        input_text=prompt,
        cancellable=True,
    )
    verdict = parse_json_output(response.stdout, 'claude').strip()
    approved = verdict.splitlines()[0:1] == ['APPROVED'] and test is not None and test.returncode == 0
    if test is not None and test.returncode:
        verdict += f'\nTestes do worktree falharam (código {test.returncode}):\n{test_output}'
    return approved, verdict, test_output


def execute(plan, max_workers, retries, tests, reservation=None):
    global RUN_DIR, STOP_FILE
    token = reservation or reserve_run()
    run_id = reservation_id(token)
    if json.loads((STORE / 'run.lock').read_text(encoding='utf-8')).get('run_id') != run_id:
        raise RuntimeError('Reserva de execução inválida')
    # mkdir is the single-use handoff. A losing worker must never enter cleanup.
    run_dir = STORE / 'runs' / run_id
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError:
        raise RuntimeError('Reserva já assumida por outro worker') from None
    except (OSError, KeyboardInterrupt) as error:
        raise RecoveryRequired(
            'Não foi possível assumir a reserva; inspecione run.lock e o diretório da execução'
        ) from error
    RUN_DIR = run_dir
    STOP_FILE = STORE / f'stop-{run_id}'
    keep_lock = False
    state_initialized = False
    try:
        # CLI e MCP começam com estado próprio antes de qualquer preparação.
        write_json(STORE / 'status.json', {'state': 'starting', 'run_id': run_id, 'statuses': {}, 'details': {}})
        state_initialized = True
        if isinstance(plan, Path):
            plan = json.loads(plan.read_text(encoding='utf-8'))
        # Publish the current CLI or MCP plan only after proving lock ownership.
        write_json(STORE / 'submitted-plan.json', plan)
        check_stop()
        result = _execute(plan, max_workers, retries, tests)
        # Until the final state is read, recovery may already be pending.
        keep_lock = True
        state = json.loads((STORE / 'status.json').read_text(encoding='utf-8'))
        keep_lock = state.get('recovery_required', False)
        state['state'] = 'completed' if all(s == 'approved' for s in result.values()) else 'failed'
        state['run_id'] = run_id
        write_json(STORE / 'status.json', state)
        return result
    except (Exception, KeyboardInterrupt) as error:
        keep_lock = keep_lock or isinstance(error, RecoveryRequired)
        path = STORE / 'status.json'
        state = {'statuses': {}, 'details': {}}
        try:
            if isinstance(error, IntegrationStateError):
                state = error.state
            elif state_initialized:
                state = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            keep_lock = True
        # A later persistence error must never clear an earlier recovery flag.
        keep_lock = keep_lock or state.get('recovery_required', False)
        cancelled = isinstance(error, RunCancelled) and not keep_lock
        state.update(
            state='cancelled' if cancelled else 'failed',
            run_id=run_id,
            error=str(error) or type(error).__name__,
            recovery_required=keep_lock,
        )
        for tid, value in state.get('statuses', {}).items():
            if value in ('pending', 'running', 'review'):
                state['statuses'][tid] = 'cancelled' if cancelled else 'blocked'
        write_json(path, state)
        raise
    finally:
        STOP_FILE = None
        if not keep_lock:
            release_run(token)


def _execute(plan, max_workers, retries, tests):
    validate(plan)
    if max_workers not in range(1, 9) or retries not in range(4):
        raise ValueError('parallel deve ser 1..8, retries 0..3')
    if not tests:
        raise RuntimeError('Configure um comando de testes via --test (obrigatório)')
    if not shutil.which('agy') or not shutil.which('claude'):
        raise RuntimeError('Instale/autentique agy e claude; ambos devem estar no PATH')
    if changed_files(ROOT):
        raise RuntimeError('Repositório precisa estar limpo antes de executar')
    target = TargetCheckout(branch_ref(ROOT), git('rev-parse', 'HEAD').stdout.strip())
    try:
        return _run_tasks(plan, max_workers, retries, tests, target)
    except RecoveryRequired:
        raise
    except (Exception, KeyboardInterrupt):
        # An interruption may arrive between two Git calls. Only release the
        # reservation if the target still matches the last verified revision.
        target.verify()
        raise


def persist_integration_state(statuses, details):
    state = {'statuses': statuses, 'details': details}
    try:
        write_json(STORE / 'status.json', state)
    except (Exception, KeyboardInterrupt) as error:
        # The previous on-disk snapshot can predate the cherry-pick.
        raise IntegrationStateError(state, error) from error


def _run_tasks(plan, max_workers, retries, tests, target):
    statuses = {t['id']: 'pending' for t in plan['tasks']}
    details = {}
    write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
    for _ in range(len(plan['tasks'])):
        ready = [
            t
            for t in plan['tasks']
            if statuses[t['id']] == 'pending' and all(statuses[x] == 'approved' for x in t['deps'])
        ]
        if not ready:
            break
        # Evita tarefas concorrentes que anunciam ownership sobreposto.
        batch = []
        owned = set()
        for t in ready:
            files = set(t.get('files', []))
            if any(
                x == y or x.startswith(y.rstrip('/') + '/') or y.startswith(x.rstrip('/') + '/')
                for x in files
                for y in owned
            ):
                continue
            batch.append(t)
            owned |= files
            if len(batch) >= max_workers:
                break
        target.verify()
        check_stop()
        base = target.head
        context = 'Base do código: ' + base
        if plan.get('goal'):
            context += '\nObjetivo geral: ' + str(plan['goal'])
        for t in batch:
            statuses[t['id']] = 'running'
        write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
        jobs = {}
        artifacts = []
        recovery_error = None
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        try:
            with pool:
                for t in batch:
                    jobs[pool.submit(run_executor, t, base, context)] = t
                for future in concurrent.futures.as_completed(jobs):
                    t = jobs[future]
                    try:
                        artifact = future.result()
                        statuses[t['id']] = 'review'
                        artifacts.append(artifact)
                    except RunCancelled:
                        statuses[t['id']] = 'cancelled'
                    except Exception as e:
                        statuses[t['id']] = 'failed'
                        details[t['id']] = {'error': str(e)}
                        if isinstance(e, RecoveryRequired):
                            recovery_error = e
        except (Exception, KeyboardInterrupt) as interruption:
            # Submission/collection failures must also inspect existing workers.
            try:
                pool.shutdown(wait=True)
            except (Exception, KeyboardInterrupt) as error:
                raise RecoveryRequired('Espera dos workers interrompida; encerramento não confirmado') from error
            for future, t in jobs.items():
                error = future.exception()
                if error is not None:
                    statuses[t['id']] = 'failed'
                    details[t['id']] = {'error': str(error)}
                    if isinstance(error, RecoveryRequired):
                        recovery_error = error
            if recovery_error is not None:
                raise RecoveryRequired(
                    f'Execução interrompida com recuperação pendente: {recovery_error}'
                ) from interruption
            if len(jobs) != len(batch):
                raise RecoveryRequired('Submissão dos workers interrompida; resultados incompletos') from interruption
            raise
        if recovery_error is not None:
            for tid, state in statuses.items():
                if state in ('pending', 'review', 'running'):
                    statuses[tid] = 'blocked'
            write_json(STORE / 'status.json', {'statuses': statuses, 'details': details, 'recovery_required': True})
            return statuses
        write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
        check_stop()
        for artifact in artifacts:
            t = artifact['task']
            details[t['id']] = {'worktree': artifact['worktree'], 'commit': artifact['commit']}
            feedback = ''
            for attempt in range(retries + 1):
                try:
                    check_stop()
                    check_scope(t, Path(artifact['worktree']), base)
                    ok, feedback, _ = review(t, artifact, tests)
                    if ok:
                        target.verify()
                        candidate = artifact['commit']
                        # Earlier tasks in a parallel batch may have changed the
                        # destination tree. Inspect the actual three-way merge.
                        merged_tree = (
                            candidate
                            if target.head == base
                            else git('merge-tree', '--write-tree', target.head, candidate).stdout.strip()
                        )
                        check_tree_scope(t, ROOT, target.head, merged_tree)
                        protect_untracked_files(target.head, merged_tree)
                        details[t['id']].update(commit=candidate, target_before=target.head, review=feedback)
                        write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
                        check_stop()
                        try:
                            applied = git('cherry-pick', artifact['commit'], check=False)
                        except (Exception, KeyboardInterrupt) as error:
                            # O comando pode ter aplicado o commit antes de perdermos
                            # seu resultado. Não é seguro integrar outra tarefa.
                            raise RecoveryRequired(f'Resultado do cherry-pick desconhecido: {error}') from error
                        if applied.returncode:
                            try:
                                # Conflicts may dirty the tree, but abort must stay on the original branch.
                                if branch_ref(ROOT) != target.branch:
                                    raise RecoveryRequired('Branch de destino alterada antes do abort')
                                aborted = git('cherry-pick', '--abort', check=False)
                            except (Exception, KeyboardInterrupt) as error:
                                raise RecoveryRequired(f'Falha ao executar abort de cherry-pick: {error}') from error
                            if aborted.returncode:
                                raise RecoveryRequired('Falha ao abortar cherry-pick; revisão manual necessária')
                            target.verify()
                            raise RuntimeError('Conflito ao integrar commit; requer intervenção ou replanejamento')
                        # Testes de integração na branch principal após cherry-pick.
                        try:
                            integrated_head = git('rev-parse', 'HEAD').stdout.strip()
                        except (Exception, KeyboardInterrupt) as error:
                            raise RecoveryRequired(f'Não foi possível identificar o HEAD integrado: {error}') from error
                        details[t['id']]['integrated_commit'] = integrated_head
                        persist_integration_state(statuses, details)
                        try:
                            check_tree_scope(t, ROOT, target.head, integrated_head)
                        except (Exception, KeyboardInterrupt) as error:
                            raise RecoveryRequired(f'Escopo do commit integrado não confirmado: {error}') from error
                        try:
                            integrated = run_tests(
                                tests, ROOT, integrated_head, recovery=True, expected_branch=target.branch
                            )
                            check_stop()
                            if integrated.returncode:
                                raise RuntimeError(
                                    'Testes de integração falharam: ' + (integrated.stdout + integrated.stderr)[-2000:]
                                )
                        except RecoveryRequired:
                            raise
                        except (Exception, KeyboardInterrupt) as integration_error:
                            check_revision(ROOT, integrated_head, target.branch, recovery=True)
                            try:
                                protect_untracked_files(integrated_head, target.head)
                                revert = git('revert', '--no-edit', integrated_head, check=False)
                            except (Exception, KeyboardInterrupt) as error:
                                raise RecoveryRequired(f'Falha ao executar revert: {error}') from error
                            if revert.returncode:
                                raise RecoveryRequired(
                                    'Testes falharam e o revert automático também; revisão manual necessária'
                                ) from integration_error
                            try:
                                reverted_head = git('rev-parse', 'HEAD').stdout.strip()
                            except (Exception, KeyboardInterrupt) as error:
                                raise RecoveryRequired(
                                    f'Não foi possível identificar o HEAD após revert: {error}'
                                ) from error
                            check_revision(ROOT, reverted_head, target.branch, recovery=True)
                            details[t['id']]['revert_commit'] = reverted_head
                            persist_integration_state(statuses, details)
                            target.head = reverted_head
                            if isinstance(integration_error, (KeyboardInterrupt, RunCancelled)):
                                raise
                            raise RuntimeError(f'Integração revertida: {integration_error}') from integration_error
                        statuses[t['id']] = 'approved'
                        persist_integration_state(statuses, details)
                        target.head = integrated_head
                        break
                    if attempt == retries:
                        raise RuntimeError('Rejeitado após tentativas: ' + feedback[-2000:])
                    wt = Path(artifact['worktree'])
                    correction = run_agy(
                        'Corrija a tarefa abaixo considerando a revisão. Preserve a branch atual. Não faça commit/push/deploy. Não modifique arquivos fora de files.\n'
                        + json.dumps({'task': t, 'context': context, 'review': feedback}, ensure_ascii=False),
                        wt,
                    )
                    artifact['report'] += '\nCorreção: ' + correction
                    check_executor_checkout(wt, artifact['commit'], artifact['branch'])
                    check_scope(t, wt, base)
                    modified = changed_files(wt)
                    if modified:
                        git('add', '--all', '--', '.', cwd=wt)
                        git(
                            '-c',
                            'user.name=Agent Executor',
                            '-c',
                            'user.email=agent@localhost',
                            'commit',
                            '-m',
                            f'agent: repair {t["id"]}',
                            cwd=wt,
                        )
                        # Tem que integrar a sequência inteira, não apenas o último commit.
                        # Faz squash da branch sobre base original no worktree.
                        original_base = base
                        git('reset', '--soft', original_base, cwd=wt)
                        git(
                            '-c',
                            'user.name=Agent Executor',
                            '-c',
                            'user.email=agent@localhost',
                            'commit',
                            '-m',
                            f'agent: {t["id"]} revised',
                            cwd=wt,
                        )
                        artifact['commit'] = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
                except (RunCancelled, IntegrationStateError):
                    raise
                except Exception as e:
                    statuses[t['id']] = 'failed'
                    details[t['id']].update(error=str(e), review=feedback)
                    if isinstance(e, RecoveryRequired):
                        for tid, state in statuses.items():
                            if state in ('pending', 'review', 'running'):
                                statuses[tid] = 'blocked'
                        write_json(
                            STORE / 'status.json', {'statuses': statuses, 'details': details, 'recovery_required': True}
                        )
                        return statuses
                    break
            if statuses[t['id']] != 'approved':
                write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
    while True:
        blocked = [
            t['id']
            for t in plan['tasks']
            if statuses[t['id']] == 'pending' and any(statuses[x] in ('failed', 'blocked') for x in t['deps'])
        ]
        if not blocked:
            break
        for tid in blocked:
            statuses[tid] = 'blocked'
    write_json(STORE / 'status.json', {'statuses': statuses, 'details': details})
    target.verify()
    return statuses


def main():
    p = argparse.ArgumentParser()
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('plan', type=Path, nargs='?')
    action.add_argument('--stop', action='store_true', help='Solicita o encerramento da execução ativa')
    p.add_argument('--parallel', type=int, default=3)
    p.add_argument('--retries', type=int, default=2)
    p.add_argument('--reservation', help=argparse.SUPPRESS)
    p.add_argument(
        '--test',
        nargs=argparse.REMAINDER,
        default=None,
        help='Última opção. Comando confiável executado com as permissões do worker antes da revisão e na integração.',
    )
    args = p.parse_args()
    if args.stop:
        print(request_stop())
        return
    if args.parallel < 1 or args.parallel > 8 or args.retries not in range(4):
        p.error('parallel deve ser 1..8, retries 0..3')
    if args.reservation:
        result = execute(args.plan, args.parallel, args.retries, args.test, reservation=args.reservation)
    else:
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        result = execute(plan, args.parallel, args.retries, args.test)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(x != 'approved' for x in result.values()):
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except RunCancelled as error:
        sys.exit(str(error))
