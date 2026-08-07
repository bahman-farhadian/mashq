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
                'language': 'german', 'type': 'vocabulary', 'cefr_level': 'a1',
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
            times_flagged INTEGER NOT NULL DEFAULT 0,
            drill_pending INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            stage_reached INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute(
            f'INSERT INTO "{table}" (content_id, score, times_practiced, leitner_box) '
            'VALUES (?, ?, ?, ?, ?)', ('old-item', 9.0, 3, 1, 4)
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
        self.assertEqual(row, ('old-item', 9.0, 3, 1, 4))
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
        self.assertEqual(backup['word_progress']['backup'][0]['drill_pending'], 1)
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

    def test_personal_material_hides_samples_without_deleting_history(self):
        sample = Path(ll.WORD_LISTS_DIR) / 'tartarus_sample_german_a1.json'
        self.write_list(sample, [{'id': 'sample', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0}])
        ll.sync_word_list('alice', 'tartarus_sample_german_a1')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'tartarus_sample_german_a1')
        conn.execute(f'UPDATE "{table}" SET score = 5.0 WHERE content_id = ?', ('sample',))
        conn.commit()
        conn.close()
        web.init_word_list('alice', 'custom')
        lists = [row['lang'] for row in web.list_word_lists() if row['user'] == 'alice']
        self.assertNotIn('tartarus_sample_german_a1', lists)
        conn = ll.get_connection()
        self.assertEqual(conn.execute(f'SELECT score FROM "{table}" WHERE content_id = ?', ('sample',)).fetchone()[0], 5.0)
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

    def test_flagging_preserves_score_and_records_counter(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_flag.json'
        self.write_list(path, [
            {'id': 'one', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
        ])
        ll.sync_word_list('alice', 'flag')
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'flag')
        word_id = conn.execute(f'SELECT id FROM "{table}"').fetchone()[0]
        for score, box in ((0.0, None), (5.0, None), (9.0, 3)):
            conn.execute(f'UPDATE "{table}" SET score = ?, leitner_box = ?, times_flagged = 0 WHERE id = ?',
                         (score, box, word_id))
            conn.commit()
            ll.update_word_score('alice', 'flag', word_id, 'flagged')
            saved = conn.execute(f'SELECT score, leitner_box, times_flagged FROM "{table}" WHERE id = ?', (word_id,)).fetchone()
            self.assertEqual(saved, (score, box, 0))
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
        self.assertEqual(next(row for row in progress if row['lang'] == 'metrics')['to_drill'], 1)
        leitner = web.leitner_stats_data('alice', 'metrics')
        self.assertEqual(leitner['never_practiced'], 1)
        self.assertEqual(leitner['due_today'], 0)

    def test_master_schema_noun_list_can_be_created(self):
        created, path = web.init_word_list('alice', 'german_personal_nouns', 'vocabulary')
        self.assertTrue(created)
        data = ll.read_word_list(path)
        self.assertEqual(data['metadata']['type'], 'vocabulary')
        self.assertEqual(data['items'], [])

        entries = ll.load_practice_items(path)
        self.assertEqual([entry['content_id'] for entry in entries], [
            f'{item_id}:nominative', f'{item_id}:accusative',
            f'{item_id}:dative', f'{item_id}:genitive',
        ])
        self.assertEqual(entries[2]['noun_forms']['singular'], 'dative singular')
        self.assertEqual(entries[2]['noun_forms']['plural'], 'dative plural')
        session_id, session = web.start_session('alice', 'german_personal_nouns', audio_lang='german')
        question = web.next_question(session)
        self.assertIn(question['noun_case'], ll.NOUN_CASES)
        self.assertTrue(question['question_id'])
        self.assertEqual(question['sequence'], 1)
        case_name = question['noun_case']
        self.assertEqual(question['noun_forms']['singular'], f'{case_name} singular')
        self.assertEqual(question['audio_text'], f'{case_name} singular. {case_name} plural')
        web.SESSIONS.pop(session_id, None)
        web.save_noun('alice', 'german_personal_nouns', 'das Buch', 'book', forms)
        self.assertEqual(len(ll.read_word_list(path)['items']), 1)
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'german_personal_nouns')
        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE active=1').fetchone()[0], 4)
        conn.close()


if __name__ == '__main__':
    unittest.main()
