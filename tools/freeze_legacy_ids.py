#!/usr/bin/env python3
"""Freeze every bundled word-list item's derived legacy content id as an explicit
``id`` field, without changing anything else about the file.

This is a one-time, no-op-for-existing-progress preparation step: the explicit
id written here is byte-for-byte identical to what ``load_practice_items``
already computes at read time for an id-less item (source path + 1-based index
+ word). Freezing it means later structural edits (splitting/merging/renaming
files, reordering items) no longer change any word's learner-progress identity.

Usage:
    python3 tools/freeze_legacy_ids.py [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'utils'
sys.path.insert(0, str(UTILS))

import tartarus as ll  # noqa: E402


def content_ids_for(path):
    return [item['content_id'] for item in ll.load_practice_items(path)]


def freeze_file(path, dry_run):
    before_ids = content_ids_for(path)

    data = ll.read_word_list(path)
    items = data['items']
    if all('id' in item and str(item['id']).strip() for item in items):
        return before_ids, before_ids, False  # already frozen, nothing to do

    normalized = ll.validate_word_list_items(items, path)
    for original, record in zip(items, normalized):
        original['id'] = record['id']

    if not dry_run:
        ll.write_word_list_atomic(path, data)
        after_ids = content_ids_for(path)
    else:
        after_ids = [item['id'] for item in items]

    return before_ids, after_ids, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(ll.WORD_LISTS_DIR, '**', '*.json'), recursive=True))
    if not paths:
        print(f'No word-list files found under {ll.WORD_LISTS_DIR}')
        return 1

    changed = 0
    unchanged = 0
    mismatches = []
    for path in paths:
        rel = os.path.relpath(path, ll.WORD_LISTS_DIR)
        before_ids, after_ids, did_write = freeze_file(path, args.dry_run)
        if before_ids != after_ids:
            mismatches.append(rel)
            print(f'MISMATCH: {rel} content_id sequence changed!', file=sys.stderr)
        if did_write:
            changed += 1
        else:
            unchanged += 1

    print(f'{"[dry-run] " if args.dry_run else ""}Scanned {len(paths)} files: '
          f'{changed} frozen, {unchanged} already had explicit ids.')

    if mismatches:
        print(f'{len(mismatches)} file(s) had a content_id mismatch after freezing:', file=sys.stderr)
        for rel in mismatches:
            print(f'  - {rel}', file=sys.stderr)
        return 1

    print('OK: every file\'s content_id sequence is unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
