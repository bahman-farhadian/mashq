#!/usr/bin/env python3
"""One-time database migration that follows tools/merge_dataset_parts.py.

For every user, for every (old part-list-ids -> new merged list-id) mapping
in tools/group_merge_mapping.json, moves that user's word-progress rows from
the old per-part tables into one new table for the merged list, remaps
session history to the merged list id, and drops the retired per-part
Gauntlet tracker rows.

This is a one-time operational step against the live database, run manually
-- it is not wired into initialize_database()/SCHEMA_VERSION, since it is a
data remap tied to a specific dataset restructuring rather than a schema
change every install replays. Stop the web server before running for real.

Safety:
  - a verified `.pre-group-merge.<timestamp>.sqlite` backup is made first
    (unless --no-backup or --dry-run);
  - the entire run is one transaction: it either fully commits or fully
    rolls back, so a re-run after a successful run is a no-op (the old
    per-part tables are already gone) and a re-run after a failed run finds
    the database exactly as it was before this script touched it;
  - per group, the new table's row count/content-id-set/score/times_*/
    leitner_box totals are checked against the sum of the old tables'
    totals before any fresh (never-practiced) rows are backfilled in, and
    the whole transaction aborts on any mismatch.

Usage:
    python3 tools/migrate_db_group_merge.py [--dry-run] [--no-backup]
        [--db PATH] [--mapping PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'utils'
sys.path.insert(0, str(UTILS))

import tartarus as ll  # noqa: E402


def make_backup(db_path):
    stamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    backup_path = f'{db_path}.pre-group-merge.{stamp}.sqlite'
    target = sqlite3.connect(backup_path)
    try:
        source = sqlite3.connect(db_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            source.close()
    finally:
        target.close()
    with open(backup_path, 'rb+') as handle:
        handle.flush()
        os.fsync(handle.fileno())
    check = sqlite3.connect(f'file:{backup_path}?mode=ro', uri=True)
    try:
        if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Pre-migration backup integrity check failed.')
    finally:
        check.close()
    return backup_path


def sync_new_table(conn, user, new_id, new_table):
    """Backfill fresh score-0 rows for words the user never touched, and set
    ``active`` from the merged JSON -- inline equivalent of
    ll.sync_word_list(), reusing this transaction's connection instead of
    opening a second one (sync_word_list opens/commits its own connection,
    which would deadlock against this script's own write transaction)."""
    path = ll.word_list_path(user, new_id)
    entries = ll.load_practice_items(path)
    seen_ids = {entry['content_id'] for entry in entries}
    for content_id in seen_ids:
        conn.execute(f'INSERT OR IGNORE INTO "{new_table}" (content_id) VALUES (?)', (content_id,))
    rows = conn.execute(f'SELECT id,content_id FROM "{new_table}"').fetchall()
    for row_id, content_id in rows:
        conn.execute(f'UPDATE "{new_table}" SET active=? WHERE id=?', (int(content_id in seen_ids), row_id))


def migrate_one(conn, user, entry, report):
    new_id = entry['new_id']
    old_tables = [(old_id, ll.words_table_name(user, old_id)) for old_id in entry['old_ids']]
    existing = [(old_id, table) for old_id, table in old_tables if ll.table_exists(conn, table)]
    if not existing:
        return

    before = tuple(sum(values) for values in zip(*(
        ll._audit_word_table(conn, table) for _old_id, table in existing
    )))

    new_table = ll.ensure_word_table(conn, user, new_id)
    for _old_id, old_table in existing:
        columns = ', '.join(f'"{c}"' for c in ll.WORD_TABLE_COLUMNS if c != 'id')
        conn.execute(
            f'INSERT INTO "{new_table}" ({columns}) SELECT {columns} FROM "{old_table}"'
        )

    after = ll._audit_word_table(conn, new_table)
    if after != before:
        raise ValueError(
            f"Audit mismatch merging {[t for _, t in existing]} into {new_table}: "
            f"before={before} after={after}"
        )

    sync_new_table(conn, user, new_id, new_table)

    for _old_id, old_table in existing:
        conn.execute(f'DROP TABLE "{old_table}"')

    old_ids_present = [old_id for old_id, _ in existing]
    conn.executemany(
        'DELETE FROM dataset_progress WHERE user=? AND lang=?',
        [(user, old_id) for old_id in old_ids_present],
    )

    sessions_table = ll.sessions_table_name(user)
    if ll.table_exists(conn, sessions_table):
        placeholders = ', '.join('?' for _ in old_ids_present)
        conn.execute(
            f'UPDATE "{sessions_table}" SET language=? WHERE language IN ({placeholders})',
            (new_id, *old_ids_present),
        )

    report.append({
        'user': user, 'new_id': new_id, 'old_ids': old_ids_present,
        'rows_before': before[0], 'rows_after': after[0],
    })


def migrate_group_merge(mapping, database_file=None, *, create_backup=True, dry_run=False):
    db_path = database_file or ll.DATABASE_FILE
    if not os.path.exists(db_path):
        return {'backup': None, 'report': []}

    conn = sqlite3.connect(db_path)
    backup_path = None
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed before migration.')

        users = [row[0] for row in conn.execute('SELECT name FROM users')] if ll.table_exists(conn, 'users') else []

        if create_backup and not dry_run:
            backup_path = make_backup(db_path)

        conn.execute('BEGIN IMMEDIATE')
        ll.ensure_dataset_progress_table(conn)
        report = []
        for user in users:
            for entry in mapping:
                migrate_one(conn, user, entry, report)

        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed after migration.')

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {'backup': backup_path, 'report': report}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='report only, roll back before exit')
    parser.add_argument('--no-backup', action='store_true', help='skip the pre-migration backup (not recommended)')
    parser.add_argument('--db', default=None, help='database file (defaults to TARTARUS_DB / data/tartarus.db)')
    parser.add_argument('--mapping', default=str(ROOT / 'tools' / 'group_merge_mapping.json'),
                         help='group_merge_mapping.json produced by merge_dataset_parts.py')
    args = parser.parse_args()

    with open(args.mapping, encoding='utf-8') as source:
        mapping = json.load(source)

    result = migrate_group_merge(
        mapping, database_file=args.db, create_backup=not args.no_backup, dry_run=args.dry_run,
    )

    prefix = '[dry-run] ' if args.dry_run else ''
    print(f'{prefix}Migrated {len(result["report"])} (user, group) pair(s).')
    for row in result['report']:
        print(f'  {prefix}{row["user"]}: {row["old_ids"]} -> {row["new_id"]} '
              f'({row["rows_before"]} historical rows preserved)')
    if result['backup']:
        print(f'Backup: {result["backup"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
