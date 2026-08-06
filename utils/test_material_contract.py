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


if __name__ == '__main__':
    unittest.main()
