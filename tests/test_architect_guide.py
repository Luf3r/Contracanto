"""The architect guide reaches every repository through the MCP server and stays in step with the code."""

import asyncio
import unittest
from pathlib import Path

from contracanto import errors, server

ROOT = Path(__file__).resolve().parent.parent


class ArchitectGuideTests(unittest.TestCase):
    def test_tool_returns_the_packaged_guide(self):
        guide = server.planning_guide()
        self.assertEqual(guide, (ROOT / 'contracanto' / 'architect.md').read_text(encoding='utf-8'))
        for heading in ('## Como dividir', '## Como escrever a descrição', '## Critérios de aceitação'):
            self.assertIn(heading, guide)

    def test_every_failure_category_has_guidance(self):
        guide = server.planning_guide()
        missing = [name for name in errors.FAILURE_CATEGORIES if name != 'other' and f'`{name}`' not in guide]
        self.assertEqual(missing, [])

    def test_server_instructions_point_to_the_guide(self):
        self.assertIn('planning_guide', server.mcp.instructions or '')
        tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        self.assertIn('planning_guide', tools)

    def test_claude_md_imports_the_guide(self):
        self.assertIn('@contracanto/architect.md', (ROOT / 'CLAUDE.md').read_text(encoding='utf-8'))

    def test_guide_is_shipped_with_the_package(self):
        pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('contracanto = ["architect.md"]', pyproject)


if __name__ == '__main__':
    unittest.main()
