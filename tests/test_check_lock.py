"""Conferência entre requirements.lock.txt e as versões fixadas em pyproject.toml."""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'check_lock.py'
spec = importlib.util.spec_from_file_location('check_lock', SCRIPT)
assert spec is not None and spec.loader is not None
check_lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_lock)


class CheckLockTests(unittest.TestCase):
    LOCK = ('# comentário', 'mcp==1.30.0', 'pywin32==312; sys_platform == "win32"', 'typing_extensions==4.16.0')

    def test_matching_pins_pass_with_markers_and_name_variants(self):
        declared = ['mcp==1.30.0', "pywin32==312; sys_platform == 'win32'", 'Typing.Extensions == 4.16.0']
        self.assertEqual(check_lock.differences(declared, self.LOCK), [])

    def test_divergent_missing_and_unpinned_are_reported(self):
        problems = check_lock.differences(['mcp==1.31.0', 'httpx==1.0', 'anyio>=4'], self.LOCK)
        self.assertEqual(len(problems), 3)
        self.assertIn('1.31.0', problems[0])
        self.assertIn('ausente', problems[1])
        self.assertIn('==', problems[2])

    def test_the_project_files_agree(self):
        self.assertEqual(check_lock.main(), 0)
