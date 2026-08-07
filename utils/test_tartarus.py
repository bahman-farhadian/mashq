#!/usr/bin/env python3
"""Unified release-contract test suite for Tartarus.

This is intentionally the only project test module. It covers the core learning
engine, schema/material contracts, HTTP API, CLI surface, and real browser UI.
All tests use temporary databases, word-list roots, logs, ports, and browser
profiles. Production data is never read or modified.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'utils'
sys.path.insert(0, str(UTILS))

import tartarus as ll  # noqa: E402
import tartarus_web as web  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def write_material(path: Path, items, **metadata):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'metadata': {
            'name': path.stem,
            'language': 'german',
            'kind': 'vocabulary',
            'level': 'a1',
            'pos': 'noun',
            **metadata,
        },
        'items': items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def vocabulary_items(count=20, prefix='w'):
    return [
        {
            'id': f'id-{index:02d}',
            'word': f'{prefix}{index:02d}',
            'definition': [f'definition {index:02d}'],
            'word_frequency': index,
        }
        for index in range(count)
    ]


class IsolatedCoreTest(unittest.TestCase):
    """Core tests with module paths redirected to one temporary root."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tartarus-core-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'progress.db'
        self.lists = self.root / 'word_lists'
        self.lists.mkdir()
        self.old_db = ll.DATABASE_FILE
        self.old_lists = ll.WORD_LISTS_DIR
        ll.DATABASE_FILE = str(self.db)
        ll.WORD_LISTS_DIR = str(self.lists)
        with web.SESSIONS_LOCK:
            web.SESSIONS.clear()
        conn = ll.get_connection()
        for user in ('alice', 'alice_ann', 'bob'):
            ll.ensure_user(conn, user)
        conn.commit()
        conn.close()
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        with web.SESSIONS_LOCK:
            web.SESSIONS.clear()
        ll.DATABASE_FILE = self.old_db
        ll.WORD_LISTS_DIR = self.old_lists

    def personal_path(self, user='alice', lang='focus'):
        return self.lists / f'{user}_{lang}.json'

    def make_personal(self, *, user='alice', lang='focus', items=None, **metadata):
        path = self.personal_path(user, lang)
        write_material(path, items or vocabulary_items(), **metadata)
        ll.sync_word_list(user, lang)
        return path

    def row(self, lang='focus', content_id='id-00', columns='score, leitner_box, times_practiced, times_correct, times_incorrect, times_drilled'):
        conn = ll.get_connection()
        table = ll.words_table_name('alice', lang)
        value = conn.execute(
            f'SELECT {columns} FROM "{table}" WHERE content_id = ?', (content_id,)
        ).fetchone()
        conn.close()
        return value

    def update(self, lang, content_id, **values):
        conn = ll.get_connection()
        table = ll.words_table_name('alice', lang)
        columns = ', '.join(f'{key} = ?' for key in values)
        conn.execute(
            f'UPDATE "{table}" SET {columns} WHERE content_id = ?',
            (*values.values(), content_id),
        )
        conn.commit()
        conn.close()

    # --- Selection and pedagogical flow ---

    def test_new_file_pool_uses_first_sixteen_json_items_then_randomizes_equal_scores(self):
        self.make_personal(items=vocabulary_items(20))
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: values.reverse()):
            selected = ll.get_words_for_practice('alice', 'focus')
        words = [row[1] for row in selected]
        self.assertEqual(set(words), {f'w{i:02d}' for i in range(16)})
        self.assertEqual(words, [f'w{i:02d}' for i in reversed(range(16))])
        self.assertEqual({row[3] for row in selected}, {0.0})

    def test_mixed_history_selects_highest_scores_first_and_shuffles_only_ties(self):
        self.make_personal(items=vocabulary_items(20))
        for content_id, score in (
            ('id-19', 8.5), ('id-18', 8.0), ('id-17', 7.0),
            *[(f'id-{i:02d}', 1.0) for i in range(16)],
        ):
            self.update('focus', content_id, score=score)
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: values.reverse()):
            selected = ll.get_words_for_practice('alice', 'focus')
        self.assertEqual([row[1] for row in selected[:3]], ['w19', 'w18', 'w17'])
        self.assertEqual([row[3] for row in selected], sorted([row[3] for row in selected], reverse=True))
        self.assertEqual(len(selected), 16)
        self.assertNotIn('w16', [row[1] for row in selected])
        self.assertEqual([row[1] for row in selected[3:]], [f'w{i:02d}' for i in reversed(range(13))])

    def test_mastering_focus_item_removes_it_and_admits_next_json_priority_item(self):
        self.make_personal(items=vocabulary_items(20))
        self.update('focus', 'id-00', score=8.5)
        first = ll.get_words_for_practice('alice', 'focus')
        self.assertIn('w00', [row[1] for row in first])
        word_id = next(row[0] for row in first if row[1] == 'w00')
        ll.update_word_score('alice', 'focus', word_id, 'correct')
        second = ll.get_words_for_practice('alice', 'focus')
        words = [row[1] for row in second]
        self.assertNotIn('w00', words)
        self.assertIn('w16', words)
        self.assertEqual(self.row(content_id='id-00')[0], 9.0)

    def test_comma_separated_german_noun_requires_every_form(self):
        target = 'das Buch, die Bücher'
        self.assertFalse(ll.answer_matches('das Buch', target))
        self.assertFalse(ll.answer_matches('die Bücher', target))
        self.assertTrue(ll.answer_matches('das Buch, die Bücher', target))
        self.assertTrue(ll.answer_matches('die Bücher, das Buch', target))
        self.assertFalse(ll.answer_matches('Buch', target))

    # --- Scoring, drills, and Leitner ---

    def test_incorrect_then_completed_drill_preserves_score_then_adds_half_point_once(self):
        self.make_personal(items=[vocabulary_items(1)[0]])
        self.update('focus', 'id-00', score=4.0)
        word_id = ll.get_words_for_practice('alice', 'focus')[0][0]
        ll.update_word_score('alice', 'focus', word_id, 'incorrect')
        self.assertEqual(self.row(content_id='id-00')[:1], (4.0,))
        ll.complete_drill('alice', 'focus', word_id)
        self.assertEqual(
            self.row(content_id='id-00'),
            (4.5, None, 2, 0, 1, 1),
        )

    def test_leitner_maintenance_is_the_second_track_inside_the_gauntlet(self):
        self.make_personal(items=vocabulary_items(2))
        self.update('focus', 'id-00', score=9.0, leitner_box=9, last_practiced='2000-01-01')
        self.update('focus', 'id-01', score=9.0, leitner_box=10, last_practiced='2099-01-01')
        due = ll.check_leitner_due_words('alice', 'focus')
        self.assertEqual([row[1] for row in due], ['w00'])
        word_id = due[0][0]
        self.assertEqual(ll.update_word_score('alice', 'focus', word_id, 'correct'), 9.0)
        self.assertEqual(self.row(content_id='id-00')[:2], (9.0, 10))

    def test_dashboard_roadmap_contains_original_gauntlet_and_leitner_tracks(self):
        self.make_personal(items=vocabulary_items(2))
        self.update('focus', 'id-00', score=9.0, leitner_box=3)
        data = web.dashboard_data('alice', 'focus')
        roadmap = data['roadmap']
        self.assertEqual(set(roadmap), {'gauntlet', 'leitner_distribution'})
        self.assertEqual(roadmap['gauntlet']['current_stage'], 0)
        self.assertEqual(roadmap['gauntlet']['stage_name'], 'The Forging')
        self.assertEqual(roadmap['leitner_distribution']['3'], 1)

    def test_schema_v3_migrates_obsolete_columns_without_losing_progress(self):
        table = ll.words_table_name('alice', 'legacy')
        conn = ll.get_connection()
        conn.execute(f'''CREATE TABLE "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0,
            last_practiced DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            last_known_review_at TEXT,
            drill_pending INTEGER NOT NULL DEFAULT 0,
            times_flagged INTEGER NOT NULL DEFAULT 0,
            stage_reached INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute(
            f'INSERT INTO "{table}" (content_id, score, times_practiced, leitner_box) VALUES (?,?,?,?)',
            ('legacy-item', 9.0, 7, 4),
        )
        ll.ensure_word_table(conn, 'alice', 'legacy')
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        saved = conn.execute(
            f'SELECT content_id, score, times_practiced, leitner_box FROM "{table}"'
        ).fetchone()
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        conn.commit(); conn.close()
        self.assertEqual(columns, [
            'id', 'content_id', 'score', 'last_practiced', 'active',
            'times_practiced', 'times_correct', 'times_incorrect', 'times_drilled',
            'times_mastered', 'leitner_box', 'last_known_review_at',
        ])
        self.assertEqual(saved, ('legacy-item', 9.0, 7, 4))
        self.assertEqual(version, 3)

    def test_no_id_material_identity_survives_definition_edit(self):
        path = self.personal_path(lang='stable')
        items = [
            {'word': 'das Haus', 'definition': ['house'], 'word_frequency': 0},
            {'word': 'die Stadt', 'definition': ['city'], 'word_frequency': 1},
        ]
        write_material(path, items)
        ll.sync_word_list('alice', 'stable')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'stable')
        before = conn.execute(
            f'SELECT content_id FROM "{table}" ORDER BY id'
        ).fetchall()
        conn.close()
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['items'][0]['definition'] = ['a building used as a home']
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        ll.sync_word_list('alice', 'stable')
        conn = ll.get_connection()
        after = conn.execute(f'SELECT content_id FROM "{table}" ORDER BY id').fetchall()
        conn.close()
        self.assertEqual(after, before)

    def test_editor_first_personal_save_is_lossless_and_persists_generated_ids(self):
        shared = self.lists / 'german' / 'vocabulary' / 'shared.json'
        source = write_material(shared, [
            {
                'word': 'das Haus',
                'definition': ['house', 'The house is new.', 'extra line'],
                'word_frequency': 5,
                'custom': {'keep': True},
            },
            {
                'word': 'die Stadt',
                'definition': ['city'],
                'word_frequency': 2,
                'custom': 'also-keep',
            },
        ], source='test')
        shared_bytes = shared.read_bytes()
        loaded = web.load_word_list('alice', 'shared')
        first_ids = [item['id'] for item in loaded['items']]
        loaded['items'][0]['definition'][0] = 'home'
        web.save_word_list('alice', 'shared', loaded['items'])
        personal = self.personal_path(lang='shared')
        saved = ll.read_word_list(personal)
        self.assertEqual(shared.read_bytes(), shared_bytes)
        self.assertEqual([item['id'] for item in saved['items']], first_ids)
        self.assertEqual(saved['items'][0]['definition'], ['home', 'The house is new.', 'extra line'])
        self.assertEqual(saved['items'][0]['custom'], {'keep': True})
        self.assertEqual(saved['items'][0]['word_frequency'], 5)
        self.assertEqual(saved['metadata']['kind'], 'vocabulary')
        self.assertEqual(saved['metadata']['level'], 'a1')
        second = web.load_word_list('alice', 'shared')
        second['items'][0]['definition'][0] = 'home again'
        web.save_word_list('alice', 'shared', second['items'])
        saved2 = ll.read_word_list(personal)
        self.assertEqual([item['id'] for item in saved2['items']], first_ids)
        self.assertEqual(source['metadata']['name'], saved2['metadata']['name'])

    def test_personal_adoption_retires_sample_progress_for_only_that_user(self):
        sample = self.lists / 'tartarus_sample_german_a1.json'
        write_material(sample, vocabulary_items(1))
        original = sample.read_bytes()
        for user, score in (('alice', 5.0), ('bob', 7.0)):
            ll.sync_word_list(user, 'tartarus_sample_german_a1')
            conn = ll.get_connection()
            table = ll.words_table_name(user, 'tartarus_sample_german_a1')
            conn.execute(f'UPDATE "{table}" SET score=?', (score,))
            sessions = ll.ensure_sessions_table(conn, user)
            conn.execute(
                f'INSERT INTO "{sessions}" (language, session_date, duration_seconds, words_practiced, correct_count, incorrect_count, drilled_count) VALUES (?,?,?,?,?,?,?)',
                ('tartarus_sample_german_a1', '2026-08-07', 1, 1, 1, 0, 0),
            )
            conn.commit(); conn.close()
        web.init_word_list('alice', 'personal')
        self.assertEqual(sample.read_bytes(), original)
        conn = ll.get_connection()
        alice_table = ll.words_table_name('alice', 'tartarus_sample_german_a1')
        bob_table = ll.words_table_name('bob', 'tartarus_sample_german_a1')
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (alice_table,)
        ).fetchone())
        self.assertEqual(conn.execute(f'SELECT score FROM "{bob_table}"').fetchone()[0], 7.0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM "sessions_alice" WHERE language=?', ('tartarus_sample_german_a1',)
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM "sessions_bob" WHERE language=?', ('tartarus_sample_german_a1',)
        ).fetchone()[0], 1)
        conn.close()

    def test_backup_round_trip_is_atomic(self):
        self.make_personal(items=vocabulary_items(1))
        self.update('focus', 'id-00', score=5.5, times_practiced=3)
        backup = ll.export_user_data('alice')
        self.assertEqual(set(backup), {'format', 'version', 'user', 'word_progress', 'sessions', 'gauntlet_progress'})
        original = deepcopy(backup)
        invalid = deepcopy(backup)
        invalid['sessions'] = [{'unknown': 'field'}]
        with self.assertRaisesRegex(ValueError, 'invalid columns'):
            ll.import_user_data('alice', invalid)
        self.assertEqual(ll.export_user_data('alice'), original)
        second_db = self.root / 'restored.db'
        old = ll.DATABASE_FILE
        ll.DATABASE_FILE = str(second_db)
        try:
            ll.import_user_data('alice', backup)
            self.assertEqual(ll.export_user_data('alice'), backup)
        finally:
            ll.DATABASE_FILE = old

    def test_flagging_preserves_score_and_leitner_box(self):
        self.make_personal(items=vocabulary_items(1))
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'focus')
        word_id = conn.execute(f'SELECT id FROM "{table}"').fetchone()[0]
        conn.close()
        for score, box in ((0.0, None), (5.0, None), (9.0, 3)):
            self.update('focus', 'id-00', score=score, leitner_box=box)
            ll.update_word_score('alice', 'focus', word_id, 'flagged')
            self.assertEqual(self.row(content_id='id-00')[:2], (score, box))

    # --- Direct web session contract ---

    def test_gauntlet_forging_session_uses_focused_sixteen_word_pool(self):
        self.make_personal(items=vocabulary_items(20))
        for content_id, score in (('id-19', 8.5), ('id-18', 8.0), ('id-17', 7.0)):
            self.update('focus', content_id, score=score)
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: values.reverse()):
            session_id, session, meta = web.gauntlet_start_session('alice', 'focus')
        self.addCleanup(lambda: (web.SESSIONS_LOCK.acquire(), web.SESSIONS.pop(session_id, None), web.SESSIONS_LOCK.release()))
        self.assertEqual(meta['mode'], 'forging')
        self.assertEqual(meta['stage_name'], 'The Forging')
        self.assertEqual(len(session['queue']), 16)
        self.assertEqual([entry['word_text'] for entry in session['queue'][:3]], ['w19', 'w18', 'w17'])
        question = web.next_question(session)
        self.assertEqual(question['gauntlet']['mode'], 'forging')
        self.assertEqual(question['gauntlet']['stage'], 0)

class ServerHarness(unittest.TestCase):
    """Fresh process-level HTTP harness for each test."""

    TTS_DELAY_MS = 0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tartarus-http-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'progress.db'
        self.lists = self.root / 'word_lists'
        self.lists.mkdir()
        self.log = self.root / 'tartarus.log'
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        env = os.environ.copy()
        env.update({
            'TARTARUS_DB': str(self.db),
            'TARTARUS_WORD_LISTS_DIR': str(self.lists),
            'TARTARUS_PORT': str(self.port),
            'TARTARUS_LOG_FILE': str(self.log),
            'TARTARUS_SESSION_TTL_SECONDS': '2',
            'TARTARUS_TTS_TEST_DELAY_MS': str(self.TTS_DELAY_MS),
            'PYTHONDONTWRITEBYTECODE': '1',
        })
        self.server = subprocess.Popen(
            [sys.executable, str(UTILS / 'tartarus_web.py')],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                stderr = self.server.stderr.read()
                self.fail(f'web server exited during startup: {stderr}')
            try:
                status, _ = self.request_raw('/')
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.04)
        else:
            self.fail('web server did not become ready')

    def stop_server(self):
        server = getattr(self, 'server', None)
        if not server:
            return
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)
        if server.stderr and not server.stderr.closed:
            server.stderr.close()

    def request_raw(self, path, payload=None, *, raw_body=None, content_type=None, method=None, timeout=10):
        if raw_body is not None:
            body = raw_body
        elif payload is not None:
            body = json.dumps(payload).encode('utf-8')
        else:
            body = None
        headers = {}
        if body is not None:
            headers['Content-Type'] = content_type or 'application/json'
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=headers,
            method=method or ('POST' if body is not None else 'GET'),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def request(self, path, payload=None, **kwargs):
        status, raw = self.request_raw(path, payload, **kwargs)
        data = json.loads(raw or b'{}') if path.startswith('/api/') else raw
        return status, data

    def api(self, path, payload=None, expected=200, **kwargs):
        status, data = self.request(path, payload, **kwargs)
        self.assertEqual(status, expected, (path, status, data))
        return data

    def create_user(self, user='alice'):
        return self.api('/api/user/create', {'user': user})

    def create_personal(self, *, user='alice', lang='focus', items=None):
        self.create_user(user)
        self.api('/api/init', {'user': user, 'lang': lang})
        self.api('/api/wordlist', {
            'user': user,
            'lang': lang,
            'items': items or vocabulary_items(1),
        })

    def start(self, *, user='alice', lang='focus', mode=None, expected=200):
        payload = {'user': user, 'lang': lang}
        if mode is not None:
            payload['mode'] = mode
        return self.api('/api/practice/start', payload, expected=expected)

    def answer(self, started, answer, attempt_id, *, question=None, expected=200, **overrides):
        q = question or started['question']
        payload = {
            'session_id': started['session_id'],
            'question_id': q['question_id'],
            'sequence': q['sequence'],
            'attempt_id': attempt_id,
            'answer': answer,
            **overrides,
        }
        return self.api('/api/practice/answer', payload, expected=expected)


class HttpContractTest(ServerHarness):
    TTS_DELAY_MS = 80

    def test_http_requires_complete_noun_entry_in_normal_gauntlet_flow(self):
        self.create_personal(items=[{
            'id': 'buch', 'word': 'das Buch, die Bücher',
            'definition': ['book'], 'word_frequency': 0,
        }])
        started = self.start()
        self.assertEqual(started['gauntlet']['mode'], 'forging')
        self.assertEqual(started['question']['gauntlet']['stage_name'], 'The Forging')
        self.assertNotIn('noun_case', started['question'])

        singular_only = self.answer(started, 'das Buch', 'singular-only')
        self.assertEqual(singular_only['result'], 'drill_start')
        self.api('/api/practice/cancel', {'session_id': started['session_id']})

        started = self.start()
        complete = self.answer(started, 'das Buch, die Bücher', 'complete-entry')
        self.assertEqual(complete['result'], 'correct')

        status, noun_api = self.request('/api/noun')
        self.assertEqual(status, 404)
        self.assertEqual(noun_api.get('error'), 'not found')

        rejected = self.api('/api/practice/start', {
            'user': 'alice', 'lang': 'focus', 'review_mode': True,
        }, expected=400)
        self.assertIn('due-review mode has been removed', rejected['error'])

    def test_http_selection_pool_is_high_score_first_and_fixed_at_sixteen(self):
        self.create_personal(items=vocabulary_items(20))
        conn = sqlite3.connect(self.db)
        for content_id, score in (('id-19', 8.5), ('id-18', 8.0), ('id-17', 7.0)):
            conn.execute('UPDATE "words_alice_focus" SET score=? WHERE content_id=?', (score, content_id))
        conn.commit(); conn.close()
        started = self.start()
        self.assertEqual(started['progress']['total'], 16)
        self.assertEqual(started['progress']['max_questions'], 16)
        self.assertEqual(started['question']['word_unmasked'], 'w19')
        self.api('/api/practice/cancel', {'session_id': started['session_id']})

    def test_http_one_correct_answer_does_not_end_a_sixteen_word_session(self):
        self.create_personal(items=vocabulary_items(20))
        started = self.start()
        self.assertEqual(started['progress']['total'], 16)
        self.assertEqual(started['progress']['max_questions'], 16)
        first = started['question']['word_unmasked']
        result = self.answer(started, first, 'first-correct')
        self.assertFalse(result['done'])
        self.assertEqual(result['progress']['questions'], 1)
        self.assertEqual(result['progress']['max_questions'], 16)
        self.assertIn('question', result)
        self.assertNotEqual(result['question']['question_id'], started['question']['question_id'])
        self.api('/api/practice/cancel', {'session_id': started['session_id']})


    def test_http_dual_track_uses_leitner_maintenance_inside_same_gauntlet_entry(self):
        self.create_personal(items=vocabulary_items(1))
        today = time.strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_focus" SET score=9, leitner_box=9, last_practiced=?', ('2000-01-01',))
        conn.execute('INSERT OR REPLACE INTO dataset_progress (user,lang,current_stage,current_day,sessions_done_today,last_practice_date) VALUES (?,?,?,?,?,?)',
                     ('alice','focus',1,1,0,today))
        conn.commit(); conn.close()
        started = self.start()
        self.assertEqual(started['gauntlet']['mode'], 'maintenance')
        self.assertEqual(started['gauntlet']['stage_name'], 'Leitner Review')
        self.assertEqual(started['question']['type'], 'maintenance')
        result = self.answer(started, 'w00', 'maintenance-correct')
        self.assertEqual(result['result'], 'correct')
        conn = sqlite3.connect(self.db)
        saved = conn.execute('SELECT score, leitner_box FROM "words_alice_focus" WHERE content_id=?', ('id-00',)).fetchone()
        conn.close()
        self.assertEqual(saved, (9.0, 10))

    def test_http_wrong_answer_drill_is_session_local_and_idempotent(self):
        self.create_personal(items=vocabulary_items(1))
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_focus" SET score=4.0 WHERE content_id=?', ('id-00',))
        conn.commit(); conn.close()
        started = self.start()
        wrong = self.answer(started, 'wrong', 'same')
        self.assertEqual(wrong['result'], 'drill_start')
        self.assertEqual(self.answer(started, 'wrong', 'same'), wrong)
        for index in range(9):
            result = self.answer(started, 'w00', f'drill-{index}')
        self.assertEqual(result['result'], 'drilled')
        self.assertTrue(result['done'])
        conn = sqlite3.connect(self.db)
        saved = conn.execute(
            'SELECT score, times_practiced, times_incorrect, times_drilled FROM "words_alice_focus" WHERE content_id=?',
            ('id-00',),
        ).fetchone()
        conn.close()
        self.assertEqual(saved, (4.5, 2, 1, 1))
        restarted = self.start()
        self.assertNotEqual(restarted['question']['type'], 'drill')
        self.api('/api/practice/cancel', {'session_id': restarted['session_id']})

    def test_http_stale_or_missing_attempt_keys_cannot_mutate_progress(self):
        self.create_personal(items=vocabulary_items(1))
        started = self.start()
        question = started['question']
        stale = self.answer(started, 'w00', 'stale', question_id='bad', expected=409)
        self.assertIn('stale', stale['error'])
        stale_seq = self.answer(started, 'w00', 'stale-seq', sequence=999, expected=409)
        self.assertIn('stale', stale_seq['error'])
        missing = self.answer(started, 'w00', '', expected=400)
        self.assertIn('idempotency', missing['error'])
        conn = sqlite3.connect(self.db)
        counters = conn.execute(
            'SELECT times_practiced, times_correct, times_incorrect FROM "words_alice_focus" WHERE content_id=?',
            ('id-00',),
        ).fetchone()
        conn.close()
        self.assertEqual(counters, (0, 0, 0))
        self.api('/api/practice/cancel', {'session_id': started['session_id']})

    def test_http_dashboard_exposes_original_dual_track_roadmap_and_review_mode_is_gone(self):
        self.create_personal(items=vocabulary_items(1))
        dashboard = self.api('/api/dashboard?user=alice&lang=focus')
        roadmap = dashboard['roadmap']
        self.assertEqual(roadmap['gauntlet']['stage_name'], 'The Forging')
        self.assertEqual(set(roadmap['leitner_distribution']), {str(i) for i in range(1, 11)})
        status, progress = self.request('/api/gauntlet/progress?user=alice&lang=focus')
        self.assertEqual(status, 200)
        self.assertEqual(progress['roadmap']['gauntlet']['stage_name'], 'The Forging')
        rejected = self.api('/api/practice/start', {'user':'alice','lang':'focus','review_mode':True}, expected=400)
        self.assertIn('due-review mode has been removed', rejected['error'])

    def test_http_request_validation_and_tts_are_bounded(self):
        status, invalid = self.request('/api/user/create', raw_body=b'{}', content_type='text/plain')
        self.assertEqual(status, 400)
        self.assertIn('error', invalid)
        status, oversized = self.request(
            '/api/user/create', raw_body=b'{' + b'x' * 1_000_001,
            content_type='application/json',
        )
        self.assertEqual(status, 400)
        self.assertIn('error', oversized)
        status, unknown = self.request('/api/not-a-route')
        self.assertEqual((status, unknown.get('error')), (404, 'not found'))
        started = time.monotonic()
        status, tts = self.request('/api/tts', {'text': 'test', 'lang': 'english', 'wpm': 128})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertTrue(tts.get('simulated'))
        self.assertGreaterEqual(elapsed, 0.06)


class CliContractTest(unittest.TestCase):
    setUp = IsolatedCoreTest.setUp
    _restore_modules = IsolatedCoreTest._restore_modules
    personal_path = IsolatedCoreTest.personal_path
    make_personal = IsolatedCoreTest.make_personal
    row = IsolatedCoreTest.row
    update = IsolatedCoreTest.update

    def test_cli_help_keeps_supported_modes_and_removes_obsolete_drill_mode(self):
        result = subprocess.run(
            [sys.executable, str(UTILS / 'tartarus.py'), 'practice', '--help'],
            cwd=ROOT, capture_output=True, text=True, check=True,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        help_text = result.stdout
        for option in ('--drill', '--instant-drill', '--known-drill-mode', '--fast'):
            self.assertIn(option, help_text)
        self.assertNotIn('--drill-mode', help_text)

    def test_cli_normal_session_requires_complete_multi_form_noun(self):
        self.make_personal(items=[{
            'id':'buch','word':'das Buch, die Bücher','definition':['book'],'word_frequency':0,
        }])
        with mock.patch.object(ll, 'clear_screen'), mock.patch.object(ll.time, 'sleep'):
            with mock.patch('builtins.input', side_effect=['das Buch, die Bücher']):
                ll.start_practice_session('alice', 'focus', audio=False)
        self.assertEqual(self.row(content_id='buch')[0], 0.5)

class ChromiumCDP:
    """Small CDP adapter so the browser contract runs without Selenium."""

    def __init__(self):
        import websocket
        self.websocket = websocket
        self.temp = tempfile.TemporaryDirectory(prefix='tartarus-chromium-')
        self.port = free_port()
        executable = (
            os.environ.get('TARTARUS_CHROMIUM')
            or shutil.which('chromium')
            or shutil.which('chromium-browser')
            or shutil.which('google-chrome')
        )
        if not executable:
            self.temp.cleanup()
            raise unittest.SkipTest('No Chromium executable available for browser contract.')
        self.process = subprocess.Popen([
            executable,
            '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
            '--remote-allow-origins=*', f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.temp.name}', 'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = None
        self._id = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.close()
                raise AssertionError('Chromium exited before CDP became ready.')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/list', timeout=1) as response:
                    pages = json.load(response)
                if pages:
                    self.ws = websocket.create_connection(pages[0]['webSocketDebuggerUrl'], timeout=30)
                    break
            except Exception:
                time.sleep(0.04)
        if self.ws is None:
            self.close()
            raise AssertionError('Chromium CDP did not become ready.')
        self._call('Page.enable')
        self._call('Runtime.enable')

    def _call(self, method, params=None):
        self._id += 1
        call_id = self._id
        self.ws.send(json.dumps({'id': call_id, 'method': method, 'params': params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get('id') != call_id:
                continue
            if 'error' in message:
                raise AssertionError(f'CDP {method} failed: {message["error"]}')
            return message.get('result', {})

    def open(self, url):
        self._call('Page.navigate', {'url': url})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                href = self.script("return window.location.href;") or ''
                ready = self.script("return document.readyState === 'complete';")
                if href.startswith(url) and ready:
                    return
            except AssertionError:
                pass
            time.sleep(0.03)
        raise AssertionError(f'Browser page did not load: {url}')

    def script(self, script, *args):
        expression = f"(function(){{{script}}}).apply(null,{json.dumps(args, ensure_ascii=False)})"
        result = self._call('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
        })
        if result.get('exceptionDetails'):
            raise AssertionError(f'Browser script failed: {result}')
        return result.get('result', {}).get('value')

    def viewport(self, width, height):
        self._call('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False,
        })

    def close(self):
        ws = getattr(self, 'ws', None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            self.ws = None
        process = getattr(self, 'process', None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        temp = getattr(self, 'temp', None)
        if temp is not None:
            try:
                temp.cleanup()
            except OSError:
                shutil.rmtree(temp.name, ignore_errors=True)


class BrowserContractTest(unittest.TestCase):
    """Real DOM/browser coverage with a deterministic in-page API double.

    This sandbox's Chromium policy blocks loopback navigation, so backend HTTP
    behavior is tested separately above. Here the real shipped HTML/CSS/app.js
    execute in Chromium while ``fetch`` is replaced with a small API double.
    TTS uses a real 650ms timer to emulate the blocking macOS ``say`` call.
    """

    def setUp(self):
        self.browser = ChromiumCDP()
        self.addCleanup(self.browser.close)
        self.browser.viewport(1280, 900)
        index = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        css = (ROOT / 'web' / 'style.css').read_text(encoding='utf-8')
        app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
        import re
        index = re.sub(r'<link\s+rel="stylesheet"[^>]*>', f'<style>{css}</style>', index, count=1)
        index = re.sub(r'<script\s+src="/app\.js[^>]*></script>', '', index, count=1)
        self.browser.script(
            "document.open();document.write(arguments[0]);document.close();return true;",
            index,
        )
        self.browser.script(r"""
          window.__errors=[]; window.addEventListener('error', e=>window.__errors.push(String(e.error||e.message))); window.addEventListener('unhandledrejection', e=>window.__errors.push(String(e.reason)));
          const state = window.__testApi = {
            ttsDelay: 650,
            ttsCalls: 0,
            answerCount: 0,
            currentIndex: 0,
            questions: [
              {question_id:'q0',sequence:1,word:'w00',word_unmasked:'w00',audio_text:'w00',definition:['definition 00'],score:0,gauge:'○○○',band:1,gender:'none',type:'learning',sentence_mode:false,can_reveal:true,gauntlet:{mode:'forging',stage:0,stage_name:'The Forging',day:0,sessions_done:0}},
              {question_id:'q1',sequence:2,word:'w01',word_unmasked:'w01',audio_text:'w01',definition:['definition 01'],score:0,gauge:'○○○',band:1,gender:'none',type:'learning',sentence_mode:false,can_reveal:true,gauntlet:{mode:'forging',stage:0,stage_name:'The Forging',day:0,sessions_done:0}}
            ]
          };
          const jsonResponse = (payload, status=200) => new Response(JSON.stringify(payload), {
            status, headers:{'Content-Type':'application/json'}
          });
          window.fetch = function(input, init={}) {
            const url = String(typeof input === 'string' ? input : input.url || '');
            if (url.startsWith('/api/wordlists')) {
              return Promise.resolve(jsonResponse({
                users:['alice'],
                wordlists:[{user:'alice',owner:null,lang:'focus',language:'german',kind:'vocabulary',category:'german_vocabulary',cefr_level:'a1',pos:'noun',name:'Focused German',word_count:20,ordered:false,shared:true}]
              }));
            }
            if (url.startsWith('/api/user/progress')) {
              return Promise.resolve(jsonResponse({lists:[]}));
            }
            if (url.startsWith('/api/gauntlet/progress')) {
              return Promise.resolve(jsonResponse({
                progress:{current_stage:0,current_day:0,sessions_done_today:0,stage_name:'The Forging',session_mode:'forging',remaining_tasks:20,total_tasks:20,max_day:10,locked_today:false},
                roadmap:{
                  gauntlet:{current_stage:0,current_day:0,sessions_done_today:0,stage_name:'The Forging',remaining_tasks:20,total_tasks:20},
                  leitner_distribution:{'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,'10':0}
                }
              }));
            }
            if (url === '/api/practice/start') {
              state.currentIndex = 0;
              return Promise.resolve(jsonResponse({
                session_id:'session-browser',lang:'focus',audio_lang:'german',fast_mode:false,review_mode:false,gauntlet:{mode:'forging',stage:0,stage_name:'The Forging',day:0,sessions_done_today:0},
                progress:{correct:0,drilled:0,total:16,questions:0,max_questions:16},
                question:state.questions[0]
              }));
            }
            if (url === '/api/practice/answer') {
              const body = JSON.parse(init.body || '{}');
              state.answerCount += 1;
              const current = state.questions[state.currentIndex];
              if (body.answer === '!!') {
                return Promise.resolve(jsonResponse({result:'end',done:true,session:{practiced:0,correct:0,drilled:0,incorrect:[],elapsed_seconds:0}}));
              }
              if (body.answer !== current.word_unmasked) {
                return Promise.resolve(jsonResponse({result:'incorrect',done:false,word:current.word_unmasked,message:'Incorrect',question:state.questions[Math.min(state.currentIndex+1,1)],progress:{correct:0,drilled:0,total:16,questions:1,max_questions:16}}));
              }
              state.currentIndex = Math.min(state.currentIndex + 1, 1);
              return Promise.resolve(jsonResponse({
                result:'correct',word:current.word_unmasked,done:false,
                question:state.questions[state.currentIndex],
                progress:{correct:1,drilled:0,total:16,questions:1,max_questions:16}
              }));
            }
            if (url === '/api/tts') {
              state.ttsCalls += 1;
              return new Promise(resolve => setTimeout(() => resolve(jsonResponse({supported:true,spoken:true,simulated:true})), state.ttsDelay));
            }
            return Promise.resolve(jsonResponse({error:'not found'},404));
          };
          return true;
        """)
        self.browser.script("eval(arguments[0]); return true;", app)
        self.wait_js("return document.querySelectorAll('#practice-user option').length > 1;")
        self.select('practice-user', 'alice')
        self.select('practice-lang', 'german_vocabulary')
        self.select('practice-level', 'a1')
        self.select('practice-pos', 'noun')
        self.select('practice-file', 'focus')

    def wait_js(self, script, timeout=8, message='browser condition timed out'):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.browser.script(script)
            if last:
                return last
            time.sleep(0.03)
        self.fail(f'{message}; last={last!r}')

    def select(self, element_id, value):
        self.wait_js(f"return [...document.getElementById('{element_id}').options].some(o => o.value === {json.dumps(value)});")
        ok = self.browser.script(
            "const el=document.getElementById(arguments[0]);"
            "el.value=arguments[1];el.dispatchEvent(new Event('change',{bubbles:true}));"
            "return el.value===arguments[1];",
            element_id, value,
        )
        self.assertTrue(ok, (element_id, value))

    def test_definition_center_and_blocking_speech_allows_typing_only(self):
        self.wait_js("return document.querySelector('#practice-roadmap-container .roadmap-card') !== null;")
        leitner_geometry = self.browser.script(r"""
          const nodes=[...document.querySelectorAll('#practice-roadmap-container .leitner-roadmap-node')];
          const squares=nodes.map(n=>n.querySelector('.leitner-roadmap-square').getBoundingClientRect());
          return {
            count:squares.length,
            topSpread: Math.max(...squares.map(r=>r.top)) - Math.min(...squares.map(r=>r.top)),
            squareDelta: Math.max(...squares.map(r=>Math.abs(r.width-r.height))),
            ordered: squares.every((r,i)=>i===0 || r.left > squares[i-1].left),
          };
        """)
        self.assertEqual(leitner_geometry['count'], 10, leitner_geometry)
        self.assertLessEqual(leitner_geometry['topSpread'], 1.0, leitner_geometry)
        self.assertLessEqual(leitner_geometry['squareDelta'], 1.0, leitner_geometry)
        self.assertTrue(leitner_geometry['ordered'], leitner_geometry)
        setup_body = self.browser.script("return document.getElementById('practice-setup').innerText.toLowerCase();")
        self.assertIn('leitner', setup_body)
        self.assertIn('enter the gauntlet', setup_body)
        for stage in ('the forging', 'the crucible', 'the shadows', 'the depths', 'the void', 'ascension'):
            self.assertIn(stage, setup_body)
        self.assertNotIn('priority path', setup_body)
        self.assertIsNone(self.browser.script("return document.getElementById('start-review');"))
        self.assertIsNone(self.browser.script("return document.getElementById('start-leitner');"))

        self.browser.script("document.getElementById('start-session').click(); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-session')).display !== 'none';")
        self.wait_js("return !document.getElementById('answer-input').disabled && document.getElementById('submit-answer').disabled;")

        geometry = self.browser.script(r"""
          const wordBlock = document.getElementById('word-block').getBoundingClientRect();
          const defs = document.getElementById('definition-lines').getBoundingClientRect();
          const style = getComputedStyle(document.getElementById('definition-lines'));
          return {delta: Math.abs((wordBlock.left + wordBlock.width/2) - (defs.left + defs.width/2)), textAlign: style.textAlign};
        """)
        self.assertLessEqual(geometry['delta'], 1.0, geometry)
        self.assertEqual(geometry['textAlign'], 'center')

        initial_word = self.browser.script("return document.getElementById('word-display').textContent;")
        self.assertEqual(initial_word, 'w00')
        self.assertEqual(self.browser.script("return window.__testApi.answerCount;"), 0)

        self.browser.script(r"""
          const input=document.getElementById('answer-input');
          input.value='typed while speech is running';
          input.dispatchEvent(new Event('input',{bubbles:true}));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
          document.getElementById('btn-replay').click();
          document.getElementById('btn-flag').click();
          document.querySelector('nav button[data-view="report"]').click();
          document.getElementById('btn-end').click();
          return true;
        """)
        time.sleep(0.10)  # intentionally inside the 650ms emulated speech window
        during = self.browser.script(r"""
          return {
            typed: document.getElementById('answer-input').value,
            practiceActive: document.getElementById('view-practice').classList.contains('active'),
            sameWord: document.getElementById('word-display').textContent,
            submitDisabled: document.getElementById('submit-answer').disabled,
            endDisabled: document.getElementById('btn-end').disabled,
            answerCount: window.__testApi.answerCount,
            ttsCalls: window.__testApi.ttsCalls
          };
        """)
        self.assertEqual(during['typed'], 'typed while speech is running')
        self.assertTrue(during['practiceActive'])
        self.assertEqual(during['sameWord'], initial_word)
        self.assertTrue(during['submitDisabled'])
        self.assertTrue(during['endDisabled'])
        self.assertEqual(during['answerCount'], 0)
        self.assertEqual(during['ttsCalls'], 1)

        self.wait_js("return !document.getElementById('submit-answer').disabled;", timeout=3)
        self.assertEqual(
            self.browser.script("return document.getElementById('answer-input').value;"),
            'typed while speech is running',
        )

        self.browser.script(
            "const i=document.getElementById('answer-input');i.value=arguments[0];"
            "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));return true;",
            initial_word,
        )
        self.wait_js("return document.getElementById('feedback').textContent.startsWith('Correct!');", timeout=3)
        self.assertEqual(self.browser.script("return window.__testApi.answerCount;"), 1)
        self.assertEqual(self.browser.script("return document.getElementById('word-display').textContent;"), initial_word)
        self.assertTrue(self.browser.script("return document.getElementById('submit-answer').disabled;"))
        self.browser.script("document.querySelector('nav button[data-view=report]').click(); return true;")
        self.assertTrue(self.browser.script("return document.getElementById('view-practice').classList.contains('active');"))
        self.wait_js(
            f"return document.getElementById('word-display').textContent !== {json.dumps(initial_word)};",
            timeout=4,
            message='card did not advance after feedback speech completed',
        )
        self.assertEqual(self.browser.script("return document.getElementById('word-display').textContent;"), 'w01')
        self.assertEqual(self.browser.script("return window.__testApi.answerCount;"), 1)

        body = self.browser.script("return document.body.innerText.toLowerCase();")
        self.assertNotIn('priority path', body)
        self.assertNotIn('review due', body)
        self.assertNotIn('due today', body)

    def test_enter_moves_summary_to_setup_then_starts_next_session(self):
        self.browser.script("document.getElementById('start-session').click(); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-session')).display !== 'none';")
        self.wait_js("return !document.getElementById('btn-end').disabled;", timeout=3)
        self.browser.script("document.getElementById('btn-end').click(); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-summary')).display !== 'none';")

        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true})); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-setup')).display !== 'none';")
        self.assertEqual(
            self.browser.script("return getComputedStyle(document.getElementById('practice-summary')).display;"),
            'none',
        )

        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true})); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-session')).display !== 'none';")
        self.assertEqual(
            self.browser.script("return document.getElementById('session-progress').textContent;"),
            'The Forging · Day 0/10 · Q1/16',
        )

    def test_mobile_and_desktop_definition_remain_centered_without_horizontal_overflow(self):
        self.browser.script("document.getElementById('start-session').click(); return true;")
        self.wait_js("return getComputedStyle(document.getElementById('practice-session')).display !== 'none';")
        for width, height in ((1280, 900), (390, 800)):
            self.browser.viewport(width, height)
            geometry = self.browser.script(r"""
              const block=document.getElementById('word-block').getBoundingClientRect();
              const defs=document.getElementById('definition-lines').getBoundingClientRect();
              return {
                delta: Math.abs((block.left+block.width/2)-(defs.left+defs.width/2)),
                overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                left: defs.left,
                right: defs.right,
                viewport: document.documentElement.clientWidth,
              };
            """)
            self.assertLessEqual(geometry['delta'], 1.0, geometry)
            self.assertLessEqual(geometry['overflow'], 1, geometry)
            self.assertGreaterEqual(geometry['left'], -1, geometry)
            self.assertLessEqual(geometry['right'], geometry['viewport'] + 1, geometry)


class StaticContractTest(unittest.TestCase):
    def test_web_ui_has_one_gauntlet_entry_and_original_dual_track_roadmap(self):
        app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
        html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        combined = (app + '\n' + html).lower()
        self.assertEqual(html.count('id="start-session"'), 1)
        self.assertIn('enter the gauntlet', combined)
        self.assertIn('practice-roadmap-container', combined)
        self.assertIn('roadmap-timeline', combined)
        self.assertIn('lifetime leitner maintenance', combined)
        self.assertNotIn('id="start-leitner"', combined)
        self.assertNotIn('id="start-review"', combined)
        self.assertNotIn('priority path', combined)
        for stage in ('the forging', 'the crucible', 'the shadows', 'the depths', 'the void', 'ascension'):
            self.assertIn(stage, combined)

    def test_roadmap_is_the_original_six_stage_component_not_a_score_timeline(self):
        app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
        css = (ROOT / 'web' / 'style.css').read_text(encoding='utf-8')
        self.assertIn("{ id: 0, name: 'The Forging', days: 'Day 0' }", app)
        self.assertIn("{ id: 5, name: 'Ascension', days: 'Days 9-10' }", app)
        self.assertIn('roadmap-stage-progress-wrap', app)
        self.assertIn('renderLeitnerRoadmap', app)
        self.assertIn('leitner-roadmap-track', app)
        self.assertIn('timeline-node ${statusClass}', app)
        self.assertNotIn('for (let score = 0; score <= 9; score += 1)', app)
        self.assertNotIn('Score 0–1.5', app)
        self.assertIn('.roadmap-timeline::before', css)
        self.assertIn('.timeline-node.active .node-circle', css)
        self.assertIn('.leitner-roadmap-track::before', css)
        self.assertIn('.leitner-roadmap-square', css)
        self.assertIn('aspect-ratio: 1 / 1', css)


    def test_leitner_boxes_use_one_horizontal_square_roadmap_in_practice_and_report(self):
        app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
        css = (ROOT / 'web' / 'style.css').read_text(encoding='utf-8')
        self.assertGreaterEqual(app.count('renderLeitnerRoadmap('), 3)
        self.assertIn('display: flex;', css[css.index('.leitner-roadmap-track'):])
        self.assertIn('min-width: 760px;', css)
        self.assertIn('overflow-x: auto;', css)
        track_rule = css[css.index('.leitner-roadmap-track {'):css.index('.leitner-roadmap-track::before')]
        self.assertNotIn('flex-direction: column;', track_rule)

    def test_single_test_file_policy(self):
        tests = sorted(
            path.name for path in UTILS.iterdir()
            if path.is_file() and (path.name.startswith('test_') or path.name.startswith('e2e_')) and path.suffix == '.py'
        )
        self.assertEqual(tests, ['test_tartarus.py'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
