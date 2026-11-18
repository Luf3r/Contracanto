"""O que o revisor e o pedido de correção recebem, com Git real e modelos simulados."""

import json
import subprocess
import sys
from unittest.mock import patch

import support

from contracanto import agents, runner
from contracanto import process as proc


class ReviewGateTests(support.RepositoryTestCase):
    def test_reviewer_receives_byte_exact_legacy_text_patch(self):
        with self.repository() as (root, store, base, task, git):
            (root / '.gitattributes').write_text('allowed.txt -text\n')
            content = b'revis\xe3o \x80\r\n'
            (root / 'allowed.txt').write_bytes(content)
            git('add', 'allowed.txt', '.gitattributes')
            git('commit', '-m', 'legacy text')
            original_cmd = proc.run
            payload = {}

            def command(args, **kwargs):
                if args[0] == 'claude':
                    payload.update(json.loads(kwargs['input_data'].split('\n', 1)[1]))
                return original_cmd(args, **kwargs)

            with patch.object(proc, 'run', side_effect=command):
                agents.review(
                    agents.Artifact(task=task, worktree=str(root), report='done'), [sys.executable, '-c', 'pass']
                )
            git('switch', '--detach', base)
            subprocess.run(
                ['git', 'apply', '-'],
                cwd=root,
                check=True,
                capture_output=True,
                input=payload['patch'].encode(payload['patch_encoding']),
            )
            self.assertEqual((root / 'allowed.txt').read_bytes(), content)

    def test_reviewer_receives_applicable_binary_patch(self):
        with self.repository() as (root, store, base, task, git):
            data = bytes(range(256))
            (root / 'allowed.txt').write_bytes(data)
            git('add', 'allowed.txt')
            git('commit', '-m', 'binary asset')
            original_cmd = proc.run
            patches = []

            def command(args, **kwargs):
                if args[0] == 'claude':
                    patches.append(json.loads(kwargs['input_data'].split('\n', 1)[1])['patch'])
                return original_cmd(args, **kwargs)

            with patch.object(proc, 'run', side_effect=command):
                agents.review(
                    agents.Artifact(task=task, worktree=str(root), report='done'), [sys.executable, '-c', 'pass']
                )
            git('switch', '--detach', base)
            applied = subprocess.run(['git', 'apply', '-'], input=patches[0].encode(), cwd=root, capture_output=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual((root / 'allowed.txt').read_bytes(), data)

    def test_failed_test_diagnostic_reaches_repair_and_final_status(self):
        with self.repository() as (root, store, base, task, git):
            agent = agents.run_agy
            repairs = []

            def capture_repair(prompt, wt, stop=None):
                request = json.loads(prompt.split('\n', 1)[1])
                if 'review' in request:
                    repairs.append(request['review'])
                return agent(prompt, wt)

            # Passa na base, sem allowed.txt, e falha depois da implementação.
            command = [
                sys.executable,
                '-c',
                "import os, sys; sys.exit('missing expected record' if os.path.exists('allowed.txt') else 0)",
            ]
            with patch.object(agents, 'run_agy', side_effect=capture_repair):
                result = runner.execute(self.ws, {'tasks': [task]}, 1, 1, command)
            self.assertEqual(result, {'A': 'failed'})
            self.assertEqual(len(repairs), 1)
            self.assertIn('missing expected record', repairs[0])
            state = support.read_status(self.ws)
            self.assertIn('missing expected record', state['details']['A']['error'])

    def test_rejection_reason_is_recorded_before_the_repair(self):
        with self.repository() as (root, store, base, task, git):
            agent = agents.run_agy
            seen_at_repair = []

            def capture_repair(prompt, wt, stop=None):
                if 'review' in json.loads(prompt.split('\n', 1)[1]):
                    # O estado gravado no disco, como get_run_status e o --watch o leem.
                    seen_at_repair.append(support.read_status(self.ws)['details']['A'])
                return agent(prompt, wt)

            # Passa na base, sem allowed.txt, e falha depois da implementação.
            command = [
                sys.executable,
                '-c',
                "import os, sys; sys.exit('registro esperado ausente' if os.path.exists('allowed.txt') else 0)",
            ]
            with (
                patch.object(agents, 'run_agy', side_effect=capture_repair),
                self.assertLogs('contracanto', 'INFO') as logs,
            ):
                runner.execute(self.ws, {'tasks': [task]}, 1, 1, command)
            (detail,) = seen_at_repair
            self.assertEqual(support.read_status(self.ws)['statuses']['A'], 'failed')
            (first,) = detail['reviews']
            self.assertEqual((first['attempt'], first['approved']), (1, False))
            self.assertIn('registro esperado ausente', first['feedback'])
            self.assertEqual(detail['review'], first['feedback'])
            self.assertTrue(any('motivo da rejeição 1' in line and 'registro esperado' in line for line in logs.output))
            final = support.read_status(self.ws)['details']['A']['reviews']
            self.assertEqual([entry['attempt'] for entry in final], [1, 2])
