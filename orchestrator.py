"""MVP local de orquestração Claude -> Antigravity, para um repositório Git confiável.
Sem deploy/push e sem bypass de permissões. Requer CLI `agy` e `claude` autenticados.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
STORE = ROOT / '.agent-runs'
STORE.mkdir(exist_ok=True)
SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
RUN_DIR = STORE


class RecoveryRequired(RuntimeError):
    """Operação Git não pôde ser desfeita; nenhuma outra tarefa deve integrar."""


def reserve_run():
    """Reserva atômica; após crash, exige inspeção e recuperação manual."""
    token = uuid.uuid4().hex
    try:
        with (STORE / 'run.lock').open('x', encoding='utf-8') as handle:
            json.dump({'token': token, 'pid': os.getpid()}, handle)
    except FileExistsError:
        raise RuntimeError('Já existe execução reservada; consulte get_run_status e o procedimento de recuperação') from None
    return token


def release_run(token):
    path = STORE / 'run.lock'
    if path.exists() and json.loads(path.read_text(encoding='utf-8'))['token'] == token:
        path.unlink()


def cmd(args, cwd=ROOT, timeout=1800, check=True):
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout, encoding='utf-8', errors='replace')
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} exited {p.returncode}: {p.stderr[-2000:]}')
    return p


def git(*args, cwd=ROOT, check=True):
    return cmd(['git', *args], cwd=cwd, timeout=180, check=check)


def write_json(path, obj):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def validate(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('tasks'), list):
        raise ValueError('Plano deve ser objeto com lista tasks')
    tasks = plan['tasks']
    if not isinstance(tasks, list) or not tasks or len(tasks) > 100:
        raise ValueError('O plano deve ter entre 1 e 100 tarefas')
    if any(not isinstance(t, dict) or not isinstance(t.get('id'), str) for t in tasks):
        raise ValueError('Cada tarefa precisa de id textual')
    ids = [t['id'] for t in tasks]
    if len(set(ids)) != len(ids) or not all(SAFE_ID.fullmatch(v) for v in ids):
        raise ValueError('IDs duplicados/inválidos')
    lookup = set(ids)
    for t in tasks:
        if not isinstance(t.get('description'), str) or not t['description'].strip() or not isinstance(t.get('deps'), list):
            raise ValueError(f'Tarefa incompleta: {t}')
        if any(not isinstance(d, str) or d not in lookup or d == t['id'] for d in t['deps']):
            raise ValueError(f'Dependência inválida: {t["id"]}')
        for key in ('files', 'acceptance'):
            values = t.get(key)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f'{key} deve ser lista não vazia de strings')
        for name in t['files']:
            parts = name.rstrip('/').split('/')
            if ('\\' in name or ':' in name or any(p in ('', '.', '..') for p in parts)
                    or any(p.lower() in ('.git', '.agent-runs') for p in parts)):
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
    return '\n'.join(x.get('text', '') for x in data.get('result', []) if isinstance(x, dict)) if isinstance(data.get('result'), list) else str(data.get('result', ''))


def make_worktree(task, base):
    location = RUN_DIR / f'wt-{task["id"]}'
    if location.exists():
        raise RuntimeError(f'Worktree já existe: {location}; verifique antes de limpar')
    branch = f'agent/{task["id"]}-{uuid.uuid4().hex[:12]}'
    git('worktree', 'add', '-b', branch, str(location), base)
    return location


def run_executor(task, base, context):
    wt = make_worktree(task, base)
    prompt = ('Você está implementando UMA tarefa no Git worktree atual. '
              'Leia GEMINI.md quando presente. Não altere arquitetura, não faça commit/push/deploy. '
              'Não acesse segredos. NÃO altere arquivos fora da lista files. '
              'Se houver conflito de escopo, reporte sem implementar.\n' +
              json.dumps({'task':task,'context':context}, ensure_ascii=False))
    result = cmd(['agy', '-p', prompt, '--output-format', 'json', '--print-timeout', '25m'], cwd=wt, timeout=1600)
    report = parse_json_output(result.stdout, 'agy')
    # Verifica modificações fora do escopo, incluindo arquivos ainda não rastreados.
    lines = git('status', '--porcelain', '-uall', cwd=wt).stdout.splitlines()
    changed = [x[3:] for x in lines if len(x)>3]
    allowed = task.get('files', [])
    for f in changed:
        normalized = f.replace('\\', '/')
        if not any(normalized == item.rstrip('/') or normalized.startswith(item.rstrip('/') + '/') for item in allowed):
            raise RuntimeError(f'Arquivo fora do escopo em {task["id"]}: {f}')
    if not changed:
        raise RuntimeError(f'Gemini não alterou arquivos em {task["id"]}')
    git('add', '--', *changed, cwd=wt)
    git('-c', 'user.name=Agent Executor', '-c', 'user.email=agent@localhost', 'commit', '-m', f'agent: {task["id"]}', cwd=wt)
    commit = git('rev-parse', 'HEAD', cwd=wt).stdout.strip()
    return {'task':task, 'commit':commit, 'worktree':str(wt), 'report':report}


def review(task, artifact, test_command):
    wt = Path(artifact['worktree'])
    test = cmd(test_command, cwd=wt, timeout=600, check=False) if test_command else None
    test_output = (test.stdout + '\n' + test.stderr)[-10000:] if test else 'Nenhum comando de teste configurado'
    diff = git('show', '--format=medium', '--stat', 'HEAD', cwd=wt).stdout
    patch = git('show', '--format=', 'HEAD', cwd=wt).stdout[:60000]
    prompt = ('Você é revisor de código. Verifique a task e o diff contra critérios de aceitação. '
              'Responda EXATAMENTE com uma palavra na primeira linha: APPROVED ou REJECTED. '
              'Nas linhas seguintes explique problemas encontrados. Testes não executados NÃO equivalem a testes aprovados. '
              'Não modifique arquivos.\n' + json.dumps({'task': task, 'diff_summary':diff,
                     'patch':patch, 'executor_report':artifact['report'],
                     'test_exit_code':test.returncode if test else None,
                     'test_output':test_output}, ensure_ascii=False))
    response = cmd(['claude', '-p', prompt, '--output-format', 'json', '--max-turns', '1'], cwd=wt, timeout=900)
    verdict = parse_json_output(response.stdout, 'claude').strip()
    approved = verdict.startswith('APPROVED') and (test is None or test.returncode == 0)
    return approved, verdict, test_output


def execute(plan, max_workers, retries, tests, reservation=None):
    global RUN_DIR
    token = reservation or reserve_run()
    try:
        if json.loads((STORE / 'run.lock').read_text(encoding='utf-8'))['token'] != token:
            raise RuntimeError('Reserva de execução inválida')
        RUN_DIR = STORE / 'runs' / token
        RUN_DIR.mkdir(parents=True)
        return _execute(plan, max_workers, retries, tests)
    finally:
        release_run(token)


def _execute(plan, max_workers, retries, tests):
    validate(plan)
    if max_workers not in range(1, 9) or retries not in range(4):
        raise ValueError('parallel deve ser 1..8, retries 0..3')
    if not tests:
        raise RuntimeError('Configure um comando de testes via --test (obrigatório)')
    if not shutil.which('agy') or not shutil.which('claude'):
        raise RuntimeError('Instale/autentique agy e claude; ambos devem estar no PATH')
    if git('status', '--porcelain').stdout.strip():
        raise RuntimeError('Repositório precisa estar limpo antes de executar')
    statuses = {t['id']: 'pending' for t in plan['tasks']}
    details = {}
    write_json(STORE / 'status.json', {'statuses':statuses, 'details':details})
    for _ in range(len(plan['tasks'])):
        ready = [t for t in plan['tasks'] if statuses[t['id']]=='pending' and all(statuses[x]=='approved' for x in t['deps'])]
        if not ready:
            break
        # Evita tarefas concorrentes que anunciam ownership sobreposto.
        batch = []
        owned = set()
        for t in ready:
            files = set(t.get('files', []))
            if any(x == y or x.startswith(y.rstrip('/') + '/') or y.startswith(x.rstrip('/') + '/') for x in files for y in owned):
                continue
            batch.append(t)
            owned |= files
            if len(batch) >= max_workers:
                break
        base = git('rev-parse', 'HEAD').stdout.strip()
        context = 'Base do código: ' + base
        for t in batch:
            statuses[t['id']] = 'running'
        write_json(STORE/'status.json', {'statuses':statuses,'details':details})
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            jobs = {pool.submit(run_executor, t, base, context):t for t in batch}
            artifacts=[]
            for future in concurrent.futures.as_completed(jobs):
                t=jobs[future]
                try:
                    artifact = future.result()
                    statuses[t['id']]='review'
                    artifacts.append(artifact)
                except Exception as e:
                    statuses[t['id']]='failed'
                    details[t['id']]={'error':str(e)}
        write_json(STORE/'status.json', {'statuses':statuses,'details':details})
        for artifact in artifacts:
            t = artifact['task']
            details[t['id']] = {'worktree': artifact['worktree'], 'commit': artifact['commit']}
            feedback=''
            for attempt in range(retries+1):
                try:
                    ok, feedback, output = review(t, artifact, tests)
                    if ok:
                        try:
                            applied=git('cherry-pick', artifact['commit'], check=False)
                        except Exception as error:
                            # O comando pode ter aplicado o commit antes de perdermos
                            # seu resultado. Não é seguro integrar outra tarefa.
                            raise RecoveryRequired(f'Resultado do cherry-pick desconhecido: {error}') from error
                        if applied.returncode:
                            try:
                                aborted = git('cherry-pick','--abort', check=False)
                            except Exception as error:
                                raise RecoveryRequired(f'Falha ao executar abort de cherry-pick: {error}') from error
                            if aborted.returncode:
                                raise RecoveryRequired('Falha ao abortar cherry-pick; revisão manual necessária')
                            raise RuntimeError('Conflito ao integrar commit; requer intervenção ou replanejamento')
                        # Testes de integração na branch principal após cherry-pick.
                        try:
                            integrated=cmd(tests, timeout=600, check=False)
                            if integrated.returncode:
                                raise RuntimeError('Testes de integração falharam: ' + (integrated.stdout + integrated.stderr)[-2000:])
                        except Exception as integration_error:
                            try:
                                revert = git('revert','--no-edit','HEAD',check=False)
                            except Exception as error:
                                raise RecoveryRequired(f'Falha ao executar revert: {error}') from error
                            if revert.returncode:
                                raise RecoveryRequired('Testes falharam e o revert automático também; revisão manual necessária')
                            raise RuntimeError(f'Integração revertida: {integration_error}') from integration_error
                        statuses[t['id']]='approved'
                        details[t['id']].update(commit=artifact['commit'], review=feedback)
                        break
                    if attempt == retries:
                        raise RuntimeError('Rejeitado após tentativas: '+feedback[-2000:])
                    wt=Path(artifact['worktree'])
                    correction=cmd(['agy','-p','Corrija sua tarefa considerando a revisão a seguir. Não faça commit. Não modifique arquivos fora do escopo.\n'+feedback,
                                    '--output-format','json','--print-timeout','25m'],cwd=wt,timeout=1600)
                    artifact['report'] += '\nCorreção: '+parse_json_output(correction.stdout,'agy')
                    dirty=git('status','--porcelain','-uall',cwd=wt).stdout.splitlines()
                    modified=[x[3:] for x in dirty if len(x)>3]
                    if modified:
                        for f in modified:
                            if not any(f == x.rstrip('/') or f.startswith(x.rstrip('/') + '/') for x in t.get('files', [])):
                                raise RuntimeError('Correção fora de escopo: '+f)
                        git('add','--',*modified,cwd=wt)
                        git('-c','user.name=Agent Executor','-c','user.email=agent@localhost','commit','-m',f'agent: repair {t["id"]}',cwd=wt)
                        # Tem que integrar a sequência inteira, não apenas o último commit.
                        # Faz squash da branch sobre base original no worktree.
                        original_base=base
                        git('reset','--soft',original_base,cwd=wt)
                        git('-c','user.name=Agent Executor','-c','user.email=agent@localhost','commit','-m',f'agent: {t["id"]} revised',cwd=wt)
                        artifact['commit']=git('rev-parse','HEAD',cwd=wt).stdout.strip()
                except Exception as e:
                    statuses[t['id']]='failed'
                    details[t['id']].update(error=str(e), review=feedback)
                    if isinstance(e, RecoveryRequired):
                        for tid, state in statuses.items():
                            if state in ('pending', 'review', 'running'):
                                statuses[tid] = 'blocked'
                        write_json(STORE/'status.json', {'statuses':statuses,'details':details})
                        return statuses
                    break
            write_json(STORE/'status.json', {'statuses':statuses,'details':details})
    while True:
        blocked = [t['id'] for t in plan['tasks'] if statuses[t['id']] == 'pending'
                   and any(statuses[x] in ('failed', 'blocked') for x in t['deps'])]
        if not blocked:
            break
        for tid in blocked:
            statuses[tid] = 'blocked'
    write_json(STORE/'status.json', {'statuses':statuses,'details':details})
    return statuses


def main():
    p=argparse.ArgumentParser()
    p.add_argument('plan', type=Path)
    p.add_argument('--parallel',type=int,default=3)
    p.add_argument('--retries',type=int,default=2)
    p.add_argument('--reservation', help=argparse.SUPPRESS)
    p.add_argument('--test',nargs=argparse.REMAINDER,default=None,help='Última opção: comando e todos os argumentos de teste')
    args=p.parse_args()
    if args.parallel < 1 or args.parallel > 8 or args.retries not in range(4):
        p.error('parallel deve ser 1..8, retries 0..3')
    if args.reservation:
        try:
            plan=json.loads(args.plan.read_text(encoding='utf-8'))
        except Exception:
            release_run(args.reservation)
            raise
        result=execute(plan,args.parallel,args.retries,args.test,reservation=args.reservation)
    else:
        plan=json.loads(args.plan.read_text(encoding='utf-8'))
        result=execute(plan,args.parallel,args.retries,args.test)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if any(x!='approved' for x in result.values()):
        sys.exit(1)

if __name__=='__main__':
    main()
