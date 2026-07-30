import unittest
import os
import sys
import sqlite3
import json
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


class TestConjugationAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(SYS_PATH, 'data', 'test_tartarus_api.db')
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)
        
        # Override DB path for tartarus modules during API testing
        tartarus.DB_PATH = cls.db_path
        
        cls.conn = sqlite3.connect(cls.db_path)
        conjugation.seed_stage_users(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def test_stage_user_premastery_in_database(self):
        """Test that stage_07_user has stages 1-6 pre-mastered, stage_08_user has stages 1-7 pre-mastered, etc."""
        all_units = conjugation.build_units()
        
        # Test stage_07_user
        table_s7 = conjugation.table_name("stage_07_user")
        completed_keys_s7 = {
            row[0] for row in self.conn.execute(f'SELECT unit_key FROM "{table_s7}" WHERE completed = 1')
        }
        s1_6_keys = {u['unit_key'] for u in all_units if u['stage'] < 7}
        self.assertTrue(s1_6_keys.issubset(completed_keys_s7), "stage_07_user missing pre-mastered units from stages 1-6")
        
        # Test stage_08_user
        table_s8 = conjugation.table_name("stage_08_user")
        completed_keys_s8 = {
            row[0] for row in self.conn.execute(f'SELECT unit_key FROM "{table_s8}" WHERE completed = 1')
        }
        s1_7_keys = {u['unit_key'] for u in all_units if u['stage'] < 8}
        self.assertTrue(s1_7_keys.issubset(completed_keys_s8), "stage_08_user missing pre-mastered units from stages 1-7")

    def test_all_20_stage_users_api_start_sessions(self):
        """Test start_session API logic for all 20 stage users."""
        for stage_num in range(1, 21):
            user = f"stage_{stage_num:02d}_user"
            sid, sess = tartarus_web.start_session(user, "tartarus_sample_german_conjugations")
            
            self.assertIsNotNone(sid, f"Failed to generate session ID for {user}")
            self.assertIn("queue", sess, f"Session response missing queue for {user}")
            self.assertGreater(len(sess["queue"]), 0, f"Queue is empty for {user}")
            
            # Verify first question structure
            first_item = sess["queue"][0]
            self.assertIn("word_text", first_item)
            self.assertIn("definition", first_item)
            self.assertIn("conjugation", first_item)
            
            unit = first_item["conjugation"][0]
            self.assertEqual(unit["stage"], stage_num, f"{user} expected stage {stage_num}, got {unit['stage']}")

    def test_api_answer_submission_loop(self):
        """Test complete answer submission loop via tartarus_web.record_answer."""
        user = "stage_03_user"
        sid, sess = tartarus_web.start_session(user, "tartarus_sample_german_conjugations")
        
        queue = sess["queue"]
        self.assertGreater(len(queue), 0)
        
        # Loop through questions and record correct answers
        session_obj = tartarus_web.SESSIONS[sid]
        units = conjugation.build_units()
        unit_map = {u['unit_key']: u['answer'] for u in units}
        
        for step in range(5):
            if not session_obj.get('current'):
                q_res = tartarus_web.next_question(session_obj)
                if q_res.get('finished') or not session_obj.get('current'):
                    break
            
            curr_q = session_obj['current']
            word_id = curr_q['word_id']
            target = unit_map[word_id]
            
            if curr_q.get('type') == 'drill':
                res = tartarus_web.process_drill_step(session_obj, target)
            else:
                res = tartarus_web.process_answer(session_obj, target)
            
            self.assertIn("result", res)
            self.assertIn(res["result"], ["correct", "mastered", "drill_start", "finished", "drilled", "drill_progress"])


if __name__ == '__main__':
    unittest.main()
