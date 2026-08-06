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

    def test_wrong_answer_persists_one_drill_debt_until_completed(self):
        path = Path(ll.WORD_LISTS_DIR) / 'alice_drill.json'
        self.write_list(path, [
            {'id': 'haus', 'word': 'das Haus', 'definition': ['house'], 'word_frequency': 0},
        ])
        session_id, session = web.start_session('alice', 'drill', audio_lang='german')
        web.next_question(session)
        web.process_answer(session, 'wrong')
        web.SESSIONS.pop(session_id, None)
        conn = ll.get_connection()
        table = ll.words_table_name('alice', 'drill')
        row = conn.execute(f'SELECT score, times_practiced, times_incorrect, times_drilled, drill_pending FROM "{table}"').fetchone()
        conn.close()
        self.assertEqual(row, (0.0, 1, 1, 0, 1))
        session_id, session = web.start_session('alice', 'drill', audio_lang='german')
        question = web.next_question(session)
        self.assertIn('drill_start', question)
        blocked = web.process_answer(session, '!!')
        self.assertEqual(blocked['result'], 'drill_required')
        for _ in range(ll.DRILL_TARGET):
            result = web.process_answer(session, 'das Haus')
        self.assertIn(result['result'], {'drilled', 'correct'})
        web.SESSIONS.pop(session_id, None)
        conn = ll.get_connection()
        row = conn.execute(f'SELECT score, times_practiced, times_incorrect, times_drilled, drill_pending FROM "{table}"').fetchone()
        conn.close()
        self.assertEqual(row, (0.0, 1, 1, 1, 0))

    def test_noun_list_can_be_created_without_mutating_shared_material(self):
        created, path = web.init_word_list('alice', 'german_personal_nouns', 'nouns')
        self.assertTrue(created)
        data = ll.read_word_list(path)
        self.assertEqual(data['metadata']['type'], 'nouns')
        self.assertEqual(data['metadata']['language'], 'german')
        self.assertEqual(data['items'], [])

    def test_noun_record_expands_into_four_stable_case_items(self):
        forms = {}
        for case_name in ll.NOUN_CASES:
            for number in ('singular', 'plural'):
                forms[(case_name, number)] = {
                    'form': f'{case_name} {number}',
                    'sentence': f'{case_name} example {number}.',
                    'translation': f'{case_name} English {number}.',
                }
        path, item_id = web.save_noun('alice', 'german_personal_nouns', 'das Buch', 'book', forms)
        source = ll.read_word_list(path)
        self.assertEqual(source['metadata']['type'], 'nouns')
        self.assertEqual(source['items'][0]['id'], item_id)
        self.assertEqual(source['items'][0]['noun_forms']['dative']['plural']['form'], 'dative plural')
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
