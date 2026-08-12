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

import backfill_mastery_events as mastery_backfill  # noqa: E402
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
        self.old_db, self.old_lists = ll.DATABASE_FILE, ll.WORD_LISTS_DIR
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR = str(self.db), str(self.lists)
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
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR = self.old_db, self.old_lists

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

    def set_progress(self, day, last_date=None, lang='focus'):
        conn = ll.get_connection(); ll.ensure_dataset_progress_table(conn)
        stage = ll.gauntlet_stage_for_day(day)[0]
        conn.execute(
            'INSERT OR REPLACE INTO dataset_progress(user,lang,current_stage,current_day,sessions_done_today,last_practice_date) VALUES(?,?,?,?,0,?)',
            ('alice', lang, stage, day, last_date),
        )
        conn.commit(); conn.close()

    def test_exact_answer_equality_has_no_softening(self):
        target = 'das Buch, die Bücher'
        self.assertTrue(ll.answer_matches(target, target))
        for answer in ('das Buch', 'die Bücher', 'die Bücher, das Buch', 'das Buch,die Bücher',
                       'das buch, die bücher', f' {target}', f'{target} '):
            self.assertFalse(ll.answer_matches(answer, target), answer)
        self.assertTrue(ll.answer_matches('Hallo, Welt!', 'Hallo, Welt!'))
        self.assertFalse(ll.answer_matches('Hallo, Welt! ', 'Hallo, Welt!'))

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

    def test_late_new_word_resets_roadmap_to_forging_without_losing_progress(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=5, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        self.set_progress(5, '2026-08-07')
        self.update('id-01', score=0.0, leitner_box=None, leitner_last_reviewed=None)
        state = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-08')
        self.assertEqual(state['current_day'], 0)
        self.assertEqual(self.row('id-00')['leitner_box'], 5)
        words = ll.get_words_for_gauntlet_stage('alice', 'focus', 0, today='2026-08-08')
        self.assertEqual([r[1] for r in words], ['w01'])

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

    def test_day_zero_completion_locks_until_next_calendar_day(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=1, leitner_last_reviewed='2026-08-08', last_tartarus_completed='2026-08-08')
        self.set_progress(0, '2026-08-08')
        same_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-08')
        self.assertEqual(same_day['current_day'], 0)
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice', 'focus', 0, '2026-08-08'), 0)
        next_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-09')
        self.assertEqual(next_day['current_day'], 1)
        self.assertEqual(next_day['last_practice_date'], '2026-08-09')
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice', 'focus', 1, '2026-08-09'), 2)

    def test_same_day_completed_later_stage_cannot_advance_to_next_day(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=2, leitner_last_reviewed='2026-08-01',
                        last_tartarus_completed='2026-08-08', last_practiced='2026-08-08')
        self.set_progress(1, '2026-08-08')
        same_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-08')
        self.assertEqual(same_day['current_day'], 1)
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice','focus',1,'2026-08-08'), 0)
        next_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-09')
        self.assertEqual(next_day['current_day'], 2)

    def test_repairs_old_same_day_forging_to_day_one_promotion_signature(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=1, leitner_last_reviewed='2026-08-08',
                        last_tartarus_completed=None, last_practiced='2026-08-08')
        self.set_progress(1, '2026-08-08')
        conn=ll.get_connection(); st=ll.ensure_sessions_table(conn,'alice')
        conn.execute(f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)', ('focus','2026-08-08',120,2,2,0,0))
        conn.commit(); conn.close()
        state=ll.reconcile_gauntlet_progress('alice','focus',today='2026-08-08')
        self.assertEqual(state['current_day'],0)
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice','focus',0,'2026-08-08'),0)

    def test_one_day_incomplete_stage_rolls_forward_without_losing_completed_tasks(self):
        self.make(material_items(3))
        for cid in ('id-00', 'id-01', 'id-02'):
            self.update(cid, score=9.0, leitner_box=2, leitner_last_reviewed='2026-08-01')
        self.update('id-00', last_tartarus_completed='2026-08-07', last_practiced='2026-08-07')
        self.update('id-01', last_tartarus_completed='2026-08-07', last_practiced='2026-08-07')
        self.update('id-02', last_tartarus_completed=None, last_practiced='2026-08-07')
        self.set_progress(3, '2026-08-07')
        conn = ll.get_connection(); st = ll.ensure_sessions_table(conn, 'alice')
        conn.execute('UPDATE dataset_progress SET sessions_done_today=2 WHERE user=? AND lang=?', ('alice','focus'))
        conn.execute(f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)', ('focus','2026-08-07',120,2,2,0,0))
        conn.execute(f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)', ('focus','2026-08-06',61,3,1,2,1))
        conn.commit(); before_sessions=conn.execute(f'SELECT * FROM "{st}"').fetchall(); conn.close()
        self.assertEqual(len(before_sessions),2)

        state = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-08')
        self.assertEqual((state['current_day'], state['sessions_done_today'], state['last_practice_date']), (3, 2, '2026-08-08'))
        self.assertEqual(self.row('id-00')['last_tartarus_completed'], '2026-08-08')
        self.assertEqual(self.row('id-01')['last_tartarus_completed'], '2026-08-08')
        self.assertIsNone(self.row('id-02')['last_tartarus_completed'])
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice','focus',3,'2026-08-08'), 1)
        conn=ll.get_connection(); preserved=conn.execute(f'SELECT * FROM "{st}"').fetchall(); conn.close()
        self.assertEqual(len(preserved),2)
        self.assertEqual(preserved, before_sessions)

    def test_split_forging_midnight_is_repaired_and_does_not_make_box_one_ready(self):
        self.make(material_items(64))
        for i in range(32):
            day = '2026-08-07' if i < 7 else '2026-08-08'
            self.update(f'id-{i:02d}', score=9.0, leitner_box=1, leitner_last_reviewed=day,
                        last_tartarus_completed=day, last_practiced=day)
        self.set_progress(0, '2026-08-08')
        conn=ll.get_connection(); st=ll.ensure_sessions_table(conn,'alice')
        conn.execute('UPDATE dataset_progress SET sessions_done_today=19 WHERE user=? AND lang=?',('alice','focus'))
        conn.execute(f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)', ('focus','2026-08-07',120,16,16,0,0))
        conn.execute(f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)', ('focus','2026-08-06',75,9,7,2,1))
        conn.commit(); before_sessions=conn.execute(f'SELECT * FROM "{st}"').fetchall(); conn.close()
        self.assertEqual(len(before_sessions),2)

        state=ll.reconcile_gauntlet_progress('alice','focus',today='2026-08-08')
        self.assertEqual((state['current_day'],state['sessions_done_today']), (0,19))
        for i in range(7):
            row=self.row(f'id-{i:02d}')
            self.assertEqual((row['last_tartarus_completed'],row['leitner_last_reviewed'],row['last_practiced']), ('2026-08-08','2026-08-08','2026-08-08'))
        self.assertEqual(len(ll.maintenance_ready_words('alice','focus',today='2026-08-08')),0)
        conn=ll.get_connection(); preserved=conn.execute(f'SELECT * FROM "{st}"').fetchall(); conn.close()
        self.assertEqual(len(preserved),2)
        self.assertEqual(preserved, before_sessions)

    def test_midnight_reconciliation_never_rewrites_session_history(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid,score=9.0,leitner_box=3,leitner_last_reviewed='2026-08-01',last_practiced='2026-08-07')
        self.update('id-00',last_tartarus_completed='2026-08-07')
        self.set_progress(3,'2026-08-07')
        conn=ll.get_connection(); st=ll.ensure_sessions_table(conn,'alice')
        rows=[('focus','2026-08-06',41,4,3,1,1),('focus','2026-08-07',123,8,5,3,2)]
        conn.executemany(
            f'INSERT INTO "{st}" (language,session_date,duration_seconds,words_practiced,correct_count,incorrect_count,drilled_count) VALUES (?,?,?,?,?,?,?)',
            rows,
        )
        conn.commit(); before=conn.execute(f'SELECT * FROM "{st}" ORDER BY id').fetchall(); conn.close()
        ll.reconcile_gauntlet_progress('alice','focus',today='2026-08-08')
        conn=ll.get_connection(); after=conn.execute(f'SELECT * FROM "{st}" ORDER BY id').fetchall(); conn.close()
        self.assertEqual(len(after),2)
        self.assertEqual(after,before)

    def test_completed_previous_stage_advances_instead_of_being_rolled_forward(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=2, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07', last_practiced='2026-08-07')
        self.set_progress(3,'2026-08-07')
        state=ll.reconcile_gauntlet_progress('alice','focus',today='2026-08-08')
        self.assertEqual(state['current_day'],4)
        self.assertEqual(self.row('id-00')['last_tartarus_completed'],'2026-08-07')

    def test_day_ten_completion_is_locked_until_next_calendar_day(self):
        self.make(material_items(2))
        for cid in ('id-00','id-01'):
            self.update(cid, score=9.0, leitner_box=2, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-08')
        self.set_progress(10, '2026-08-08')
        same_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-08')
        self.assertEqual(same_day['current_day'], 10)
        next_day = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-09')
        self.assertEqual(next_day['current_day'], ll.GAUNTLET_COMPLETE_DAY)
        self.assertEqual(ll.get_gauntlet_tasks_remaining('alice', 'focus', 11, '2026-08-09'), 0)

    def test_intense_same_day_practice_cannot_compress_the_ten_day_plan(self):
        """No amount of practice in one calendar day should ever advance the
        Gauntlet by more than one day -- practicing intensely can finish
        Forging in a single sitting, but it can never buy access to a later
        Gauntlet day early. Drives the real scoring/reconcile functions
        directly (not raw SQL) so this is evidence the live code path holds,
        not just a hand-set database row."""
        self.make(material_items(5))
        today = '2026-08-11'

        def master_everything():
            rounds = 0
            while True:
                try:
                    words = ll.get_words_for_gauntlet_stage('alice', 'focus', 0, today=today)
                except ValueError:
                    return rounds
                if not words:
                    return rounds
                for row_id, *_ in words:
                    ll.record_tartarus_answer('alice', 'focus', row_id, True, today=today)
                rounds += 1
                self.assertLess(rounds, 200, 'runaway loop mastering the list')

        def reinforce_everything():
            while True:
                progress = ll.get_dataset_progress('alice', 'focus')
                if progress['current_stage'] == 0:
                    return
                try:
                    words = ll.get_words_for_gauntlet_stage('alice', 'focus', progress['current_stage'], today=today)
                except ValueError:
                    return
                if not words:
                    return
                for row_id, *_ in words:
                    ll.record_tartarus_answer('alice', 'focus', row_id, True, today=today)

        start = ll.reconcile_gauntlet_progress('alice', 'focus', today=today)
        self.assertEqual(start['current_day'], 0)
        master_everything()

        # Hammer every mutating entry point 20 times over, all pinned to the
        # SAME calendar day, exactly like a learner refreshing the page
        # repeatedly "later today" trying to force the next day open.
        for _ in range(20):
            ll.reconcile_gauntlet_progress('alice', 'focus', today=today)
            reinforce_everything()
            ll.advance_gauntlet_session('alice', 'focus', today=today)
        final = ll.reconcile_gauntlet_progress('alice', 'focus', today=today)
        self.assertEqual(final['current_day'], 0, 'the whole list was mastered today, but day must stay 0 today')

        # Only a genuinely later calendar date may open day 1.
        tomorrow = ll.reconcile_gauntlet_progress('alice', 'focus', today='2026-08-12')
        self.assertEqual(tomorrow['current_day'], 1)

    def test_due_leitner_review_has_priority_over_tartarus(self):
        """Due review is "the practice from previous days" a learner must
        clear first; starting a session is the only decision they make, and
        the engine picks due review over new/continuing Forging material
        whenever both are available. See select_practice_words()."""
        self.make(material_items(2))
        self.update('id-00', score=9.0, leitner_box=1, leitner_last_reviewed='2000-01-01')
        self.update('id-01', score=8.0)
        sid, session, meta = web.gauntlet_start_session('alice', 'focus')
        self.addCleanup(lambda: web.SESSIONS.pop(sid, None))
        self.assertEqual(session['learning_context'], 'maintenance')
        self.assertEqual(meta['mode'], 'maintenance')
        self.assertEqual([q['word_text'] for q in session['queue']], ['w00'])
        self.assertTrue(meta['is_maintenance'])

    def test_select_practice_words_clears_due_review_before_resuming_forging(self):
        """The exact scenario a learner hits starting a session on a new
        calendar day: due Leitner review (from mastering words on a previous
        day) must be served first; once it's cleared, the same call resumes
        Forging on whatever's left, in-progress words before fresh ones."""
        self.make(material_items(3))
        self.update('id-00', score=9.0, leitner_box=1, leitner_last_reviewed='2000-01-01')  # due
        self.update('id-01', score=0.5)  # in-progress Forging
        self.update('id-02', score=0.0)  # untouched Forging

        words, context, mode, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context, mode), ('maintenance', 'maintenance'))
        self.assertEqual([w[1] for w in words], ['w00'])

        ll.record_maintenance_answer('alice', 'focus', self.row('id-00')['id'], True, today='2026-08-11')

        words2, context2, mode2, *_ = ll.select_practice_words('alice', 'focus', today='2026-08-11')
        self.assertEqual((context2, mode2), ('tartarus', 'forging'))
        self.assertEqual([w[1] for w in words2], ['w01', 'w02'])

    def test_when_tartarus_daily_work_is_done_same_entry_serves_maintenance(self):
        today = date.today().isoformat()
        self.make(material_items(1))
        self.update(score=9.0, leitner_box=1, leitner_last_reviewed='2000-01-01', last_tartarus_completed=today)
        self.set_progress(1, today)
        sid, session, meta = web.gauntlet_start_session('alice', 'focus')
        self.addCleanup(lambda: web.SESSIONS.pop(sid, None))
        self.assertEqual(session['learning_context'], 'maintenance')
        self.assertTrue(meta['is_maintenance'])

    def test_shadows_drill_completion_marks_tartarus_task_without_moving_leitner(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=4, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        word_id = self.row()['id']
        ll.complete_tartarus_drill('alice','focus',word_id,today='2026-08-08')
        row=self.row()
        self.assertEqual((row['last_tartarus_completed'],row['leitner_box'],row['leitner_last_reviewed']),('2026-08-08',4,'2026-08-01'))

    def test_interrupted_wrong_does_not_complete_tartarus_task(self):
        self.make(material_items(1)); self.update(score=9.0, leitner_box=4, leitner_last_reviewed='2026-08-01', last_tartarus_completed='2026-08-07')
        ll.record_tartarus_answer('alice','focus',self.row()['id'],False,today='2026-08-08')
        self.assertEqual(self.row()['last_tartarus_completed'],'2026-08-07')

    def test_ten_daily_gauntlet_days_finish_despite_corrected_mistakes(self):
        self.make(material_items(1))
        started=date(2026,8,1); started_text=started.isoformat()
        self.update(score=9.0,leitner_box=1,leitner_last_reviewed=started_text,last_tartarus_completed=started_text)
        self.set_progress(0,started_text); word_id=self.row()['id']
        previous=started_text
        for day in range(1,11):
            today=(started+timedelta(days=day)).isoformat()
            state=ll.reconcile_gauntlet_progress('alice','focus',today=today)
            self.assertEqual(state['current_day'],day)
            ll.record_tartarus_answer('alice','focus',word_id,False,today=today)
            row=self.row()
            self.assertEqual((row['score'],row['leitner_box'],row['last_tartarus_completed']),(9.0,1,previous))
            ll.complete_tartarus_drill('alice','focus',word_id,today=today)
            row=self.row()
            self.assertEqual((row['score'],row['leitner_box'],row['last_tartarus_completed']),(9.0,1,today))
            previous=today
        terminal=ll.reconcile_gauntlet_progress('alice','focus',today=(started+timedelta(days=11)).isoformat())
        self.assertEqual(terminal['current_day'],ll.GAUNTLET_COMPLETE_DAY)

    def test_maintenance_readiness_has_one_definition(self):
        self.make(material_items(3))
        self.update('id-00',score=9.0,leitner_box=1,leitner_last_reviewed='2026-08-07')
        self.update('id-01',score=9.0,leitner_box=3,leitner_last_reviewed='2026-08-06')
        self.update('id-02',score=9.0,leitner_box=10,leitner_last_reviewed=None)
        ready=ll.maintenance_ready_words('alice','focus',today='2026-08-08')
        self.assertEqual([r[1] for r in ready],['w00','w02'])
        self.assertEqual(ll.maintenance_next_date(3,'2026-08-06'),'2026-08-09')

    def test_progress_payload_has_factual_track_metrics_only(self):
        self.make(material_items(2)); self.update('id-00',score=9.0,leitner_box=10); self.update('id-01',score=9.0,leitner_box=2)
        self.set_progress(11,'2026-08-08')
        item=next(x for x in web.user_progress_data('alice') if x['lang']=='focus')
        self.assertEqual((item['tartarus_score9'],item['leitner_box10'],item['tartarus_track_complete'],item['learning_complete']), (2,1,True,False))
        self.assertNotIn('due_today',item); self.assertNotIn('learned',item); self.assertNotIn('progress',item)

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
        self.update('id-00', score=9.0, leitner_box=2, leitner_last_reviewed='2026-08-01',
                    last_practiced='2026-08-01', last_tartarus_completed='2026-08-01',
                    times_practiced=5, times_correct=5, times_mastered=1)
        self.update('id-01', score=3.0, times_practiced=2, times_incorrect=2, last_practiced='2026-08-01')
        self.set_progress(3, last_date='2026-08-01')
        ll.log_session('alice', 'focus', 60, 3, 2, 1, 0)
        conn = ll.get_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions_alice').fetchone()[0], 1)
        conn.close()

        ll.reset_word_list_progress('alice', 'focus')

        row0 = self.row('id-00')
        self.assertEqual(row0['score'], 0.0)
        self.assertIsNone(row0['last_practiced'])
        self.assertIsNone(row0['last_tartarus_completed'])
        self.assertEqual((row0['times_practiced'], row0['times_correct'], row0['times_mastered']), (0, 0, 0))
        self.assertIsNone(row0['leitner_box'])
        self.assertIsNone(row0['leitner_last_reviewed'])
        self.assertEqual(row0['content_id'], 'id-00')
        self.assertEqual(row0['active'], 1)

        row1 = self.row('id-01')
        self.assertEqual((row1['score'], row1['times_incorrect']), (0.0, 0))

        conn = ll.get_connection()
        progress = conn.execute("SELECT * FROM dataset_progress WHERE user='alice' AND lang='focus'").fetchall()
        sessions_after = conn.execute('SELECT COUNT(*) FROM sessions_alice').fetchone()[0]
        conn.close()
        self.assertEqual(progress, [])
        self.assertEqual(sessions_after, 1)

        reconciled = ll.reconcile_gauntlet_progress('alice', 'focus')
        self.assertEqual((reconciled['current_stage'], reconciled['current_day']), (0, 0))

    def test_reset_word_list_progress_rejects_unknown_list(self):
        with self.assertRaises(ValueError):
            ll.reset_word_list_progress('alice', 'doesnotexist')

    def test_personal_list_is_not_exposed_to_another_user(self):
        self.make(material_items(1),user='alice',lang='secret')
        descriptors=web.list_word_lists()
        self.assertTrue(any(x['user']=='alice' and x['lang']=='secret' for x in descriptors))
        self.assertFalse(any(x['user']=='bob' and x['lang']=='secret' for x in descriptors))

    def test_cli_uses_exact_answers_and_same_score_engine(self):
        self.make([{'id':'buch','word':'das Buch, die Bücher','definition':'book','word_frequency':0}])
        with mock.patch.object(ll, 'clear_screen'), mock.patch.object(ll, 'speak'), mock.patch('builtins.input', side_effect=['das Buch, die Bücher']):
            ll.start_practice_session('alice','focus',audio=False)
        self.assertEqual(self.row('buch')['score'],0.5)

    def test_cli_reconciles_after_each_answer_on_final_gauntlet_day(self):
        self.make(material_items(1)); today=date.today().isoformat()
        yesterday=(date.today()-timedelta(days=1)).isoformat()
        self.update(score=9.0,leitner_box=10,leitner_last_reviewed=today,last_tartarus_completed=yesterday)
        self.set_progress(ll.GAUNTLET_MAX_DAY,today)
        calls=[]; original=ll.reconcile_gauntlet_progress
        def tracked(*args,**kwargs):
            calls.append(self.row()['last_tartarus_completed'])
            return original(*args,**kwargs)
        with mock.patch.object(ll,'clear_screen'),mock.patch.object(ll,'speak'),\
             mock.patch.object(ll,'reconcile_gauntlet_progress',side_effect=tracked),\
             mock.patch('builtins.input',side_effect=['w00']):
            ll.start_practice_session('alice','focus',audio=False)
        self.assertGreaterEqual(len(calls),3)
        self.assertEqual(calls[0],yesterday)
        self.assertIn(today,calls[1:])
        self.assertEqual(ll.get_dataset_progress('alice','focus')['current_day'],ll.GAUNTLET_MAX_DAY)

    def test_cli_depths_drill_audio_is_manual_only(self):
        calls=[]
        with mock.patch.object(ll, 'clear_screen'), mock.patch.object(ll, 'show_definition'), mock.patch.object(ll, 'speak', side_effect=lambda *a,**k:calls.append(a[0]) or True), mock.patch('builtins.input', side_effect=['/replay','target']):
            ll.drill_word('alice','focus','target',1,'definition','header',True,update_score=False,target=1,auto_audio=False)
        self.assertEqual(calls,['target'])

    def test_cli_shadows_mistake_escalates_to_nine_correct_answers(self):
        self.make(material_items(1)); today=date.today().isoformat()
        self.update(score=9.0,leitner_box=1,leitner_last_reviewed=today); word_id=self.row()['id']
        answers=mock.Mock(side_effect=['wrong']+['w00']*9)
        with mock.patch.object(ll,'clear_screen'),mock.patch.object(ll,'show_definition'),mock.patch.object(ll,'speak'),mock.patch('builtins.input',answers):
            attempt=ll.drill_word('alice','focus','w00',word_id,'definition','header',False,target=2,escalate_on_wrong=True)
        self.assertEqual((answers.call_count,attempt),(10,'wrong'))
        row=self.row()
        self.assertEqual((row['score'],row['leitner_box'],row['last_tartarus_completed'],row['times_incorrect'],row['times_drilled']),(9.0,1,today,1,1))

    def test_cli_surface_contains_only_guided_practice_and_transport_controls(self):
        parser=ll.build_parser(); help_text=parser.format_help()
        self.assertIn('practice',help_text)
        practice_parser = next(action for action in parser._actions if action.dest == 'command').choices['practice']
        option_strings = {option for action in practice_parser._actions for option in action.option_strings}
        self.assertNotIn('--fast', option_strings)
        self.assertNotIn('--drill', option_strings)
        source=Path(ll.__file__).read_text(encoding='utf-8')
        for option in ('--known-drill-mode','--instant-drill','--drill-mode'):
            self.assertNotIn(option,source)
        self.assertIn('/replay',source); self.assertIn('/quit',source)


    def test_progress_can_be_filtered_to_the_exact_selected_list(self):
        self.make(material_items(2), lang='focus')
        self.make(material_items(3), lang='other')
        rows = web.user_progress_data('alice', 'german_vocabulary', 'a1', 'focus')
        self.assertEqual([row['lang'] for row in rows], ['focus'])
        self.assertEqual(rows[0]['total'], 2)

    def test_report_gauge_uses_red_yellow_green_visual_bands(self):
        self.make(material_items(3))
        self.update('id-00', score=1.0)
        self.update('id-01', score=5.0)
        self.update('id-02', score=9.0, leitner_box=1, leitner_last_reviewed='2026-08-08')
        stats = web.word_list_stats('alice', 'focus')
        by_word = {row['word']: row for row in stats}
        self.assertEqual(by_word['w00']['gauge_band'], ll.score_band(1.0))
        self.assertEqual(by_word['w01']['gauge_band'], ll.score_band(5.0))
        self.assertEqual(by_word['w02']['gauge_band'], ll.score_band(9.0))


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

    def test_v3_to_v4_is_atomic_preserves_history_and_creates_verified_backup(self):
        path=self.make_v3(); backup=ll.migrate_database_v4(str(path),create_backup=True)
        self.assertTrue(Path(backup).exists())
        conn=sqlite3.connect(path); table='words_alice_focus0'
        self.assertEqual([r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')],ll.WORD_TABLE_COLUMNS)
        row=conn.execute(f'SELECT content_id,score,last_practiced,last_tartarus_completed,times_practiced,times_correct,times_incorrect,times_drilled,times_mastered,leitner_box,leitner_last_reviewed FROM "{table}"').fetchone()
        self.assertEqual(row,('id-0',9.0,'2026-08-07','2026-08-07',7,5,2,1,3,4,'2026-08-07'))
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],4)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok'); conn.close()
        check=sqlite3.connect(f'file:{backup}?mode=ro',uri=True); self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok'); check.close()
        self.assertIsNone(ll.migrate_database_v4(str(path),create_backup=False))

    def test_application_startup_removes_verified_migration_snapshots_after_success(self):
        path = self.make_v3(name='runtime.db')
        lists = self.root / 'lists'; lists.mkdir()
        old_db, old_lists = ll.DATABASE_FILE, ll.WORD_LISTS_DIR
        ll.DATABASE_FILE, ll.WORD_LISTS_DIR = str(path), str(lists)
        try:
            before = logical_db_dump(path)
            ll.initialize_database(create_backup=True)
            self.assertFalse(list(self.root.glob('runtime.db.pre-v*.sqlite')))
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)
            row = conn.execute('SELECT content_id,score,times_practiced,times_correct,times_incorrect,times_drilled,times_mastered,leitner_box FROM words_alice_focus0').fetchone()
            conn.close()
            old_row = before['words_alice_focus0']['rows'][0]
            self.assertEqual(row, (old_row[1], old_row[2], old_row[5], old_row[6], old_row[7], old_row[8], old_row[9], old_row[10]))
            stale = Path(f'{path}.pre-v4.19990101000000000000.sqlite')
            shutil.copy2(path, stale)
            self.assertTrue(stale.exists())
            ll.initialize_database(create_backup=True)
            self.assertFalse(stale.exists())
        finally:
            ll.DATABASE_FILE, ll.WORD_LISTS_DIR = old_db, old_lists

    def test_injected_failure_rolls_back_every_table(self):
        path=self.make_v3(tables=2); before=logical_db_dump(path)
        with self.assertRaisesRegex(RuntimeError,'Injected migration failure'):
            ll.migrate_database_v4(str(path),create_backup=False,fail_after_tables=1)
        self.assertEqual(logical_db_dump(path),before)

    def test_v1_backup_imports_into_v4_and_v3_round_trips(self):
        tmp=self.root/'runtime.db'; lists=self.root/'lists'; lists.mkdir()
        old_db,old_lists=ll.DATABASE_FILE,ll.WORD_LISTS_DIR; ll.DATABASE_FILE,ll.WORD_LISTS_DIR=str(tmp),str(lists)
        try:
            ll.initialize_database(create_backup=False)
            v1={'format':ll.BACKUP_FORMAT,'version':1,'user':{'name':'alice','created_at':'2026-08-01'},'word_progress':{'focus':[{
                'id':1,'content_id':'x','score':9.0,'last_practiced':'2026-08-01','active':1,'times_practiced':2,'times_correct':2,'times_incorrect':0,'times_drilled':0,'times_mastered':0,'leitner_box':3,'last_known_review_at':'2026-08-02'
            }]},'sessions':[],'gauntlet_progress':[]}
            ll.import_user_data('alice',v1)
            conn=ll.get_connection()
            ll.record_mastery_event(conn,'alice','focus',1,'mastered','2026-08-01')
            conn.commit(); conn.close()
            exported=ll.export_user_data('alice'); self.assertEqual(exported['version'],3)
            self.assertEqual(exported['mastery_events'],[{'lang':'focus','word_id':1,'event_type':'mastered','mastered_date':'2026-08-01'}])
            row=exported['word_progress']['focus'][0]
            self.assertEqual(row['last_tartarus_completed'],'2026-08-01'); self.assertEqual(row['leitner_last_reviewed'],'2026-08-01'); self.assertNotIn('last_known_review_at',row)
            copydb=self.root/'copy.db'; ll.DATABASE_FILE=str(copydb); ll.import_user_data('alice',exported)
            self.assertEqual(ll.export_user_data('alice'),exported)
        finally:
            ll.DATABASE_FILE,ll.WORD_LISTS_DIR=old_db,old_lists

    def test_mastery_backfill_is_conservative_backed_up_and_idempotent(self):
        db=self.root/'backfill.db'; lists=self.root/'lists'; lists.mkdir()
        old_db,old_lists=ll.DATABASE_FILE,ll.WORD_LISTS_DIR
        ll.DATABASE_FILE,ll.WORD_LISTS_DIR=str(db),str(lists)
        try:
            ll.initialize_database(create_backup=False)
            conn=ll.get_connection(); ll.ensure_user(conn,'alice'); conn.commit(); conn.close()
            write_material(lists/'alice_focus.json',material_items(2))
            ll.sync_word_list('alice','focus')
            conn=ll.get_connection(); table=ll.words_table_name('alice','focus')
            conn.execute(f'UPDATE "{table}" SET score=9,last_tartarus_completed=? WHERE content_id=?',('2026-08-08','id-00'))
            conn.execute(f'UPDATE "{table}" SET score=9,last_tartarus_completed=NULL WHERE content_id=?',('id-01',))
            conn.execute(
                'INSERT INTO dataset_progress(user,lang,current_stage,current_day,sessions_done_today,last_practice_date) '
                'VALUES(?,?,0,0,0,?)',
                ('alice','focus','2026-08-08'),
            )
            conn.commit(); conn.close()

            dry=mastery_backfill.backfill(str(db))
            self.assertEqual((dry['pending'],dry['skipped_missing_date'],dry['backup']),(1,1,None))
            applied=mastery_backfill.backfill(str(db),apply=True)
            self.assertEqual(applied['inserted'],1)
            self.assertTrue(Path(applied['backup']).exists())
            check=sqlite3.connect(f"file:{applied['backup']}?mode=ro",uri=True)
            self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok'); check.close()
            conn=sqlite3.connect(db)
            self.assertEqual(conn.execute(
                'SELECT user,lang,word_id,event_type,mastered_date FROM mastery_events'
            ).fetchall(),[('alice','focus',1,'mastered','2026-08-08')])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok'); conn.close()
            again=mastery_backfill.backfill(str(db),apply=True)
            self.assertEqual((again['pending'],again['inserted']),(0,0))
        finally:
            ll.DATABASE_FILE,ll.WORD_LISTS_DIR=old_db,old_lists


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
        self.root=Path(self.tmp.name); self.db=self.root/'progress.db'; self.lists=self.root/'word_lists'; self.lists.mkdir(); self.log=self.root/'app.log'; self.port=free_port(); self.base=f'http://127.0.0.1:{self.port}'
        env=os.environ.copy(); env.update({'TARTARUS_DB':str(self.db),'TARTARUS_WORD_LISTS_DIR':str(self.lists),'TARTARUS_LOG_FILE':str(self.log),'TARTARUS_PORT':str(self.port),'TARTARUS_HOST':'127.0.0.1','TARTARUS_SESSION_TTL_SECONDS':'2','TARTARUS_TTS_TEST_DELAY_MS':str(self.TTS_DELAY_MS),'PYTHONDONTWRITEBYTECODE':'1'})
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

    def start(self,user='alice',lang='focus',**extra):
        return self.api('/api/practice/start',{'user':user,'lang':lang,**extra})

    def answer(self,started,answer,attempt='a1',question=None,expected=200,**extra):
        q=question or started['question']; return self.api('/api/practice/answer',{'session_id':started['session_id'],'question_id':q['question_id'],'sequence':q['sequence'],'attempt_id':attempt,'answer':answer,**extra},expected=expected)


class HttpContractTest(ServerHarness):
    TTS_DELAY_MS=80
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
        self.create(items=material_items(1)); today=date.today().isoformat()
        conn=sqlite3.connect(self.db); table=ll.words_table_name('alice','focus')
        conn.execute(f'UPDATE "{table}" SET score=9.0,leitner_box=1,leitner_last_reviewed=?,last_tartarus_completed=NULL',(today,))
        conn.execute('INSERT OR REPLACE INTO dataset_progress(user,lang,current_stage,current_day,sessions_done_today,last_practice_date) VALUES (?,?,?,?,0,?)',('alice','focus',2,3,today))
        conn.commit(); conn.close()
        started=self.start(); question=started['question']
        self.assertEqual(question['drill_start']['target'],2)
        result=self.answer(started,'wrong','wrong',question=question)
        self.assertEqual((result['result'],result['drill']['target'],result['drill']['correct_in_a_row']),('drill_progress',9,0))
        for index in range(9):
            result=self.answer(started,question['word_unmasked'],f'drill-{index}',question=question)
        self.assertEqual(result['result'],'drilled')
        conn=sqlite3.connect(self.db); row=conn.execute(f'SELECT score,leitner_box,last_tartarus_completed,times_incorrect,times_drilled FROM "{table}"').fetchone(); conn.close()
        self.assertEqual(row,(9.0,1,today,1,1))
        self.assertEqual(len(result['session']['incorrect']),1)

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

    def test_normal_cancel_returns_summary_but_drill_cancel_is_forbidden(self):
        self.create(items=material_items(2)); started=self.start(); q=started['question']
        correct=self.answer(started,q['word_unmasked'],'ok',question=q); summary=self.api('/api/practice/cancel',{'session_id':started['session_id']})['session']
        self.assertTrue(summary['ended_early']); self.assertEqual(summary['practiced'],1)
        started=self.start(); q=started['question']; self.answer(started,'wrong','bad',question=q)
        self.api('/api/practice/cancel',{'session_id':started['session_id']},expected=409)

    def test_wordlist_restart_endpoint_resets_progress(self):
        self.create(items=material_items(2))
        conn=sqlite3.connect(self.db); table=ll.words_table_name('alice','focus'); today=date.today().isoformat()
        conn.execute(f'UPDATE "{table}" SET score=9.0,times_practiced=3,leitner_box=2,leitner_last_reviewed=?,last_tartarus_completed=? WHERE content_id=?',(today,today,'id-00'))
        conn.execute('INSERT OR REPLACE INTO dataset_progress(user,lang,current_stage,current_day,sessions_done_today,last_practice_date) VALUES(?,?,?,?,0,?)',('alice','focus',1,3,today))
        conn.commit(); conn.close()

        self.api('/api/wordlist/restart',{'user':'alice','lang':'focus'})

        conn=sqlite3.connect(self.db)
        row=conn.execute(f'SELECT score,times_practiced,leitner_box FROM "{table}" WHERE content_id=?',('id-00',)).fetchone()
        progress=conn.execute("SELECT * FROM dataset_progress WHERE user='alice' AND lang='focus'").fetchall()
        conn.close()
        self.assertEqual(row,(0.0,0,None))
        self.assertEqual(progress,[])

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
        paths=['/api/wordlists','/api/report?user=alice&lang=focus','/api/report/summary?user=alice','/api/user/progress?user=alice','/api/wordlist?user=alice&lang=focus','/api/wordlist/stats?user=alice&lang=focus','/api/dashboard?user=alice&lang=focus','/api/export?user=alice','/api/wordlist/leitner?user=alice&lang=focus','/api/gauntlet/progress?user=alice&lang=focus']
        for path in paths:self.raw(path)
        self.assertEqual(logical_db_dump(self.db),before)

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
            if(url.startsWith('/api/gauntlet/progress'))return Promise.resolve(jr({progress:{current_stage:0,current_day:0,sessions_done_today:0,stage_name:'The Forging',session_mode:'forging',remaining_tasks:20,total_tasks:20,max_day:10,complete:false},roadmap:{gauntlet:{current_stage:0,current_day:0,sessions_done_today:0,stage_name:'The Forging',remaining_tasks:20,total_tasks:20,complete:false},leitner_distribution:{'1':0,'2':0,'3':0,'4':0,'5':0,'6':0,'7':0,'8':0,'9':0,'10':0},maintenance_ready:0}}));
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
        self.browser.script("document.getElementById('start-session').click();return true;")
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

    def test_mandatory_drill_disables_end_and_escape(self):
        self.browser.script("__api.ttsDelay=0;__api.forceWrong=true;document.getElementById('start-session').click();return true;")
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='wrong';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('drill-block')).display!=='none'")
        self.assertTrue(self.browser.script("return document.getElementById('btn-end').disabled"))
        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));return true;")
        time.sleep(.08)
        self.assertTrue(self.browser.script("return document.getElementById('view-practice').classList.contains('active')"))
        self.assertTrue(self.browser.script("return __api.drill"))

    def test_final_drill_success_keeps_complete_word_lit_before_advancing(self):
        self.browser.script("__api.ttsDelay=0;__api.forceWrong=true;document.getElementById('start-session').click();return true;")
        self.wait("return document.getElementById('word-display').classList.contains('can-submit')")
        self.browser.script("const i=document.getElementById('answer-input');i.value='wrong';i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
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
        self.wait("return __api.progressUrls.some(u=>u.includes('lang=focus'))")
        self.assertEqual(self.browser.script("return document.querySelectorAll('#practice-progress .progress-row').length"),1)
        self.browser.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return true;")
        self.wait("return getComputedStyle(document.getElementById('practice-setup')).display!=='none'")
        self.wait("return getComputedStyle(document.getElementById('practice-overview')).display!=='none' && document.querySelector('#practice-roadmap-container .roadmap-card')!==null")
        restored=self.browser.script("return {rows:document.querySelectorAll('#practice-progress .progress-row').length,stage:document.getElementById('gauntlet-stage-label').textContent,roadmap:!!document.querySelector('#practice-roadmap-container .roadmap-card'),errors:__errors.slice()};")
        self.assertEqual(restored['rows'],1); self.assertEqual(restored['stage'],'The Forging'); self.assertTrue(restored['roadmap']); self.assertEqual(restored['errors'],[])
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
