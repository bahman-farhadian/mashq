#!/usr/bin/env python3
"""Isolated HTTP integration test for the Tartarus practice API."""
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class TartarusHttpTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix='tartarus-e2e-')
        root = Path(self.tempdir.name)
        self.db = root / 'progress.db'
        self.word_lists = root / 'word_lists'
        self.word_lists.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update({
            'TARTARUS_DB': str(self.db),
            'TARTARUS_WORD_LISTS_DIR': str(self.word_lists),
            'TARTARUS_PORT': str(self.port),
            'PYTHONDONTWRITEBYTECODE': '1',
            'TARTARUS_LOG_FILE': str(root / 'tartarus.log'),
            'TARTARUS_SESSION_TTL_SECONDS': '1',
        })
        self.server = subprocess.Popen(
            [sys.executable, 'utils/tartarus_web.py'], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.base = f'http://127.0.0.1:{self.port}'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.request('/')
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            stderr = self.server.stderr.read()
            self.fail(f'server did not start: {stderr}')

    def tearDown(self):
        if self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=5)
        self.server.stderr.close()
        self.tempdir.cleanup()

    def request(self, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=body,
            headers={'Content-Type': 'application/json'} if body else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if path.startswith('/api/') else raw
        except urllib.error.HTTPError as error:
            return json.loads(error.read())

    def answer(self, session_id, question, answer, attempt_id):
        return self.request('/api/practice/answer', {
            'session_id': session_id, 'question_id': question['question_id'],
            'sequence': question['sequence'], 'attempt_id': attempt_id, 'answer': answer,
        })

    def test_personal_list_and_persisted_drill_over_http(self):
        self.assertEqual(self.request('/api/user/create', {'user': 'alice'})['status'], 'ok')
        self.assertTrue(self.request('/api/init', {'user': 'alice', 'lang': 'personal'})['created'])
        saved = self.request('/api/wordlist', {
            'user': 'alice', 'lang': 'personal',
            'items': [{'id': 'haus', 'word': 'das Haus', 'definition': ['house'], 'word_frequency': 0}],
        })
        self.assertEqual(saved['count'], 1)
        lists = self.request('/api/wordlists')['wordlists']
        self.assertEqual([entry['user'] for entry in lists], ['alice'])
        started = self.request('/api/practice/start', {'user': 'alice', 'lang': 'personal'})
        self.assertIn('question', started, started)
        question = started['question']
        wrong = self.answer(started['session_id'], question, 'wrong', 'first-wrong')
        self.assertIn('result', wrong, wrong)
        self.assertEqual(wrong['result'], 'drill_start')
        duplicate = self.answer(started['session_id'], question, 'wrong', 'first-wrong')
        self.assertEqual(duplicate, wrong)
        resumed = self.request('/api/practice/start', {'user': 'alice', 'lang': 'personal'})
        drill_question = resumed['question']
        self.assertEqual(drill_question['type'], 'drill')
        result = None
        for number in range(9):
            result = self.answer(resumed['session_id'], drill_question, 'das Haus', f'drill-{number}')
        self.assertEqual(result['result'], 'drilled')
        self.assertTrue(result['done'])


    def test_concurrent_duplicate_answer_and_expired_session_over_http(self):
        self.assertEqual(self.request('/api/user/create', {'user': 'alice'})['status'], 'ok')
        self.request('/api/init', {'user': 'alice', 'lang': 'personal'})
        self.request('/api/wordlist', {
            'user': 'alice', 'lang': 'personal',
            'items': [{'id': 'one', 'word': 'one', 'definition': ['one'], 'word_frequency': 0}],
        })
        started = self.request('/api/practice/start', {'user': 'alice', 'lang': 'personal'})
        question = started['question']
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(
                lambda _: self.answer(started['session_id'], question, 'wrong', 'same-attempt'), range(2),
            ))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0]['result'], 'drill_start')

        expiring = self.request('/api/practice/start', {'user': 'alice', 'lang': 'personal'})
        time.sleep(1.1)
        expired = self.answer(expiring['session_id'], expiring['question'], 'one', 'expired-attempt')
        self.assertIn('unknown or expired session', expired['error'])

    def test_noun_practice_and_session_cancellation_over_http(self):
        self.assertEqual(self.request('/api/user/create', {'user': 'alice'})['status'], 'ok')
        self.assertTrue(self.request('/api/init', {
            'user': 'alice', 'lang': 'nouns', 'type': 'nouns',
        })['created'])
        forms = {}
        for case_name, singular, plural in (
            ('nominative', 'das Buch', 'die Bücher'),
            ('accusative', 'das Buch', 'die Bücher'),
            ('dative', 'dem Buch', 'den Büchern'),
            ('genitive', 'des Buches', 'der Bücher'),
        ):
            forms[f'{case_name}_singular'] = {
                'form': singular, 'sentence': f'{singular} ist neu.', 'translation': 'The book is new.',
            }
            forms[f'{case_name}_plural'] = {
                'form': plural, 'sentence': f'{plural} sind neu.', 'translation': 'The books are new.',
            }
        saved = self.request('/api/noun', {
            'user': 'alice', 'lang': 'nouns', 'noun': 'Buch', 'translation': 'book', **forms,
        })
        self.assertTrue(saved['saved'])
        noun_list = self.request('/api/noun?user=alice&lang=nouns')
        self.assertEqual(noun_list['metadata']['type'], 'nouns')
        self.assertEqual(len(noun_list['items']), 1)
        started = self.request('/api/practice/start', {'user': 'alice', 'lang': 'nouns'})
        self.assertIn('noun_forms', started['question'])
        cancelled = self.request('/api/practice/cancel', {'session_id': started['session_id']})
        self.assertTrue(cancelled['cancelled'])

    def test_due_review_and_static_practice_controls_over_http(self):
        self.assertIn(b'Review due', self.request('/'))
        self.assertEqual(self.request('/api/user/create', {'user': 'alice'})['status'], 'ok')
        self.request('/api/init', {'user': 'alice', 'lang': 'personal'})
        self.request('/api/wordlist', {
            'user': 'alice', 'lang': 'personal',
            'items': [{'id': 'due', 'word': 'one', 'definition': ['one'], 'word_frequency': 0}],
        })
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE "words_alice_personal" SET score = 9, leitner_box = 1, last_practiced = ?',
            ('2026-01-01',),
        )
        conn.commit()
        conn.close()
        reviewed = self.request('/api/practice/start', {
            'user': 'alice', 'lang': 'personal', 'review_mode': True,
        })
        self.assertTrue(reviewed['review_mode'])
        self.assertTrue(reviewed['question']['review_mode'])
        self.assertEqual(reviewed['question']['word_unmasked'], 'one')
        self.assertTrue(self.request('/api/practice/cancel', {'session_id': reviewed['session_id']})['cancelled'])

    def test_reports_backup_and_json_errors_over_http(self):
        self.assertEqual(self.request('/api/user/create', {'user': 'alice'})['status'], 'ok')
        self.request('/api/init', {'user': 'alice', 'lang': 'personal'})
        self.request('/api/wordlist', {
            'user': 'alice', 'lang': 'personal',
            'items': [
                {'id': 'one', 'word': 'one', 'definition': ['one'], 'word_frequency': 1},
                {'id': 'two', 'word': 'two', 'definition': ['two'], 'word_frequency': 0},
            ],
        })
        for path in (
            '/api/report?user=alice',
            '/api/report/summary?user=alice',
            '/api/user/progress?user=alice',
            '/api/wordlist?user=alice&lang=personal',
            '/api/wordlist/stats?user=alice&lang=personal',
            '/api/wordlist/leitner?user=alice&lang=personal',
            '/api/dashboard?user=alice&lang=personal',
            '/api/gauntlet/progress?user=alice&lang=personal',
        ):
            self.assertNotIn('error', self.request(path), path)
        backup = self.request('/api/export?user=alice')
        self.assertEqual(backup['format'], 'tartarus-progress')
        rejected = self.request('/api/import', {'user': 'bob', 'data': backup})
        self.assertIn('does not match', rejected['error'])
        imported = self.request('/api/import', {'user': 'alice', 'data': backup})
        self.assertEqual(imported['status'], 'ok')
        restored = self.request('/api/export?user=alice')
        self.assertEqual(restored['word_progress'], backup['word_progress'])
        self.assertEqual(self.request('/api/not-a-route')['error'], 'not found')

        request = urllib.request.Request(
            self.base + '/api/user/create', data=b'{}', headers={'Content-Type': 'text/plain'}, method='POST',
        )
        with self.assertRaises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(response.exception.code, 400)
        self.assertIn('error', json.loads(response.exception.read()))


if __name__ == '__main__':
    unittest.main()
