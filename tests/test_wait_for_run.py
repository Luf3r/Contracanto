"""Ferramenta MCP wait_for_run."""

import asyncio
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import support

from contracanto import maintenance, server
from contracanto import state as st


class WaitForRunTests(unittest.TestCase):
    def wait(self, ws, timeout=5):
        with patch.object(server, 'WORKSPACE', ws), patch.object(server, 'WAIT_POLL_INTERVAL', 0.05):
            return json.loads(asyncio.run(server.wait_for_run(timeout)))

    def test_returns_at_once_without_a_run_or_with_a_dead_worker(self):
        with support.temp_workspace() as ws:
            summary = self.wait(ws)
            self.assertEqual((summary['reservation_state'], summary['timed_out']), ('none', False))
        ws, run_id, _ = support.reserved_repository(self, support.finished_process_pid())
        summary = self.wait(ws)
        self.assertEqual(summary['run_id'], run_id)
        self.assertEqual((summary['state'], summary['worker_alive'], summary['timed_out']), ('running', False, False))

    def test_times_out_while_the_worker_is_still_running(self):
        ws, _, _ = support.reserved_repository(self, os.getpid())
        started = time.monotonic()
        summary = self.wait(ws, timeout=1)
        self.assertTrue(summary['timed_out'])
        self.assertTrue(summary['worker_alive'])
        self.assertGreaterEqual(time.monotonic() - started, 1)

    def test_returns_when_the_run_finishes_during_the_wait(self):
        ws, run_id, _ = support.reserved_repository(self, os.getpid())

        def finish():
            time.sleep(0.3)
            run = st.RunState(ws.status(run_id), run_id)
            run.statuses = {'A': 'failed'}
            run.target = {'branch': 'refs/heads/lote', 'base': 'abc', 'branch_removed': True}
            run.details = {'A': {'error': 'x' * 2000, 'review': 'parecer longo'}}
            run.finish('failed')
            run.save()

        worker = threading.Thread(target=finish)
        worker.start()
        summary = self.wait(ws, timeout=20)
        worker.join()
        self.assertEqual((summary['state'], summary['timed_out']), ('failed', False))
        self.assertEqual(summary['statuses'], {'A': 'failed'})
        self.assertEqual(summary['target'], {'branch': 'refs/heads/lote', 'base': 'abc', 'branch_removed': True})
        # O resumo corta os erros e deixa pareceres e log para get_run_status.
        self.assertEqual(len(summary['task_errors']['A']), maintenance.SUMMARY_ERROR_LIMIT)
        self.assertNotIn('parecer longo', json.dumps(summary))
