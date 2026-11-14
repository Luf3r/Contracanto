"""Fluxo completo com Git e subprocessos reais, substituindo apenas os modelos."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import orchestrator as o


FAKE_CLI = '''import json,sys
from pathlib import Path
engine=sys.argv[1]
text=sys.stdin.read()
if engine == 'claude':
    print(json.dumps({'result':'APPROVED'}))
else:
    prompt=json.loads(text)['message']['content']
    data=json.loads(prompt.split('\\n',1)[1])
    task=data['task']
    content='bad\\n' if task['id']=='A' and 'context' in data else 'ok\\n'
    Path(task['files'][0]).write_text(content,encoding='utf-8')
    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'done'}}))
'''


class WorkflowTests(unittest.TestCase):
    def test_parallel_dependencies_and_repair_integrate_with_real_git(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            root = Path(directory)
            original_cmd = o.cmd
            def git(*args, cwd=None, check=True):
                return original_cmd(['git', *args], cwd=cwd or root, check=check)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@localhost')
            (root / '.gitignore').write_text('.agent-runs/\n')
            (root / 'fake_cli.py').write_text(FAKE_CLI, encoding='utf-8')
            (root / 'check.py').write_text("from pathlib import Path\nassert all(p.read_text() == 'ok\\n' for p in Path('.').glob('*.txt'))\n")
            git('add', '.')
            git('commit', '-m', 'baseline')
            store = root / '.agent-runs'
            store.mkdir()
            barrier = threading.Barrier(2)
            def command(args, **kwargs):
                kwargs.setdefault('cwd', root)
                if args[0] in ('agy', 'claude'):
                    if args[0] == 'agy':
                        data = json.loads(json.loads(kwargs['input_text'])['message']['content'].split('\n', 1)[1])
                        if 'context' in data and data['task']['id'] in ('A', 'B'):
                            barrier.wait(timeout=10)
                    args = [sys.executable, str(root / 'fake_cli.py'), *args]
                return original_cmd(args, **kwargs)
            tasks = [dict(id=tid, description='Create file', deps=deps, files=[filename], acceptance=['Contains ok'])
                     for tid, deps, filename in [('A', [], 'ação espaço.txt'), ('B', [], 'B.txt'), ('C', ['A', 'B'], 'C.txt')]]
            with contextlib.ExitStack() as stack:
                for name, value in [('STORE', store), ('git', git), ('cmd', command)]:
                    stack.enter_context(patch.object(o, name, value))
                stack.enter_context(patch.object(o.shutil, 'which', return_value='test-cli'))
                states = o.execute({'tasks': tasks}, 2, 1, [sys.executable, 'check.py'])
            self.assertEqual(states, {'A': 'approved', 'B': 'approved', 'C': 'approved'})
            self.assertEqual(json.loads((store / 'status.json').read_text())['state'], 'completed')
            self.assertIn('revised', git('log', '--oneline').stdout)
            for name in ('ação espaço.txt', 'B.txt', 'C.txt'):
                self.assertEqual((root / name).read_text(), 'ok\n')
            self.assertEqual(git('status', '--porcelain').stdout, '')
            for wt in (store / 'runs').glob('*/wt-*'):
                git('worktree', 'remove', str(wt))
