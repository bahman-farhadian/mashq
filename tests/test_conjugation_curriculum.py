import unittest
import os
import sys
import json
import sqlite3
from datetime import date

# Ensure utils directory is on path
SYS_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)
UTILS_PATH = os.path.join(SYS_PATH, 'utils')
if UTILS_PATH not in sys.path:
    sys.path.insert(0, UTILS_PATH)

import conjugation
import tartarus
import tartarus_web


class TestConjugationCurriculum(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(SYS_PATH, 'data', 'test_tartarus.db')
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)
        cls.conn = sqlite3.connect(cls.db_path)
        conjugation.seed_stage_users(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def test_schema_validity(self):
        """Validate German conjugation dataset against official schema file."""
        schema_file = os.path.join(SYS_PATH, 'data', 'schemas', 'german_conjugations.schema.json')
        dataset_file = os.path.join(SYS_PATH, 'data', 'word_lists', 'german', 'tartarus_sample_german_conjugations.json')
        
        self.assertTrue(os.path.exists(schema_file), f"Missing schema file: {schema_file}")
        self.assertTrue(os.path.exists(dataset_file), f"Missing dataset file: {dataset_file}")

        with open(dataset_file, encoding='utf-8') as f:
            dataset = json.load(f)

        required_keys = ['infinitiv', 'translation', 'verb_class', 'english', 'indikativ']
        for verb, record in dataset.items():
            for key in required_keys:
                self.assertIn(key, record, f"Verb '{verb}' missing required schema key '{key}'")
            self.assertIn('praesens', record['english'], f"Verb '{verb}' english missing 'praesens'")

    def test_all_20_stages_exist_and_isolated(self):
        """Test that all 20 stages are registered and return isolated single-stage queues."""
        self.assertEqual(len(conjugation.STAGES), 20)
        
        for stage_num in range(1, 21):
            user = f"stage_{stage_num:02d}_user"
            units = conjugation.next_units(self.conn, user, limit=16)
            self.assertGreater(len(units), 0, f"Stage {stage_num} returned empty queue for user {user}")
            
            stage_ids = {u[0]['stage'] for u in units}
            self.assertEqual(stage_ids, {stage_num}, f"Stage {stage_num} user returned mixed stages: {stage_ids}")

    def test_stage_2_vs_stage_7_prompt_and_header_distinction(self):
        """Test that Stage 2 (Infinitive) and Stage 7 (Perfekt) have distinct prompts and headers."""
        units_s2 = conjugation.next_units(self.conn, "stage_02_user", limit=5)
        u2 = units_s2[0][0]
        self.assertEqual(u2['stage'], 2)
        self.assertEqual(u2['verb'], "to make, do")
        self.assertIn("Infinitive · English: to make, do", u2['prompt'])
        self.assertEqual(u2['answer'], "machen")

        units_s7 = conjugation.next_units(self.conn, "stage_07_user", limit=5)
        u7 = units_s7[0][0]
        self.assertEqual(u7['stage'], 7)
        self.assertEqual(u7['verb'], "machen")
        self.assertIn("Indikativ Perfekt", u7['prompt'])
        self.assertEqual(u7['answer'], "ich habe gemacht")

    def test_verb_progression_no_infinite_machen(self):
        """Test that completing 'machen' in Stage 7 advances to 'können' and 'fahren' without endless loops."""
        user = "stage_07_user"
        table = conjugation.table_name(user)
        
        # Initial queue should start with 'machen'
        initial_units = conjugation.next_units(self.conn, user, limit=16)
        verbs_in_initial = [u[0]['verb'] for u in initial_units]
        self.assertEqual(verbs_in_initial[0], "machen")

        # Mark all 'machen' Stage 7 units as completed (score = 9.0)
        units_all = conjugation.build_units()
        machen_keys = [u['unit_key'] for u in units_all if u['stage'] == 7 and u['verb'] == 'machen']
        self.assertTrue(len(machen_keys) > 0)
        
        today_iso = date.today().isoformat()
        placeholders = ','.join('?' for _ in machen_keys)
        self.conn.execute(
            f'UPDATE "{table}" SET score = 9.0, leitner_box = 1, completed = 1, last_practiced = ? WHERE unit_key IN ({placeholders})',
            [today_iso] + machen_keys
        )
        self.conn.commit()

        # Query queue again after 'machen' completion
        next_session_units = conjugation.next_units(self.conn, user, limit=16)
        next_verbs = [u[0]['verb'] for u in next_session_units]
        
        # Verify 'machen' is NO LONGER in the incomplete queue, and queue HAS ADVANCED to next verb
        self.assertNotIn("machen", next_verbs, f"Endless 'machen' bug detected! Queue still contained 'machen': {next_verbs}")
        self.assertIn(next_verbs[0], ["können", "fahren", "lernen", "gehen"], f"Unexpected next verb: {next_verbs[0]}")

    def test_er_sie_es_separated_in_all_stages(self):
        """Test that er, sie, es forms are split into 3 distinct questions."""
        units_s3 = conjugation.next_units(self.conn, "stage_03_user", limit=16)
        prompts_s3 = [u[0]['prompt'] for u in units_s3]
        
        er_prompts = [p for p in prompts_s3 if "er (note:" in p]
        sie_prompts = [p for p in prompts_s3 if "sie (note:" in p]
        es_prompts = [p for p in prompts_s3 if "es (note:" in p]
        
        self.assertGreater(len(er_prompts), 0)
        self.assertGreater(len(sie_prompts), 0)
        self.assertGreater(len(es_prompts), 0)


if __name__ == '__main__':
    unittest.main()
