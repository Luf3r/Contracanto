"""Token counts read from the real CLI outputs and accumulated per phase in the status."""

import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import agents, runner, usage
from contracanto import process as proc
from contracanto import state as st

FIXTURES = Path(__file__).parent / 'fixtures' / 'agents'
AGY = (FIXTURES / 'agy-stream.jsonl').read_text(encoding='utf-8')
CLAUDE = (FIXTURES / 'claude-review.json').read_text(encoding='utf-8')
AGY_USAGE = usage.Usage(
    input=20780,
    cached_input=37372,
    output=1061 + 229,
    reasoning=229,
    model='gemini-4-hydrogen-high',
    peak_input=2372 + 10500,
)
CLAUDE_USAGE = usage.Usage(
    input=2, cached_input=649, cache_write=2797, output=35, model='claude-sonnet-5-5', reported_usd=0.0116069
)


def tokens_of(total):
    """Only the summed token fields of a role total, which also carries cost and billing."""
    return {name: total[name] for name in usage.FIELDS}


def agy_result(**result):
    return json.dumps({'event': 'result', 'result': result}) + '\n'


def new_state(directory):
    return st.RunState(Path(directory) / 'status.json', 'a' * 64)


class ParsingTests(unittest.TestCase):
    def test_agy_counts_cache_apart_and_thinking_as_output(self):
        self.assertEqual(usage.from_agy(AGY), AGY_USAGE)

    def test_claude_separates_cache_reads_and_writes(self):
        self.assertEqual(usage.from_claude(CLAUDE), CLAUDE_USAGE)

    def test_output_without_counts_yields_nothing(self):
        cases = [
            ('agy without usage', usage.from_agy, agy_result(status='SUCCESS', response='done')),
            ('agy without result', usage.from_agy, '{"event": "init"}\n'),
            ('agy not json', usage.from_agy, 'not json'),
            ('agy negative count', usage.from_agy, agy_result(usage={'input_tokens': -1})),
            ('agy text count', usage.from_agy, agy_result(usage={'input_tokens': '12'})),
            ('claude without usage', usage.from_claude, json.dumps({'result': 'x'})),
            ('claude not an object', usage.from_claude, '[]'),
            ('claude not json', usage.from_claude, ''),
        ]
        for name, parse, stdout in cases:
            with self.subTest(name):
                self.assertIsNone(parse(stdout))

    def test_missing_fields_count_as_zero(self):
        self.assertEqual(
            usage.from_agy(agy_result(usage={'input_tokens': 5, 'output_tokens': 2})),
            usage.Usage(input=5, output=2),
        )


class AccumulationTests(unittest.TestCase):
    def test_phase_sums_calls_and_role_totals(self):
        with support.temp_dir() as directory:
            run = new_state(directory)
            with run.phase('A', 'implementation'):
                usage.record('executor', AGY_USAGE)
                usage.record('executor', usage.Usage(input=10, output=1))
            with run.phase('A', 'review'):
                usage.record('reviewer', CLAUDE_USAGE)
            snapshot = run.snapshot()
        phases = snapshot['timings']['A']['phases']
        self.assertEqual(
            phases['implementation']['tokens'],
            {'input': 20790, 'cached_input': 37372, 'cache_write': 0, 'output': 1291, 'reasoning': 229, 'calls': 2},
        )
        self.assertEqual(phases['implementation']['count'], 1)
        self.assertEqual(phases['review']['tokens'], CLAUDE_USAGE.as_dict())
        self.assertEqual(snapshot['usage']['executor']['calls'], 2)
        self.assertEqual(tokens_of(snapshot['usage']['reviewer']), CLAUDE_USAGE.as_dict())

    def test_status_without_measured_calls_has_no_usage(self):
        with support.temp_dir() as directory:
            run = new_state(directory)
            with run.phase('A', 'implementation'):
                usage.record('executor', None)
            snapshot = run.snapshot()
        self.assertNotIn('usage', snapshot)
        self.assertNotIn('tokens', snapshot['timings']['A']['phases']['implementation'])

    def test_calls_outside_a_phase_are_ignored(self):
        usage.record('executor', AGY_USAGE)

    def test_parallel_phases_keep_their_own_tokens(self):
        with support.temp_dir() as directory:
            run = new_state(directory)
            inside = threading.Barrier(2)

            def work(task_id, used):
                with run.phase(task_id, 'implementation'):
                    inside.wait(timeout=10)
                    usage.record('executor', used)

            threads = [
                threading.Thread(target=work, args=('A', usage.Usage(input=1))),
                threading.Thread(target=work, args=('B', usage.Usage(input=100))),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            snapshot = run.snapshot()
        self.assertEqual(snapshot['timings']['A']['phases']['implementation']['tokens']['input'], 1)
        self.assertEqual(snapshot['timings']['B']['phases']['implementation']['tokens']['input'], 100)
        self.assertEqual(snapshot['usage']['executor']['input'], 101)


class AgentCallTests(unittest.TestCase):
    def test_executor_call_records_its_tokens(self):
        with support.temp_dir() as directory:
            run = new_state(directory)
            completed = subprocess.CompletedProcess([], 0, AGY, '')
            with patch.object(proc, 'run', return_value=completed), run.phase('A', 'repair'):
                agents.run_agy('prompt', '.')
            tokens = run.snapshot()['timings']['A']['phases']['repair']['tokens']
        self.assertEqual(tokens, AGY_USAGE.as_dict())

    def test_failed_executor_call_still_records_its_tokens(self):
        with support.temp_dir() as directory:
            run = new_state(directory)
            stdout = agy_result(status='ERROR', error='quota', usage={'input_tokens': 7, 'output_tokens': 3})
            completed = subprocess.CompletedProcess([], 0, stdout, '')
            with patch.object(proc, 'run', return_value=completed), run.phase('A', 'implementation'):
                with self.assertRaisesRegex(RuntimeError, 'quota'):
                    agents.run_agy('prompt', '.')
            tokens = run.snapshot()['timings']['A']['phases']['implementation']['tokens']
        self.assertEqual((tokens['input'], tokens['output'], tokens['calls']), (7, 3, 1))

    def test_review_records_reviewer_tokens(self):
        answer = json.loads(CLAUDE)
        answer['result'] = '{"verdict": "APPROVED"}'

        def command(args, **kwargs):
            if args[0] == 'claude':
                return subprocess.CompletedProcess(args, 0, json.dumps(answer), '')
            output = b'' if '--binary' in args else support.fake_git_output(args[1:], 'commit')
            return subprocess.CompletedProcess(args, 0, output, '')

        with support.temp_dir() as directory:
            run = new_state(directory)
            artifact = agents.Artifact(task=support.task('A'), worktree='.', report='done')
            with patch.object(proc, 'run', side_effect=command), run.phase('A', 'review'):
                approved, _ = agents.review(artifact, ['python', 'test.py'])
            snapshot = run.snapshot()
        self.assertTrue(approved)
        self.assertEqual(snapshot['timings']['A']['phases']['review']['tokens'], CLAUDE_USAGE.as_dict())
        self.assertEqual(list(snapshot['usage']), ['reviewer'])
        self.assertEqual(tokens_of(snapshot['usage']['reviewer']), CLAUDE_USAGE.as_dict())


class RunStatusTests(support.RepositoryTestCase):
    def test_execution_writes_reviewer_tokens_to_the_status(self):
        answer = json.loads(CLAUDE)
        answer['result'] = '{"verdict": "APPROVED"}'
        with self.repository() as (root, store, base, task, git):
            original = proc.run

            def command(args, **kwargs):
                if args[0] == 'claude':
                    return subprocess.CompletedProcess(args, 0, json.dumps(answer), '')
                return original(args, **kwargs)

            with patch.object(proc, 'run', side_effect=command):
                runner.execute(self.ws, {'tasks': [task]}, 1, 0, [sys.executable, '-c', 'pass'])
            status = support.read_status(self.ws)
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['timings']['A']['phases']['review']['tokens'], CLAUDE_USAGE.as_dict())
        self.assertEqual(list(status['usage']), ['reviewer'])
        self.assertEqual(tokens_of(status['usage']['reviewer']), CLAUDE_USAGE.as_dict())


if __name__ == '__main__':
    unittest.main()
