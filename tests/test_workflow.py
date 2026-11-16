"""Fluxo completo com Git e subprocessos reais, substituindo apenas os modelos."""

import contextlib
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import gitops, runner
from contracanto import process as proc

FAKE_CLI = """import json,sys
from pathlib import Path
engine=sys.argv[1]
text=sys.stdin.read()
if engine == 'claude':
    print(json.dumps({'result':json.dumps({'verdict':'APPROVED'})}))
else:
    prompt=json.loads(text)['message']['content']
    data=json.loads(prompt.split('\\n',1)[1])
    task=data['task']
    content='bad\\n' if task['id']=='A' and 'review' not in data else 'ok\\n'
    Path(task['files'][0]).write_text(content,encoding='utf-8')
    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'done'}}))
"""


class WorkflowTests(unittest.TestCase):
    def test_parallel_dependencies_and_repair_integrate_with_real_git(self):
        self.run_workflow()

    def test_goal_reaches_implementation_review_and_repair(self):
        self.run_workflow(goal='Preservar compatibilidade e documentação em português.')

    def run_workflow(self, goal=None):
        with support.temp_dir() as directory:
            root = Path(directory)
            original_cmd = proc.run
            git = support.init_repository(root)
            (root / 'fake_cli.py').write_text(FAKE_CLI, encoding='utf-8')
            (root / 'check.py').write_text(
                "from pathlib import Path\nassert all(p.read_text() == 'ok\\n' for p in Path('.').glob('*.txt'))\n"
            )
            git('add', '.')
            git('commit', '-m', 'baseline')
            store = root / '.agent-runs'
            store.mkdir()
            barrier = threading.Barrier(2)
            prompts = []

            def command(args, **kwargs):
                kwargs.setdefault('cwd', root)
                if args[0] in ('agy', 'claude'):
                    prompt = kwargs['input_data']
                    if args[0] == 'agy':
                        prompt = json.loads(prompt)['message']['content']
                    data = json.loads(prompt.split('\n', 1)[1])
                    stage = 'review' if args[0] == 'claude' else ('repair' if 'review' in data else 'implementation')
                    prompts.append((stage, data))
                    if args[0] == 'agy':
                        if stage == 'implementation' and data['task']['id'] in ('A', 'B'):
                            barrier.wait(timeout=10)
                    args = [sys.executable, str(root / 'fake_cli.py'), *args]
                return original_cmd(args, **kwargs)

            tasks = [
                dict(id=tid, description='Create file', deps=deps, files=[filename], acceptance=['Contains ok'])
                for tid, deps, filename in [
                    ('A', [], 'ação espaço.txt'),
                    ('B', [], 'B.txt'),
                    ('C', ['A', 'B'], 'C.txt'),
                ]
            ]
            with contextlib.ExitStack() as stack:
                ws = support.workspace(store, root)
                stack.enter_context(patch.object(gitops, 'git', git))
                stack.enter_context(patch.object(proc, 'run', command))
                stack.enter_context(patch.object(runner.shutil, 'which', return_value='test-cli'))
                plan = {'tasks': tasks}
                if goal is not None:
                    plan['goal'] = goal
                states = runner.execute(ws, plan, 2, 1, [sys.executable, 'check.py'])
            self.assertEqual(states, {'A': 'approved', 'B': 'approved', 'C': 'approved'})
            self.assertEqual(support.read_status(ws)['state'], 'completed')
            # Um commit por tarefa, mesmo para a que foi corrigida, com a descrição no assunto.
            subjects = git('log', '--format=%s').stdout.splitlines()
            self.assertEqual(len(subjects), 4, subjects)
            self.assertEqual({subject.split(':')[0] for subject in subjects[:3]}, {'A', 'B', 'C'})
            self.assertIn('do Contracanto', git('log', '-1', '--format=%b').stdout)
            for name in ('ação espaço.txt', 'B.txt', 'C.txt'):
                self.assertEqual((root / name).read_text(), 'ok\n')
            self.assertEqual(git('status', '--porcelain').stdout, '')
            for wt in (store / 'runs').glob('*/wt-*'):
                git('worktree', 'remove', str(wt))
            if goal is not None:
                self.assertEqual({stage for stage, _ in prompts}, {'implementation', 'review', 'repair'})
                for stage, data in prompts:
                    with self.subTest(stage=stage, task=data['task']['id']):
                        self.assertIn(goal, data.get('context', ''))
