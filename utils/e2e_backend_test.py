#!/usr/bin/env python3
"""Isolated HTTP integration test for the Tartarus practice API."""
import json
import os
import socket
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


if __name__ == '__main__':
    unittest.main()
