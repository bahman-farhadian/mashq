#!/usr/bin/env python3
"""Merge numbered ``partNN`` word-list files into one file per
(language, kind, level, part-of-speech) group, preserving item order.

Must be run only after tools/freeze_legacy_ids.py, so every item already
carries a stable explicit id that is immune to the path/index changes this
script makes.

Historical: this operated on the corpus's original four-level layout
(language/kind/level/pos/file.json). The corpus has since been flattened
one level further, to language/kind/level/file.json (each pos directory
held exactly one file post-merge, so the directory added nothing -- see
DATASET_SCHEMA_GUIDE.md section 1). Re-running this script against the
current, already-flattened corpus finds zero groups; it is kept only as a
record of how the original part-file merge was done.

Usage:
    python3 tools/merge_dataset_parts.py [--dry-run]

Writes tools/group_merge_mapping.json describing, for every group, which old
list-ids were merged into which new list-id -- consumed by
tools/migrate_db_group_merge.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'utils'
sys.path.insert(0, str(UTILS))

import tartarus as ll  # noqa: E402

PART_RE = re.compile(r'^(.*)_part(\d+)$')
NAME_PART_RE = re.compile(r'\s+Part\s+\d+\s*$', re.IGNORECASE)


def discover_groups():
    """Group bundled files by their parent directory: language/kind/level/pos."""
    groups = defaultdict(list)
    for root, _dirs, names in os.walk(ll.WORD_LISTS_DIR):
        rel = os.path.relpath(root, ll.WORD_LISTS_DIR)
        parts = [] if rel == '.' else rel.split(os.sep)
        if len(parts) != 4:
            continue  # not a language/kind/level/pos leaf directory
        language, kind, level, pos = parts
        for name in sorted(names):
            if not name.endswith('.json'):
                continue
            stem = name[:-len('.json')]
            match = PART_RE.match(stem)
            if not match:
                raise ValueError(f'Unexpected filename outside the partNN convention: {root}/{name}')
            base, part_num = match.group(1), int(match.group(2))
            groups[(language, kind, level, pos)].append((part_num, stem, os.path.join(root, name)))
    return groups


def new_stem(language, kind, level, pos):
    if kind == 'sentences':
        return f'{language}_sentences_{pos}_{level}'
    return f'{language}_{pos}_{level}'


def strip_part_suffix(name):
    return NAME_PART_RE.sub('', name).strip()


def merge_group(key, entries, dry_run):
    language, kind, level, pos = key
    entries = sorted(entries, key=lambda e: e[0])  # ascending part number
    old_stems = [stem for _, stem, _ in entries]
    old_paths = [path for _, _, path in entries]

    merged_items = []
    merged_metadata = None
    pre_ids_per_file = []
    for path in old_paths:
        data = ll.read_word_list(path)
        if merged_metadata is None:
            merged_metadata = dict(data['metadata'])
        merged_items.extend(data['items'])
        pre_ids_per_file.append([item['content_id'] for item in ll.load_practice_items(path)])

    merged_metadata['name'] = strip_part_suffix(merged_metadata.get('name', ''))
    merged_metadata['language'] = language
    merged_metadata['kind'] = kind
    merged_metadata['level'] = level

    stem = new_stem(language, kind, level, pos)
    directory = os.path.dirname(old_paths[0])
    new_path = os.path.join(directory, f'{stem}.json')

    expected_count = sum(len(ids) for ids in pre_ids_per_file)
    if len(merged_items) != expected_count:
        raise ValueError(f'{key}: item count mismatch during concatenation')

    expected_id_sequence = [cid for ids in pre_ids_per_file for cid in ids]
    if len(set(expected_id_sequence)) != len(expected_id_sequence):
        raise ValueError(f'{key}: duplicate ids across parts -- freeze step did not run?')

    merged_data = {'metadata': merged_metadata, 'items': merged_items}

    if not dry_run:
        ll.write_word_list_atomic(new_path, merged_data)
        actual_id_sequence = [item['content_id'] for item in ll.load_practice_items(new_path)]
        if actual_id_sequence != expected_id_sequence:
            raise ValueError(f'{key}: merged file content_id sequence does not match concatenation of parts')
        for path in old_paths:
            if path != new_path:
                os.remove(path)

    return {
        'language': language, 'kind': kind, 'level': level, 'pos': pos,
        'old_ids': old_stems, 'new_id': stem, 'item_count': len(merged_items),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    args = parser.parse_args()

    groups = discover_groups()
    mapping = []
    for key in sorted(groups):
        mapping.append(merge_group(key, groups[key], args.dry_run))

    new_stems_seen = {}
    for entry in mapping:
        stem = entry['new_id']
        if stem in new_stems_seen:
            raise ValueError(f'Duplicate merged list-id {stem!r} in groups '
                              f'{new_stems_seen[stem]!r} and {entry!r}')
        new_stems_seen[stem] = entry

    total_old = sum(len(entry['old_ids']) for entry in mapping)
    total_items = sum(entry['item_count'] for entry in mapping)
    multi_part_groups = sum(1 for entry in mapping if len(entry['old_ids']) > 1)
    print(f'{"[dry-run] " if args.dry_run else ""}{len(mapping)} groups '
          f'({multi_part_groups} multi-part), {total_old} old files -> {len(mapping)} merged files, '
          f'{total_items} items total.')

    mapping_path = ROOT / 'tools' / 'group_merge_mapping.json'
    if not args.dry_run:
        with open(mapping_path, 'w', encoding='utf-8') as target:
            json.dump(mapping, target, indent=2)
            target.write('\n')
        print(f'Wrote mapping for {len(mapping)} groups to {mapping_path}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
