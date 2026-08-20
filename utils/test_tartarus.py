#!/usr/bin/env python3
"""Unified release-contract suite for Tartarus v0.0.9.

This is intentionally the only project test module. Every test uses temporary
progress databases, material roots, logs, ports and browser profiles. The
repository datasets and production progress database are never mutated.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
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
from datetime import date, timedelta
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


def material_items(count=20, prefix='w'):
    return [
        {
            'id': f'id-{i:02d}',
            'word': f'{prefix}{i:02d}',
            'definition': f'definition {i:02d}',
            'word_frequency': i,
        }
        for i in range(count)
    ]


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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def logical_db_dump(path: Path):
    """Stable logical dump that ignores SQLite file-layout changes."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        result = {}
        for table in tables:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            result[table] = {'columns': columns, 'rows': rows}
        result['_user_version'] = conn.execute('PRAGMA user_version').fetchone()[0]
        return result
    finally:
        conn.close()


class CoreContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tartarus-core-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'progress.db'
        self.lists = self.root / 'word_lists'
        self.lists.mkdir()
        self.audio = self.root / 'audio'
        self.audio.mkdir()
        self.old_db, self.old_lists, self.old_audio = ll.DATABASE_FILE, ll.WORD_LISTS_DIR, ll.AUDIO_DIR
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR, ll.AUDIO_DIR = str(self.db), str(self.lists), str(self.audio)
        with ll._PRACTICE_ITEM_CACHE_LOCK:
            ll._PRACTICE_ITEM_CACHE.clear()
        ll.initialize_database(create_backup=False)
        conn = ll.get_connection()
        for user in ('alice', 'alice_ann', 'bob'):
            ll.ensure_user(conn, user)
        conn.commit(); conn.close()
        with web.SESSIONS_LOCK:
            web.SESSIONS.clear()
        self.addCleanup(self.restore)

    def restore(self):
        with web.SESSIONS_LOCK:
            web.SESSIONS.clear()
        with ll._PRACTICE_ITEM_CACHE_LOCK:
            ll._PRACTICE_ITEM_CACHE.clear()
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR, ll.AUDIO_DIR = self.old_db, self.old_lists, self.old_audio

    def make(self, items=None, user='alice', lang='focus', **metadata):
        path = self.lists / f'{user}_{lang}.json'
        write_material(path, items or material_items(), **metadata)
        ll.sync_word_list(user, lang)
        return path

    def table(self, user='alice', lang='focus'):
        return ll.words_table_name(user, lang)

    def update(self, content_id='id-00', lang='focus', user='alice', **values):
        conn = ll.get_connection(); table = self.table(user, lang)
        clause = ','.join(f'"{k}"=?' for k in values)
        conn.execute(f'UPDATE "{table}" SET {clause} WHERE content_id=?', (*values.values(), content_id))
        conn.commit(); conn.close()

    def row(self, content_id='id-00', lang='focus', user='alice'):
        conn = ll.get_connection(); table = self.table(user, lang)
        columns = ','.join(f'"{c}"' for c in ll.WORD_TABLE_COLUMNS)
        row = conn.execute(f'SELECT {columns} FROM "{table}" WHERE content_id=?', (content_id,)).fetchone()
        conn.close()
        return dict(zip(ll.WORD_TABLE_COLUMNS, row))

    def master(self, content_id, mastered_date, *, lang='focus', box=1,
               last_completed=None, last_reviewed=None, completed_day=0):
        self.update(
            content_id, lang=lang, score=9.0, leitner_box=box,
            leitner_last_reviewed=last_reviewed or mastered_date,
            last_tartarus_completed=last_completed,
            gauntlet_completed_day=completed_day,
        )
        conn = ll.get_connection()
        row = self.row(content_id, lang=lang)
        ll.record_mastery_event(
            conn, 'alice', lang, row['id'], 'mastered', mastered_date
        )
        conn.commit(); conn.close()

    def test_lookup_bundled_audio_hits_bundled_misses_personal_and_unknown_text(self):
        shared_dir = self.lists / 'german' / 'vocabulary' / 'a1'
        shared_dir.mkdir(parents=True)
        (shared_dir / 'bfocus.json').write_text(json.dumps({
            'metadata': {'name': 'bfocus', 'language': 'german', 'kind': 'vocabulary', 'level': 'a1'},
            'items': [{'word': 'w00', 'definition': 'd', 'id': 'id-0'}],
        }), encoding='utf-8')
        db_path = self.audio / 'german' / 'vocabulary' / 'a1' / 'bfocus.part1.db'
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            'CREATE TABLE audio(id INTEGER PRIMARY KEY,source_file TEXT,language TEXT,text TEXT,'
            'voice TEXT,rate INTEGER,content_type TEXT,audio BLOB,byte_size INTEGER,generated_at TEXT,UNIQUE(text))'
        )
        conn.execute(
            "INSERT INTO audio(source_file,language,text,voice,rate,content_type,audio,byte_size,generated_at) "
            "VALUES ('german/vocabulary/a1/bfocus','german','w00','Anna',128,'audio/mp4',?,4,'2026-01-01')",
            (b'FAKE',),
        )
        conn.commit(); conn.close()

        hit = ll.lookup_bundled_audio('alice', 'bfocus', 'w00')
        self.assertEqual(hit, (b'FAKE', 'audio/mp4'))
        self.assertIsNone(ll.lookup_bundled_audio('alice', 'bfocus', 'unknown-text'))
        self.assertIsNone(ll.lookup_bundled_audio('alice', 'no-such-list', 'w00'))

        self.make(material_items(1), user='alice', lang='personal-list')
        self.assertIsNone(ll.lookup_bundled_audio('alice', 'personal-list', 'w00'))

    def test_exact_answer_equality_has_no_softening(self):
        target = 'das Buch, die Bücher'
        self.assertTrue(ll.answer_matches(target, target))
        for answer in ('das Buch', 'die Bücher', 'die Bücher, das Buch', 'das Buch,die Bücher',
                       'das buch, die bücher', f' {target}', f'{target} '):
            self.assertFalse(ll.answer_matches(answer, target), answer)
        self.assertTrue(ll.answer_matches('Hallo, Welt!', 'Hallo, Welt!'))
        self.assertFalse(ll.answer_matches('Hallo, Welt! ', 'Hallo, Welt!'))

    def test_nfc_equivalent_forms_match_but_nothing_else_softens(self):
        # P6: a precomposed "ü" (single codepoint) and a decomposed "u" +
        # combining diaeresis (two codepoints) are the same character to a
        # learner and to any input method -- must match. Nothing else
        # about the strict-matching contract changes.
        precomposed = 'die Bücher'   # u+00fc = precomposed u-umlaut
        decomposed = 'die Bücher'   # u + u+0308 combining diaeresis
        self.assertNotEqual(precomposed, decomposed)  # genuinely different bytes
        self.assertTrue(ll.answer_matches(decomposed, precomposed))
        self.assertTrue(ll.answer_matches(precomposed, decomposed))
        # Case and whitespace strictness are untouched by normalization.
        self.assertFalse(ll.answer_matches(decomposed.lower(), precomposed))
        self.assertFalse(ll.answer_matches(f' {decomposed}', precomposed))

    def test_masking_preserves_spaces_and_punctuation_as_literal_structure(self):
        target='das Buch, die Bücher'
        self.assertEqual(ll.mask_sentence(target, 8.0), '___ ____, ___ ______')
        self.assertEqual(ll.mask_sentence('a, b.', 8.0), '_, _.')
        with mock.patch.object(ll.random, 'sample', return_value=[]):
            self.assertEqual(ll.mask_sentence('a, b!', 4.0), '_, _!')

    def test_material_loader_preserves_target_string_exactly(self):
        path = self.lists / 'raw.json'
        write_material(path, [{'id':'x','word':'  Exact target  ','definition':'x','word_frequency':0}])
        self.assertEqual(ll.load_practice_items(path)[0]['word'], '  Exact target  ')

    def test_material_cache_reuses_parse_and_invalidates_after_file_change(self):
        path=self.lists/'cache.json'
        write_material(path,[{'id':'one','word':'one','definition':'first','word_frequency':0}])
        with mock.patch.object(ll,'read_word_list',wraps=ll.read_word_list) as reader:
            self.assertEqual(ll.load_practice_items(path)[0]['word'],'one')
            self.assertEqual(ll.load_practice_items(path)[0]['word'],'one')
            old_mtime=path.stat().st_mtime_ns
            write_material(path,[{'id':'two','word':'second','definition':'changed','word_frequency':0}])
            os.utime(path,ns=(old_mtime+1_000_000_000,old_mtime+1_000_000_000))
            self.assertEqual(ll.load_practice_items(path)[0]['word'],'second')
        self.assertEqual(reader.call_count,2)

    def test_batch_sync_preserves_rows_and_toggles_active_membership(self):
        path=self.make(material_items(3))
        self.update('id-00',score=5.0,times_practiced=7)
        before={row['content_id']:row for row in (self.row(f'id-{i:02d}') for i in range(3))}
        replacement=[material_items(4)[i] for i in (1,2,3)]
        old_mtime=path.stat().st_mtime_ns
        write_material(path,replacement)
        os.utime(path,ns=(old_mtime+1_000_000_000,old_mtime+1_000_000_000))
        ll.sync_word_list('alice','focus')
        after={cid:self.row(cid) for cid in ('id-00','id-01','id-02','id-03')}
        self.assertEqual((after['id-00']['id'],after['id-00']['score'],after['id-00']['times_practiced'],after['id-00']['active']),
                         (before['id-00']['id'],5.0,7,0))
        for cid in ('id-01','id-02'):
            self.assertEqual((after[cid]['id'],after[cid]['active']),(before[cid]['id'],1))
        self.assertEqual(after['id-03']['active'],1)

    def test_shared_word_list_is_parsed_once_for_all_users(self):
        path=self.lists/'german'/'vocabulary'/'a1'/'shared.json'
        write_material(path,material_items(2))
        resolved=str(path.resolve())
        calls=[]; original=ll.read_word_list
        def counted(candidate):
            calls.append(str(Path(candidate).resolve()))
            return original(candidate)
        with mock.patch.object(ll,'read_word_list',side_effect=counted):
            descriptors=web.list_word_lists()
        self.assertEqual(calls.count(resolved),1)
        self.assertEqual({item['user'] for item in descriptors if item['lang']=='shared'},{'alice','alice_ann','bob'})

    def test_new_file_selects_first_sixteen_json_items_then_shuffles_only_ties(self):
        self.make(material_items(20))
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: values.reverse()):
            selected = ll.get_words_for_gauntlet_stage('alice', 'focus', 0)
        self.assertEqual({r[1] for r in selected}, {f'w{i:02d}' for i in range(16)})
        self.assertEqual([r[1] for r in selected], [f'w{i:02d}' for i in reversed(range(16))])

    def test_forging_membership_is_highest_score_then_json_order(self):
        self.make(material_items(20))
        self.update('id-19', score=8.5)
        self.update('id-18', score=8.0)
        self.update('id-17', score=7.0)
        for i in range(16): self.update(f'id-{i:02d}', score=1.0)
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: values.reverse()):
            selected = ll.get_words_for_gauntlet_stage('alice', 'focus', 0)
        self.assertEqual([r[1] for r in selected[:3]], ['w19', 'w18', 'w17'])
        self.assertEqual(len(selected), 16)
        self.assertNotIn('w16', [r[1] for r in selected])
        self.assertEqual([r[3] for r in selected], sorted([r[3] for r in selected], reverse=True))

    def test_wrong_then_mandatory_drill_changes_score_and_counters_once(self):
        self.make(material_items(1)); self.update(score=4.0)
        word_id = self.row()['id']
        ll.record_tartarus_answer('alice', 'focus', word_id, False, today='2026-08-08')
        mid = self.row()
        self.assertEqual((mid['score'], mid['times_practiced'], mid['times_incorrect'], mid['times_drilled']), (4.0, 1, 1, 0))
        ll.complete_tartarus_drill('alice', 'focus', word_id, today='2026-08-08')
        end = self.row()
        self.assertEqual((end['score'], end['times_practiced'], end['times_incorrect'], end['times_drilled']), (4.5, 2, 1, 1))
        self.assertIsNone(end['last_tartarus_completed'])

    def test_reaching_score_nine_enters_leitner_once_without_advancing_it(self):
        self.make(material_items(1)); self.update(score=8.5)
        word_id = self.row()['id']
        ll.record_tartarus_answer('alice', 'focus', word_id, True, today='2026-08-08')
        row = self.row()
        self.assertEqual((row['score'], row['leitner_box'], row['leitner_last_reviewed'], row['last_tartarus_completed']),
                         (9.0, 1, '2026-08-08', '2026-08-08'))
        conn=ll.get_connection()
        events=conn.execute('SELECT event_type,mastered_date FROM mastery_events WHERE user=? AND lang=?',('alice','focus')).fetchall()
        conn.close()
        self.assertEqual(events,[('mastered','2026-08-08')])
        self.assertEqual(web.trend_data('alice','focus','mastered'),[
            {'date':'2026-08-08','cumulative':1},
        ])
        ll.record_tartarus_answer('alice', 'focus', word_id, True, today='2026-08-09')
        row = self.row()
        self.assertEqual((row['leitner_box'], row['leitner_last_reviewed'], row['last_tartarus_completed']),
                         (1, '2026-08-08', '2026-08-09'))
        conn=ll.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM mastery_events WHERE event_type='mastered'").fetchone()[0],1)
        conn.close()

    def test_drill_mastery_crossing_records_one_append_only_event(self):
        self.make(material_items(1)); self.update(score=8.5)
        word_id=self.row()['id']
        ll.complete_tartarus_drill('alice','focus',word_id,today='2026-08-08')
        ll.complete_tartarus_drill('alice','focus',word_id,today='2026-08-09')
        conn=ll.get_connection()
        events=conn.execute(
            'SELECT event_type,mastered_date FROM mastery_events WHERE user=? AND lang=?',
            ('alice','focus'),
        ).fetchall()
        conn.close()
        self.assertEqual(events,[('mastered','2026-08-08')])

    def test_maintenance_is_independent_from_tartarus_state(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=3, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        word_id = self.row()['id']
        ll.record_maintenance_answer('alice', 'focus', word_id, True, today='2026-08-08')
        row = self.row()
        self.assertEqual((row['score'], row['leitner_box'], row['leitner_last_reviewed'], row['last_tartarus_completed']),
                         (9.0, 4, '2026-08-08', '2026-08-07'))

    def test_maintenance_wrong_then_drill_advances_box_and_sets_review_date(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=4, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        word_id = self.row()['id']
        ll.record_maintenance_answer('alice', 'focus', word_id, False, today='2026-08-08')
        self.assertEqual((self.row()['leitner_box'], self.row()['leitner_last_reviewed']), (4, '2026-08-01'))
        ll.complete_maintenance_drill('alice', 'focus', word_id, today='2026-08-08')
        row = self.row()
        self.assertEqual((row['leitner_box'], row['leitner_last_reviewed'], row['last_tartarus_completed']),
                         (5, '2026-08-08', '2026-08-07'))

    def test_maintenance_drill_moves_box_one_and_caps_box_ten(self):
        self.make(material_items(2))
        for content_id, box in (('id-00', 1), ('id-01', 10)):
            self.update(content_id, score=9.0, leitner_box=box, leitner_last_reviewed='2026-08-01')
            word_id = self.row(content_id)['id']
            ll.record_maintenance_answer('alice', 'focus', word_id, False, today='2026-08-08')
            ll.complete_maintenance_drill('alice', 'focus', word_id, today='2026-08-08')
        self.assertEqual((self.row('id-00')['leitner_box'], self.row('id-01')['leitner_box']), (2, 10))

    def test_late_new_word_coexists_with_independent_reinforcement(self):
        self.make(material_items(2))
        self.master('id-00', '2026-08-03', box=5, last_completed='2026-08-07')
        state = ll.gauntlet_state_breakdown('alice', 'focus', today='2026-08-08')
        self.assertEqual((state['forging'], state['reinforcement_total']), (1, 1))
        self.assertEqual(self.row('id-00')['leitner_box'], 5)
        words = ll.get_words_for_gauntlet_stage('alice', 'focus', 0, today='2026-08-08')
        self.assertEqual([row[1] for row in words], ['w01'])

    def test_reaching_box_ten_records_one_append_only_event(self):
        self.make(material_items(1)); self.update(score=9.0,leitner_box=9,leitner_last_reviewed='2026-08-01')
        word_id=self.row()['id']
        ll.record_maintenance_answer('alice','focus',word_id,True,today='2026-08-10')
        ll.record_maintenance_answer('alice','focus',word_id,True,today='2026-08-11')
        conn=ll.get_connection()
        events=conn.execute(
            'SELECT event_type,mastered_date FROM mastery_events WHERE user=? AND lang=?',
            ('alice','focus'),
        ).fetchall()
        conn.close()
        self.assertEqual(events,[('box10','2026-08-10')])
        self.assertEqual(web.trend_data('alice','focus','box10'),[
            {'date':'2026-08-10','cumulative':1},
        ])

    def test_maintenance_drill_box_ten_crossing_records_one_append_only_event(self):
        self.make(material_items(1)); self.update(score=9.0,leitner_box=9,leitner_last_reviewed='2026-08-01')
        word_id=self.row()['id']
        ll.complete_maintenance_drill('alice','focus',word_id,today='2026-08-10')
        ll.complete_maintenance_drill('alice','focus',word_id,today='2026-08-11')
        conn=ll.get_connection()
        events=conn.execute(
            'SELECT event_type,mastered_date FROM mastery_events WHERE user=? AND lang=?',
            ('alice','focus'),
        ).fetchall()
        conn.close()
        self.assertEqual(events,[('box10','2026-08-10')])

    def test_gauntlet_next_day_derives_from_completed_steps_not_calendar_time(self):
        self.assertEqual(ll.gauntlet_next_day(0), 1)
        self.assertEqual(ll.gauntlet_next_day(1), 2)
        self.assertEqual(ll.gauntlet_next_day(9), 10)
        self.assertEqual(ll.gauntlet_next_day(10), 10)  # clamped, not day 11
        self.assertEqual(ll.gauntlet_next_day(None), 1)

    def test_missed_calendar_days_never_skip_a_reinforcement_step(self):
        # P1: a word due for day 3 that isn't touched for a week must still
        # be presented as day 3 when the learner returns, not day 8 or
        # dropped from reinforcement -- completed steps, not elapsed time,
        # decide the stage.
        self.make(material_items(1))
        self.master('id-00', '2026-08-01', completed_day=2, last_completed='2026-08-03')
        far_future = '2026-08-20'
        rows = ll.get_words_for_reinforcement('alice', 'focus', today=far_future)
        self.assertEqual([(row[1], row[7], row[9]) for row in rows], [('w00', 2, 3)])

    def test_mixed_mastery_cohorts_are_tracked_independently_in_the_breakdown(self):
        # Each cohort's own stage/day is still tracked independently even
        # though (see the next test) a single session may only ever draw
        # from one of them at a time.
        self.make(material_items(4))
        self.master('id-00', '2026-08-01', completed_day=0, last_reviewed='2026-08-08')
        self.master('id-01', '2026-08-01', completed_day=2, last_reviewed='2026-08-08')
        self.master('id-02', '2026-08-01', completed_day=5, last_reviewed='2026-08-08')
        self.master('id-03', '2026-08-01', completed_day=10, last_reviewed='2026-08-08')
        state = ll.gauntlet_state_breakdown('alice', 'focus', today='2026-08-08')
        self.assertEqual((state['reinforcement_total'], state['long_term_review']), (3, 1))
        self.assertEqual(
            {stage['stage']: stage['count'] for stage in state['reinforcement_stages']},
            {1: 1, 2: 1, 3: 1, 4: 0, 5: 0},
        )

    def test_reinforcement_session_never_mixes_stages_even_when_several_are_due(self):
        # Even though three different stages are simultaneously due, one
        # call must return exactly one stage's words, never a blend --
        # switching masking/audio/timer policy mid-session is a context
        # switch the learner must never see.
        self.make(material_items(3))
        self.master('id-00', '2026-08-01', completed_day=0, last_reviewed='2026-08-08')  # crucible
        self.master('id-01', '2026-08-01', completed_day=2, last_reviewed='2026-08-08')  # shadows
        self.master('id-02', '2026-08-01', completed_day=5, last_reviewed='2026-08-08')  # depths
        rows = ll.get_words_for_reinforcement('alice', 'focus', today='2026-08-08')
        stages_present = {row[7] for row in rows}
        self.assertEqual(len(stages_present), 1)
        self.assertEqual([row[1] for row in rows], ['w00'])  # earliest due stage (Crucible) wins

    def test_same_day_completion_suppresses_only_completed_word(self):
        self.make(material_items(2))
        self.master('id-00', '2026-08-05', last_completed='2026-08-08', last_reviewed='2026-08-08')
        self.master('id-01', '2026-08-05', last_completed='2026-08-07', last_reviewed='2026-08-08')
        rows = ll.get_words_for_reinforcement('alice', 'focus', today='2026-08-08')
        self.assertEqual([row[1] for row in rows], ['w01'])
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice', 'focus', '2026-08-08'), 1)

    def test_word_after_day_ten_leaves_reinforcement_but_remains_leitner_due(self):
        self.make(material_items(1))
        self.master('id-00', '2026-07-28', box=10, last_reviewed='2026-07-28', completed_day=10)
        self.assertEqual(ll.get_words_for_reinforcement('alice', 'focus', today='2026-08-08'), [])
        self.assertEqual([row[1] for row in ll.maintenance_ready_words('alice', 'focus', today='2026-08-08')], ['w00'])
        state = ll.gauntlet_state_breakdown('alice', 'focus', today='2026-08-08')
        self.assertEqual((state['reinforcement_total'], state['long_term_review']), (0, 1))
        # P7: nothing is due for reinforcement and Forging is empty (the
        # only word is already mastered), but a Leitner review IS ready --
        # available_tasks must report that, not silently report 0 just
        # because it isn't due_reinforcement or forging.
        self.assertEqual((state['due_maintenance'], state['available_tasks']), (1, 1))

















    def test_due_leitner_review_has_priority_over_tartarus(self):
        """Due review is "the practice from previous days" a learner must
        clear first; starting a session is the only decision they make, and
        the engine picks due review over new/continuing Forging material
        whenever both are available. See select_practice_words()."""
        self.make(material_items(2))
        # Mastered long enough ago to be past its reinforcement track
        # entirely (long-term review only), isolating this to the
        # maintenance-vs-Forging comparison the test name describes.
        self.master('id-00', '2000-01-01', box=1, last_reviewed='2000-01-01', completed_day=10)
        self.update('id-01', score=8.0)
        sid, session, meta = web.gauntlet_start_session('alice', 'focus')
        self.addCleanup(lambda: web.SESSIONS.pop(sid, None))
        self.assertEqual(session['learning_context'], 'maintenance')
        self.assertEqual(meta['mode'], 'maintenance')
        self.assertEqual([q['word_text'] for q in session['queue']], ['w00'])
        self.assertTrue(meta['is_maintenance'])

    def test_selection_priority_is_reinforcement_then_maintenance_then_forging(self):
        # Both reinforcement and Leitner maintenance are already-mastered
        # review; reinforcement's scaffolded presentation goes first so a
        # session warms up before its hardest (unscaffolded) recall demand.
        # Forging (brand-new material) still loses to either.
        self.make(material_items(3))
        # id-00: 2 completed reinforcement steps -> due for day 3 (Shadows)
        # today; reviewed today so its Leitner interval has not elapsed --
        # isolated to the reinforcement pool only.
        self.master('id-00', '2026-08-08', box=1, last_reviewed='2026-08-11', completed_day=2)
        # id-01: all 10 reinforcement steps already completed -> past its
        # track entirely (long-term review), but overdue for Leitner --
        # isolated to the maintenance pool only.
        self.master('id-01', '2026-07-01', box=1, last_reviewed='2000-01-01', completed_day=10)
        self.update('id-02', score=0.5)

        words, context, mode, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context, mode, [row[1] for row in words]), ('tartarus', 'shadows', ['w00']))
        ll.record_tartarus_answer('alice', 'focus', self.row('id-00')['id'], True, today='2026-08-11')

        words, context, mode, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context, mode, [row[1] for row in words]), ('maintenance', 'maintenance', ['w01']))
        ll.record_maintenance_answer('alice', 'focus', self.row('id-01')['id'], True, today='2026-08-11')

        words, context, mode, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context, mode, [row[1] for row in words]), ('tartarus', 'forging', ['w02']))

    def test_stale_overdue_maintenance_outranks_a_freshly_due_reinforcement_stage(self):
        # P2: a large reinforcement backlog must never be able to starve an
        # old overdue Leitner item indefinitely just by track type -- the
        # pool that has been waiting longest goes next, regardless of kind.
        self.make(material_items(2))
        # id-00: became due for reinforcement only yesterday; reviewed
        # today so its own Leitner interval has not elapsed -- isolated to
        # the reinforcement pool only.
        self.master('id-00', '2026-08-01', completed_day=1, last_completed='2026-08-10', last_reviewed='2026-08-11')
        # id-01: box 1 (1-day interval), last reviewed 9 days ago -- has
        # been sitting due far longer than id-00.
        self.master('id-01', '2026-08-01', box=1, last_reviewed='2026-08-02', completed_day=10)
        words, context, mode, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context, mode, [row[1] for row in words]), ('maintenance', 'maintenance', ['w01']))

    def test_web_session_never_mixes_cohort_stages(self):
        today = date.today()
        self.make(material_items(2))
        self.master('id-00', today.isoformat(), last_reviewed=today.isoformat(), completed_day=0)
        self.master('id-01', today.isoformat(), last_reviewed=today.isoformat(), completed_day=4)
        with mock.patch.object(ll.random, 'shuffle', side_effect=lambda values: None):
            sid, session, meta = web.gauntlet_start_session('alice', 'focus')
        self.addCleanup(lambda: web.SESSIONS.pop(sid, None))
        self.assertEqual(meta['mode'], 'crucible')
        self.assertEqual(
            [(entry['mode'], entry['stage'], entry['day']) for entry in session['queue']],
            [('crucible', 1, 1)],
        )

    def test_shadows_drill_completion_marks_tartarus_task_without_moving_leitner(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=4, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        word_id = self.row()['id']
        ll.complete_tartarus_drill('alice','focus',word_id,today='2026-08-08')
        row=self.row()
        self.assertEqual((row['last_tartarus_completed'],row['leitner_box'],row['leitner_last_reviewed']),('2026-08-08',4,'2026-08-01'))

    def test_same_day_dual_track_eligibility_is_confirmed_intentional(self):
        # P3: mastery starts both tracks together (Box 1 assigned the same
        # day reinforcement day 1 begins), and completing one never touches
        # the other's due-ness -- a word can genuinely be due for both a
        # Gauntlet reinforcement check-in and a Leitner review on the same
        # calendar date. This is a confirmed, deliberate product decision
        # (not a bug): the two tracks stay fully independent on purpose.
        self.make(material_items(1))
        self.master('id-00', '2026-08-10', completed_day=0, last_reviewed='2026-08-10', box=1)
        today = '2026-08-11'
        self.assertTrue(ll.get_words_for_reinforcement('alice', 'focus', today=today))
        self.assertTrue(ll.maintenance_ready_words('alice', 'focus', today=today))
        word_id = self.row('id-00')['id']
        ll.complete_tartarus_drill('alice', 'focus', word_id, today=today)
        # Completing today's reinforcement check-in must not satisfy or
        # move the still-independent Leitner due-ness.
        self.assertTrue(ll.maintenance_ready_words('alice', 'focus', today=today))

    def test_interrupted_wrong_does_not_complete_tartarus_task(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=4, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        ll.record_tartarus_answer('alice','focus',self.row()['id'],False,today='2026-08-08')
        self.assertEqual(self.row()['last_tartarus_completed'],'2026-08-07')

    def test_ten_daily_word_reinforcements_finish_despite_corrected_mistakes(self):
        self.make(material_items(1))
        started = date(2026, 8, 1)
        self.master('id-00', started.isoformat(), last_completed=started.isoformat(), last_reviewed=started.isoformat())
        word_id = self.row()['id']
        previous = started.isoformat()
        for offset in range(1, 11):
            today = (started + timedelta(days=offset)).isoformat()
            rows = ll.get_words_for_reinforcement('alice', 'focus', today=today)
            self.assertEqual([(row[1], row[9]) for row in rows], [('w00', offset)])
            ll.record_tartarus_answer('alice', 'focus', word_id, False, today=today)
            self.assertEqual(self.row()['last_tartarus_completed'], previous)
            ll.complete_tartarus_drill('alice', 'focus', word_id, today=today)
            row = self.row()
            self.assertEqual((row['score'], row['leitner_box'], row['last_tartarus_completed']), (9.0, 1, today))
            previous = today
        self.assertEqual(
            ll.get_words_for_reinforcement('alice', 'focus', today='2026-08-12'), []
        )

    def test_maintenance_readiness_has_one_definition(self):
        self.make(material_items(3))
        self.update('id-00',score=9.0,leitner_box=1,leitner_last_reviewed='2026-08-07')
        self.update('id-01',score=9.0,leitner_box=3,leitner_last_reviewed='2026-08-06')
        self.update('id-02',score=9.0,leitner_box=10,leitner_last_reviewed=None)
        ready=ll.maintenance_ready_words('alice','focus',today='2026-08-08')
        self.assertEqual([r[1] for r in ready],['w00','w02'])
        self.assertEqual(ll.maintenance_next_date(3,'2026-08-06'),'2026-08-09')

    def test_progress_payload_has_factual_track_metrics_only(self):
        self.make(material_items(2))
        recent = (date.today() - timedelta(days=1)).isoformat()
        self.master('id-00', recent, box=10, last_reviewed=recent)
        self.master('id-01', recent, box=2, last_reviewed=recent)
        item = next(row for row in web.user_progress_data('alice') if row['lang'] == 'focus')
        self.assertEqual((item['tartarus_score9'], item['leitner_box10'], item['tartarus_track_complete'], item['learning_complete']), (2, 1, False, False))
        self.assertNotIn('due_today', item); self.assertNotIn('learned', item); self.assertNotIn('progress', item)

    def test_mistake_history_is_historical_and_does_not_drive_selection(self):
        self.make(material_items(2)); self.update('id-00',score=1.0,times_incorrect=99); self.update('id-01',score=8.0,times_incorrect=0)
        dash=web.dashboard_data('alice','focus')
        self.assertEqual(dash['nemesis'][0]['word'],'w00')
        selected=ll.get_words_for_gauntlet_stage('alice','focus',0)
        self.assertEqual(selected[0][1],'w01')

    def test_editor_copy_is_lossless_and_keeps_stable_generated_id(self):
        shared=self.lists/'german'/'a1'/'shared.json'
        source=write_material(shared,[{'word':'das Haus','definition':['home','Das ist mein Haus.'],'word_frequency':5,'custom':{'keep':True}}],name='Shared')
        first=web.load_word_list('alice','shared'); generated=first['items'][0]['id']
        first['items'][0]['definition'][0]='house'
        personal,_=web.save_word_list('alice','shared',first['items'])
        self.assertEqual(shared.read_text(encoding='utf-8'), json.dumps(source,ensure_ascii=False,indent=2)+'\n')
        saved=ll.read_word_list(personal); self.assertEqual(saved['items'][0]['id'],generated); self.assertEqual(saved['items'][0]['custom'],{'keep':True})
        second=web.load_word_list('alice','shared'); second['items'][0]['definition'][0]='home again'; web.save_word_list('alice','shared',second['items'])
        self.assertEqual(ll.read_word_list(personal)['items'][0]['id'],generated)

    def test_custom_import_accepts_no_id_and_persists_exact_target_and_stable_id(self):
        imported = {
            'metadata': {'name':'Imported','language':'german','kind':'vocabulary','level':'a1'},
            'items': [{'word':'das Buch, die Bücher','definition':'book','word_frequency':1}],
        }
        ll.save_custom_list('alice','imported',imported)
        path=ll.word_list_path_user_specific('alice','imported')
        saved=ll.read_word_list(path)
        self.assertEqual(saved['items'][0]['word'],'das Buch, die Bücher')
        first_id=saved['items'][0]['id']
        self.assertTrue(first_id.startswith('legacy-'))
        saved['items'][0]['definition']='book changed'
        ll.save_custom_list('alice','imported',saved)
        again=ll.read_word_list(path)
        self.assertEqual(again['items'][0]['id'],first_id)
        self.assertEqual(again['items'][0]['word'],'das Buch, die Bücher')

    def test_reset_word_list_progress_restarts_scores_and_keeps_sessions(self):
        self.make(material_items(3))
        self.master('id-00', '2026-08-01', box=2)
        self.update('id-00', times_practiced=5, times_correct=5, times_mastered=1)
        self.update('id-01', score=3.0, times_practiced=2, times_incorrect=2, last_practiced='2026-08-01')
        ll.log_session('alice', 'focus', 60, 3, 2, 1, 0)

        ll.reset_word_list_progress('alice', 'focus')

        row0 = self.row('id-00')
        self.assertEqual((row0['score'], row0['times_practiced'], row0['times_correct'], row0['times_mastered']), (0.0, 0, 0, 0))
        self.assertIsNone(row0['last_practiced'])
        self.assertIsNone(row0['last_tartarus_completed'])
        self.assertIsNone(row0['leitner_box'])
        self.assertIsNone(row0['leitner_last_reviewed'])
        self.assertEqual((row0['content_id'], row0['active']), ('id-00', 1))
        self.assertEqual((self.row('id-01')['score'], self.row('id-01')['times_incorrect']), (0.0, 0))

        conn = ll.get_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions_alice').fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM mastery_events WHERE user='alice' AND lang='focus'").fetchone()[0], 0)
        self.assertFalse(ll.table_exists(conn, 'dataset_progress'))
        conn.close()
        state = ll.gauntlet_state_breakdown('alice', 'focus')
        self.assertEqual((state['forging'], state['reinforcement_total']), (3, 0))

    def test_reset_word_list_progress_rejects_unknown_list(self):
        with self.assertRaises(ValueError):
            ll.reset_word_list_progress('alice', 'doesnotexist')

    def test_personal_list_is_not_exposed_to_another_user(self):
        self.make(material_items(1),user='alice',lang='secret')
        descriptors=web.list_word_lists()
        self.assertTrue(any(x['user']=='alice' and x['lang']=='secret' for x in descriptors))
        self.assertFalse(any(x['user']=='bob' and x['lang']=='secret' for x in descriptors))

    def test_progress_can_be_filtered_to_the_exact_selected_list(self):
        self.make(material_items(2), lang='focus')
        self.make(material_items(3), lang='other')
        rows = web.user_progress_data('alice', 'german_vocabulary', 'a1', 'focus')
        self.assertEqual([row['lang'] for row in rows], ['focus'])
        self.assertEqual(rows[0]['total'], 2)

    def test_report_gauge_uses_red_yellow_green_visual_bands(self):
        # gauge_band must always be one of the 3 values the CSS actually has a
        # rule for (.band-1/.band-2/.band-3) -- not score_band's 0-9 mastery
        # scale, which would leave most scores (anything 0, 4-7, or 8-9)
        # rendering with no color at all.
        self.make(material_items(6))
        boundary_scores = {'id-00': 0.0, 'id-01': 3.5, 'id-02': 4.0, 'id-03': 7.5, 'id-04': 8.0, 'id-05': 8.5}
        for content_id, score in boundary_scores.items():
            kwargs = {'leitner_box': 1, 'leitner_last_reviewed': '2026-08-08'} if score >= 9 else {}
            self.update(content_id, score=score, **kwargs)
        stats = web.word_list_stats('alice', 'focus')
        by_word = {row['word']: row for row in stats}
        for index, (content_id, score) in enumerate(boundary_scores.items()):
            gauge_band = by_word[f'w0{index}']['gauge_band']
            self.assertIn(gauge_band, (1, 2, 3), f'score {score} produced unrenderable gauge_band {gauge_band}')
            self.assertEqual(gauge_band, ll.score_color_band(score))
        self.assertEqual(by_word['w00']['gauge_band'], 1)  # 0.0 -> red
        self.assertEqual(by_word['w02']['gauge_band'], 2)  # 4.0 -> yellow
        self.assertEqual(by_word['w04']['gauge_band'], 3)  # 8.0 -> green


class MigrationContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tartarus-migration-'); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def make_v3(self, name='v3.db', tables=1):
        path=self.root/name; conn=sqlite3.connect(path)
        conn.execute('CREATE TABLE users(name TEXT PRIMARY KEY,created_at TEXT)')
        conn.execute("INSERT INTO users VALUES('alice','2026-08-01')")
        conn.execute('CREATE TABLE dataset_progress(user TEXT,lang TEXT,current_stage INTEGER,current_day INTEGER,sessions_done_today INTEGER,last_practice_date TEXT,PRIMARY KEY(user,lang))')
        for n in range(tables):
            lang=f'focus{n}'
            table=f'words_alice_{lang}'
            conn.execute(f'''CREATE TABLE "{table}"(
              id INTEGER PRIMARY KEY, content_id TEXT UNIQUE NOT NULL, score REAL NOT NULL DEFAULT 0,
              last_practiced TEXT, active INTEGER DEFAULT 1, times_practiced INTEGER DEFAULT 0,
              times_correct INTEGER DEFAULT 0, times_incorrect INTEGER DEFAULT 0, times_drilled INTEGER DEFAULT 0,
              times_mastered INTEGER DEFAULT 0, leitner_box INTEGER, last_known_review_at TEXT,
              drill_pending INTEGER DEFAULT 0, times_flagged INTEGER DEFAULT 0, stage_reached INTEGER DEFAULT 0)''')
            conn.execute(f'INSERT INTO "{table}"(content_id,score,last_practiced,times_practiced,times_correct,times_incorrect,times_drilled,times_mastered,leitner_box,last_known_review_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (f'id-{n}',9.0,'2026-08-07',7,5,2,1,3,4,'2026-08-06'))
            conn.execute('INSERT INTO dataset_progress VALUES(?,?,5,10,1,?)',('alice',lang,'2026-08-07'))
        conn.execute('PRAGMA user_version=3'); conn.commit(); conn.close(); return path

    def test_v3_to_v5_is_atomic_preserves_history_drops_progress_and_backs_up(self):
        path = self.make_v3()
        before = logical_db_dump(path)
        backup = ll.migrate_database(str(path), create_backup=True)
        self.assertTrue(Path(backup).exists())
        conn = sqlite3.connect(path); table = 'words_alice_focus0'
        self.assertEqual([row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')], ll.WORD_TABLE_COLUMNS)
        row = conn.execute(f'SELECT content_id,score,last_practiced,last_tartarus_completed,times_practiced,times_correct,times_incorrect,times_drilled,times_mastered,leitner_box,leitner_last_reviewed FROM "{table}"').fetchone()
        self.assertEqual(row, ('id-0',9.0,'2026-08-07','2026-08-07',7,5,2,1,3,4,'2026-08-07'))
        self.assertFalse(ll.table_exists(conn, 'dataset_progress'))
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], ll.SCHEMA_VERSION)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], len(before[table]['rows']))
        conn.close()
        check = sqlite3.connect(f'file:{backup}?mode=ro', uri=True)
        self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0], 'ok'); check.close()
        self.assertIsNone(ll.migrate_database(str(path), create_backup=False))

    def test_application_startup_keeps_verified_migration_snapshot(self):
        path = self.make_v3(name='runtime.db')
        lists = self.root / 'lists'; lists.mkdir()
        old_db, old_lists = ll.DATABASE_FILE, ll.WORD_LISTS_DIR
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR = str(path), str(lists)
        try:
            ll.initialize_database(create_backup=True)
            snapshots = list(self.root.glob(f'runtime.db.pre-v{ll.SCHEMA_VERSION}.*.sqlite'))
            self.assertEqual(len(snapshots), 1)
            check = sqlite3.connect(f'file:{snapshots[0]}?mode=ro', uri=True)
            self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0], 'ok'); check.close()
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], ll.SCHEMA_VERSION)
            self.assertFalse(ll.table_exists(conn, 'dataset_progress'))
            conn.close()
            ll.initialize_database(create_backup=True)
            self.assertEqual(list(self.root.glob(f'runtime.db.pre-v{ll.SCHEMA_VERSION}.*.sqlite')), snapshots)
        finally:
            ll.DATABASE_FILE, ll.WORD_LISTS_DIR = old_db, old_lists

    def test_injected_failure_rolls_back_every_table(self):
        path = self.make_v3(tables=2); before = logical_db_dump(path)
        with self.assertRaisesRegex(RuntimeError, 'Injected migration failure'):
            ll.migrate_database(str(path), create_backup=False, fail_after_tables=1)
        self.assertEqual(logical_db_dump(path), before)

    def test_v1_backup_imports_and_current_v4_backup_round_trips(self):
        tmp = self.root / 'runtime.db'; lists = self.root / 'lists'; lists.mkdir()
        old_db, old_lists = ll.DATABASE_FILE, ll.WORD_LISTS_DIR
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR = str(tmp), str(lists)
        try:
            ll.initialize_database(create_backup=False)
            v1 = {'format':ll.BACKUP_FORMAT,'version':1,'user':{'name':'alice','created_at':'2026-08-01'},'word_progress':{'focus':[{
                'id':1,'content_id':'x','score':9.0,'last_practiced':'2026-08-01','active':1,'times_practiced':2,'times_correct':2,'times_incorrect':0,'times_drilled':0,'times_mastered':0,'leitner_box':3,'last_known_review_at':'2026-08-02'
            }]},'sessions':[],'gauntlet_progress':[]}
            ll.import_user_data('alice', v1)
            conn = ll.get_connection(); ll.record_mastery_event(conn,'alice','focus',1,'mastered','2026-08-01'); conn.commit(); conn.close()
            exported = ll.export_user_data('alice')
            self.assertEqual(exported['version'], ll.BACKUP_VERSION)
            self.assertEqual(exported['mastery_events'], [{'lang':'focus','word_id':1,'event_type':'mastered','mastered_date':'2026-08-01'}])
            self.assertNotIn('gauntlet_progress', exported)
            row = exported['word_progress']['focus'][0]
            self.assertEqual(row['last_tartarus_completed'], '2026-08-01')
            self.assertEqual(row['leitner_last_reviewed'], '2026-08-01')
            self.assertNotIn('last_known_review_at', row)
            copydb = self.root / 'copy.db'; ll.DATABASE_FILE = str(copydb)
            ll.import_user_data('alice', exported)
            self.assertEqual(ll.export_user_data('alice'), exported)
        finally:
            ll.DATABASE_FILE, ll.WORD_LISTS_DIR = old_db, old_lists




class BundledCorpusContractTest(unittest.TestCase):
    """Structural invariants the real bundled corpus must hold for word-list
    resolution (section 14 of data/DATASET_SCHEMA_GUIDE.md) and learner-progress
    identity stability (section 5/15) to keep working."""

    def bundled_files(self):
        root = Path(ll.PROJECT_DIR) / 'data' / 'word_lists'
        return sorted(root.glob('*/*/*/*.json'))

    def test_every_bundled_list_id_is_globally_unique(self):
        files = self.bundled_files()
        self.assertTrue(files)
        stems = [path.stem for path in files]
        duplicates = {stem for stem in stems if stems.count(stem) > 1}
        self.assertEqual(duplicates, set())

    def test_every_bundled_item_has_a_stable_explicit_id(self):
        files = self.bundled_files()
        self.assertTrue(files)
        for path in files:
            data = ll.read_word_list(str(path))
            for index, item in enumerate(data['items'], start=1):
                explicit_id = str(item.get('id', '')).strip()
                self.assertTrue(explicit_id, f'{path.name} item {index} is missing an explicit id')
            ids = [item['id'] for item in data['items']]
            self.assertEqual(len(ids), len(set(ids)), f'{path.name} has duplicate ids')


class ServerHarness(unittest.TestCase):
    TTS_DELAY_MS=0
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tartarus-http-'); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.db=self.root/'progress.db'; self.lists=self.root/'word_lists'; self.lists.mkdir(); self.audio=self.root/'audio'; self.audio.mkdir(); self.log=self.root/'app.log'; self.port=free_port(); self.base=f'http://127.0.0.1:{self.port}'
        env=os.environ.copy(); env.update({'TARTARUS_DB':str(self.db),'TARTARUS_WORD_LISTS_DIR':str(self.lists),'TARTARUS_AUDIO_DIR':str(self.audio),'TARTARUS_LOG_FILE':str(self.log),'TARTARUS_PORT':str(self.port),'TARTARUS_HOST':'127.0.0.1','TARTARUS_SESSION_TTL_SECONDS':'2','TARTARUS_TTS_TEST_DELAY_MS':str(self.TTS_DELAY_MS),'PYTHONDONTWRITEBYTECODE':'1'})
        self.server=subprocess.Popen([sys.executable,str(UTILS/'tartarus_web.py')],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop)
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            if self.server.poll() is not None: self.fail('server startup failed: '+self.server.stderr.read())
            try:
                if self.raw('/')[0]==200: break
            except Exception: time.sleep(.04)
        else: self.fail('server did not start')

    def stop(self):
        if getattr(self,'server',None) and self.server.poll() is None:
            self.server.terminate()
            try:self.server.wait(5)
            except subprocess.TimeoutExpired:self.server.kill();self.server.wait(5)
        if getattr(self,'server',None) and self.server.stderr and not self.server.stderr.closed:self.server.stderr.close()

    def raw(self,path,payload=None,method=None,content_type='application/json',raw=None,timeout=10):
        body=raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        headers={'Content-Type':content_type} if body is not None else {}
        req=urllib.request.Request(self.base+path,data=body,headers=headers,method=method or ('POST' if body is not None else 'GET'))
        try:
            with urllib.request.urlopen(req,timeout=timeout) as resp:return resp.status,resp.read()
        except urllib.error.HTTPError as e:return e.code,e.read()

    def api(self,path,payload=None,expected=200,**kwargs):
        status,raw=self.raw(path,payload,**kwargs); data=json.loads(raw or b'{}') if path.startswith('/api/') else raw
        self.assertEqual(status,expected,(path,status,data)); return data

    def create(self,items=None,user='alice',lang='focus'):
        self.api('/api/user/create',{'user':user}); self.api('/api/init',{'user':user,'lang':lang}); self.api('/api/wordlist',{'user':user,'lang':lang,'items':items or material_items(1)})

    def bundled_list(self,lang,words,*,language='german',kind='vocabulary',level='a1'):
        """A shared (non-personal) list under the nested bundled layout, the
        only shape lookup_bundled_audio()/word_list_path() resolve as shared."""
        directory=self.lists/language/kind/level; directory.mkdir(parents=True,exist_ok=True)
        payload={'metadata':{'name':lang,'language':language,'kind':kind,'level':level},
                 'items':[{'word':w,'definition':'d','id':f'id-{i}'} for i,w in enumerate(words)]}
        (directory/f'{lang}.json').write_text(json.dumps(payload),encoding='utf-8')
        return f'{language}/{kind}/{level}/{lang}'

    def bundled_audio(self,stem,entries,*,content_type='audio/mp4'):
        """entries: {text: bytes}. Writes one part1.db matching the generator's schema."""
        path=self.audio/f'{stem}.part1.db'; path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(path)
        conn.execute('CREATE TABLE audio(id INTEGER PRIMARY KEY,source_file TEXT,language TEXT,text TEXT,voice TEXT,rate INTEGER,content_type TEXT,audio BLOB,byte_size INTEGER,generated_at TEXT,UNIQUE(text))')
        conn.executemany('INSERT INTO audio(source_file,language,text,voice,rate,content_type,audio,byte_size,generated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                          [(stem,'german',text,'Anna',128,content_type,data,len(data),'2026-01-01T00:00:00') for text,data in entries.items()])
        conn.commit(); conn.close()

    def start(self,user='alice',lang='focus',**extra):
        return self.api('/api/practice/start',{'user':user,'lang':lang,**extra})

    def answer(self,started,answer,attempt='a1',question=None,expected=200,**extra):
        q=question or started['question']; return self.api('/api/practice/answer',{'session_id':started['session_id'],'question_id':q['question_id'],'sequence':q['sequence'],'attempt_id':attempt,'answer':answer,**extra},expected=expected)


class HttpContractTest(ServerHarness):
    TTS_DELAY_MS=80

    def test_client_disconnect_mid_response_is_logged_quietly_not_as_a_crash(self):
        self.create(items=material_items(16))
        # A closed-without-reading socket reproduces what a browser tab
        # closing/refreshing mid-request looks like on the wire: the server's
        # write to that connection fails with a broken pipe partway through a
        # large response. Several attempts since not every close lands while
        # a write is in flight.
        for _ in range(8):
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as s:
                s.sendall(b'GET /api/wordlists HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        deadline = time.monotonic() + 5
        log_text = ''
        while time.monotonic() < deadline:
            log_text = self.log.read_text(encoding='utf-8') if self.log.exists() else ''
            if 'CLIENT_DISCONNECTED' in log_text:
                break
            time.sleep(.05)
        self.assertIn('CLIENT_DISCONNECTED', log_text)
        self.assertNotIn('SERVER_CRASH', log_text)
        self.assertIsNone(self.server.poll(), 'server process must survive a client disconnect')
        # The server is still fully functional afterward -- not left in a bad state.
        self.assertEqual(self.raw('/')[0], 200)

    def test_exact_german_noun_and_sixteen_question_session(self):
        items=material_items(16); items[0]={'id':'buch','word':'das Buch, die Bücher','definition':'book','word_frequency':0}; self.create(items)
        started=self.start(); self.assertEqual(started['progress']['max_questions'],16)
        # Locate Buch even though equal-score session order is randomized.
        q=started['question']; seen=0
        while q['word_unmasked']!='das Buch, die Bücher':
            result=self.answer({'session_id':started['session_id'],'question':q},q['word_unmasked'],f'ok{seen}',question=q); seen+=1
            self.assertFalse(result['done']); q=result['question']
        wrong=self.answer({'session_id':started['session_id'],'question':q},'das Buch','partial',question=q)
        self.assertEqual(wrong['result'],'drill_start')
        self.api('/api/practice/cancel',{'session_id':started['session_id']},expected=409)
        result=wrong
        for i in range(9):
            result=self.answer({'session_id':started['session_id'],'question':q},'das Buch, die Bücher',f'd{i}',question=q)
        self.assertEqual(result['result'],'drilled')

    def test_shadows_mistake_escalates_two_productions_to_nine_answer_drill(self):
        self.create(items=material_items(1)); today = date.today(); mastered = (today - timedelta(days=3)).isoformat()
        conn = sqlite3.connect(self.db); table = ll.words_table_name('alice','focus')
        word_id = conn.execute(f'SELECT id FROM "{table}"').fetchone()[0]
        conn.execute(f'UPDATE "{table}" SET score=9.0,leitner_box=1,leitner_last_reviewed=?,last_tartarus_completed=?,gauntlet_completed_day=2', (today.isoformat(), mastered))
        conn.execute('INSERT INTO mastery_events(user,lang,word_id,event_type,mastered_date) VALUES(?,?,?,?,?)', ('alice','focus',word_id,'mastered',mastered))
        conn.commit(); conn.close()
        started = self.start(); question = started['question']
        self.assertEqual((question['gauntlet']['mode'], question['gauntlet']['day']), ('shadows', 3))
        self.assertEqual(question['drill_start']['target'], 2)
        self.assertEqual(question['drill_start']['word'], question['word_unmasked'])
        self.assertEqual(question['drill_start']['definition'], question['definition'])
        self.assertFalse(question['drill_start']['show_word'])
        result = self.answer(started, 'wrong', 'wrong', question=question)
        self.assertEqual((result['result'],result['drill']['target'],result['drill']['correct_in_a_row']), ('drill_progress',9,0))
        for index in range(9):
            result = self.answer(started, question['word_unmasked'], f'drill-{index}', question=question)
        self.assertEqual(result['result'], 'drilled')
        conn = sqlite3.connect(self.db)
        row = conn.execute(f'SELECT score,leitner_box,last_tartarus_completed,times_incorrect,times_drilled FROM "{table}"').fetchone(); conn.close()
        self.assertEqual(row, (9.0,1,today.isoformat(),1,1))
        self.assertEqual(len(result['session']['incorrect']), 1)

    def test_abandoned_practice_options_are_rejected(self):
        self.create()
        for key,value in [('review_mode',True),('fast_mode',True),('known_drill_mode',True),('drill_all',True),('mode','leitner')]:
            data=self.api('/api/practice/start',{'user':'alice','lang':'focus',key:value},expected=400)
            self.assertIn('unsupported practice option',data['error'])

    def test_duplicate_answer_is_idempotent_and_stale_requests_are_rejected(self):
        self.create(items=material_items(2)); started=self.start(); q=started['question']
        first=self.answer(started,q['word_unmasked'],'dup',question=q); second=self.answer(started,q['word_unmasked'],'dup',question=q)
        self.assertEqual(first,second)
        self.answer(started,q['word_unmasked'],'stale-q',question=q,expected=409)
        current=first['question']; self.answer(started,current['word_unmasked'],'stale-seq',question=current,sequence=current['sequence']-1,expected=409)
        self.api('/api/practice/answer',{'session_id':started['session_id'],'question_id':current['question_id'],'sequence':current['sequence'],'answer':current['word_unmasked']},expected=400)

    def test_cancel_during_drill_is_rejected_without_advancing_word(self):
        self.create(items=material_items(2)); started=self.start(); q=started['question']
        correct=self.answer(started,q['word_unmasked'],'ok',question=q); summary=self.api('/api/practice/cancel',{'session_id':started['session_id']})['session']
        self.assertTrue(summary['ended_early']); self.assertEqual(summary['practiced'],1)
        started=self.start(); q=started['question']
        conn=sqlite3.connect(self.db); table=ll.words_table_name('alice','focus')
        score_before=conn.execute(f'SELECT score FROM "{table}" WHERE id=?',(q['word_id'],)).fetchone()[0]; conn.close()
        self.answer(started,'wrong','bad',question=q)
        rejected=self.api('/api/practice/cancel',{'session_id':started['session_id']},expected=409)
        self.assertIn('Complete the mandatory drill',rejected['error'])
        conn=sqlite3.connect(self.db)
        row=conn.execute(f'SELECT score,last_tartarus_completed,leitner_box,times_incorrect FROM "{table}" WHERE id=?',(q['word_id'],)).fetchone(); conn.close()
        self.assertEqual(row,(score_before,None,None,1))

    def test_pending_drill_is_durable_and_resumes_after_a_simulated_restart(self):
        # P5: a mandatory drill obligation must survive a crash/restart, not
        # just live in the in-memory session dict.
        self.create(items=material_items(1)); started=self.start(); q=started['question']
        word = q['word_unmasked']
        self.answer(started, 'wrong-first-try', 'bad', question=q)
        conn=sqlite3.connect(self.db)
        pending=conn.execute('SELECT word_id,correct_in_a_row,target,context,mode FROM pending_drills').fetchall()
        conn.close()
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0][0], pending[0][1]), (q['word_id'], 0))

        # One correct drill repetition -- progress persists, drill not done.
        progress=self.answer(started, word, 'r1', question=q)
        self.assertEqual(progress['result'], 'drill_progress')
        conn=sqlite3.connect(self.db)
        row=conn.execute('SELECT correct_in_a_row FROM pending_drills WHERE word_id=?',(q['word_id'],)).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

        # Simulate a crash/restart: start a brand-new session before the
        # drill is resolved. It must resume the exact same obligation.
        resumed=self.start(); rq=resumed['question']
        self.assertEqual(rq['word_unmasked'], word)
        self.assertEqual(rq['drill_start']['correct_in_a_row'], 1)

        # Finishing it from the resumed session clears the durable record.
        for i in range(8):
            result=self.answer(resumed, word, f'finish-{i}', question=rq)
        self.assertEqual(result['result'], 'drilled')
        conn=sqlite3.connect(self.db)
        remaining=conn.execute('SELECT COUNT(*) FROM pending_drills').fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    def test_session_row_is_tagged_with_its_single_mode_and_accuracy_excludes_drills(self):
        # P4: a session is single-mode by construction now, so tagging its
        # row with that mode/stage is unambiguous; accuracy is first-attempt
        # (correct / (correct+incorrect)) everywhere, matching report/dashboard.
        self.create(items=material_items(1)); started=self.start(); q=started['question']
        result=self.answer(started, q['word_unmasked'], 'ok', question=q)
        self.assertTrue(result['done'])
        self.assertEqual(result['session']['accuracy'], 100.0)
        conn=sqlite3.connect(self.db)
        row=conn.execute('SELECT mode,stage FROM sessions_alice ORDER BY id DESC LIMIT 1').fetchone()
        conn.close()
        self.assertEqual(row, ('forging', 0))

    def test_wordlist_restart_endpoint_resets_progress(self):
        self.create(items=material_items(2))
        conn = sqlite3.connect(self.db); table = ll.words_table_name('alice','focus'); today = date.today().isoformat()
        word_id = conn.execute(f'SELECT id FROM "{table}" WHERE content_id=?', ('id-00',)).fetchone()[0]
        conn.execute(f'UPDATE "{table}" SET score=9.0,times_practiced=3,leitner_box=2,leitner_last_reviewed=?,last_tartarus_completed=? WHERE content_id=?', (today,today,'id-00'))
        conn.execute('INSERT INTO mastery_events(user,lang,word_id,event_type,mastered_date) VALUES(?,?,?,?,?)', ('alice','focus',word_id,'mastered',today))
        conn.commit(); conn.close()

        self.api('/api/wordlist/restart', {'user':'alice','lang':'focus'})

        conn = sqlite3.connect(self.db)
        row = conn.execute(f'SELECT score,times_practiced,leitner_box FROM "{table}" WHERE content_id=?', ('id-00',)).fetchone()
        events = conn.execute("SELECT COUNT(*) FROM mastery_events WHERE user='alice' AND lang='focus'").fetchone()[0]
        self.assertFalse(ll.table_exists(conn, 'dataset_progress')); conn.close()
        self.assertEqual(row, (0.0,0,None)); self.assertEqual(events, 0)

    def test_wordlist_restart_endpoint_rejects_unknown_list(self):
        self.create()
        data=self.api('/api/wordlist/restart',{'user':'alice','lang':'doesnotexist'},expected=400)
        self.assertIn('error',data)

    def test_requests_and_client_errors_are_logged_at_default_level(self):
        self.create()
        self.api('/api/wordlist/restart',{'user':'alice','lang':'doesnotexist'},expected=400)
        self.api('/api/client-log',{'level':'error','message':'boom on the client','stack':'at x.js:1'})
        log_text=self.log.read_text(encoding='utf-8')
        self.assertIn('HTTP_POST',log_text)
        self.assertIn("HTTP_ERROR | path: /api/wordlist/restart | status: 400",log_text)
        self.assertIn('CLIENT_ERROR | message: boom on the client',log_text)

    def test_session_expires_without_mutating_after_expiry(self):
        self.create(); started=self.start(); time.sleep(2.2)
        self.answer(started,started['question']['word_unmasked'],'late',expected=404)

    def test_all_status_report_gets_are_logically_read_only(self):
        self.create(items=material_items(2)); before=logical_db_dump(self.db)
        paths=['/api/wordlists','/api/report?user=alice&lang=focus','/api/report/summary?user=alice','/api/user/progress?user=alice','/api/wordlist?user=alice&lang=focus','/api/wordlist/stats?user=alice&lang=focus','/api/dashboard?user=alice&lang=focus','/api/export?user=alice','/api/wordlist/leitner?user=alice&lang=focus','/api/gauntlet/progress?user=alice&lang=focus','/api/report/trend?user=alice&lang=focus&metric=mastered','/api/report/trend?user=alice&lang=focus&metric=box10','/api/audio?user=alice&lang=focus&text=w00']
        for path in paths:self.raw(path)
        self.assertEqual(logical_db_dump(self.db),before)

    def test_pregenerated_audio_is_served_with_content_type_and_is_cacheable(self):
        stem=self.bundled_list('bundled_focus',['w00','w01'])
        self.bundled_audio(stem,{'w00':b'FAKE-M4A-BYTES'})
        status,body=self.raw('/api/audio?user=alice&lang=bundled_focus&text=w00')
        self.assertEqual((status,body),(200,b'FAKE-M4A-BYTES'))
        req=urllib.request.Request(self.base+'/api/audio?user=alice&lang=bundled_focus&text=w00')
        with urllib.request.urlopen(req,timeout=10) as resp:
            self.assertEqual(resp.headers.get('Content-Type'),'audio/mp4')
            self.assertIn('max-age',resp.headers.get('Cache-Control',''))
            self.assertNotIn('no-store',resp.headers.get('Cache-Control',''))

    def test_pregenerated_audio_404s_for_unknown_text_and_personal_lists(self):
        stem=self.bundled_list('bundled_focus2',['w00'])
        self.bundled_audio(stem,{'w00':b'FAKE'})
        self.assertEqual(self.raw('/api/audio?user=alice&lang=bundled_focus2&text=nope')[0],404)
        self.create(items=material_items(1))
        self.assertEqual(self.raw('/api/audio?user=alice&lang=focus&text=w00')[0],404)

    def test_pregenerated_audio_requires_user_lang_and_text(self):
        self.assertEqual(self.raw('/api/audio?user=alice&lang=focus')[0],400)
        self.assertEqual(self.raw('/api/audio')[0],400)

    def test_trend_endpoint_returns_cumulative_multi_day_series(self):
        self.create(items=material_items(3))
        conn=sqlite3.connect(self.db); table=ll.words_table_name('alice','focus')
        ids=[row[0] for row in conn.execute(f'SELECT id FROM "{table}" ORDER BY id')]
        conn.executemany(
            'INSERT INTO mastery_events(user,lang,word_id,event_type,mastered_date) VALUES(?,?,?,?,?)',
            [
                ('alice','focus',ids[0],'mastered','2026-08-01'),
                ('alice','focus',ids[1],'mastered','2026-08-03'),
                ('alice','focus',ids[2],'mastered','2026-08-03'),
                ('alice','focus',ids[0],'box10','2026-08-05'),
            ],
        )
        conn.commit(); conn.close()
        self.assertEqual(self.api('/api/report/trend?user=alice&lang=focus&metric=mastered')['series'],[
            {'date':'2026-08-01','cumulative':1},
            {'date':'2026-08-03','cumulative':3},
        ])
        self.assertEqual(self.api('/api/report/trend?user=alice&lang=focus&metric=box10')['series'],[
            {'date':'2026-08-05','cumulative':1},
        ])
        self.api('/api/report/trend?user=alice&lang=focus&metric=unknown',expected=400)

    def test_dashboard_and_progress_share_canonical_gauntlet_roadmap_shape(self):
        self.create(items=material_items(2))
        dashboard = self.api('/api/dashboard?user=alice&lang=focus')['roadmap']['gauntlet']
        progress = self.api('/api/gauntlet/progress?user=alice&lang=focus')['roadmap']['gauntlet']
        expected = {'total_tasks','forging','mastered_total','reinforcement_total','reinforcement_stages','long_term_review','due_reinforcement','due_maintenance','available_tasks','complete','locked_today'}
        self.assertEqual(set(dashboard), expected)
        self.assertEqual(progress, dashboard)

    def test_report_contract_has_no_due_known_or_manual_mastery_fields(self):
        self.create(); progress=self.api('/api/user/progress?user=alice')['lists'][0]; self.assertNotIn('due_today',progress); self.assertNotIn('learned',progress)
        words=self.api('/api/wordlist/stats?user=alice&lang=focus')['words']; self.assertNotIn('last_known_review_at',words[0]); self.assertNotIn('times_mastered',words[0])
        dash=self.api('/api/dashboard?user=alice&lang=focus'); self.assertNotIn('prediction',dash); self.assertNotIn('benchmark',dash)

    def test_tts_failure_is_bounded_and_unknown_route_is_structured(self):
        self.api('/api/nope',expected=404)
        result=self.api('/api/tts',{'text':'hello','lang':'german'})
        self.assertTrue(result.get('simulated'))
        self.api('/api/user/create',raw=b'not json',content_type='text/plain',expected=400)

    def test_loopback_release_binding_contract(self):
        self.assertTrue(web._loopback_host('127.0.0.1')); self.assertTrue(web._loopback_host('::1')); self.assertTrue(web._loopback_host('localhost')); self.assertFalse(web._loopback_host('0.0.0.0'))


class SafariWebDriver:
    """Minimal W3C WebDriver adapter for the macOS Safari release gate."""
    def __init__(self):
        exe = shutil.which('safaridriver')
        if not exe:
            raise unittest.SkipTest('safaridriver unavailable')
        self.port = free_port()
        self.process = subprocess.Popen(
            [exe, '-p', str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.session_id = None
        deadline = time.monotonic() + 15
        last_error = None
        while time.monotonic() < deadline:
            try:
                result = self.call('POST', '/session', {
                    'capabilities': {'alwaysMatch': {'browserName': 'safari'}},
                })
                self.session_id = result['value']['sessionId']
                break
            except Exception as error:
                last_error = error
                if self.process.poll() is not None:
                    break
                time.sleep(.1)
        if not self.session_id:
            self.close()
            raise AssertionError(
                'Safari WebDriver startup failed. Enable Safari Develop > Allow Remote Automation '
                f'and run `safaridriver --enable` once if required. Last error: {last_error}'
            )

    def call(self, method, path, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        body = None if payload is None else json.dumps(payload)
        try:
            connection.request(method, path, body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read().decode()
        finally:
            connection.close()
        if response.status >= 400:
            raise AssertionError(f'WebDriver {method} {path}: {response.status} {raw}')
        return json.loads(raw) if raw else {'value': None}

    def script(self, script, *args):
        return self.call(
            'POST', f'/session/{self.session_id}/execute/sync',
            {'script': script, 'args': list(args)},
        )['value']

    def viewport(self, width, height):
        self.call(
            'POST', f'/session/{self.session_id}/window/rect',
            {'width': width, 'height': height},
        )

    def close(self):
        if getattr(self, 'session_id', None):
            with contextlib.suppress(Exception):
                self.call('DELETE', f'/session/{self.session_id}')
            self.session_id = None
        process = getattr(self, 'process', None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(5)


def make_browser_driver():
    requested = os.environ.get('TARTARUS_BROWSER', '').strip().lower()
    if requested == 'safari':
        return SafariWebDriver()
    if requested == 'chromium':
        return ChromiumCDP()
    if sys.platform == 'darwin' and shutil.which('safaridriver'):
        return SafariWebDriver()
    return ChromiumCDP()


class ChromiumCDP:
    """Tiny CDP wrapper; skips cleanly when Chromium/websocket are unavailable."""
    def __init__(self):
        try: import websocket
        except ImportError: raise unittest.SkipTest('websocket-client unavailable')
        self.websocket=websocket; self.tmp=tempfile.TemporaryDirectory(prefix='tartarus-chromium-'); self.port=free_port(); self.ws=None; self._id=0
        exe=os.environ.get('TARTARUS_CHROMIUM') or shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
        if not exe: self.tmp.cleanup(); raise unittest.SkipTest('Chromium unavailable')
        self.process=subprocess.Popen([exe,'--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-allow-origins=*',f'--remote-debugging-port={self.port}',f'--user-data-dir={self.tmp.name}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/list',timeout=1) as r: pages=json.load(r)
                if pages:
                    self.ws=websocket.create_connection(pages[0]['webSocketDebuggerUrl'],timeout=30); break
            except Exception: time.sleep(.04)
        if not self.ws: self.close(); raise AssertionError('Chromium CDP startup failed')
        self.call('Page.enable'); self.call('Runtime.enable')
    def call(self,method,params=None):
        self._id+=1; cid=self._id; self.ws.send(json.dumps({'id':cid,'method':method,'params':params or {}}))
        while True:
            msg=json.loads(self.ws.recv())
            if msg.get('id')!=cid: continue
            if 'error' in msg: raise AssertionError(msg['error'])
            return msg.get('result',{})
    def script(self,script,*args):
        expr=f"(function(){{{script}}}).apply(null,{json.dumps(args,ensure_ascii=False)})"; result=self.call('Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True})
        if result.get('exceptionDetails'): raise AssertionError(result)
        return result.get('result',{}).get('value')
    def viewport(self,w,h): self.call('Emulation.setDeviceMetricsOverride',{'width':w,'height':h,'deviceScaleFactor':1,'mobile':False})
    def close(self):
        with contextlib.suppress(Exception):
            if self.ws:self.ws.close()
        if getattr(self,'process',None) and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(5)
        with contextlib.suppress(Exception):self.tmp.cleanup()


class BrowserContractTest(unittest.TestCase):
    def setUp(self):
        self.browser=make_browser_driver(); self.addCleanup(self.browser.close); self.browser.viewport(1280,900)
        import re
        index=(ROOT/'web/index.html').read_text(encoding='utf-8'); css=(ROOT/'web/style.css').read_text(encoding='utf-8'); app=(ROOT/'web/app.js').read_text(encoding='utf-8')
        index=re.sub(r'<link\s+rel="stylesheet"[^>]*>',f'<style>{css}</style>',index,count=1); index=re.sub(r'<script\s+src="/app\.js[^>]*></script>','',index,count=1)
        self.browser.script("document.open();document.write(arguments[0]);document.close();return true;",index)
        self.browser.script(r"""
          window.__errors=[];addEventListener('error',e=>__errors.push(String(e.error||e.message)));addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
          const q=(id,seq,type='learning',word='w00',prompt=null)=>({question_id:id,sequence:seq,word:prompt!==null?prompt:(type==='learning'?word:''),word_unmasked:word,audio_text:word,definition:['definition'],score:type==='production'?8:0,gauge:'○○○',gender:'none',type,gauntlet:{mode:type==='learning'?'forging':type,stage:0,stage_name:'The Forging',day:0,sessions_done:0}});
          const state=window.__api={ttsDelay:500,ttsCalls:0,answers:0,current:q('q0',1),lastBody:null,startType:'learning',startWord:'w00',startPrompt:null,finishOnAnswer:false,forceWrong:false,drill:false,drillComplete:false,startCount:0,progressUrls:[]};
          const jr=(x,status=200)=>new Response(JSON.stringify(x),{status,headers:{'Content-Type':'application/json'}});
          window.fetch=(input,init={})=>{const url=String(input);if(url.startsWith('/api/wordlists'))return Promise.resolve(jr({users:['alice'],wordlists:[{user:'alice',lang:'focus',language:'german',kind:'vocabulary',category:'german_vocabulary',cefr_level:'a1',pos:'noun',name:'Focus',word_count:20,shared:true}]}));
            if(url.startsWith('/api/user/progress')){state.progressUrls.push(url);return Promise.resolve(jr({lists:[{lang:'focus',name:'Focus',total:20,tartarus_score9:0,leitner_box10:0,tartarus_track_complete:false,learning_complete:false}]}));}
            if(url.startsWith('/api/report/trend'))return Promise.resolve(jr({series:[{date:'2026-08-01',cumulative:1},{date:'2026-08-03',cumulative:3}]}));
            if(url.startsWith('/api/report?'))return Promise.resolve(jr({reports:[],roadmap:{gauntlet:{total_tasks:20,forging:20,mastered_total:0,reinforcement_total:0,reinforcement_stages:[{stage:1,name:'The Crucible',mode:'crucible',days:'1-2',count:0},{stage:2,name:'The Shadows',mode:'shadows',days:'3-4',count:0},{stage:3,name:'The Depths',mode:'depths',days:'5-6',count:0},{stage:4,name:'The Void',mode:'void',days:'7-8',count:0},{stage:5,name:'Ascension',mode:'ascension',days:'9-10',count:0}],long_term_review:0,due_reinforcement:0,available_tasks:20,complete:false,locked_today:false},leitner_distribution:{'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,'10':0},maintenance_ready:0}}));
            if(url.startsWith('/api/dashboard'))return Promise.resolve(jr({overview:{streak:{current:1,best:2},total_seconds:120,overall_accuracy:90},velocity:{avg_seconds_per_word:6,sessions:1},tracks:{total:20,tartarus_score9:3,leitner_box10:1,tartarus_track_complete:false,learning_complete:false},nemesis:[],roadmap:null}));
            if(url.startsWith('/api/wordlist/leitner'))return Promise.resolve(jr({leitner:{distribution:{'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,'10':0},ready:0,box10:0}}));
            if(url.startsWith('/api/wordlist/stats'))return Promise.resolve(jr({words:[]}));
            if(url.startsWith('/api/gauntlet/progress'))return Promise.resolve(jr({progress:{total_tasks:20,forging:20,mastered_total:0,reinforcement_total:0,reinforcement_stages:[{stage:1,name:'The Crucible',mode:'crucible',days:'1-2',count:0},{stage:2,name:'The Shadows',mode:'shadows',days:'3-4',count:0},{stage:3,name:'The Depths',mode:'depths',days:'5-6',count:0},{stage:4,name:'The Void',mode:'void',days:'7-8',count:0},{stage:5,name:'Ascension',mode:'ascension',days:'9-10',count:0}],long_term_review:0,due_reinforcement:0,available_tasks:20,complete:false,locked_today:false},roadmap:{gauntlet:{total_tasks:20,forging:20,mastered_total:0,reinforcement_total:0,reinforcement_stages:[{stage:1,name:'The Crucible',mode:'crucible',days:'1-2',count:0},{stage:2,name:'The Shadows',mode:'shadows',days:'3-4',count:0},{stage:3,name:'The Depths',mode:'depths',days:'5-6',count:0},{stage:4,name:'The Void',mode:'void',days:'7-8',count:0},{stage:5,name:'Ascension',mode:'ascension',days:'9-10',count:0}],long_term_review:0,due_reinforcement:0,available_tasks:20,complete:false,locked_today:false},leitner_distribution:{'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,'10':0},maintenance_ready:0}}));
            if(url==='/api/practice/start'){state.startCount++;state.current=q('q0',1,state.startType,state.startWord,state.startPrompt);state.drill=false;return Promise.resolve(jr({session_id:'s'+state.startCount,lang:'focus',audio_lang:'german',gauntlet:{mode:state.startType,stage:0,stage_name:'The Forging',day:0},progress:{correct:0,drilled:0,total:16,questions:0,max_questions:16},question:state.current}));}
            if(url==='/api/practice/answer'){state.answers++;state.lastBody=JSON.parse(init.body||'{}');if(state.drill){if(state.drillComplete){state.drill=false;const next=q('q1',2,state.startType,'w01');state.current=next;return Promise.resolve(jr({result:'drilled',done:false,drill:{word:'w00',definition:['definition'],repetition:9,correct_in_a_row:9,target:9,correct:true,show_word:true},question:next,progress:{correct:0,drilled:1,total:16,questions:1,max_questions:16}}));}return Promise.resolve(jr({result:'drill_progress',done:false,drill:{word:state.current.word_unmasked,definition:['definition'],repetition:2,correct_in_a_row:0,target:9,correct:false,show_word:true}}));}if(state.forceWrong){state.drill=true;return Promise.resolve(jr({result:'drill_start',done:false,message:'Incorrect. Complete the mandatory drill before continuing.',drill:{word:state.current.word_unmasked,definition:['definition'],repetition:1,correct_in_a_row:0,target:9,correct:false,show_word:true}}));}if(state.finishOnAnswer){return Promise.resolve(jr({result:'correct',word:state.current.word_unmasked,done:true,session:{practiced:1,correct:1,incorrect:[],drilled:0,elapsed_seconds:1,ended_early:false}}));}const next=q('q1',2,state.startType,'w01');state.current=next;return Promise.resolve(jr({result:'correct',word:state.lastBody.answer,done:false,question:next,progress:{correct:1,drilled:0,total:16,questions:1,max_questions:16}}));}
            if(url==='/api/practice/cancel'){if(state.drill)return Promise.resolve(jr({error:'Complete the mandatory drill before ending the session.'},409));return Promise.resolve(jr({cancelled:true,session:{practiced:0,correct:0,incorrect:[],drilled:0,elapsed_seconds:0,ended_early:true}}));}
            if(url==='/api/tts'){state.ttsCalls++;return new Promise(r=>setTimeout(()=>r(jr({supported:true,spoken:true,simulated:true})),state.ttsDelay));}
            return Promise.resolve(jr({error:'not found'},404));};return true;
        """)
        self.browser.script('eval(arguments[0]);return true;',app)
        self.wait("return document.querySelectorAll('#practice-user option').length>1")
        for eid,val in [('practice-user','alice'),('practice-lang','german_vocabulary'),('practice-level','a1'),('practice-pos','noun'),('practice-file','focus')]:self.select(eid,val)

    def wait(self,script,timeout=6):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if self.browser.script(script):return
            time.sleep(.03)
        self.fail('browser condition timed out')
    def select(self,eid,val):
        self.wait(f"return [...document.getElementById('{eid}').options].some(o=>o.value==={json.dumps(val)})")
        self.browser.script("const e=document.getElementById(arguments[0]);e.value=arguments[1];e.dispatchEvent(new Event('change',{bubbles:true}));return true;",eid,val)

    def test_ui_keeps_approved_cards_horizontal_leitner_and_answer_only_controls(self):
        self.wait("return document.querySelector('#practice-roadmap-container .roadmap-card')!==null")
        info=self.browser.script(r"""const setup=document.getElementById('practice-setup'),road=document.querySelector('#practice-roadmap-container .roadmap-card'),nodes=[...road.querySelectorAll('.leitner-roadmap-square')].map(x=>x.getBoundingClientRect());return {nested:setup.contains(road),count:nodes.length,spread:Math.max(...nodes.map(x=>x.top))-Math.min(...nodes.map(x=>x.top)),square:Math.max(...nodes.map(x=>Math.abs(x.width-x.height))),body:document.body.innerText.toLowerCase(),ids:['btn-replay','btn-end'].map(id=>!!document.getElementById(id))};""")
        self.assertFalse(info['nested']);self.assertEqual(info['count'],10);self.assertLessEqual(info['spread'],1);self.assertLessEqual(info['square'],1);self.assertEqual(info['ids'],[True,True])
        self.assertIsNone(self.browser.script("return document.getElementById('submit-answer')"))
        capture=self.browser.script("const i=document.getElementById('answer-input'),s=getComputedStyle(i),r=i.getBoundingClientRect();return {opacity:s.opacity,width:r.width,height:r.height};")
        self.assertEqual(capture['opacity'],'0'); self.assertLessEqual(capture['width'],1); self.assertLessEqual(capture['height'],1)
        for stale in ('due today','known review','mark mastered','flag for extra practice','manual drill'):self.assertNotIn(stale,info['body'])
        for stale_id in ('btn-flag','btn-master','btn-drill','btn-reveal','start-review','start-leitner'):self.assertIsNone(self.browser.script("return document.getElementById(arguments[0])",stale_id))

    def test_mastery_trends_render_in_roadmap_progress_and_report(self):
        self.wait("return document.querySelectorAll('#practice-roadmap-container .trend-chart').length===1")
        self.wait("return document.querySelectorAll('#practice-progress .trend-chart-compact').length===1")
        self.browser.script("document.querySelector('nav button[data-view=\"report\"]').click();return true;")
        for eid,val in [('report-user','alice'),('report-lang','german_vocabulary'),('report-level','a1'),('report-pos','noun'),('report-file','focus')]:self.select(eid,val)
        self.browser.script("document.getElementById('load-report').click();return true;")
        self.wait("return document.querySelectorAll('#report-results .dash-card-tracks .trend-chart').length===2")
        state=self.browser.script("return {charts:document.querySelectorAll('#report-results .trend-chart').length,raw:document.querySelector('#report-results .dash-card-tracks').innerText,errors:__errors.slice()};")
        self.assertGreaterEqual(state['charts'],3)
        self.assertNotIn('3 / 20',state['raw'])
        self.assertNotIn('1 / 20',state['raw'])
        self.assertEqual(state['errors'],[])

    def test_preloaded_shadows_drill_keeps_typed_text_visible(self):
        target = 'das Baby, die Babys'
        self.browser.script(r"""
          const baseFetch=window.fetch;
          window.fetch=(input,init={})=>{
            if(String(input)!=='/api/practice/start')return baseFetch(input,init);
            return baseFetch(input,init).then(async response=>{
              const data=await response.json();
              data.question.drill_start={correct_in_a_row:0,repetition:1,target:2,show_word:false};
              return new Response(JSON.stringify(data),{status:response.status,headers:{'Content-Type':'application/json'}});
            });
          };
          __api.startType='shadows';__api.startWord=arguments[0];__api.startPrompt=null;__api.ttsDelay=0;
          document.getElementById('start-session').click();return true;
        """, target)
        self.wait("return !document.getElementById('answer-input').disabled")
        initial=self.browser.script("return {text:document.getElementById('word-display').textContent,maxLength:document.getElementById('answer-input').maxLength,definition:document.getElementById('definition-lines').textContent};")
        self.assertEqual(initial['text'],'___ ____, ___ _____')
        self.assertEqual(initial['maxLength'],len(target))
        self.assertEqual(initial['definition'],'definition')

        self.browser.script("document.getElementById('answer-input').focus();return true;")
        self.browser.call('Input.insertText',{'text':'das B'})
        self.wait("return document.getElementById('answer-input').value==='das B'")
        typed=self.browser.script("return {visible:document.getElementById('word-display').textContent,typed:[...document.querySelectorAll('#word-display .answer-char.typed')].map(node=>node.textContent).join(''),errors:__errors.slice()};")
        self.assertTrue(typed['visible'].startswith('das B'))
        self.assertEqual(typed['typed'],'das B')
        self.assertEqual(typed['errors'],[])

    def test_inline_answer_surface_fills_mask_and_fully_masked_target(self):
        self.wait("return document.querySelector('#practice-roadmap-container .roadmap-card')!==null")
        glow=self.browser.script(r"""const scroll=document.querySelector('.leitner-roadmap-scroll'),node=scroll.querySelector('.leitner-roadmap-node'),sq=node.querySelector('.leitner-roadmap-square');node.classList.add('has-words');const a=scroll.getBoundingClientRect(),b=sq.getBoundingClientRect(),cs=getComputedStyle(sq);return {topGap:b.top-a.top,shadow:cs.boxShadow};""")
        self.assertGreaterEqual(glow['topGap'], 14)
        self.assertNotEqual(glow['shadow'], 'none')

        # A partially masked word is the answer surface itself: no visible input box
        # or Submit button. Typing replaces/fills the prompt from left to right.
        target='der Film, die Filme'; prompt='de_ Fi__, _ie F_lme'
        self.browser.script("__api.startType='learning';__api.startWord=arguments[0];__api.startPrompt=arguments[1];__api.ttsDelay=0;document.getElementById('start-session').click();return true;",target,prompt)
        self.wait("return !document.getElementById('answer-input').disabled")
        initial=self.browser.script(r"""const i=document.getElementById('answer-input'),cs=getComputedStyle(i),w=document.getElementById('word-display'),wc=getComputedStyle(w),chars=[...w.querySelectorAll('.answer-char')].map(x=>{const r=x.getBoundingClientRect();return {left:r.left,width:r.width}});return {text:w.textContent,input:{opacity:cs.opacity},submit:document.getElementById('submit-answer'),chars,background:wc.backgroundColor,shadow:wc.boxShadow};""")
        initial_text=initial['text'].replace('\n','')
        self.assertEqual(len(initial_text),len(target))
        for index,ch in enumerate(target):
            if ch.isspace() or not ch.isalnum():
                self.assertEqual(initial_text[index],ch, (index,ch,initial_text))
            elif prompt[index]=='_':
                self.assertEqual(initial_text[index],'_')
            else:
                self.assertEqual(initial_text[index],prompt[index])
        self.assertIsNone(initial['submit'])
        self.assertEqual(initial['input']['opacity'],'0')
        self.assertIn(initial['background'],('rgba(0, 0, 0, 0)','transparent'))
        self.assertEqual(initial['shadow'],'none')
        # Fully masked targets hide only letters/digits. Spaces and punctuation
        # remain literal so the sentence keeps its readable text structure.
        self.browser.script("document.getElementById('btn-end').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-summary')).display!=='none'")
        self.browser.script("document.getElementById('summary-restart').click();__api.startType='production';__api.startWord='a, b.';__api.startPrompt=null;return true;")
        self.browser.script("document.getElementById('start-session').click();return true;")
        self.wait("return !document.getElementById('answer-input').disabled")
        self.assertEqual(self.browser.script("return document.getElementById('word-display').textContent"),'_, _.')
        self.browser.script("document.getElementById('btn-end').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-summary')).display!=='none'")
        self.browser.script("document.getElementById('summary-restart').click();__api.startType='learning';__api.startWord=arguments[0];__api.startPrompt=arguments[1];return true;",target,prompt)
        self.browser.script("document.getElementById('start-session').click();return true;")
        self.wait("return !document.getElementById('answer-input').disabled")
        # The spelling surface uses one fixed cell per target character. Every
        # cell has the same advance, so glyph shape/glow can never alter spacing.
        widths=[c['width'] for c in initial['chars']]
        self.assertLessEqual(max(widths)-min(widths), .25)
        advances=[initial['chars'][i+1]['left']-initial['chars'][i]['left'] for i in range(len(initial['chars'])-1)]
        self.assertLessEqual(max(advances)-min(advances), .25)
        # Include both a space and punctuation in the typed prefix: those used to
        # change slot widths and visibly move the remaining letters while typing.
        typed_prefix='der Film, d'
        self.browser.script("const i=document.getElementById('answer-input');i.value=arguments[0];i.dispatchEvent(new Event('input',{bubbles:true}));return true;",typed_prefix)
        lit=self.browser.script(r"""const w=document.getElementById('word-display'),chars=[...w.querySelectorAll('.answer-char')].map(x=>{const r=x.getBoundingClientRect();return {left:r.left,width:r.width}});return {typed:[...w.querySelectorAll('.answer-char.typed')].map(x=>x.textContent).join(''),chars,promptOpacity:getComputedStyle(w.querySelector('.answer-char.prompt')).opacity,typedOpacity:getComputedStyle(w.querySelector('.answer-char.typed')).opacity,definitionClass:document.getElementById('definition-lines').className,definitionBorder:getComputedStyle(document.getElementById('definition-lines')).borderTopColor,primary:!!document.querySelector('#definition-lines .definition-primary')};""")
        self.assertEqual(lit['typed'],typed_prefix)
        self.assertEqual(self.browser.script("return document.getElementById('answer-input').value"),typed_prefix)
        self.assertLess(float(lit['promptOpacity']),float(lit['typedOpacity']))
        self.assertEqual(len(initial['chars']),len(lit['chars']))
        for before,after in zip(initial['chars'],lit['chars']):
            self.assertAlmostEqual(before['left'],after['left'],delta=.1)
            self.assertAlmostEqual(before['width'],after['width'],delta=.1)
        self.assertIn('has-content',lit['definitionClass'])
        self.assertTrue(lit['primary'])
        self.assertNotIn(lit['definitionBorder'],('rgba(0, 0, 0, 0)','transparent'))

        # Typing past the target must remain visible so the learner can correct
        # it before submitting, but the target grid itself must not move or grow.
        # Overflow is rendered on its own bounded correction row so it can never
        # escape the card, even after a large accidental key repeat.
        target_len=len(target)
        overflow_value=target+'e'*96
        before_overflow=self.browser.script("const s=document.querySelector('#word-display .answer-sequence').getBoundingClientRect(),first=document.querySelector('#word-display .answer-char').getBoundingClientRect();return {width:s.width,left:s.left,first:first.left,count:document.querySelectorAll('#word-display .answer-sequence > .answer-char').length};")
        self.browser.script("const i=document.getElementById('answer-input');i.value=arguments[0];i.dispatchEvent(new Event('input',{bubbles:true}));return true;",overflow_value)
        overflow=self.browser.script(r"""const w=document.getElementById('word-display').getBoundingClientRect(),s=document.querySelector('#word-display .answer-sequence').getBoundingClientRect(),first=document.querySelector('#word-display .answer-sequence > .answer-char').getBoundingClientRect(),tail=document.querySelector('#word-display .answer-extra-tail'),tr=tail.getBoundingClientRect();return {width:s.width,left:s.left,first:first.left,count:document.querySelectorAll('#word-display .answer-sequence > .answer-char').length,targetText:[...document.querySelectorAll('#word-display .answer-sequence > .answer-char')].map(x=>x.textContent).join(''),value:document.getElementById('answer-input').value,tailText:tail.textContent,tailLeft:tr.left,tailRight:tr.right,wordLeft:w.left,wordRight:w.right,tailHeight:tr.height,charHeight:tail.querySelector('.answer-char').getBoundingClientRect().height,docOverflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};""")
        self.assertEqual(overflow['count'],target_len)
        self.assertAlmostEqual(overflow['width'],before_overflow['width'],delta=.1)
        self.assertAlmostEqual(overflow['left'],before_overflow['left'],delta=.1)
        self.assertAlmostEqual(overflow['first'],before_overflow['first'],delta=.1)
        self.assertEqual(overflow['targetText'],target)
        self.assertEqual(overflow['value'],overflow_value)
        self.assertEqual(overflow['tailText'],'e'*96)
        self.assertGreaterEqual(overflow['tailLeft'],overflow['wordLeft']-.5)
        self.assertLessEqual(overflow['tailRight'],overflow['wordRight']+.5)
        self.assertGreater(overflow['tailHeight'],overflow['charHeight'])
        self.assertLessEqual(overflow['docOverflow'],1)
        # Backspace/removal must immediately remove the overflow tail again.
        self.browser.script("const i=document.getElementById('answer-input');i.value=arguments[0];i.dispatchEvent(new Event('input',{bubbles:true}));return true;",target)
        self.assertIsNone(self.browser.script("return document.querySelector('#word-display .answer-extra-tail')"))

        # Production/band-8 presentation keeps the same focal point and exposes
        # only fillable gaps, not an empty placeholder bar.
        self.browser.script("document.getElementById('btn-end').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-summary')).display!=='none'")
        self.browser.script("document.getElementById('summary-restart').click();__api.startType='production';__api.startWord='Film';__api.startPrompt=null;return true;")
        self.browser.script("document.getElementById('start-session').click();return true;")
        self.wait("return !document.getElementById('answer-input').disabled")
        masked=self.browser.script("return {display:getComputedStyle(document.getElementById('word-display')).display,text:document.getElementById('word-display').textContent,masked:document.querySelectorAll('#word-display .answer-char.masked').length,height:document.getElementById('word-display').getBoundingClientRect().height};")
        self.assertEqual(masked['display'],'flex')
        self.assertEqual(masked['text'],'____')
        self.assertEqual(masked['masked'],4)
        self.assertGreater(masked['height'],0)

    def test_long_sentence_wraps_inside_card_and_definition_dividers_span_width(self):
        # Sentence lists can contain long targets. The fixed-cell answer surface
        # must wrap inside the practice card rather than creating horizontal
        # overflow, and the definition separators should span the full content
        # width instead of stopping at an arbitrary narrow max-width.
        sentence='Ich möchte heute Abend mit meinen Freunden im kleinen Restaurant am Bahnhof gemeinsam Deutsch sprechen.'
        self.browser.script("__api.startType='learning';__api.startWord=arguments[0];__api.startPrompt=arguments[0];__api.ttsDelay=0;document.getElementById('start-session').click();return true;",sentence)
        self.wait("return !document.getElementById('answer-input').disabled")
        geom=self.browser.script(r"""const w=document.getElementById('word-display').getBoundingClientRect(),b=document.getElementById('word-block').getBoundingClientRect(),d=document.getElementById('definition-lines').getBoundingClientRect(),chars=[...document.querySelectorAll('#word-display .answer-sequence > .answer-char')].map(x=>x.getBoundingClientRect());return {word:{left:w.left,right:w.right},block:{left:b.left,right:b.right,width:b.width},def:{left:d.left,right:d.right,width:d.width},rows:new Set(chars.map(r=>Math.round(r.top))).size,minLeft:Math.min(...chars.map(r=>r.left)),maxRight:Math.max(...chars.map(r=>r.right)),docOverflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};""")
        self.assertGreater(geom['rows'],1)
        self.assertGreaterEqual(geom['minLeft'],geom['word']['left']-.5)
        self.assertLessEqual(geom['maxRight'],geom['word']['right']+.5)
        self.assertAlmostEqual(geom['def']['left'],geom['block']['left'],delta=1)
        self.assertAlmostEqual(geom['def']['right'],geom['block']['right'],delta=1)
        self.assertAlmostEqual(geom['def']['width'],geom['block']['width'],delta=1)
        self.assertLessEqual(geom['docOverflow'],1)

    def test_speech_allows_typing_but_blocks_submission_navigation_and_card_change(self):
        self.browser.script("__api.ttsDelay=1500;document.getElementById('start-session').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-session')).display!=='none'")
        self.wait("return !document.getElementById('answer-input').disabled && document.getElementById('btn-end').disabled && !document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='typed';i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));document.getElementById('btn-end').click();document.querySelector('nav button[data-view=\"report\"]').click();return true;")
        time.sleep(.1)
        state=self.browser.script("return {value:document.getElementById('answer-input').value,answers:__api.answers,active:document.getElementById('view-practice').classList.contains('active'),visible:document.getElementById('word-display').textContent,ready:document.getElementById('word-display').classList.contains('can-submit')};")
        self.assertEqual(state['value'],'typed');self.assertEqual(state['answers'],0);self.assertTrue(state['active']);self.assertEqual(state['visible'],'typed');self.assertFalse(state['ready'])
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')",timeout=3)
        self.browser.script("const i=document.getElementById('answer-input');i.value='w00';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return __api.answers===1")
        self.assertEqual(self.browser.script("return __api.lastBody.answer"),'w00')

    def test_definition_is_centered_and_report_has_pos_selector(self):
        self.browser.script("document.getElementById('start-session').click();return true;");self.wait("return getComputedStyle(document.getElementById('practice-session')).display!=='none'")
        geom=self.browser.script("const b=document.getElementById('word-block').getBoundingClientRect(),d=document.getElementById('definition-lines').getBoundingClientRect();return {delta:Math.abs((b.left+b.width/2)-(d.left+d.width/2)),align:getComputedStyle(document.getElementById('definition-lines')).textAlign};")
        self.assertLessEqual(geom['delta'],1);self.assertEqual(geom['align'],'center');self.assertIsNotNone(self.browser.script("return document.getElementById('report-pos')"))

    def test_stage_audio_policy_is_enforced(self):
        # Depths is manual-only: no prompt speech, Replay is available.
        self.browser.script("__api.startType='depths';__api.ttsCalls=0;document.getElementById('start-session').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-session')).display!=='none'")
        time.sleep(.08)
        self.assertEqual(self.browser.script("return __api.ttsCalls"),0)
        self.assertFalse(self.browser.script("return document.getElementById('btn-replay').disabled"))
        hint=self.browser.script("return document.querySelector('.session-control-hint').innerText")
        self.assertIn('Shift+Enter',hint.replace(' ',''))
        align=self.browser.script("return getComputedStyle(document.querySelector('.session-control-hint')).textAlign")
        self.assertEqual(align,'center')
        self.browser.script("document.getElementById('answer-input').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true,cancelable:true}));return true;")
        self.wait("return __api.ttsCalls===1")
        self.assertEqual(self.browser.script("return __api.answers"),0)
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')",timeout=3)
        # End the normal session after speech, then verify Void is silent with replay disabled.
        self.browser.script("document.getElementById('btn-end').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-summary')).display!=='none'")
        self.browser.script("document.getElementById('summary-restart').click();__api.startType='void';__api.ttsCalls=0;return true;")
        self.browser.script("document.getElementById('start-session').click();return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-session')).display!=='none'")
        time.sleep(.08)
        self.assertEqual(self.browser.script("return __api.ttsCalls"),0)
        self.assertTrue(self.browser.script("return document.getElementById('btn-replay').disabled"))

    def test_corrective_drill_end_button_and_escape_show_required_prompt(self):
        self.browser.script("__api.ttsDelay=0;__api.forceWrong=true;document.getElementById('start-session').click();return true;")
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='bad';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('drill-block')).display!=='none'")
        self.wait("return !document.getElementById('btn-end').disabled")
        self.browser.script("document.getElementById('btn-end').click();return true;")
        self.wait("return document.getElementById('feedback').textContent.includes('Complete the mandatory drill')")
        state=self.browser.script("return {summary:getComputedStyle(document.getElementById('practice-summary')).display,drill:getComputedStyle(document.getElementById('drill-block')).display,active:__api.drill,disabled:document.getElementById('answer-input').disabled};")
        self.assertEqual(state['summary'],'none'); self.assertNotEqual(state['drill'],'none')
        self.assertTrue(state['active']); self.assertFalse(state['disabled'])
        self.browser.script("document.getElementById('feedback').textContent='';return true;")
        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));return true;")
        self.wait("return document.getElementById('feedback').textContent.includes('Complete the mandatory drill')")
        self.assertEqual(self.browser.script("return getComputedStyle(document.getElementById('practice-summary')).display"),'none')

    def test_final_drill_success_keeps_complete_word_lit_before_advancing(self):
        self.browser.script("__api.ttsDelay=0;__api.forceWrong=true;document.getElementById('start-session').click();return true;")
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='bad';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('drill-block')).display!=='none'")
        self.browser.script("__api.drillComplete=true;const i=document.getElementById('answer-input');i.value='w00';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return document.getElementById('drill-streak').textContent==='9'")
        lit=self.browser.script("return {typed:[...document.querySelectorAll('#word-display .answer-sequence > .answer-char.typed')].map(x=>x.textContent).join(''),count:document.querySelectorAll('#word-display .answer-sequence > .answer-char.typed').length,opacity:[...document.querySelectorAll('#word-display .answer-sequence > .answer-char')].map(x=>getComputedStyle(x).opacity)}")
        self.assertEqual(lit['typed'],'w00')
        self.assertEqual(lit['count'],3)
        self.assertTrue(all(float(value) == 1 for value in lit['opacity']))

    def test_enter_summary_to_setup_to_next_session(self):
        self.browser.script("__api.ttsDelay=0;__api.finishOnAnswer=true;document.getElementById('start-session').click();return true;")
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='w00';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-summary')).display!=='none'")
        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-setup')).display!=='none'")
        self.wait("return __api.progressUrls.some(u=>u.includes('lang=focus'))")
        self.assertEqual(self.browser.script("return document.querySelectorAll('#practice-progress .progress-row').length"),1)
        self.wait("return getComputedStyle(document.getElementById('practice-overview')).display!=='none' && document.querySelector('#practice-roadmap-container .roadmap-card')!==null")
        restored=self.browser.script("return {rows:document.querySelectorAll('#practice-progress .progress-row').length,stage:document.getElementById('gauntlet-stage-label').textContent,roadmap:!!document.querySelector('#practice-roadmap-container .roadmap-card'),errors:__errors.slice()};")
        self.assertEqual(restored['rows'],1); self.assertEqual(restored['stage'],'Per-word Gauntlet'); self.assertTrue(restored['roadmap']); self.assertEqual(restored['errors'],[])
        const_before=self.browser.script("return __api.startCount")
        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait(f"return __api.startCount>{const_before}")

    def test_mobile_layout_has_no_document_overflow(self):
        self.browser.viewport(390,800); time.sleep(.1)
        overflow=self.browser.script("return document.documentElement.scrollWidth-document.documentElement.clientWidth")
        offenders=self.browser.script(r"""return [...document.querySelectorAll('body *')].map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {tag:e.tagName,id:e.id,cls:String(e.className||''),left:r.left,right:r.right,width:r.width,pos:s.position,overflow:s.overflowX}}).filter(x=>x.right>document.documentElement.clientWidth+1||x.left<-1).sort((a,b)=>b.right-a.right).slice(0,20);""")
        self.assertLessEqual(overflow,1,(overflow,offenders))


class StaticReleaseContractTest(unittest.TestCase):
    def test_one_test_file_and_no_abandoned_runtime_tokens(self):
        tests=[p.name for p in UTILS.glob('*test*.py')]
        self.assertEqual(tests,['test_tartarus.py'])
        combined='\n'.join((ROOT/p).read_text(encoding='utf-8') for p in ['utils/tartarus.py','utils/tartarus_web.py','web/app.js','web/index.html'])
        for token in ('last_known_review_at','known_drill_mode','review_mode','times_flagged','due_today','--fast','--known-drill-mode'):
            self.assertNotIn(token,combined)

    def test_timer_event_is_not_encoded_as_learning_answer_text(self):
        runtime='\n'.join((ROOT/p).read_text(encoding='utf-8') for p in ['utils/tartarus_web.py','web/app.js'])
        self.assertNotIn('!!TIMEOUT!!',runtime)
        self.assertIn('/api/practice/timeout',runtime)

    def test_web_answer_field_has_no_symbol_command_parser(self):
        source=(ROOT/'web/app.js').read_text(encoding='utf-8')
        for fragment in ("answer === '@'","answer === '$'","answer === '?'","answer === '!'","answer === '+'","answer === '!!'"):
            self.assertNotIn(fragment,source)
        html=(ROOT/'web/index.html').read_text(encoding='utf-8')
        for stale_id in ('btn-flag','btn-master','btn-drill','btn-reveal','submit-answer'):self.assertNotIn(f'id="{stale_id}"',html)
        self.assertIn('class="answer-capture"',html); self.assertIn('class="word-display answer-entry"',html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
