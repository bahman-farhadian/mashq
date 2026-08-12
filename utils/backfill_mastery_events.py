#!/usr/bin/env python3
"""Backfill reliable pre-ledger mastery dates; dry-run unless --apply is used."""
import argparse
import os
import sqlite3

import tartarus as ll


def collect_candidates(conn):
    candidates=[]; skipped_later=missing_dates=0
    if not ll.table_exists(conn,'dataset_progress'):
        return candidates,skipped_later,missing_dates
    for user,lang,current_day in conn.execute(
        'SELECT user,lang,current_day FROM dataset_progress ORDER BY user,lang'
    ):
        table=ll.words_table_name(user,lang)
        if not ll.table_exists(conn,table):
            continue
        mastered=conn.execute(
            f'SELECT id,last_tartarus_completed FROM "{table}" WHERE score>=9 ORDER BY id'
        ).fetchall()
        if int(current_day or 0)!=0:
            skipped_later+=len(mastered)
            continue
        for word_id,event_date in mastered:
            if event_date:
                candidates.append((user,lang,word_id,'mastered',str(event_date)[:10]))
            else:
                missing_dates+=1
    return candidates,skipped_later,missing_dates


def existing_keys(conn):
    if not ll.table_exists(conn,'mastery_events'):
        return set()
    return {
        (user,lang,word_id,event_type)
        for user,lang,word_id,event_type in conn.execute(
            'SELECT user,lang,word_id,event_type FROM mastery_events'
        )
    }


def backfill(database,apply=False):
    database=os.path.abspath(database)
    uri=f'file:{database}?mode={"rw" if apply else "ro"}'
    conn=sqlite3.connect(uri,uri=True)
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Database integrity check failed before backfill.')
        candidates,skipped_later,missing_dates=collect_candidates(conn)
        existing=existing_keys(conn)
        pending=[
            row for row in candidates
            if (row[0],row[1],row[2],row[3]) not in existing
        ]
    finally: conn.close()

    result={
        'reliable_candidates':len(candidates),
        'already_present':len(candidates)-len(pending),
        'pending':len(pending),
        'skipped_later_stage':skipped_later,
        'skipped_missing_date':missing_dates,
        'inserted':0,
        'backup':None,
    }
    if not apply:
        return result

    if not pending:
        return result
    result['backup']=ll.verified_database_backup(database,'mastery-events')
    conn=sqlite3.connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        ll.ensure_mastery_events_table(conn)
        before=conn.execute(
            "SELECT COUNT(*) FROM mastery_events WHERE event_type='mastered'"
        ).fetchone()[0]
        conn.executemany(
            'INSERT OR IGNORE INTO mastery_events(user,lang,word_id,event_type,mastered_date) '
            'VALUES(?,?,?,?,?)',
            pending,
        )
        after=conn.execute(
            "SELECT COUNT(*) FROM mastery_events WHERE event_type='mastered'"
        ).fetchone()[0]
        inserted=after-before
        if inserted!=len(pending):
            raise RuntimeError(f'Expected {len(pending)} inserts, observed {inserted}.')
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Database integrity check failed after backfill.')
        result['inserted']=inserted
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=ll.DATABASE_FILE)
    parser.add_argument('--apply',action='store_true',help='Create a verified backup and insert reliable events.')
    args=parser.parse_args()
    result=backfill(args.database,args.apply)
    print('mode=' + ('apply' if args.apply else 'dry-run'))
    for key,value in result.items():
        print(f'{key}={value}')


if __name__=='__main__':
    main()
