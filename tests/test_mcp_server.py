import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o
import mcp_server as server


PLAN = json.dumps({'tasks': [dict(id='A', description='Example', deps=[],
                                 files=['A.txt'], acceptance=['Tests pass'])]})


class SubmissionTests(unittest.TestCase):
    def test_second_submission_cannot_overwrite_first(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), patch.object(server.subprocess, 'Popen') as launch:
                launch.return_value.pid = 123
                first = server.submit_plan(PLAN, test_command_json='["python", "-m", "unittest"]')
                second = server.submit_plan(PLAN.replace('Example', 'Second'), test_command_json='["python", "-m", "unittest"]')
                self.assertIn('PID', first)
                self.assertIn('execução', second.lower())
                self.assertEqual(launch.call_count, 1)
                self.assertIn('Example', (store / 'submitted-plan.json').read_text())

    def test_failed_launch_releases_reservation(self):
        with tempfile.TemporaryDirectory(dir=o.STORE) as directory:
            store = Path(directory)
            with patch.object(o, 'STORE', store), patch.object(server, 'STORE', store), patch.object(server.subprocess, 'Popen', side_effect=OSError('launch failed')):
                with self.assertRaises(OSError):
                    server.submit_plan(PLAN, test_command_json='["test"]')
                self.assertFalse((store / 'run.lock').exists())
