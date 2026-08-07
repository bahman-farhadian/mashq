import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import tartarus as ll
import tartarus_web as web


class MaterialContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='tartarus-material-'))
        self.old_database = ll.DATABASE_FILE
        self.old_word_lists = ll.WORD_LISTS_DIR
        ll.DATABASE_FILE = str(self.root / 'progress.db')
        ll.WORD_LISTS_DIR = str(self.root / 'word_lists')
        Path(ll.WORD_LISTS_DIR).mkdir()
        conn = ll.get_connection()
        for user in ('alice', 'alice_ann', 'bob'):
            ll.ensure_user(conn, user)
        conn.commit()
        conn.close()

    def tearDown(self):
        ll.DATABASE_FILE = self.old_database
        ll.WORD_LISTS_DIR = self.old_word_lists
        shutil.rmtree(self.root)

    def write_list(self, path, items, **metadata):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'metadata': {
                'language': 'german', 'kind': 'vocabulary', 'level': 'a1',
                'name': path.stem, **metadata,
            },
            'items': items,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return payload

    def test_word_table_legacy_migration_is_transactional_and_versioned(self):
        table = ll.words_table_name('alice', 'legacy')
        conn = ll.get_connection()
        conn.execute(f'''CREATE TABLE "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0.0,
            last_practiced DATE,
            last_decay_at DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            stage_reached INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute(
            f'INSERT INTO "{table}" (content_id, score, times_practiced, leitner_box) '
            'VALUES (?, ?, ?, ?)', ('old-item', 9.0, 3, 1)
        )
        ll.ensure_word_table(conn, 'alice', 'legacy')
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        row = conn.execute(
            f'SELECT content_id, score, times_practiced, leitner_box FROM "{table}"'
        ).fetchone()
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        conn.commit()
        conn.close()
        self.assertNotIn('last_decay_at', columns)
        self.assertNotIn('stage_reached', columns)
        self.assertNotIn('drill_pending', columns)
        self.assertNotIn('times_flagged', columns)
        self.assertEqual(row, ('old-item', 9.0, 3, 1))
        self.assertGreaterEqual(version, ll.SCHEMA_VERSION)

    def test_review_selector_returns_only_due_mastered_items(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_review.json'
        self.write_list(path, [
            {'id': 'due', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
            {'id': 'new', 'word': 'zwei', 'definition': ['two'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'review')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'review')
        conn.execute(f'UPDATE "{table}" SET score = 9, leitner_box = 1, last_practiced = ? WHERE content_id = ?', ('2026-01-01', 'due'))
        conn.execute(f'UPDATE "{table}" SET score = 9, leitner_box = 1, last_practiced = ? WHERE content_id = ?', (ll.date.today().isoformat(), 'new'))
        conn.commit()
        conn.close()
        reviewed = ll.get_due_review_words('alice', 'review')
        self.assertEqual([row[1] for row in reviewed], ['eins'])

    def test_versioned_backup_round_trip_and_atomic_rejection(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_backup.json'
        self.write_list(path, [{'id': 'item', 'word': 'das Haus', 'definition': ['house'], 'word_frequency': 1}])
        ll.sync_word_list('alice', 'backup')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'backup')
        conn.execute(f'UPDATE "{table}" SET score = 5.0 WHERE content_id = ?', ('item',))
        ll.ensure_sessions_table(conn, 'alice')
        conn.execute('INSERT INTO "sessions_alice" (language, session_date, duration_seconds, words_practiced, correct_count, incorrect_count, drilled_count) VALUES (?, ?, ?, ?, ?, ?, ?)', ('backup', '2026-08-07', 12, 1, 1, 0, 0))
        conn.execute('INSERT INTO dataset_progress (user, lang, current_stage, current_day, sessions_done_today, last_practice_date) VALUES (?, ?, ?, ?, ?, ?)', ('alice', 'backup', 2, 3, 1, '2026-08-07'))
        conn.commit()
        conn.close()
        backup = ll.export_user_data('alice')
        self.assertEqual(backup['format'], ll.BACKUP_FORMAT)
        # drill_pending is no longer a persisted field in the schema
        first_row = backup['word_progress']['backup'][0]
        self.assertNotIn('drill_pending', first_row)
        self.assertNotIn('times_flagged', first_row)
        self.assertEqual(backup['gauntlet_progress'][0]['current_day'], 3)

        second_db = ll.DATABASE_FILE
        ll.DATABASE_FILE = str(self.root / 'restored.db')
        ll.import_user_data('alice', backup)
        restored = ll.export_user_data('alice')
        self.assertEqual(restored, backup)
        invalid = dict(backup)
        invalid['sessions'] = [dict(backup['sessions'][0], unknown='x')]
        with self.assertRaisesRegex(ValueError, 'invalid columns'):
            ll.import_user_data('alice', invalid)
        self.assertEqual(ll.export_user_data('alice'), restored)
        ll.DATABASE_FILE = second_db

    def test_personal_material_retires_samples_for_only_that_user(self):
        sample = Path(ll.WORD_LISTS_DIR) / 'tartarus_sample_german_a1.json'
        self.write_list(sample, [{'id': 'sample', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0}])
        original_bytes = sample.read_bytes()
        for user, score in (('alice', 5.0), ('bob', 7.0)):
            ll.sync_word_list(user, 'tartarus_sample_german_a1')
            conn = ll.get_connection()
            table = ll.words_table_name(user, 'tartarus_sample_german_a1')
            conn.execute(f'UPDATE "{table}" SET score = ? WHERE content_id = ?', (score, 'sample'))
            sessions = ll.ensure_sessions_table(conn, user)
            conn.execute(
                f'INSERT INTO "{sessions}" (language, session_date, duration_seconds, words_practiced, correct_count, incorrect_count, drilled_count) '
                'VALUES (?, ?, 1, 1, 1, 0, 0)', ('tartarus_sample_german_a1', '2026-08-07'),
            )
            ll.ensure_dataset_progress_table(conn)
            conn.execute(
                'INSERT OR REPLACE INTO dataset_progress '
                '(user, lang, current_stage, current_day, sessions_done_today, last_practice_date) '
                'VALUES (?, ?, 1, 1, 1, ?)',
                (user, 'tartarus_sample_german_a1', '2026-08-07'),
            )
            conn.commit(); conn.close()

        web.init_word_list('alice', 'custom')
        lists = [row['lang'] for row in web.list_word_lists() if row['user'] == 'alice']
        self.assertNotIn('tartarus_sample_german_a1', lists)
        self.assertEqual(sample.read_bytes(), original_bytes)

        conn = ll.get_connection()
        alice_table = ll.words_table_name('alice', 'tartarus_sample_german_a1')
        bob_table = ll.words_table_name('bob', 'tartarus_sample_german_a1')
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (alice_table,)
        ).fetchone())
        self.assertEqual(conn.execute(f'SELECT score FROM "{bob_table}" WHERE content_id=?', ('sample',)).fetchone()[0], 7.0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM "sessions_alice" WHERE language=?', ('tartarus_sample_german_a1',)
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM "sessions_bob" WHERE language=?', ('tartarus_sample_german_a1',)
        ).fetchone()[0], 1)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM dataset_progress WHERE user=? AND lang=?',
            ('alice', 'tartarus_sample_german_a1'),
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM dataset_progress WHERE user=? AND lang=?',
            ('bob', 'tartarus_sample_german_a1'),
        ).fetchone()[0], 1)
        conn.close()

    def test_personal_lists_are_not_shared_and_longest_owner_wins(self):
        word_lists = Path(ll.WORD_LISTS_DIR)
        self.write_list(word_lists / 'german' / 'vocabulary' / 'shared.json', [
            {'id': 'shared-1', 'word': 'das Haus', 'definition': ['house'], 'word_frequency': 1},
        ])
        self.write_list(word_lists / 'alice_ann_custom.json', [
            {'id': 'private-1', 'word': 'das Buch', 'definition': ['book'], 'word_frequency': 1},
        ])
        descriptors = web.list_word_lists()
        alice_ann = [item for item in descriptors if item['user'] == 'alice_ann']
        bob = [item for item in descriptors if item['user'] == 'bob']
        self.assertEqual([item['lang'] for item in alice_ann], ['custom', 'shared'])
        self.assertTrue(any(item['lang'] == 'custom' and not item['shared'] for item in alice_ann))
        self.assertEqual([item['lang'] for item in bob], ['shared'])
        self.assertTrue(all({'language', 'kind', 'category', 'cefr_level', 'pos', 'owner', 'shared'} <= item.keys() for item in descriptors))

    def test_editor_round_trip_preserves_schema_ids_and_extra_fields(self):
        source = self.write_list(Path(ll.WORD_LISTS_DIR) / 'alice_custom.json', [
            {'id': 'item-a', 'word': 'das Haus', 'definition': ['house', 'The house is new.', 'extra line'],
             'word_frequency': 5, 'pos': 'noun', 'custom': {'note': 'keep'}},
            {'id': 'item-b', 'word': 'die Stadt', 'definition': ['city'], 'word_frequency': 2},
        ], source='test')
        loaded = web.load_word_list('alice', 'custom')
        self.assertEqual(loaded['metadata'], source['metadata'])
        edited = list(reversed(loaded['items']))
        edited[1]['word'] = 'das Zuhause'
        edited[1]['definition'][0] = 'home'
        web.save_word_list('alice', 'custom', edited)
        saved = ll.read_word_list(Path(ll.WORD_LISTS_DIR) / 'alice_custom.json')
        self.assertEqual(saved['metadata'], source['metadata'])
        self.assertEqual([item['id'] for item in saved['items']], ['item-b', 'item-a'])
        self.assertEqual(saved['items'][1]['definition'], ['home', 'The house is new.', 'extra line'])
        self.assertEqual(saved['items'][1]['custom'], {'note': 'keep'})
        self.assertEqual(saved['items'][1]['word_frequency'], 5)

    def test_ambiguous_shared_stems_are_rejected(self):
        word_lists = Path(ll.WORD_LISTS_DIR)
        item = [{'id': 'a', 'word': 'word', 'definition': ['meaning'], 'word_frequency': 0}]
        self.write_list(word_lists / 'english' / 'vocabulary' / 'same.json', item)
        self.write_list(word_lists / 'german' / 'vocabulary' / 'same.json', item)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            ll.word_list_path('bob', 'same')

    def test_invalid_frequency_and_duplicate_ids_are_rejected(self):
        path = Path(ll.WORD_LISTS_DIR) / 'bad.json'
        self.write_list(path, [{'id': 'same', 'word': 'one', 'definition': 'one', 'word_frequency': 'x'},
                               {'id': 'same', 'word': 'two', 'definition': 'two', 'word_frequency': 0}])
        with self.assertRaisesRegex(ValueError, 'word_frequency'):
            ll.load_practice_items(path)

    def test_gauntlet_transition_uses_completed_prior_day_once(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_gauntlet.json'
        self.write_list(path, [
            {'id': 'one', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
            {'id': 'two', 'word': 'zwei', 'definition': ['two'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'gauntlet')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'gauntlet')
        conn.execute(f"UPDATE \"{table}\" SET score = 9, leitner_box = 1, last_practiced = '2026-08-06'")
        conn.execute("INSERT OR REPLACE INTO dataset_progress (user, lang, current_stage, current_day, sessions_done_today, last_practice_date) VALUES ('alice', 'gauntlet', 1, 1, 2, '2026-08-06')")
        conn.commit()
        conn.close()
        progressed = ll.transition_gauntlet_day('alice', 'gauntlet', '2026-08-07')
        self.assertEqual((progressed['current_day'], progressed['sessions_done_today']), (2, 0))
        repeated = ll.transition_gauntlet_day('alice', 'gauntlet', '2026-08-07')
        self.assertEqual((repeated['current_day'], repeated['sessions_done_today']), (2, 0))
        conn = ll.get_connection()
        conn.execute(f"UPDATE \"{table}\" SET last_practiced = '2026-08-06'")
        conn.execute("UPDATE dataset_progress SET current_stage = 1, current_day = 1, sessions_done_today = 1, last_practice_date = '2026-08-06' WHERE user = 'alice' AND lang = 'gauntlet'")
        conn.execute(f"UPDATE \"{table}\" SET last_practiced = NULL WHERE content_id = 'one'")
        conn.commit()
        conn.close()
        partial = ll.transition_gauntlet_day('alice', 'gauntlet', '2026-08-07')
        self.assertEqual(partial['current_day'], 1)

    def test_flagging_preserves_score_and_box(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_flag.json'
        self.write_list(path, [
            {'id': 'one', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'flag')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'flag')
        word_id = conn.execute(f'SELECT id FROM "{table}"').fetchone()[0]
        # times_flagged no longer exists; only score and leitner_box must be preserved
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        self.assertNotIn('times_flagged', columns)
        for score, box in ((0.0, None), (5.0, None), (9.0, 3)):
            conn.execute(f'UPDATE "{table}" SET score = ?, leitner_box = ? WHERE id = ?',
                         (score, box, word_id))
            conn.commit()
            ll.update_word_score('alice', 'flag', word_id, 'flagged')
            saved = conn.execute(f'SELECT score, leitner_box FROM "{table}" WHERE id = ?', (word_id,)).fetchone()
            self.assertEqual(saved, (score, box))
        conn.close()

    def test_shadows_drill_uses_two_correct_answers(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_shadows.json'
        self.write_list(path, [
            {'id': 'haus', 'word': 'das Haus', 'definition': ['house'], 'word_frequency': 0},
        ])
        session_id, session = web.start_session('alice', 'shadows', audio_lang='german')
        session.update({'is_gauntlet': True, 'gauntlet_mode': 'shadows', 'drill_all': True, 'drill_target': 2})
        question = web.next_question(session)
        self.assertEqual(question['drill_start']['target'], 2)
        first = web.process_answer(session, 'das Haus')
        self.assertEqual(first['drill']['target'], 2)
        self.assertEqual(first['drill']['correct_in_a_row'], 1)
        second = web.process_answer(session, 'das Haus')
        self.assertEqual(second['result'], 'drilled')
        web.SESSIONS.pop(session_id, None)

        path = Path(ll.WORD_LISTS_DIR) / 'alice_metrics.json'
        self.write_list(path, [
            {'id': 'new', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
            {'id': 'debt', 'word': 'zwei', 'definition': ['two'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'metrics')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'metrics')
        conn.execute(f'UPDATE "{table}" SET times_practiced = 1 WHERE content_id = ?', ('debt',))
        conn.commit()
        conn.close()
        progress = web.user_progress_data('alice')
        metrics_row = next(row for row in progress if row['lang'] == 'metrics')
        # to_drill (drill_pending) is no longer persisted; it should not appear in progress
        self.assertNotIn('to_drill', metrics_row)
        leitner = web.leitner_stats_data('alice', 'metrics')
        self.assertEqual(leitner['never_practiced'], 1)
        self.assertEqual(leitner['due_today'], 0)

    def test_final_schema_columns_and_review_era_migration(self):
        expected = [
            'id', 'content_id', 'score', 'last_practiced', 'active',
            'times_practiced', 'times_correct', 'times_incorrect', 'times_drilled',
            'times_mastered', 'leitner_box', 'last_known_review_at',
        ]
        for lang, extra_columns in (
            ('accepted', ''),
            ('review', ', drill_pending INTEGER NOT NULL DEFAULT 0, times_flagged INTEGER NOT NULL DEFAULT 0'),
        ):
            table = ll.words_table_name('alice', lang)
            conn = ll.get_connection()
            conn.execute(f'''CREATE TABLE "{table}" (
                id INTEGER PRIMARY KEY,
                content_id TEXT NOT NULL UNIQUE,
                score REAL NOT NULL DEFAULT 0.0,
                last_practiced DATE,
                active INTEGER NOT NULL DEFAULT 1,
                times_practiced INTEGER NOT NULL DEFAULT 0,
                times_correct INTEGER NOT NULL DEFAULT 0,
                times_incorrect INTEGER NOT NULL DEFAULT 0,
                times_drilled INTEGER NOT NULL DEFAULT 0,
                times_mastered INTEGER NOT NULL DEFAULT 0,
                leitner_box INTEGER,
                last_known_review_at TEXT
                {extra_columns}
            )''')
            conn.execute(
                f'INSERT INTO "{table}" (content_id, score, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, leitner_box, last_known_review_at) '
                'VALUES (?, 8.5, 7, 5, 2, 1, 1, 3, ?)',
                ('keep', '2026-08-01T10:00:00'),
            )
            ll.ensure_word_table(conn, 'alice', lang)
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            row = conn.execute(
                f'SELECT content_id, score, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, leitner_box, last_known_review_at FROM "{table}"'
            ).fetchone()
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            conn.commit(); conn.close()
            self.assertEqual(columns, expected)
            self.assertEqual(row, ('keep', 8.5, 7, 5, 2, 1, 1, 3, '2026-08-01T10:00:00'))
            self.assertEqual(version, 3)

    def test_identifier_compatibility_and_canonical_new_list_metadata(self):
        for value in ('alice_ann', 'user-name', 'list.name', 'list!name', '123'):
            self.assertEqual(ll.sanitize_name(value, 'name'), value)
        for value in ('../x', 'white space', 'quoted"name', "single'name", 'slash/name'):
            with self.assertRaises(ValueError):
                ll.sanitize_name(value, 'name')
        created, path = web.init_word_list('alice', 'list.name')
        self.assertTrue(created)
        metadata = ll.read_word_list(path)['metadata']
        self.assertEqual(metadata['kind'], 'vocabulary')
        self.assertEqual(metadata['level'], 'all')
        self.assertNotIn('type', metadata)
        self.assertNotIn('cefr_level', metadata)

    def test_generated_id_stays_stable_and_first_personal_save_persists_it(self):
        shared = Path(ll.WORD_LISTS_DIR) / 'german' / 'vocabulary' / 'shared_noid.json'
        self.write_list(shared, [
            {'word': 'das Haus', 'definition': ['house', 'line two'], 'word_frequency': 9,
             'custom': {'keep': True}},
            {'word': 'die Stadt', 'definition': ['city'], 'word_frequency': 2},
        ], source='shared')
        original_bytes = shared.read_bytes()
        first = ll.load_practice_items(shared)[0]['content_id']
        payload = ll.read_word_list(shared)
        payload['items'][0]['definition'][0] = 'home'
        payload['items'][0]['word_frequency'] = 10
        ll.write_word_list_atomic(shared, payload)
        second = ll.load_practice_items(shared)[0]['content_id']
        self.assertEqual(first, second)
        shared.write_bytes(original_bytes)
        loaded = web.load_word_list('alice', 'shared_noid')
        generated_id = loaded['items'][0]['id']
        loaded['items'][0]['definition'][0] = 'home'
        web.save_word_list('alice', 'shared_noid', loaded['items'])
        self.assertEqual(shared.read_bytes(), original_bytes)
        personal = Path(ll.WORD_LISTS_DIR) / 'alice_shared_noid.json'
        saved = ll.read_word_list(personal)
        self.assertEqual(saved['items'][0]['id'], generated_id)
        self.assertEqual(saved['items'][0]['custom'], {'keep': True})
        self.assertEqual(saved['items'][0]['word_frequency'], 9)
        self.assertEqual(saved['metadata']['source'], 'shared')
        second_loaded = web.load_word_list('alice', 'shared_noid')
        second_loaded['items'][0]['definition'][0] = 'dwelling'
        web.save_word_list('alice', 'shared_noid', second_loaded['items'])
        self.assertEqual(ll.read_word_list(personal)['items'][0]['id'], generated_id)

    def test_completed_drill_has_exact_score_and_counter_accounting(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_metrics.json'
        self.write_list(path, [{'id': 'item', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0}])
        ll.sync_word_list('alice', 'metrics')
        conn = ll.get_connection(); table = ll.words_table_name('alice', 'metrics')
        conn.execute(f'UPDATE "{table}" SET score=4.0, times_practiced=0, times_correct=0, times_incorrect=0, times_drilled=0 WHERE content_id=?', ('item',))
        conn.commit(); conn.close()
        sid, session = web.start_session('alice', 'metrics', instant_drill=True)
        web.next_question(session)
        wrong = web.process_answer(session, 'wrong')
        self.assertEqual(wrong['result'], 'drill_start')
        mid = web.process_answer(session, 'wrong')
        self.assertEqual(mid['drill']['correct_in_a_row'], 0)
        for _ in range(9):
            result = web.process_answer(session, 'eins')
        self.assertEqual(result['result'], 'drilled')
        with web.SESSIONS_LOCK:
            web.SESSIONS.pop(sid, None)
        conn = ll.get_connection()
        row = conn.execute(f'SELECT score, times_practiced, times_incorrect, times_drilled FROM "{table}" WHERE content_id=?', ('item',)).fetchone()
        conn.close()
        self.assertEqual(row, (4.5, 2, 1, 1))

    def test_read_only_due_review_does_not_mutate_progress(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_readonly.json'
        self.write_list(path, [
            {'id': 'due', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
            {'id': 'later', 'word': 'zwei', 'definition': ['two'], 'word_frequency': 0},
            {'id': 'today', 'word': 'drei', 'definition': ['three'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'readonly')
        conn = ll.get_connection(); table = ll.words_table_name('alice', 'readonly')
        conn.execute(f'UPDATE "{table}" SET score=9, leitner_box=1, times_practiced=4, times_correct=3, times_incorrect=1, last_practiced=? WHERE content_id=?', ('2026-01-01', 'due'))
        conn.execute(f'UPDATE "{table}" SET score=9, leitner_box=5, last_practiced=? WHERE content_id=?', (ll.date.today().isoformat(), 'later'))
        conn.execute(f'UPDATE "{table}" SET score=9, leitner_box=1, last_practiced=? WHERE content_id=?', (ll.date.today().isoformat(), 'today'))
        before = conn.execute(f'SELECT score, leitner_box, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, last_practiced, last_known_review_at FROM "{table}" WHERE content_id=?', ('due',)).fetchone()
        conn.commit(); conn.close()
        sid, session = web.start_session('alice', 'readonly', review_mode=True)
        question = web.next_question(session)
        self.assertEqual(question['word_unmasked'], 'eins')
        completed = web.process_answer(session, 'ArrowRight')
        self.assertTrue(completed['done'])
        with web.SESSIONS_LOCK:
            web.SESSIONS.pop(sid, None)
        conn = ll.get_connection()
        after = conn.execute(f'SELECT score, leitner_box, times_practiced, times_correct, times_incorrect, times_drilled, times_mastered, last_practiced, last_known_review_at FROM "{table}" WHERE content_id=?', ('due',)).fetchone()
        conn.close()
        self.assertEqual(after, before)

    def test_master_schema_vocabulary_noun_practices_as_normal_item(self):
        """A German noun row in Master Schema produces ONE practice item and accepts
        either comma-separated form as a correct answer."""
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                        dir=ll.WORD_LISTS_DIR, delete=False) as f:
            _json.dump({
                'metadata': {'name': 'Nouns', 'language': 'german',
                             'kind': 'vocabulary', 'level': 'a1'},
                'items': [{'id': 'buch', 'word': 'das Buch, die B\u00fccher',
                            'definition': ['book'], 'word_frequency': 1}]
            }, f)
            tmp_path = Path(f.name)
        try:
            items = ll.load_practice_items(tmp_path)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['word'], 'das Buch, die B\u00fccher')
            # Both singular and plural forms must be accepted by answer_matches
            self.assertTrue(ll.answer_matches('das Buch', items[0]['word']))
            self.assertTrue(ll.answer_matches('die B\u00fccher', items[0]['word']))
            self.assertFalse(ll.answer_matches('das B\u00fccher', items[0]['word']))
        finally:
            tmp_path.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
