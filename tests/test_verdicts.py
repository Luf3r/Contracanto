"""Interpretação da resposta do revisor e limite do patch enviado a ele."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, config, gitops
from contracanto import process as proc

PASS = [sys.executable, '-c', 'pass']


class VerdictTests(unittest.TestCase):
    def test_json_verdict_variants(self):
        cases = [
            ('{"verdict": "APPROVED", "summary": "ok", "problems": []}', True),
            ('{"verdict": "approved"}', True),
            ('Segue a análise:\n```json\n{"verdict": "APPROVED", "summary": "ok"}\n```', True),
            ('{"verdict": "REJECTED", "problems": ["falta teste"]}', False),
            ('{"verdict": "TALVEZ"}', False),
            ('{"outro": 1} e depois {"verdict": "APPROVED"}', True),
            # Só o objeto JSON vale: a palavra solta na primeira linha não aprova.
            ('APPROVED\nTudo certo', False),
            ('REJECTED\nFalta teste', False),
            ('Aprovado, parece bom', False),
            ('', False),
        ]
        for response, approved in cases:
            with self.subTest(response=response):
                self.assertIs(agents.parse_verdict(response).approved, approved)

    def test_rejection_text_lists_problems_for_the_repair(self):
        verdict = agents.parse_verdict('{"verdict": "REJECTED", "summary": "Incompleto", "problems": ["a", "b"]}')
        self.assertEqual(verdict.text, 'REJECTED\nIncompleto\n- a\n- b')

    def test_unrecognized_answer_is_rejected_with_the_raw_answer(self):
        verdict = agents.parse_verdict('Parece ótimo!')
        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.text.startswith('REJECTED\n'))
        self.assertIn('Parece ótimo!', verdict.text)

    def test_review_requires_passing_tests_even_with_json_approval(self):
        artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')

        def command(args, **kwargs):
            if args[0] == 'claude':
                return subprocess.CompletedProcess(args, 0, json.dumps({'result': '{"verdict": "APPROVED"}'}), '')
            return subprocess.CompletedProcess(args, 0, b'' if '--binary' in args else '', '')

        failing = subprocess.CompletedProcess([], 3, 'out', 'err')
        with (
            patch.object(proc, 'run', side_effect=command),
            patch.object(support.gitops, 'run_tests', return_value=failing),
            patch.object(support.gitops, 'head', return_value='commit'),
        ):
            approved, feedback = agents.review(artifact, ['test'])
        self.assertFalse(approved)
        self.assertIn('código 3', feedback)


class PatchLimitTests(unittest.TestCase):
    def test_oversized_patch_fails_before_calling_the_reviewer(self):
        artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')
        called = []

        def command(args, **kwargs):
            called.append(args[0])
            return subprocess.CompletedProcess(args, 0, b'x' * 50 if '--binary' in args else '', '')

        passing = subprocess.CompletedProcess([], 0, '', '')
        with (
            patch.object(proc, 'run', side_effect=command),
            patch.object(gitops, 'run_tests', return_value=passing),
            patch.object(gitops, 'head', return_value='commit'),
            patch.object(config, 'REVIEW_PATCH_LIMIT', 10),
            self.assertRaisesRegex(RuntimeError, 'REVIEW_PATCH_LIMIT'),
        ):
            agents.review(artifact, ['test'])
        self.assertNotIn('claude', called)


class ReviewerDirectoryTests(support.RepositoryTestCase):
    def test_reviewer_runs_outside_the_worktree_in_an_empty_directory(self):
        with self.repository() as (root, store, base, task, git):
            (root / 'allowed.txt').write_text('conteúdo')
            (root / 'CLAUDE.md').write_text('Você é o arquiteto.')
            git('add', '.')
            git('commit', '-m', 'candidato')
            original = proc.run
            seen = []

            def command(args, **kwargs):
                if args[0] == 'claude':
                    directory = Path(kwargs['cwd'])
                    seen.append((directory.resolve(), list(directory.iterdir())))
                return original(args, **kwargs)

            with patch.object(proc, 'run', side_effect=command):
                approved, _ = agents.review(agents.Artifact(task=task, worktree=str(root), report='done'), PASS)
            self.assertTrue(approved)
            ((directory, contents),) = seen
            self.assertEqual(contents, [])
            self.assertNotEqual(directory, root)
            self.assertNotIn(root, directory.parents)
            self.assertFalse(directory.exists())
