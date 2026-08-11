#!/usr/bin/env python3
"""Simulate many days of real practice for a test user, so every Gauntlet
stage and every Leitner box can be inspected -- both in this script's
day-by-day report and live in the web UI -- without waiting real calendar
days.

This drives the actual scoring/progress functions (record_tartarus_answer,
record_maintenance_answer, reconcile_gauntlet_progress, ...), the same ones
the web/CLI surfaces call. It never touches raw SQL for progress state, so
a successful run is real evidence the day-to-day flow works, not just that
a database row was hand-set.

Each simulated day:
  1. reconcile_gauntlet_progress()      -- settle the day/stage counter.
  2. master new words via the real Forging path, up to --new-per-day.
  3. clear that day's Tartarus reinforcement task for already-mastered
     words (this is what lets current_day advance on a later run).
  4. push every due Leitner box word forward one box.
  5. reconcile again, then print the day's Gauntlet day/stage and the
     box-1..10 distribution.

The default word pool/user/list are named for QA use and are fully
isolated from any other user's tables -- safe to run against the real
configured database.

Usage:
    python3 tools/simulate_learning_flow.py
    python3 tools/simulate_learning_flow.py --days 60 --pool-size 60 --new-per-day 30
    python3 tools/simulate_learning_flow.py --user qa2 --lang qa2_list --reset
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'utils'
sys.path.insert(0, str(UTILS))

import tartarus as ll  # noqa: E402


def ensure_test_list(user, lang, pool_size):
    path = ll.word_list_path_user_specific(user, lang)
    if os.path.isfile(path):
        return
    items = [
        {'word': f'qa-word-{i:04d}', 'definition': f'definition for qa-word-{i:04d}'}
        for i in range(pool_size)
    ]
    # Explicit language/level/kind so it shows up under a real category in
    # the practice/report/editor cascades (an unset language defaults to
    # "unknown", which none of those dropdowns list).
    ll.save_custom_list(user, lang, {
        'metadata': {'name': f'QA flow test ({lang})', 'language': 'german', 'kind': 'vocabulary', 'level': 'all'},
        'items': items,
    })


def master_new_words(user, lang, today, target_new):
    """Drive the real Forging path until ``target_new`` more words cross
    9.0 today, or the whole list is already mastered."""
    newly_mastered = 0
    while newly_mastered < target_new:
        try:
            words = ll.get_words_for_gauntlet_stage(user, lang, 0, today=today)
        except ValueError:
            break  # Forging complete for this list
        if not words:
            break
        for row_id, _word, _definition, score, _box, _freq in words:
            new_score = ll.record_tartarus_answer(user, lang, row_id, True, today=today)
            if score < 9.0 <= new_score:
                newly_mastered += 1
    return newly_mastered


def reinforce_today(user, lang, today):
    """Clear today's Tartarus reinforcement task for every mastered word,
    the thing that lets current_day advance past Forging."""
    while True:
        conn = ll.get_connection()
        try:
            progress = ll.get_dataset_progress(user, lang, conn=conn)
        finally:
            conn.close()
        if progress['current_stage'] == 0:
            return
        try:
            words = ll.get_words_for_gauntlet_stage(user, lang, progress['current_stage'], today=today)
        except ValueError:
            return
        if not words:
            return
        for row_id, _word, _definition, _score, _box, _freq in words:
            ll.record_tartarus_answer(user, lang, row_id, True, today=today)


def advance_leitner(user, lang, today):
    """Push every due Leitner box word forward one box (capped at 10)."""
    moved = 0
    for row_id, _word, _definition, _score, _box, _freq in ll.maintenance_ready_words(user, lang, today=today):
        ll.record_maintenance_answer(user, lang, row_id, True, today=today)
        moved += 1
    return moved


def box_distribution(user, lang):
    table = ll.words_table_name(user, lang)
    conn = ll.get_connection()
    try:
        rows = conn.execute(
            f'SELECT leitner_box, COUNT(*) FROM "{table}" WHERE active=1 AND leitner_box IS NOT NULL GROUP BY leitner_box'
        ).fetchall()
    finally:
        conn.close()
    dist = {i: 0 for i in range(1, 11)}
    for box, count in rows:
        if box is not None:
            dist[int(box)] = count
    return dist


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', default='qa_flow')
    parser.add_argument('--lang', default='qa_flow_list')
    parser.add_argument('--pool-size', type=int, default=120)
    parser.add_argument('--new-per-day', type=int, default=20)
    parser.add_argument('--days', type=int, default=20)
    parser.add_argument('--reset', action='store_true', help="wipe this user/list's existing progress first")
    args = parser.parse_args()

    conn = ll.get_connection()
    ll.ensure_user(conn, args.user)
    conn.commit()
    conn.close()

    ensure_test_list(args.user, args.lang, args.pool_size)

    if args.reset:
        try:
            ll.reset_word_list_progress(args.user, args.lang)
        except ValueError:
            pass

    start = date.today() - timedelta(days=args.days - 1)
    print(f"{'day':>3} {'date':>10} {'stage':>10} {'gday':>4}  {'new':>4}  {'reviewed':>8}   box1  box2  box3  box4  box5  box6  box7  box8  box9 box10")
    for i in range(args.days):
        today = (start + timedelta(days=i)).isoformat()

        ll.reconcile_gauntlet_progress(args.user, args.lang, today=today)
        newly_mastered = master_new_words(args.user, args.lang, today, args.new_per_day)
        reinforce_today(args.user, args.lang, today)
        moved = advance_leitner(args.user, args.lang, today)
        progress = ll.reconcile_gauntlet_progress(args.user, args.lang, today=today)

        dist = box_distribution(args.user, args.lang)
        boxes = ' '.join(f'{dist[b]:>5}' for b in range(1, 11))
        stage_name = ll.gauntlet_stage_for_day(progress['current_day'])[1]
        print(f"{i + 1:>3} {today:>10} {stage_name:>10} {progress['current_day']:>4}  "
              f"{newly_mastered:>4}  {moved:>8}   {boxes}")

    print()
    print(f"Seeded user={args.user!r} lang={args.lang!r} in {ll.DATABASE_FILE}")
    print('Open the web UI, pick this user, then German vocabulary -> ALL -> ALL to inspect it live.')


if __name__ == '__main__':
    raise SystemExit(main())
