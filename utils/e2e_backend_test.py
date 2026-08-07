#!/usr/bin/env python3
"""Isolated HTTP release-contract tests for Tartarus.

Every test gets a fresh temporary database/list/log root and a dynamically
allocated port.  No repository dataset or production database is touched.
"""
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
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path


class TartarusHttpTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix='tartarus-e2e-')
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.db = root / 'progress.db'
        self.word_lists = root / 'word_lists'
        self.word_lists.mkdir()
        self.log = root / 'tartarus.log'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update({
            'TARTARUS_DB': str(self.db),
            'TARTARUS_WORD_LISTS_DIR': str(self.word_lists),
            'TARTARUS_PORT': str(self.port),
            'PYTHONDONTWRITEBYTECODE': '1',
            'TARTARUS_LOG_FILE': str(self.log),
            'TARTARUS_SESSION_TTL_SECONDS': '1',
        })
        self.server = subprocess.Popen(
            [sys.executable, 'utils/tartarus_web.py'], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_server)
        self.base = f'http://127.0.0.1:{self.port}'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                stderr = self.server.stderr.read()
                self.fail(f'server exited during startup: {stderr}')
            try:
                status, _ = self.request_raw('/')
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            stderr = self.server.stderr.read()
            self.fail(f'server did not start: {stderr}')

    def stop_server(self):
        server = getattr(self, 'server', None)
        if server is None:
            return
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if server.stderr and not server.stderr.closed:
            server.stderr.close()

    def request_raw(self, path, payload=None, *, content_type=None, raw_body=None, method=None):
        if raw_body is not None:
            body = raw_body
        elif payload is not None:
            body = json.dumps(payload).encode('utf-8')
        else:
            body = None
        headers = {}
        if body is not None:
            headers['Content-Type'] = content_type or 'application/json'
        req = urllib.request.Request(
            self.base + path, data=body, headers=headers,
            method=method or ('POST' if body is not None else 'GET'),
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
                return response.status, raw
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def request(self, path, payload=None, **kwargs):
        status, raw = self.request_raw(path, payload, **kwargs)
        if path.startswith('/api/'):
            data = json.loads(raw or b'{}')
        else:
            data = raw
        return status, data

    def api(self, path, payload=None, expected=200, **kwargs):
        status, data = self.request(path, payload, **kwargs)
        self.assertEqual(status, expected, (path, status, data))
        return data

    def create_material(self, *, user='alice', lang='personal', word='one', definition='one', content_id='one'):
        self.api('/api/user/create', {'user': user})
        created = self.api('/api/init', {'user': user, 'lang': lang})
        self.assertIn('created', created)
        saved = self.api('/api/wordlist', {
            'user': user, 'lang': lang,
            'items': [{'id': content_id, 'word': word, 'definition': [definition], 'word_frequency': 0}],
        })
        self.assertEqual(saved['count'], 1)
        return user, lang

    def start(self, user='alice', lang='personal', **extra):
        data = self.api('/api/practice/start', {'user': user, 'lang': lang, **extra})
        self.assertIn('session_id', data)
        self.assertIn('question', data)
        return data

    def answer_payload(self, started, answer, attempt_id, *, question=None, **overrides):
        q = question or started['question']
        payload = {
            'session_id': started['session_id'],
            'question_id': q['question_id'],
            'sequence': q['sequence'],
            'attempt_id': attempt_id,
            'answer': answer,
        }
        payload.update(overrides)
        return payload

    def answer(self, started, answer, attempt_id, *, question=None, expected=200, **overrides):
        return self.api(
            '/api/practice/answer',
            self.answer_payload(started, answer, attempt_id, question=question, **overrides),
            expected=expected,
        )

    def test_standard_german_noun_http_practice_has_no_case_api(self):
        self.create_material(word='das Buch, die Bücher', definition='book', content_id='buch')
        status, noun_get = self.request('/api/noun?user=alice&lang=personal')
        self.assertEqual((status, noun_get.get('error')), (404, 'not found'))
        status, noun_post = self.request('/api/noun', {
            'user': 'alice', 'lang': 'personal', 'noun': 'Buch',
        })
        self.assertEqual((status, noun_post.get('error')), (404, 'not found'))

        first = self.start()
        for forbidden in ('noun_forms', 'noun_case', 'noun_answers'):
            self.assertNotIn(forbidden, first['question'])
        singular = self.answer(first, 'das Buch', 'singular')
        self.assertEqual(singular['result'], 'correct')

        second = self.start()
        plural = self.answer(second, 'die Bücher', 'plural')
        self.assertEqual(plural['result'], 'correct')

    def test_immediate_drill_accounting_and_cancellation_are_session_local(self):
        self.create_material(word='das Haus', definition='house', content_id='haus')
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE "words_alice_personal" SET score=4.0, times_practiced=0, times_correct=0, '
            'times_incorrect=0, times_drilled=0 WHERE content_id=?', ('haus',),
        )
        conn.commit(); conn.close()

        started = self.start()
        wrong = self.answer(started, 'wrong', 'first-wrong')
        self.assertEqual(wrong['result'], 'drill_start')
        duplicate = self.answer(started, 'wrong', 'first-wrong')
        self.assertEqual(duplicate, wrong)
        reset = self.answer(started, 'wrong', 'drill-reset')
        self.assertEqual(reset['drill']['correct_in_a_row'], 0)
        for number in range(9):
            result = self.answer(started, 'das Haus', f'drill-{number}')
        self.assertEqual(result['result'], 'drilled')
        self.assertTrue(result['done'])

        conn = sqlite3.connect(self.db)
        saved = conn.execute(
            'SELECT score, times_practiced, times_incorrect, times_drilled '
            'FROM "words_alice_personal" WHERE content_id=?', ('haus',),
        ).fetchone()
        conn.close()
        self.assertEqual(saved, (4.5, 2, 1, 1))

        cancelling = self.start()
        self.assertEqual(self.answer(cancelling, 'wrong', 'cancel-wrong')['result'], 'drill_start')
        cancelled = self.api('/api/practice/cancel', {'session_id': cancelling['session_id']})
        self.assertTrue(cancelled['cancelled'])
        resumed = self.start()
        self.assertNotEqual(resumed['question']['type'], 'drill')
        self.api('/api/practice/cancel', {'session_id': resumed['session_id']})

    def test_answer_idempotency_and_stale_requests_never_double_mutate(self):
        self.create_material(word='one')
        started = self.start()
        question = started['question']
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(
                self.answer, started, 'wrong', 'same-attempt', question=question,
            ) for _ in range(2)]
            replies = [future.result() for future in futures]
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0]['result'], 'drill_start')

        conn = sqlite3.connect(self.db)
        before = conn.execute(
            'SELECT score, times_practiced, times_incorrect FROM "words_alice_personal" WHERE content_id=?', ('one',),
        ).fetchone()
        conn.close()
        self.assertEqual(before, (0.0, 1, 1))

        stale_q = self.answer(started, 'one', 'stale-q', question=question, expected=409,
                              question_id='not-current')
        self.assertIn('stale', stale_q['error'])
        stale_seq = self.answer(started, 'one', 'stale-seq', question=question, expected=409,
                                sequence=question['sequence'] + 99)
        self.assertIn('stale', stale_seq['error'])
        missing = self.answer(started, 'one', '', question=question, expected=400)
        self.assertIn('idempotency', missing['error'])

        conn = sqlite3.connect(self.db)
        after = conn.execute(
            'SELECT score, times_practiced, times_incorrect FROM "words_alice_personal" WHERE content_id=?', ('one',),
        ).fetchone()
        conn.close()
        self.assertEqual(after, before)

    def test_expired_session_is_removed_without_progress_mutation(self):
        self.create_material(word='one')
        started = self.start()
        conn = sqlite3.connect(self.db)
        before = conn.execute(
            'SELECT score, times_practiced, times_correct, times_incorrect FROM "words_alice_personal" WHERE content_id=?', ('one',),
        ).fetchone()
        conn.close()
        time.sleep(1.1)
        expired = self.answer(started, 'one', 'expired', expected=404)
        self.assertIn('unknown or expired session', expired['error'])
        status, cancel = self.request('/api/practice/cancel', {'session_id': started['session_id']})
        self.assertEqual(status, 404)
        self.assertIn('unknown or expired session', cancel['error'])
        conn = sqlite3.connect(self.db)
        after = conn.execute(
            'SELECT score, times_practiced, times_correct, times_incorrect FROM "words_alice_personal" WHERE content_id=?', ('one',),
        ).fetchone()
        conn.close()
        self.assertEqual(after, before)

    def test_due_review_is_filtered_and_read_only_over_http(self):
        self.api('/api/user/create', {'user': 'alice'})
        self.api('/api/init', {'user': 'alice', 'lang': 'review'})
        self.api('/api/wordlist', {
            'user': 'alice', 'lang': 'review',
            'items': [
                {'id': 'due', 'word': 'due', 'definition': ['due'], 'word_frequency': 0},
                {'id': 'later', 'word': 'later', 'definition': ['later'], 'word_frequency': 0},
                {'id': 'today', 'word': 'today', 'definition': ['today'], 'word_frequency': 0},
            ],
        })
        conn = sqlite3.connect(self.db)
        old = (date.today() - timedelta(days=30)).isoformat()
        today = date.today().isoformat()
        conn.execute('UPDATE "words_alice_review" SET score=9, leitner_box=1, times_practiced=4, times_correct=3, times_incorrect=1, last_practiced=? WHERE content_id=?', (old, 'due'))
        conn.execute('UPDATE "words_alice_review" SET score=9, leitner_box=10, last_practiced=? WHERE content_id=?', ((date.today() - timedelta(days=1)).isoformat(), 'later'))
        conn.execute('UPDATE "words_alice_review" SET score=9, leitner_box=1, last_practiced=? WHERE content_id=?', (today, 'today'))
        before = conn.execute(
            'SELECT score, leitner_box, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, last_practiced, last_known_review_at '
            'FROM "words_alice_review" WHERE content_id=?', ('due',),
        ).fetchone()
        conn.commit(); conn.close()

        reviewed = self.start(lang='review', review_mode=True)
        self.assertTrue(reviewed['review_mode'])
        self.assertEqual(reviewed['progress']['total'], 1)
        self.assertEqual(reviewed['question']['word_unmasked'], 'due')
        done = self.answer(reviewed, 'ArrowRight', 'review-right')
        self.assertTrue(done['done'])

        conn = sqlite3.connect(self.db)
        after = conn.execute(
            'SELECT score, leitner_box, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, last_practiced, last_known_review_at '
            'FROM "words_alice_review" WHERE content_id=?', ('due',),
        ).fetchone()
        conn.close()
        self.assertEqual(after, before)

    def test_backup_import_is_atomic_and_round_trip_is_exact(self):
        self.create_material(word='one')
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_personal" SET score=5.5, times_practiced=3 WHERE content_id=?', ('one',))
        conn.commit(); conn.close()
        backup = self.api('/api/export?user=alice')

        # Invalid import must not leave a partial change.
        invalid = deepcopy(backup)
        invalid['sessions'] = [{'unknown': 'field'}]
        rejected = self.api('/api/import', {'user': 'alice', 'data': invalid}, expected=400)
        self.assertIn('invalid columns', rejected['error'])
        self.assertEqual(self.api('/api/export?user=alice'), backup)

        # Mutate all three logical areas, then restore exactly.
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_personal" SET score=0, times_practiced=99 WHERE content_id=?', ('one',))
        conn.execute('INSERT INTO "sessions_alice" (language, session_date, duration_seconds, words_practiced, correct_count, incorrect_count, drilled_count) VALUES ("personal", "2026-08-07", 9, 9, 9, 0, 0)')
        conn.execute('INSERT OR REPLACE INTO dataset_progress (user, lang, current_stage, current_day, sessions_done_today, last_practice_date) VALUES ("alice", "personal", 5, 9, 2, "2026-08-07")')
        conn.commit(); conn.close()
        self.api('/api/import', {'user': 'alice', 'data': backup})
        restored = self.api('/api/export?user=alice')
        self.assertEqual(restored, backup)

    def test_request_validation_and_tts_failures_are_bounded_json(self):
        status, non_json = self.request('/api/user/create', raw_body=b'{}', content_type='text/plain')
        self.assertEqual(status, 400)
        self.assertIn('error', non_json)

        status, oversized = self.request(
            '/api/user/create', raw_body=b'{' + b'x' * 1_000_001,
            content_type='application/json',
        )
        self.assertEqual(status, 400)
        self.assertIn('error', oversized)

        status, unknown = self.request('/api/not-a-route')
        self.assertEqual((status, unknown.get('error')), (404, 'not found'))

        status, tts = self.request('/api/tts', {'text': 'test', 'lang': 'english', 'wpm': 128})
        self.assertIn(status, (200, 501))
        self.assertIsInstance(tts, dict)
        if status == 501:
            self.assertFalse(tts['supported'])

    def test_report_and_progress_endpoints_match_current_schema(self):
        self.create_material(word='one')
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
            data = self.api(path)
            self.assertNotIn('error', data, path)
        stats = self.api('/api/wordlist/stats?user=alice&lang=personal')['words'][0]
        self.assertNotIn('times_flagged', stats)
        progress = self.api('/api/user/progress?user=alice')['lists'][0]
        self.assertNotIn('to_drill', progress)


if __name__ == '__main__':
    unittest.main()
