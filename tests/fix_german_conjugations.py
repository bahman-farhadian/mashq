#!/usr/bin/env python3
"""German-teacher dataset correction for conjugations.json.

One-shot, idempotent. Applies linguistic fixes the source review requires:

1. DEDUP: 28 verbs are duplicated under both an annotated key (e.g.
   'aufgeben (hat aufgegeben)') and a clean key ('aufgeben'). For each pair
   the correct record is kept and the other removed. Where the two differ,
   the record with the valid German forms wins
   (aufgeben/tauschen/vorlesen keep the annotated copy; the rest keep clean).
2. RENAME: 82 non-colliding annotated keys are renamed to their clean form
   (strips '(hat geX)'/'(ist geX)' aux hints, '(fuer + A.)' preposition
   hints, normalizes '(sich)' suffix to 'sich ' prefix). Idioms
   ('Bescheid geben') and 'sich X' prefix verbs keep their form.
3. FORM FIXES:
   - geschehen: Präsens 'geschehst'->'geschiehst', 'gescheht'(3rd)->'geschieht';
     imperative nulled (geschehen is impersonal, 'Gescheh!' is not German).
   - schlagen: 3rd person 'schlaegst'->'schlaegt'.
   - wachsen: 2nd/3rd person 'wachst'->'wächst' (missing umlaut).
   - messen: 2nd/3rd person 'messt'->'misst' (missing e->i vowel change).
   - fressen: 2nd/3rd person 'fresst'->'frisst' (missing e->i vowel change).
   - English: 'occured'->'occurred' (vorkommen, einfallen),
     'forgived'->'forgave' (verzeihen), 'catched'->'caught' (sich erkaelten).

unit_key uses verb_order (position), not the verb name, so renaming keys
preserves all learner progress; a later sync retires the orphaned units.
"""
import json
import os
import re
from collections import Counter

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'data', 'word_lists', 'german', 'conjugations.json')


def normalize_key(v):
    name = v
    if '(sich)' in name:
        name = name.replace('(sich)', '').strip()
        if not name.startswith('sich '):
            name = 'sich ' + name
    name = re.sub(r'\s*\([^)]*\)', '', name)
    return name.strip()


def fix_forms(key, record):
    rec = json.loads(json.dumps(record))  # deep copy
    nkey = normalize_key(key)

    if nkey == 'geschehen':
        praes = rec.get('indikativ', {}).get('praesens')
        if isinstance(praes, list) and len(praes) == 6:
            praes[1] = 'geschiehst'
            praes[2] = 'geschieht'
        if 'imperativ' in rec:
            rec['imperativ'] = None
    # Irregular Präsens corrections: (verb, {index: correct_form})
    praes_fixes = {
        'schlagen': {1: 'schlägst', 2: 'schlägt'},
        'wachsen': {1: 'wächst', 2: 'wächst'},
        'messen': {1: 'misst', 2: 'misst'},
        'fressen': {1: 'frisst', 2: 'frisst'},
    }
    if nkey in praes_fixes:
        praes = rec.get('indikativ', {}).get('praesens')
        if isinstance(praes, list) and len(praes) == 6:
            for idx, val in praes_fixes[nkey].items():
                praes[idx] = val

    # English typos (operate on the english blob, whole-word replacements).
    eng = rec.get('english')
    if isinstance(eng, dict):
        blob = json.dumps(eng, ensure_ascii=False)
        changed = False
        for bad, good in (('occured', 'occurred'), ('forgived', 'forgave'),
                          ('catched', 'caught'), ('fighted', 'fought')):
            if bad in blob:
                blob = blob.replace(bad, good)
                changed = True
        if changed:
            rec['english'] = json.loads(blob)
    return rec


def main():
    with open(PATH, encoding='utf-8') as f:
        data = json.load(f)

    new_keys = {old: normalize_key(old) for old in data}
    cnt = Counter(new_keys.values())
    collision_groups = {k for k, c in cnt.items() if c > 1}

    # Keep the annotated copy (its German forms are valid; the clean copy is
    # broken) and drop the clean key.
    keep_annotated = {
        'aufgeben (hat aufgegeben)',
        'tauschen (hat getauscht)',
        'vorlesen (hat vorgelesen)',
    }

    losers = set()
    groups = {}
    for old in data:
        groups.setdefault(new_keys[old], []).append(old)
    for nkey, members in groups.items():
        if len(members) == 1:
            continue
        kept_annotated = [m for m in members if m in keep_annotated]
        if kept_annotated:
            winner = kept_annotated[0]
        else:
            clean = [m for m in members if m == nkey]
            winner = clean[0] if clean else members[0]
        for member in members:
            if member != winner:
                losers.add(member)

    final = {}
    for old in data:
        if old in losers:
            continue
        nkey = new_keys[old]
        final[nkey] = fix_forms(old, data[old])

    # sanity: no duplicate keys, no remaining parenthetical noise
    assert len(final) == len(set(final)), 'duplicate keys after correction'
    remaining_paren = [k for k in final if '(' in k]
    assert not remaining_paren, f'annotated keys remain: {remaining_paren[:5]}'

    with open(PATH, 'w', encoding='utf-8') as f:
        f.write(json.dumps(final, ensure_ascii=False, indent=1) + '\n')

    print(f'before: {len(data)} verbs')
    print(f'after:  {len(final)} verbs  (removed {len(data) - len(final)} duplicates)')
    print(f'renamed {len([o for o in data if o not in losers and new_keys[o] != o])} annotated keys')
    print('kept-annotated (correct forms, renamed):')
    for k in sorted(keep_annotated):
        print(f'  {k!r} -> {normalize_key(k)!r}')


if __name__ == '__main__':
    main()
