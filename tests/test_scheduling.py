"""Agendamento de lotes e regras de escopo, sem Git nem subprocessos."""

import unittest

from contracanto import plan, runner


def task(tid, files, deps=()):
    return dict(id=tid, description='Example', deps=list(deps), files=files, acceptance=['Pass'])


class SchedulingTests(unittest.TestCase):
    def test_batch_respects_dependencies_and_worker_limit(self):
        tasks = [task('A', ['a.txt']), task('B', ['b.txt']), task('C', ['c.txt']), task('D', ['d.txt'], ['A'])]
        statuses = dict.fromkeys('ABCD', 'pending')
        self.assertEqual([t['id'] for t in runner.schedule_batch(tasks, statuses, 2)], ['A', 'B'])
        statuses.update(A='approved', B='failed')
        self.assertEqual([t['id'] for t in runner.schedule_batch(tasks, statuses, 8)], ['C', 'D'])

    def test_batch_never_runs_overlapping_ownership_together(self):
        for first, second in [('src/app.py', 'src/app.py'), ('src', 'src/app.py'), ('src/app.py', 'src/'), ('a/', 'a')]:
            with self.subTest(first=first, second=second):
                tasks = [task('A', [first]), task('B', [second]), task('C', ['other.txt'])]
                batch = runner.schedule_batch(tasks, dict.fromkeys('ABC', 'pending'), 8)
                self.assertEqual([t['id'] for t in batch], ['A', 'C'])

    def test_similar_names_do_not_overlap(self):
        tasks = [task('A', ['src']), task('B', ['src2/app.py']), task('C', ['src.txt'])]
        batch = runner.schedule_batch(tasks, dict.fromkeys('ABC', 'pending'), 8)
        self.assertEqual([t['id'] for t in batch], ['A', 'B', 'C'])

    def test_failure_blocks_transitive_descendants_only(self):
        tasks = [task('A', ['a']), task('B', ['b'], ['A']), task('C', ['c'], ['B']), task('D', ['d'])]
        statuses = {'A': 'failed', 'B': 'pending', 'C': 'pending', 'D': 'pending'}
        runner.block_descendants(tasks, statuses)
        self.assertEqual(statuses, {'A': 'failed', 'B': 'blocked', 'C': 'blocked', 'D': 'pending'})

    def test_scope_covers_directory_contents_but_not_prefix_matches(self):
        self.assertTrue(plan.in_scope(['src/'], 'src/app.py'))
        self.assertTrue(plan.in_scope(['src'], 'src'))
        self.assertFalse(plan.in_scope(['src'], 'src2/app.py'))
        self.assertFalse(plan.in_scope(['src/app.py'], 'src/app.py.bak'))


if __name__ == '__main__':
    unittest.main()
