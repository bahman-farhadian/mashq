"""Tartarus dataset-quality, determinism, and end-to-end tests.

Standard library only. Run with:

    python3 -m unittest discover -s tests -v
    # or directly:
    python3 tests/test_tartarus.py

The end-to-end suite uses an isolated temporary SQLite database so the
production data/tartarus.db is never touched.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, 'utils'))

import conjugation  # noqa: E402
import tartarus  # noqa: E402

DATA_DIR = os.path.join(PROJECT_DIR, 'data', 'word_lists', 'german')
CONJ_PATH = os.path.join(DATA_DIR, 'conjugations.json')
VOCAB_DIR = os.path.join(DATA_DIR, 'vocabulary')
SENT_DIR = os.path.join(DATA_DIR, 'sentences')

SIX_PERSON = ("ich", "du", "er, sie, es", "wir", "ihr", "sie, Sie")
IMPERATIVE_ORDER = ("du", "ihr", "Sie", "wir")


def load_conjugations():
    with open(CONJ_PATH, encoding='utf-8') as handle:
        return json.load(handle)


class TestConjugationDataset(unittest.TestCase):
    """Structural and linguistic validity of conjugations.json."""

    @classmethod
    def setUpClass(cls):
        cls.data = load_conjugations()
        cls.units = conjugation.build_units(cls.data)

    def test_verb_inventory_size(self):
        self.assertEqual(len(self.data), 859)

    def test_required_fields_present(self):
        required = ('infinitiv', 'partizip1', 'partizip2', 'zu_infinitiv',
                    'hilfsverb', 'imperativ', 'passiv', 'english')
        for verb, record in self.data.items():
            for field in required:
                self.assertIn(field, record, f'{verb}: missing {field}')

    def test_hilfsverb_values(self):
        for verb, record in self.data.items():
            self.assertIn(record['hilfsverb'], ('haben', 'sein'),
                          f'{verb}: unexpected hilfsverb {record["hilfsverb"]!r}')

    def test_six_person_arrays_have_six_forms(self):
        blocks = (
            ('indikativ', ('praesens', 'praeteritum', 'perfekt',
                           'plusquamperfekt', 'futur1', 'futur2')),
            ('konjunktiv1', ('praesens', 'perfekt', 'futur1', 'futur2')),
            ('konjunktiv2', ('praeteritum', 'plusquamperfekt', 'futur1', 'futur2')),
        )
        for verb, record in self.data.items():
            for group, tenses in blocks:
                block = record.get(group) or {}
                for tense in tenses:
                    if tense not in block:
                        continue
                    forms = block[tense]
                    self.assertIsInstance(forms, list, f'{verb}.{group}.{tense}')
                    self.assertEqual(len(forms), 6,
                                     f'{verb}.{group}.{tense}: {len(forms)} forms')
                    for form in forms:
                        self.assertTrue(form, f'{verb}.{group}.{tense}: empty form')

    def test_passive_arrays_have_six_forms(self):
        for verb, record in self.data.items():
            passive = record.get('passiv')
            if not passive:
                continue
            for tense, forms in passive.items():
                self.assertEqual(len(forms), 6,
                                 f'{verb}.passiv.{tense}: {len(forms)} forms')
                for form in forms:
                    self.assertTrue(form, f'{verb}.passiv.{tense}: empty form')

    def test_imperative_keys_and_order(self):
        for verb, record in self.data.items():
            imperativ = record.get('imperativ')
            if not imperativ:
                continue
            self.assertEqual(list(imperativ.keys()), list(IMPERATIVE_ORDER),
                             f'{verb}: imperative keys out of order')
            for pronoun, form in imperativ.items():
                self.assertTrue(form, f'{verb}.imperativ.{pronoun}: empty')

    def test_passive_verbs_are_transitive_haben(self):
        # Passive subjects require an accusative object; sein-verbs (motion/
        # state change) are intransitive and have no learner passive.
        for verb, record in self.data.items():
            if record.get('passiv'):
                self.assertEqual(record['hilfsverb'], 'haben',
                                 f'{verb}: passive with sein auxiliary')

    def test_core_verbs_present(self):
        for verb in conjugation.CORE_VERBS:
            self.assertIn(verb, self.data, f'CORE_VERBS: {verb} missing')

    def test_unit_count_matches_evidence(self):
        # Task evidence: 91,489 learner units after excluding haben passive.
        self.assertEqual(len(self.units), 91489)


class TestEngineDeterminism(unittest.TestCase):
    """The conjugation contract: no random selection anywhere."""

    @classmethod
    def setUpClass(cls):
        cls.data = load_conjugations()
        cls.units = conjugation.build_units(cls.data)

    def test_build_units_is_stable(self):
        rebuilt = conjugation.build_units(self.data)
        self.assertEqual(rebuilt, self.units)

    def test_unit_keys_unique(self):
        keys = [u['unit_key'] for u in self.units]
        self.assertEqual(len(keys), len(set(keys)))

    def test_stage_order_is_fixed(self):
        expected = tuple(stage for stage, _ in conjugation.STAGES)
        self.assertEqual(expected, tuple(range(1, 21)))
        # Stage 1 (pronouns) is context only, never a standalone question.
        stages_present = sorted(u['stage'] for u in self.units)
        self.assertEqual(stages_present[0], 2)

    def test_six_person_pronoun_order(self):
        # For any person-dependent stage, pronoun_order maps 0..5 to the
        # fixed ich -> du -> er/sie/es -> wir -> ihr -> sie/Sie sequence.
        # Multi-block stages (14, 16, 17, 19) repeat the six-person cycle,
        # so pronoun_order % 6 recovers the canonical label.
        person_stages = (3, 4, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19)
        for stage in person_stages:
            seen = {}
            for u in self.units:
                if u['stage'] != stage:
                    continue
                po = u['pronoun_order']
                if po < 0:
                    continue
                label = conjugation.PRONOUNS[po % 6]
                self.assertIn(label, u['prompt'],
                              f'stage {stage}: pronoun label missing in prompt')
                seen.setdefault(po % 6, u['verb'])
            self.assertEqual(sorted(seen), list(range(min(seen), max(seen) + 1)),
                             f'stage {stage}: pronoun indices not contiguous')

    def test_imperative_pronoun_order(self):
        # Imperative order is du -> ihr -> Sie -> wir, not the six-person order.
        seen = []
        for u in self.units:
            if u['stage'] != 5:
                continue
            pronoun = u['prompt'].rsplit('·', 1)[-1].strip()
            if pronoun and pronoun not in seen:
                seen.append(pronoun)
        # First occurrence order must follow the imperative sequence.
        ordered = [p for p in IMPERATIVE_ORDER if p in seen]
        self.assertEqual(seen, ordered,
                         f'imperative order {seen} != {ordered}')

    def test_haben_excluded_from_passive_stages(self):
        for u in self.units:
            if u['verb'] == 'haben':
                self.assertNotIn(u['stage'], (14, 16),
                                 'haben passive unit generated')

    def test_imperative_answer_has_no_pronoun_prefix(self):
        for u in self.units:
            if u['stage'] != 5:
                continue
            first = u['answer'].split(' ', 1)[0]
            self.assertNotIn(first, IMPERATIVE_ORDER,
                             f'{u["verb"]}: imperative answer prefixed with {first!r}')

    def test_answers_traceable_to_source(self):
        # Every generated answer is either the source form verbatim, or the
        # approved engine combination of pronoun + source form. No silent
        # transformation into a different grammatical form is permitted.
        pronoun_map = {"er, sie, es": "er", "sie, Sie": "sie"}
        for u in self.units:
            answer = u['answer']
            verb = u['verb']
            record = self.data[verb]
            candidates = self._source_answer_candidates(record, u)
            self.assertIn(answer, candidates,
                          f'{verb} (stage {u["stage"]}): answer {answer!r} not in source')

    @staticmethod
    def _source_answer_candidates(record, unit):
        stage = unit['stage']
        indikativ = record.get('indikativ') or {}
        konj1 = record.get('konjunktiv1') or {}
        konj2 = record.get('konjunktiv2') or {}
        passive = record.get('passiv') or {}
        pronoun_map = {"er, sie, es": "er", "sie, Sie": "sie"}
        pronoun = None
        if '·' in unit['prompt']:
            tail = unit['prompt'].rsplit('·', 1)[-1].strip()
            if tail in conjugation.PRONOUNS:
                pronoun = tail
        prefix = ''
        if pronoun in conjugation.PRONOUNS:
            prefix = pronoun_map.get(pronoun, pronoun) + ' '
        pools = []
        if stage == 2:
            pools = [[record.get('infinitiv')]]
        elif stage == 4:
            pools = [indikativ.get('praesens') or []]
        elif stage == 6:
            pools = [[record.get('partizip2')]]
        elif stage == 7:
            pools = [[record.get('hilfsverb')]]
        elif stage == 10:
            pools = [[record.get('zu_infinitiv')]]
        elif stage == 20:
            pools = [[record.get('partizip1')]]
        elif stage == 5:
            pools = [list((record.get('imperativ') or {}).values())]
            prefix = ''  # imperative answers are stored verbatim
        else:
            pool_map = {
                3: indikativ.get('praesens'), 8: indikativ.get('perfekt'),
                9: indikativ.get('praeteritum'), 11: indikativ.get('plusquamperfekt'),
                12: indikativ.get('futur1'), 13: konj2.get('praeteritum'),
                14: (passive.get('praesens') or []) + (passive.get('praeteritum') or []),
                15: konj2.get('plusquamperfekt'),
                16: (passive.get('perfekt') or []) + (passive.get('plusquamperfekt') or [])
                     + (passive.get('futur1') or []),
                17: (konj1.get('praesens') or []) + (konj1.get('perfekt') or []),
                18: indikativ.get('futur2'),
                19: (konj1.get('futur1') or []) + (konj1.get('futur2') or [])
                     + (konj2.get('futur1') or []) + (konj2.get('futur2') or []),
            }
            pools = [pool_map.get(stage) or []]
        candidates = set()
        for pool in pools:
            for form in (pool or []):
                if form:
                    candidates.add(form)
                    candidates.add(prefix + form)
        return candidates


class TestEndToEnd(unittest.TestCase):
    """Full session behaviour against an isolated temporary database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='tartarus_test_')
        cls.db = os.path.join(cls.tmp, 't.db')
        cls._orig_db = tartarus.DATABASE_FILE
        tartarus.DATABASE_FILE = cls.db
        cls.data = load_conjugations()

    @classmethod
    def tearDownClass(cls):
        tartarus.DATABASE_FILE = cls._orig_db

    def _connect(self):
        return sqlite3.connect(self.db)

    def test_01_sync_creates_table_with_all_units(self):
        conn = self._connect()
        conjugation.sync(conn, 'tester')
        count = conn.execute(
            f'SELECT COUNT(*) FROM "{conjugation.table_name("tester")}"'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, len(conjugation.build_units(self.data)))

    def test_02_first_unit_is_lowest_stage(self):
        conn = self._connect()
        queue = conjugation.next_units(conn, 'tester', 16)
        conn.close()
        self.assertTrue(queue)
        self.assertEqual(queue[0]['stage'], 2)
        self.assertEqual(queue[0]['pronoun_order'], -1)
        self.assertEqual(queue[0]['verb_order'], 0)

    def test_03_stage_locking_no_skip(self):
        # A learner cannot reach stage 4 while stage 3 still has new material.
        conn = self._connect()
        t = conjugation.table_name('tester')
        conn.execute(f"UPDATE \"{t}\" SET completed=1, score=9.0, "
                     f"last_practiced=date('now','localtime') WHERE stage=2")
        conn.commit()
        queue = conjugation.next_units(conn, 'tester', 16)
        conn.close()
        self.assertEqual(set(u['stage'] for u in queue), {3})

    def test_04_pronoun_locking(self):
        # Pronoun 1 (du) is not unlocked while pronoun 0 (ich) is incomplete.
        conn = self._connect()
        t = conjugation.table_name('tester')
        conn.execute(f"UPDATE \"{t}\" SET completed=1, score=9.0, "
                     f"last_practiced=date('now','localtime') "
                     f"WHERE stage=3 AND pronoun_order=0")
        conn.commit()
        queue = conjugation.next_units(conn, 'tester', 16)
        conn.close()
        stages = set(u['stage'] for u in queue)
        pronouns = set(u['pronoun_order'] for u in queue)
        self.assertEqual(stages, {3})
        self.assertEqual(pronouns, {1})

    def test_05_pronoun_progression_unlocks_next_stage(self):
        # Completing every pronoun of stage 3 unlocks stage 4 pronoun 0.
        conn = self._connect()
        t = conjugation.table_name('tester')
        conn.execute(f"UPDATE \"{t}\" SET completed=1, score=9.0, "
                     f"last_practiced=date('now','localtime') WHERE stage=3")
        conn.commit()
        queue = conjugation.next_units(conn, 'tester', 16)
        conn.close()
        self.assertEqual(queue[0]['stage'], 4)
        self.assertEqual(queue[0]['pronoun_order'], 0)

    def test_06_next_units_is_deterministic(self):
        # Same learner state -> identical session order, no random seed needed.
        conn = self._connect()
        a = [u['unit_key'] for u in conjugation.next_units(conn, 'tester', 16)]
        b = [u['unit_key'] for u in conjugation.next_units(conn, 'tester', 16)]
        conn.close()
        self.assertEqual(a, b)

    def test_07_scoring_adds_repetitions_not_reordering(self):
        # A correct answer raises the score but the next unlocked curriculum
        # unit keeps its prescribed identity (verb + pronoun unchanged).
        conn = self._connect()
        t = conjugation.table_name('tester')
        before = conjugation.next_units(conn, 'tester', 1)[0]
        # Score this unit to mastery.
        conn.execute(f"UPDATE \"{t}\" SET score=9.0, completed=1, "
                     f"last_practiced=date('now','localtime') "
                     f"WHERE unit_key=?", (before['unit_key'],))
        conn.commit()
        after = conjugation.next_units(conn, 'tester', 1)[0]
        conn.close()
        self.assertNotEqual(after['unit_key'], before['unit_key'])
        # The successor must be the next prescribed verb in the same pronoun.
        self.assertEqual(after['pronoun_order'], before['pronoun_order'])
        self.assertEqual(after['stage'], before['stage'])
        self.assertGreaterEqual(after['verb_order'], before['verb_order'])

    def test_08_record_attempt_increments_counters(self):
        conn = self._connect()
        queue = conjugation.next_units(conn, 'tester', 1)
        key = queue[0]['unit_key']
        conjugation.record_attempt(conn, 'tester', key, correct=False)
        row = conn.execute(
            f'SELECT attempts, incorrect FROM "{conjugation.table_name("tester")}" '
            f'WHERE unit_key=?', (key,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 1)

    def test_09_mark_completed(self):
        conn = self._connect()
        queue = conjugation.next_units(conn, 'tester', 1)
        key = queue[0]['unit_key']
        conjugation.mark_completed(conn, 'tester', key)
        row = conn.execute(
            f'SELECT completed FROM "{conjugation.table_name("tester")}" '
            f'WHERE unit_key=?', (key,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_10_progress_report_shape(self):
        conn = self._connect()
        prog = conjugation.progress(conn, 'tester')
        conn.close()
        self.assertEqual(prog['current_stage'], 4)  # stages 2,3 completed
        self.assertEqual(prog['total'], len(conjugation.build_units(self.data)))
        self.assertEqual(len(prog['stages']), 20)


class TestVocabularyDataset(unittest.TestCase):
    """Source-to-output coverage and structural checks for vocabulary lists."""

    @classmethod
    def setUpClass(cls):
        cls.records_by_file = {}
        for level in sorted(os.listdir(VOCAB_DIR)):
            level_dir = os.path.join(VOCAB_DIR, level)
            if not os.path.isdir(level_dir):
                continue
            for fn in sorted(os.listdir(level_dir)):
                if not fn.endswith('.json'):
                    continue
                with open(os.path.join(level_dir, fn), encoding='utf-8') as fh:
                    cls.records_by_file[(level, fn)] = json.load(fh)

    def test_no_internal_duplicates(self):
        for (level, fn), records in self.records_by_file.items():
            words = [r.get('word') for r in records]
            self.assertEqual(len(words), len(set(words)),
                             f'{level}/{fn}: {len(words) - len(set(words))} dupes')

    def test_required_fields(self):
        for (level, fn), records in self.records_by_file.items():
            for rec in records:
                self.assertIn('word', rec)
                self.assertIn('definition', rec)
                self.assertIsInstance(rec['definition'], list,
                                      f'{level}/{fn}: {rec.get("word")} def not list')
                self.assertTrue(rec['definition'],
                                f'{level}/{fn}: {rec.get("word")} empty def')

    def test_known_fragment_review_candidates(self):
        # These are REVIEW CANDIDATES flagged by the teacher, not auto-fixes.
        # The test documents their presence so a regression is visible.
        trailing_dash = []
        for (level, fn), records in self.records_by_file.items():
            for rec in records:
                w = rec.get('word', '')
                if w.endswith('-'):
                    trailing_dash.append((level, w))
        # Snapshot: the dataset currently carries these fragments. When the
        # linguistic review resolves them, update the expected count to 0.
        self.assertEqual(len(trailing_dash), 18,
                         f'expected 18 trailing-dash fragments, found {len(trailing_dash)}')


class TestSentenceDataset(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.records_by_file = {}
        for level in sorted(os.listdir(SENT_DIR)):
            level_dir = os.path.join(SENT_DIR, level)
            if not os.path.isdir(level_dir):
                continue
            for fn in sorted(os.listdir(level_dir)):
                if not fn.endswith('.json'):
                    continue
                with open(os.path.join(level_dir, fn), encoding='utf-8') as fh:
                    cls.records_by_file[(level, fn)] = json.load(fh)

    def test_sentences_have_required_fields(self):
        for (level, fn), records in self.records_by_file.items():
            for rec in records:
                self.assertIn('word', rec)
                self.assertIn('definition', rec)
                self.assertTrue(rec['word'], f'{level}/{fn}: empty sentence')
                self.assertTrue(rec['definition'], f'{level}/{fn}: empty translation')

    def test_no_internal_duplicates(self):
        for (level, fn), records in self.records_by_file.items():
            words = [r.get('word') for r in records]
            self.assertEqual(len(words), len(set(words)),
                             f'{level}/{fn}: sentence dupes')


if __name__ == '__main__':
    unittest.main(verbosity=2)
