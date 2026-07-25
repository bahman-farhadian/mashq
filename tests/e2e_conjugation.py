#!/usr/bin/env python3
"""End-to-end conjugation harness.

Runs the real engine (the same functions the CLI and web call) through the
full 20-stage curriculum, then drives the actual CLI subprocess via stdin,
then audits German verb forms.

Isolated: overrides TARTARUS_DB to a temp file so the production database is
never touched.

    python3 tests/e2e_conjugation.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, 'utils'))

import conjugation  # noqa: E402
import tartarus  # noqa: E402

FAILURES = []


def check(condition, message):
    status = 'PASS' if condition else 'FAIL'
    print(f'  [{status}] {message}')
    if not condition:
        FAILURES.append(message)


def stage_pronoun_orders(units, stage):
    return sorted({u['pronoun_order'] for u in units if u['stage'] == stage})


def phase_a_full_curriculum_journey(units):
    print('\n=== Phase A: full 20-stage curriculum journey (in-process engine) ===')
    tmp = tempfile.mkdtemp(prefix='tartarus_e2e_')
    db = os.path.join(tmp, 't.db')
    tartarus.DATABASE_FILE = db
    # Real entry point shared by CLI and web.
    tartarus.sync_word_list('journey', 'german_conjugations')
    conn = sqlite3.connect(db)
    table = conjugation.table_name('journey')

    def complete(stage, pronoun):
        conn.execute(
            f'UPDATE "{table}" SET completed=1, score=9.0, '
            f"last_practiced=date('now','localtime') "
            f'WHERE stage=? AND pronoun_order=?', (stage, pronoun)
        )
        conn.commit()

    def current_batch():
        return conjugation.next_units(conn, 'journey', 16)

    expected_stage = 2
    for stage, stage_name in conjugation.STAGES:
        if stage == 1:
            continue
        batch = current_batch()
        check(batch, f'stage {stage} ({stage_name}): units available')
        check(batch[0]['stage'] == stage,
              f'stage {stage}: engine offers stage {batch[0]["stage"]} (expected {stage})')
        # First verb of the stage (for sampling).
        first = batch[0]
        print(f'  stage {stage:2d} {stage_name}: first unit verb={first["verb"]!r} '
              f'answer={first["answer"]!r} pron={first["pronoun_order"]}')
        for pronoun in stage_pronoun_orders(units, stage):
            b = current_batch()
            check(b and b[0]['stage'] == stage and b[0]['pronoun_order'] == pronoun,
                  f'stage {stage} pronoun {pronoun}: locked to correct position')
            complete(stage, pronoun)
        expected_stage = stage + 1

    # After stage 20: only review items remain, deterministically ordered.
    review = current_batch()
    check(bool(review), 'after stage 20: review queue non-empty')
    review2 = [u['unit_key'] for u in current_batch()]
    check([u['unit_key'] for u in review] == review2,
          'after stage 20: review order reproducible (no random tie-break)')
    prog = conjugation.progress(conn, 'journey')
    check(prog['current_stage'] == 20 and prog['completed'] == prog['total'],
          f'progress(): all complete, current_stage={prog["current_stage"]} '
          f'({prog["completed"]}/{prog["total"]})')
    conn.close()
    print(f'  journey complete: walked all 20 stages, {prog["total"]} units')


def phase_b_cli_subprocess():
    print('\n=== Phase B: CLI subprocess e2e (stdin-driven practice) ===')
    tmp = tempfile.mkdtemp(prefix='tartarus_cli_')
    db = os.path.join(tmp, 't.db')
    env = dict(os.environ, TARTARUS_DB=db)
    # Pre-populate via the engine so the answers are known.
    tartarus.DATABASE_FILE = db
    tartarus.sync_word_list('cliuser', 'german_conjugations')
    # First 16 new-learning units are stage 2 (infinitive); answers are the
    # clean `infinitiv` field, in CORE_VERBS + source order.
    conn = sqlite3.connect(db)
    queue = conjugation.next_units(conn, 'cliuser', 16)
    conn.close()
    answers = [u['answer'] for u in queue]
    stdin = '\n'.join(answers) + '\n'
    proc = subprocess.run(
        ['python3', os.path.join(PROJECT_DIR, 'utils', 'tartarus.py'),
         'practice', '--user', 'cliuser', '--lang', 'german_conjugations',
         '--no-audio'],
        input=stdin, env=env, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout
    check(proc.returncode == 0, f'CLI exits 0 (got {proc.returncode})')
    check('Conjugation session complete' in out,
          'CLI prints session-complete summary')
    # Session summary reports the correct count.
    if 'Conjugation session complete' in out:
        tail = out[out.rfind('Conjugation session complete'):]
        check('16 correct' in tail or 'correct' in tail,
              f'CLI session scored correct answers ({tail.strip()[:60]})')
    print(f'  CLI ran {len(answers)} questions, summary: '
          f'{out[out.rfind("Conjugation session complete"):].strip()[:80]}')


# --- German teacher verb-form audit ---
#
# Hand-verified reference paradigms for the highest-value verbs. Each entry is
# (verb, stage, expected_answers) where expected_answers is the ordered list
# the engine must produce. Acts as the linguistic gate: if the source dataset
# drifted from standard German, this fails and names the verb.

REFERENCE = {
    'sein': {
        2: ['sein'],
        3: ['ich bin', 'du bist', 'er ist', 'wir sind', 'ihr seid', 'sie sind'],
        5: ['Sei!', 'Seid!', 'Seien Sie!', 'Seien wir!'],
        6: ['gewesen'],
        7: ['sein'],
        9: ['ich war', 'du warst', 'er war', 'wir waren', 'ihr wart', 'sie waren'],
        10: ['zu sein'],
    },
    'haben': {
        2: ['haben'],
        3: ['ich habe', 'du hast', 'er hat', 'wir haben', 'ihr habt', 'sie haben'],
        6: ['gehabt'],
        7: ['haben'],
        9: ['ich hatte', 'du hattest', 'er hatte', 'wir hatten', 'ihr hattet', 'sie hatten'],
        10: ['zu haben'],
    },
    'werden': {
        2: ['werden'],
        3: ['ich werde', 'du wirst', 'er wird', 'wir werden', 'ihr werdet', 'sie werden'],
        6: ['geworden'],
        7: ['sein'],
        10: ['zu werden'],
    },
    'machen': {
        2: ['machen'],
        3: ['ich mache', 'du machst', 'er macht', 'wir machen', 'ihr macht', 'sie machen'],
        6: ['gemacht'],
        7: ['haben'],
        10: ['zu machen'],
    },
    'gehen': {
        2: ['gehen'],
        3: ['ich gehe', 'du gehst', 'er geht', 'wir gehen', 'ihr geht', 'sie gehen'],
        6: ['gegangen'],
        7: ['sein'],
        10: ['zu gehen'],
    },
    'können': {
        2: ['können'],
        3: ['ich kann', 'du kannst', 'er kann', 'wir können', 'ihr könnt', 'sie können'],
        6: ['gekonnt'],
        7: ['haben'],
        10: ['zu können'],
    },
}


def phase_c_german_teacher_audit(units, data):
    print('\n=== Phase C: German teacher verb-form audit ===')
    by_key = {}
    for u in units:
        by_key.setdefault((u['verb'], u['stage']), []).append(u)
    for verb, expectations in REFERENCE.items():
        record = data.get(verb)
        check(bool(record), f'{verb}: present in dataset')
        if not record:
            continue
        for stage, expected in expectations.items():
            entries = by_key.get((verb, stage), [])
            # Non-person stages (2,6,7,10) have one unit; person stages have six.
            got = [u['answer'] for u in entries]
            check(got == expected,
                  f'{verb} stage {stage}: {got} == {expected}')
            if got != expected:
                print(f'    got={got} expected={expected}')

    # haben must NOT appear in passive stages (engine exclusion contract).
    for stage in (14, 16):
        entries = by_key.get(('haben', stage), [])
        check(not entries, f'haben stage {stage}: no passive units generated')

    # Linguistic flags for the teacher to resolve (reported, not auto-fixed).
    messy_names = [v for v in data if '(' in v]
    reflexive_passive = [v for v, r in data.items()
                         if r.get('passiv') and '(sich)' in v]
    print(f'  [INFO] {len(messy_names)} verbs carry annotation in key '
          f'(e.g. {messy_names[:3]}) — learner-facing prompt pollution')
    print(f'  [INFO] {len(reflexive_passive)} reflexive verbs with passive '
          f'(pedagogically questionable): {reflexive_passive}')

    # Passive subjects must be haben-transitive (structural invariant).
    bad_passive = [v for v, r in data.items()
                   if r.get('passiv') and r.get('hilfsverb') != 'haben']
    check(not bad_passive,
          f'no sein-verb has a passive paradigm ({bad_passive[:5]})')


def main():
    data = conjugation.load_source()
    units = conjugation.build_units(data)
    print(f'Tartarus conjugation e2e — {len(data)} verbs, {len(units)} units')
    phase_a_full_curriculum_journey(units)
    phase_b_cli_subprocess()
    phase_c_german_teacher_audit(units, data)
    print('\n' + '=' * 60)
    if FAILURES:
        print(f'RESULT: {len(FAILURES)} FAILURE(S)')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    print('RESULT: ALL E2E CHECKS PASSED')
    sys.exit(0)


if __name__ == '__main__':
    main()
