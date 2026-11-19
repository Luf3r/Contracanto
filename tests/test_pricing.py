"""Estimated cost of each call from the price table, split by how each role is billed."""

import json
import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import support

from contracanto import config, errors, pricing, usage, watch
from contracanto import state as st

ROOT = Path(__file__).resolve().parent.parent
LUNA = {
    'gpt-6-luna': {
        'input': 0.10,
        'cached_input': 0.01,
        'cache_write': 0.125,
        'output': 0.50,
        'long_context': {'threshold': 272000, 'input': 0.20, 'cached_input': 0.02, 'cache_write': 0.25, 'output': 0.75},
    }
}
PRICES = pricing.load(json.dumps(LUNA))
# The clean GPT-6 Luna call measured in F0: 64,286 input tokens, 55,040 of them cached.
LUNA_CALL = usage.Usage(input=64286 - 55040, cached_input=55040, output=314, model='gpt-6-luna', peak_input=64286)
LUNA_CALL_USD = (9246 * 0.10 + 55040 * 0.01 + 314 * 0.50) / 1_000_000


class TableTests(unittest.TestCase):
    def test_empty_table_prices_nothing(self):
        self.assertEqual(pricing.load(None), {})
        self.assertEqual(pricing.load('  '), {})

    def test_table_keeps_short_and_long_context_rates(self):
        price = PRICES['gpt-6-luna']
        self.assertEqual(price.rates, pricing.Rates(0.10, 0.01, 0.125, 0.50))
        self.assertEqual(price.long_context, pricing.Rates(0.20, 0.02, 0.25, 0.75))
        self.assertEqual(price.threshold, 272000)

    def test_invalid_tables_name_the_variable(self):
        cases = {
            'not json': '{',
            'not an object': '[]',
            'model not an object': '{"m": 1}',
            'negative rate': '{"m": {"input": -1}}',
            'boolean rate': '{"m": {"input": true}}',
            'text rate': '{"m": {"output": "0.5"}}',
            'long context without threshold': '{"m": {"long_context": {"input": 1}}}',
            'zero threshold': '{"m": {"long_context": {"threshold": 0}}}',
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaisesRegex(errors.ConfigError, 'CONTRACANTO_PRICES_JSON'):
                pricing.load(raw)


class CallCostTests(unittest.TestCase):
    def cost(self, used, prices=PRICES):
        return pricing.call_cost(prices, used.model, used.as_dict(), used.peak_input, used.reported_usd)

    def test_short_context_call(self):
        cost = self.cost(LUNA_CALL)
        self.assertAlmostEqual(cost.usd, LUNA_CALL_USD)
        self.assertFalse(cost.long_context)

    def test_call_over_the_threshold_uses_long_context_rates(self):
        cost = self.cost(usage.Usage(input=300000, output=1000, model='gpt-6-luna', peak_input=300000))
        self.assertAlmostEqual(cost.usd, (300000 * 0.20 + 1000 * 0.75) / 1_000_000)
        self.assertTrue(cost.long_context)

    def test_without_a_price_the_reported_cost_or_nothing(self):
        self.assertEqual(self.cost(usage.Usage(input=10, model='claude-sonnet-5-5', reported_usd=0.0116)).usd, 0.0116)
        self.assertIsNone(self.cost(usage.Usage(input=10, model='gemini-4-hydrogen-high')).usd)
        self.assertIsNone(self.cost(usage.Usage(input=10)).usd)

    def test_table_price_wins_over_the_reported_one(self):
        reported = usage.Usage(input=1_000_000, model='gpt-6-luna', reported_usd=99.0)
        self.assertAlmostEqual(self.cost(reported).usd, 0.10)


class StatusCostTests(unittest.TestCase):
    def test_costs_are_summed_per_phase_role_and_billing(self):
        with (
            support.temp_dir() as directory,
            patch.object(config, 'PRICES', PRICES),
            patch.object(config, 'EXECUTOR_BILLING', 'api'),
            patch.object(config, 'REVIEWER_BILLING', 'plan'),
        ):
            run = st.RunState(Path(directory) / 'status.json', 'a' * 64)
            with run.phase('A', 'implementation'):
                usage.record('executor', LUNA_CALL)
                usage.record('executor', LUNA_CALL)
            with run.phase('A', 'review'):
                usage.record('reviewer', usage.Usage(input=2, model='claude-sonnet-5-5', reported_usd=0.0116069))
            with run.phase('A', 'repair'):
                usage.record('executor', usage.Usage(input=10, model='modelo-sem-preco'))
            snapshot = run.snapshot()
        phases = snapshot['timings']['A']['phases']
        self.assertAlmostEqual(phases['implementation']['cost_usd'], round(2 * LUNA_CALL_USD, 6))
        self.assertEqual(phases['repair']['unpriced_calls'], 1)
        executor = snapshot['usage']['executor']
        self.assertEqual((executor['billing'], executor['unpriced_calls']), ('api', 1))
        self.assertEqual(executor['models'], ['gpt-6-luna', 'modelo-sem-preco'])
        self.assertEqual(snapshot['usage']['reviewer']['billing'], 'plan')
        self.assertEqual(
            snapshot['cost'],
            {'api_usd': round(2 * LUNA_CALL_USD, 6), 'plan_equivalent_usd': 0.011607, 'unpriced_calls': 1},
        )

    def test_long_context_calls_are_counted(self):
        with support.temp_dir() as directory, patch.object(config, 'PRICES', PRICES):
            run = st.RunState(Path(directory) / 'status.json', 'a' * 64)
            with run.phase('A', 'implementation'):
                usage.record('executor', usage.Usage(input=300000, model='gpt-6-luna', peak_input=300000))
            snapshot = run.snapshot()
        self.assertEqual(snapshot['timings']['A']['phases']['implementation']['long_context_calls'], 1)
        self.assertEqual(snapshot['usage']['executor']['long_context_calls'], 1)

    def test_waste_includes_the_cost_of_failed_tasks(self):
        with support.temp_dir() as directory, patch.object(config, 'PRICES', PRICES):
            run = st.RunState(Path(directory) / 'status.json', 'a' * 64)
            for task_id in ('A', 'B'):
                with run.phase(task_id, 'implementation'):
                    usage.record('executor', LUNA_CALL)
            run.mark('A', 'failed')
            run.mark('B', 'approved')
            snapshot = run.snapshot()
        self.assertAlmostEqual(snapshot['waste']['cost_usd'], round(LUNA_CALL_USD, 6))
        self.assertAlmostEqual(snapshot['cost']['plan_equivalent_usd'], round(2 * LUNA_CALL_USD, 6))


class ConfigTests(unittest.TestCase):
    def read_config(self, **env):
        code = (
            'import json; from contracanto import config as c; '
            'print(json.dumps([sorted(c.PRICES), c.EXECUTOR_BILLING, c.REVIEWER_BILLING]))'
        )
        environment = {k: v for k, v in os.environ.items() if not k.startswith('CONTRACANTO_')}
        environment.update(env, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True)

    def test_defaults_bill_both_roles_to_a_plan(self):
        self.assertEqual(json.loads(self.read_config().stdout), [[], 'plan', 'plan'])

    def test_environment_sets_prices_and_billing(self):
        result = self.read_config(CONTRACANTO_PRICES_JSON=json.dumps(LUNA), CONTRACANTO_EXECUTOR_BILLING='api')
        self.assertEqual(json.loads(result.stdout), [['gpt-6-luna'], 'api', 'plan'])

    def test_invalid_values_name_the_variable(self):
        for name, value in {'CONTRACANTO_PRICES_JSON': '{', 'CONTRACANTO_REVIEWER_BILLING': 'gratis'}.items():
            with self.subTest(name=name):
                result = self.read_config(**{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)


class PanelTests(unittest.TestCase):
    def test_panel_shows_costs_by_billing_and_waste_cost(self):
        run = {
            'state': 'failed',
            'run_id': 'abc',
            'statuses': {'A': 'failed'},
            'details': {},
            'timings': {},
            'cost': {'api_usd': 1.5, 'plan_equivalent_usd': 0.0116, 'unpriced_calls': 2},
            'waste': {'tasks': 1, 'seconds': 60, 'by_category': {}, 'tokens': {'input': 1}, 'cost_usd': 0.25},
        }
        report = {'run': run, 'reservation_state': 'none', 'reservation': None, 'log': ''}
        text = '\n'.join(watch.render(report, '', datetime.now(UTC), width=200))
        self.assertIn('API US$ 1.50', text)
        self.assertIn('assinatura (equivalente) US$ 0.0116', text)
        self.assertIn('2 chamada(s) sem preço', text)
        self.assertIn('1 tarefa(s), 1m00s, US$ 0.2500', text)


if __name__ == '__main__':
    unittest.main()
