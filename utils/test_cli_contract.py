#!/usr/bin/env python3
"""Executable CLI characterization tests for every advertised practice mode."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'utils' / 'tartarus.py'


class CliContractTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix='tartarus-cli-')
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.db = root / 'progress.db'
        self.word_lists = root / 'word_lists'
        self.word_lists.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            'TARTARUS_DB': str(self.db),
            'TARTARUS_WORD_LISTS_DIR': str(self.word_lists),
            'TARTARUS_LOG_FILE': str(root / 'tartarus.log'),
            'PYTHONDONTWRITEBYTECODE': '1',
            'TERM': 'xterm',
        })
        result = self.run_cli('init', '--user', 'alice', '--lang', 'cli')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.list_path = self.word_lists / 'alice_cli.json'
        self.list_path.write_text(json.dumps({
            'metadata': {
                'name': 'CLI', 'language': 'german',
                'kind': 'vocabulary', 'level': 'a1',
            },
            'items': [{
                'id': 'buch', 'word': 'das Buch, die Bücher',
                'definition': ['book'], 'word_frequency': 1,
            }],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        # Trigger synchronization once without interactive practice.
        self.run_python(
            "import tartarus as t; t.sync_word_list('alice','cli')",
        )

    def run_cli(self, *args, input_text='', timeout=30):
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=ROOT, env=self.env,
            input=input_text, text=True, capture_output=True, timeout=timeout,
        )

    def run_python(self, code):
        result = subprocess.run(
            [sys.executable, '-c', f"import sys; sys.path.insert(0, r'{ROOT / 'utils'}'); {code}"],
            cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def set_progress(self, *, score, practiced=0, correct=0, incorrect=0, drilled=0, box=None, known=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE "words_alice_cli" SET score=?, times_practiced=?, times_correct=?, '
            'times_incorrect=?, times_drilled=?, leitner_box=?, last_known_review_at=?, last_practiced=? '
            'WHERE content_id=?',
            (score, practiced, correct, incorrect, drilled, box, known,
             '2026-01-01' if practiced else None, 'buch'),
        )
        conn.commit(); conn.close()

    def row(self):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            'SELECT score, times_practiced, times_correct, times_incorrect, times_drilled, '
            'leitner_box, last_known_review_at FROM "words_alice_cli" WHERE content_id=?', ('buch',),
        ).fetchone()
        conn.close()
        return row

    def test_normal_practice_accepts_one_comma_separated_noun_form(self):
        self.set_progress(score=0)
        result = self.run_cli('practice', '--user', 'alice', '--lang', 'cli', input_text='das Buch\n')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('NameError', result.stdout + result.stderr)
        self.assertEqual(self.row()[:5], (0.5, 1, 1, 0, 0))

    def test_fast_mode_is_progress_neutral_except_review_marker(self):
        self.set_progress(score=9, practiced=5, correct=4, incorrect=1, drilled=2, box=3)
        before = self.row()
        result = self.run_cli('practice', '--user', 'alice', '--lang', 'cli', '--fast', input_text='die Bücher\n')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = self.row()
        self.assertEqual(after[:6], before[:6])
        self.assertIsNotNone(after[6])

    def test_drill_mode_advances_exactly_half_a_point_once(self):
        self.set_progress(score=4.0)
        result = self.run_cli(
            'practice', '--user', 'alice', '--lang', 'cli', '--drill',
            input_text='das Buch\n' * 9,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.row()[:5], (4.5, 1, 0, 0, 1))

    def test_instant_drill_wrong_answer_has_exact_accounting(self):
        self.set_progress(score=4.0)
        result = self.run_cli(
            'practice', '--user', 'alice', '--lang', 'cli', '--instant-drill',
            input_text='wrong\n' + ('die Bücher\n' * 9),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.row()[:5], (4.5, 2, 0, 1, 1))
        self.assertNotIn('Score set to 5.0', result.stdout)

    def test_known_drill_is_score_and_leitner_neutral(self):
        self.set_progress(score=9.0, practiced=3, correct=2, incorrect=1, drilled=0, box=2)
        result = self.run_cli(
            'practice', '--user', 'alice', '--lang', 'cli', '--known-drill-mode',
            input_text='das Buch\n' * 9,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = self.row()
        self.assertEqual(after[:6], (9.0, 3, 2, 1, 1, 2))
        self.assertIsNotNone(after[6])

    def test_obsolete_drill_mode_option_is_not_advertised_or_accepted(self):
        help_result = self.run_cli('practice', '--help')
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn('--drill-mode', help_result.stdout)
        rejected = self.run_cli('practice', '--user', 'alice', '--lang', 'cli', '--drill-mode')
        self.assertNotEqual(rejected.returncode, 0)


if __name__ == '__main__':
    unittest.main()
