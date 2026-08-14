#!/usr/bin/env python3
"""Remove exact ``word`` duplicates from JSON material; dry-run by default."""
import argparse
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_DIR / 'data' / 'word_lists'
DEFAULT_DATABASE = PROJECT_DIR / 'data' / 'tartarus.db'


def progressed_rows(database):
    """Return the strongest persisted evidence for each stable content ID."""
    if not database.is_file():
        return {}
    conn = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed.')
        stats = {}
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'words_%' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if 'content_id' not in columns:
                continue
            score = 'COALESCE(score,0)' if 'score' in columns else '0'
            evidence = [name for name in (
                'times_practiced', 'times_correct', 'times_incorrect',
                'times_drilled', 'times_mastered',
            ) if name in columns]
            tests = [f'COALESCE({name},0)>0' for name in evidence]
            tests.extend(f'{name} IS NOT NULL' for name in (
                'last_practiced', 'last_tartarus_completed',
                'leitner_box', 'leitner_last_reviewed',
            ) if name in columns)
            progressed = ' OR '.join(tests) or '0'
            for content_id, row_score, row_progressed in conn.execute(
                f'SELECT content_id,{score},CASE WHEN {progressed} THEN 1 ELSE 0 END '
                f'FROM "{table}"'
            ):
                current = stats.get(content_id, (0.0, False))
                stats[content_id] = (
                    max(current[0], float(row_score or 0)),
                    current[1] or bool(row_progressed),
                )
        return stats
    finally:
        conn.close()


def choose_index(group, progress):
    """Choose by live score, then actual progress, then original order."""
    ranked = []
    for index, item in group:
        score, progressed = progress.get(item.get('id'), (0.0, False))
        ranked.append((index, score, progressed))
    if not any(score > 0 or progressed for _, score, progressed in ranked):
        return group[0][0]
    best = max((score, progressed) for _, score, progressed in ranked)
    return next(index for index, score, progressed in ranked if (score, progressed) == best)


def plan_file(path, progress):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError(f'{path}: expected an object with an items array')
    groups = defaultdict(list)
    for index, item in enumerate(data['items']):
        if not isinstance(item, dict) or not isinstance(item.get('word'), str):
            raise ValueError(f'{path}: item {index + 1} has no string word')
        groups[item['word']].append((index, item))
    duplicate_groups = {
        word: group for word, group in groups.items() if len(group) > 1
    }
    if not duplicate_groups:
        return data, data, []
    keep = set(range(len(data['items'])))
    report = []
    for word, group in duplicate_groups.items():
        kept_index = choose_index(group, progress)
        dropped = [index for index, _ in group if index != kept_index]
        keep.difference_update(dropped)
        kept_item = data['items'][kept_index]
        report.append({
            'word': word,
            'kept_index': kept_index,
            'kept_id': kept_item.get('id'),
            'dropped_indices': dropped,
            'dropped_ids': [data['items'][index].get('id') for index in dropped],
        })
    updated = dict(data)
    updated['items'] = [item for index, item in enumerate(data['items']) if index in keep]
    if updated.get('metadata') != data.get('metadata'):
        raise AssertionError(f'{path}: metadata changed')
    expected = [item for index, item in enumerate(data['items']) if index in keep]
    if updated['items'] != expected:
        raise AssertionError(f'{path}: retained order changed')
    return data, updated, report


def atomic_write(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run(root, database, apply=False):
    progress = progressed_rows(database)
    files = sorted(root.rglob('*.json'))
    affected = removed = 0
    reports = []
    for path in files:
        original, updated, report = plan_file(path, progress)
        if not report:
            continue
        affected += 1
        count = len(original['items']) - len(updated['items'])
        removed += count
        reports.append((path, report, count))
        for item in report:
            print(
                f'{path.relative_to(root)} | {item["word"]!r} | '
                f'keep={item["kept_id"]} drop={",".join(map(str, item["dropped_ids"]))}'
            )
        if apply:
            atomic_write(path, updated)
    print(
        f'mode={"apply" if apply else "dry-run"} files={len(files)} '
        f'affected={affected} duplicates_removed={removed}'
    )
    return {'files': len(files), 'affected': affected, 'removed': removed, 'reports': reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(args.root.resolve(), args.database.resolve(), args.apply)


if __name__ == '__main__':
    main()
