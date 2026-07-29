"""Deterministic German conjugation curriculum and progress storage."""

import json
import os
from datetime import date, datetime


PRONOUNS = ("ich", "du", "er, sie, es", "wir", "ihr", "sie, Sie")
IMPERATIVE_PRONOUNS = ("du", "ihr", "Sie", "wir")
PERSONAL_PRONOUNS = (
    ("ich", "I"),
    ("du", "you (informal singular)"),
    ("er", "he"),
    ("sie", "she"),
    ("es", "it"),
    ("wir", "we"),
    ("ihr", "you (informal plural)"),
    ("sie", "they"),
    ("Sie", "you (formal)"),
)
# Curriculum stages and long-term-memory boxes are independent. Every
# practice item uses the shared ten-box, one-to-ten-day review schedule.
LEITNER_INTERVALS = {box: box for box in range(1, 11)}
SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'word_lists', 'german', 'tartarus_sample_german_conjugations.json'
)
LIST_ID = "tartarus_sample_german_conjugations"

STAGES = (
    (1, "Personal pronouns"),
    (2, "Infinitive"),
    (3, "Indikativ Präsens"),
    (4, "Separable and irregular present forms"),
    (5, "Imperative"),
    (6, "Partizip II"),
    (7, "haben/sein auxiliary selection"),
    (8, "Indikativ Perfekt"),
    (9, "Indikativ Präteritum"),
    (10, "zu-infinitive"),
    (11, "Indikativ Plusquamperfekt"),
    (12, "Indikativ Futur I"),
    (13, "Konjunktiv II Präteritum"),
    (14, "Passive Präsens and Präteritum"),
    (15, "Konjunktiv II Plusquamperfekt"),
    (16, "Passive Perfekt and advanced passive tenses"),
    (17, "Konjunktiv I Präsens and Perfekt"),
    (18, "Indikativ Futur II"),
    (19, "Konjunktiv I/II future forms"),
    (20, "Partizip I and stylistic mastery"),
)

CORE_VERBS = (
    "sein", "haben", "werden", "machen", "gehen", "kommen", "können",
    "müssen", "wollen", "sollen", "dürfen", "mögen", "sagen", "sprechen",
    "fragen", "antworten", "lernen", "arbeiten", "wohnen", "leben", "essen",
    "trinken", "sehen", "hören", "lesen", "schreiben", "geben", "nehmen",
    "bringen", "fahren", "laufen", "bleiben", "finden", "wissen", "kennen",
    "denken", "glauben", "brauchen", "lassen", "helfen", "zeigen", "spielen",
    "kaufen", "verkaufen", "öffnen", "schließen", "anfangen", "aufhören",
    "verstehen", "vergessen", "sich erinnern",
)


def is_conjugation_list(lang):
    return (lang or "").lower() == LIST_ID


def table_name(user):
    from tartarus import sanitize_name
    return f"conjugations_{sanitize_name(user, 'user')}"


def due_interval_case(column='leitner_box'):
    """Return the SQL CASE expression for the shared ten Leitner boxes."""
    cases = ' '.join(
        f'WHEN {box} THEN {days}'
        for box, days in LEITNER_INTERVALS.items()
    )
    return f'CASE {column} {cases} ELSE {LEITNER_INTERVALS[10]} END'


def load_source(conn=None):
    """Load conjugation material from its JSON source, never SQLite."""
    if not os.path.exists(SOURCE_PATH):
        return {}
    with open(SOURCE_PATH, encoding='utf-8') as source:
        return json.load(source)


def _special_present(verb, record):
    forms = record.get("indikativ", {}).get("praesens", [])
    if " " in verb or any(" " in str(form) for form in forms):
        return True
    base = verb.split(" ", 1)[0].replace(" (sich)", "")
    stem = base[:-2] if base.endswith("en") else base[:-1]
    regular = [stem + suffix for suffix in ("e", "st", "t", "en", "t", "en")]
    return [str(form) for form in forms] != regular


def _add_units(units, stage, stage_name, verb_order, verb, records, forms,
               prompt_name, pronouns=None, add_answer_pronoun=True):
    if not forms:
        return
    for index, answer in enumerate(forms):
        pronoun = pronouns[index] if pronouns else ""
        key = f"{stage}:{verb_order}:{index}:{prompt_name}"
        if prompt_name == "infinitive":
            english = (records.get("english", {}).get("praesens") or [""])[0]
            prompt = f"{stage_name} · English: {english or 'the verb'}"
        else:
            prompt = f"{stage_name} · Verb: {verb}"
        if pronoun:
            prompt += f" · {pronoun}"
        answer_text = str(answer)
        if add_answer_pronoun and pronoun in PRONOUNS:
            answer_pronoun = {
                "er, sie, es": "er",
                "sie, Sie": "sie",
            }.get(pronoun, pronoun)
            answer_text = f"{answer_pronoun} {answer_text}"
        units.append({
            "unit_key": key,
            "stage": stage,
            "stage_name": stage_name,
            "verb_order": verb_order,
            "pronoun_order": index if pronouns else -1,
            "exercise_order": 0,
            "verb": verb,
            "answer": answer_text,
            "prompt": prompt,
        })


def build_units(data=None, conn=None):
    """Create stable units from the source object without random selection.

    Core verbs have an explicit, reviewable order. The remaining source
    inventory is appended in its committed source order, which is stable and
    never selected by score, database IDs, or randomness.
    """
    data = data or load_source(conn)
    core = [(verb, data[verb]) for verb in CORE_VERBS if verb in data]
    core_names = {verb for verb, _ in core}
    expansion = [(verb, record) for verb, record in data.items() if verb not in core_names]
    verbs = core + expansion
    units = []
    for stage, stage_name in STAGES:
        if stage == 1:
            for index, (pronoun, english) in enumerate(PERSONAL_PRONOUNS):
                units.append({
                    "unit_key": f"1:pronoun:{index}",
                    "stage": stage,
                    "stage_name": stage_name,
                    "verb_order": -1,
                    "pronoun_order": index,
                    "exercise_order": index,
                    "verb": "personal pronouns",
                    "answer": pronoun,
                    "prompt": f"{stage_name} · English: {english}",
                    "daily_pronoun": True,
                })
            continue
        for verb_order, (verb, record) in enumerate(verbs):
            indikativ = record.get("indikativ") or {}
            konj1 = record.get("konjunktiv1") or {}
            konj2 = record.get("konjunktiv2") or {}
            passive = record.get("passiv") or {}
            if stage == 2:
                forms = [record.get("infinitiv")]
            elif stage == 3:
                forms = indikativ.get("praesens")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "praesens", PRONOUNS)
                continue
            elif stage == 4:
                if not _special_present(verb, record):
                    continue
                forms = indikativ.get("praesens")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "special_praesens", PRONOUNS)
                continue
            elif stage == 5:
                imperative = record.get("imperativ") or {}
                forms = [imperative.get(p) for p in IMPERATIVE_PRONOUNS]
                forms = [form for form in forms if form]
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "imperativ", IMPERATIVE_PRONOUNS[:len(forms)],
                           add_answer_pronoun=False)
                continue
            elif stage == 6:
                forms = [record.get("partizip2")]
            elif stage == 7:
                forms = [record.get("hilfsverb")]
            elif stage == 8:
                forms = indikativ.get("perfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "perfekt", PRONOUNS)
                continue
            elif stage == 9:
                forms = indikativ.get("praeteritum")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "praeteritum", PRONOUNS)
                continue
            elif stage == 10:
                forms = [record.get("zu_infinitiv")]
            elif stage == 11:
                forms = indikativ.get("plusquamperfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "plusquamperfekt", PRONOUNS)
                continue
            elif stage == 12:
                forms = indikativ.get("futur1")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "futur1", PRONOUNS)
                continue
            elif stage == 13:
                forms = konj2.get("praeteritum")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj2_praeteritum", PRONOUNS)
                continue
            elif stage == 14:
                # Haben has no useful passive paradigm for this curriculum;
                # its source forms would teach invalid constructions.
                if not passive or verb == "haben":
                    continue
                forms = list(passive.get("praesens") or []) + list(passive.get("praeteritum") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "passive_present_past", PRONOUNS * 2)
                continue
            elif stage == 15:
                forms = konj2.get("plusquamperfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj2_plusquamperfekt", PRONOUNS)
                continue
            elif stage == 16:
                if not passive or verb == "haben":
                    continue
                forms = list(passive.get("perfekt") or []) + list(passive.get("plusquamperfekt") or []) + list(passive.get("futur1") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "passive_advanced", PRONOUNS * 3)
                continue
            elif stage == 17:
                forms = list(konj1.get("praesens") or []) + list(konj1.get("perfekt") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj1_present_perfect", PRONOUNS * 2)
                continue
            elif stage == 18:
                forms = indikativ.get("futur2")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "futur2", PRONOUNS)
                continue
            elif stage == 19:
                forms = (list(konj1.get("futur1") or []) + list(konj1.get("futur2") or [])
                         + list(konj2.get("futur1") or []) + list(konj2.get("futur2") or []))
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj_future", PRONOUNS * 4)
                continue
            else:
                forms = [record.get("partizip1")]
            if forms and forms[0]:
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "infinitive" if stage == 2 else stage_name, None)
    return units


def ensure_table(conn, user):
    table = table_name(user)
    conn.execute(f'''CREATE TABLE IF NOT EXISTS "{table}" (
        unit_key TEXT PRIMARY KEY,
        score REAL NOT NULL DEFAULT 0.0,
        leitner_box INTEGER,
        completed INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        incorrect INTEGER NOT NULL DEFAULT 0,
        times_practiced INTEGER NOT NULL DEFAULT 0,
        times_correct INTEGER NOT NULL DEFAULT 0,
        times_incorrect INTEGER NOT NULL DEFAULT 0,
        times_drilled INTEGER NOT NULL DEFAULT 0,
        times_mastered INTEGER NOT NULL DEFAULT 0,
        drill_pending INTEGER NOT NULL DEFAULT 0,
        last_decay_at DATE,
        last_practiced TEXT
    )''')
    box_column = next(
        row for row in conn.execute(f'PRAGMA table_info("{table}")')
        if row[1] == 'leitner_box'
    )
    if box_column[3]:
        legacy = f"{table}_legacy_boxes"
        conn.execute(f'DROP TABLE IF EXISTS "{legacy}"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')
        conn.execute(f'''CREATE TABLE "{table}" (
            unit_key TEXT PRIMARY KEY,
            score REAL NOT NULL DEFAULT 0.0,
            leitner_box INTEGER,
            completed INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            incorrect INTEGER NOT NULL DEFAULT 0,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            drill_pending INTEGER NOT NULL DEFAULT 0,
            last_decay_at DATE,
            last_practiced TEXT
        )''')
        columns = (
            'unit_key, score, leitner_box, completed, attempts, incorrect, '
            'times_practiced, times_correct, times_incorrect, times_drilled, '
            'times_mastered, drill_pending, last_decay_at, last_practiced'
        )
        conn.execute(
            f'INSERT INTO "{table}" ({columns}) '
            f'SELECT unit_key, score, CASE WHEN score >= 9 THEN MIN(leitner_box, 10) END, '
            f'completed, attempts, incorrect, times_practiced, times_correct, '
            f'times_incorrect, times_drilled, times_mastered, drill_pending, '
            f'last_decay_at, last_practiced FROM "{legacy}"'
        )
        conn.execute(f'DROP TABLE "{legacy}"')
    return table


def sync(conn, user):
    table = ensure_table(conn, user)
    units = build_units(conn=conn)
    seen = set()
    for unit in units:
        seen.add(unit["unit_key"])
        conn.execute(f'INSERT OR IGNORE INTO "{table}" (unit_key) VALUES (?)', (unit['unit_key'],))
    if seen:
        placeholders = ",".join("?" for _ in seen)
        # Remove retired curriculum units while retaining valid progress.
        conn.execute(f'DELETE FROM "{table}" WHERE unit_key NOT IN ({placeholders})', tuple(seen))
    conn.commit()
    return table


def next_units(conn, user, limit=16, drill_mode=False, known_drill_mode=False):
    """Select daily pronouns, due reviews, then the current learning stage."""
    table = ensure_table(conn, user)
    units = {unit['unit_key']: unit for unit in build_units()}
    state = {
        row[0]: {
            'score': row[1], 'leitner_box': row[2], 'completed': row[3],
            'last_practiced': row[4], 'incorrect': row[5],
            'times_practiced': row[6],
        }
        for row in conn.execute(
            f'SELECT unit_key, score, leitner_box, completed, last_practiced, '
            f'incorrect, times_practiced FROM "{table}"'
        )
    }
    today = date.today()
    daily = [
        (unit, state[key]) for key, unit in units.items()
        if unit['stage'] == 1
        and state.get(key)
        and str(state[key]['last_practiced'] or '')[:10] != today.isoformat()
    ]
    daily.sort(key=lambda item: item[0]['pronoun_order'])

    non_pronouns = [
        (unit, state[key]) for key, unit in units.items()
        if unit['stage'] != 1 and state.get(key)
    ]
    if drill_mode:
        selected = [item for item in non_pronouns if item[1]['incorrect'] > 0]
        selected.sort(key=lambda item: (
            -item[1]['incorrect'], item[1]['last_practiced'] or '',
            item[0]['stage'], item[0]['verb_order'], item[0]['pronoun_order'],
        ))
    elif known_drill_mode:
        selected = [
            item for item in non_pronouns
            if item[1]['score'] >= 9 and item[1]['times_practiced'] > 0
        ]
        selected.sort(key=lambda item: (
            item[1]['last_practiced'] or '', item[0]['stage'],
            item[0]['verb_order'], item[0]['pronoun_order'],
        ))
    else:
        due = []
        for unit, progress in non_pronouns:
            if progress['score'] < 9 or not progress['last_practiced']:
                continue
            last_day = date.fromisoformat(str(progress['last_practiced'])[:10])
            interval = LEITNER_INTERVALS.get(progress['leitner_box'] or 1, 1)
            if (today - last_day).days >= interval:
                due.append((unit, progress))
        due.sort(key=lambda item: (
            item[1]['last_practiced'], item[0]['stage'],
            item[0]['verb_order'], item[0]['pronoun_order'],
        ))

        incomplete = [item for item in non_pronouns if item[1]['score'] < 9]
        current = []
        if incomplete:
            stage = min(unit['stage'] for unit, _ in incomplete)
            stage_units = [item for item in incomplete if item[0]['stage'] == stage]
            pronoun_order = min(unit['pronoun_order'] for unit, _ in stage_units)
            current = [
                item for item in stage_units
                if item[0]['pronoun_order'] == pronoun_order
            ]
            current.sort(key=lambda item: (
                item[0]['verb_order'], item[0]['exercise_order'],
                item[0]['unit_key'],
            ))
        selected = due + current

    remaining = max(0, limit - len(daily))
    chosen = selected[:remaining]
    if not (drill_mode or known_drill_mode):
        chosen.sort(key=lambda item: (
            -item[1]['score'], item[0]['stage'], item[0]['pronoun_order'],
            item[0]['verb_order'], item[0]['exercise_order'],
        ))
    return [
        {**unit, **progress} for unit, progress in daily + chosen
    ]


def record_attempt(conn, user, unit_key, correct):
    table = ensure_table(conn, user)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(f'''UPDATE "{table}" SET attempts = attempts + 1,
        incorrect = incorrect + ?,
        last_practiced = ? WHERE unit_key = ?''',
        (0 if correct else 1, now, unit_key))
    conn.commit()


def mark_completed(conn, user, unit_key):
    table = ensure_table(conn, user)
    conn.execute(f'UPDATE "{table}" SET completed = 1 WHERE unit_key = ?', (unit_key,))
    conn.commit()


def progress(conn, user):
    table = ensure_table(conn, user)
    units = build_units()
    completed = {row[0] for row in conn.execute(f'SELECT unit_key FROM "{table}" WHERE completed = 1')}
    pending = [
        unit['stage'] for unit in units
        if unit['stage'] != 1 and unit['unit_key'] not in completed
    ]
    return {"current_stage": min(pending) if pending else 20, "total": len(units),
            "completed": len(completed), "stages": STAGES}
