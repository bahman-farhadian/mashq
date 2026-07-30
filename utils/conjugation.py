"""Deterministic German conjugation curriculum and progress storage."""

import json
import os
import random
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
    (7, "Indikativ Perfekt"),
    (8, "Indikativ Präteritum"),
    (9, "zu-infinitive"),
    (10, "Indikativ Plusquamperfekt"),
    (11, "Indikativ Futur I"),
    (12, "Konjunktiv II Präteritum"),
    (13, "Passive Präsens and Präteritum"),
    (14, "Konjunktiv II Plusquamperfekt"),
    (15, "Passive Perfekt and advanced passive tenses"),
    (16, "Konjunktiv I Präsens and Perfekt"),
    (17, "Indikativ Futur II"),
    (18, "Konjunktiv I/II future forms"),
    (19, "Partizip I"),
    (20, "Curriculum Mastery"),
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

    # Extract English translation of verb according to data/schemas/german_conjugations.schema.json
    english_meaning = records.get("translation")
    if not english_meaning:
        english_list = (records.get("english", {}).get("praesens") if isinstance(records.get("english"), dict) else []) or []
        english_meaning = english_list[0] if english_list else verb
    verb_label = f"{verb} (meaning: {english_meaning})"

    for index, answer in enumerate(forms):
        pronoun = pronouns[index] if pronouns else ""
        answer_text = str(answer)

        if prompt_name == "infinitive":
            prompt = f"{stage_name} · English: {english_meaning}"
            key = f"{stage}:{verb_order}:{index}:infinitive"
            units.append({
                "unit_key": key,
                "stage": stage,
                "stage_name": stage_name,
                "verb_order": verb_order,
                "pronoun_order": -1,
                "exercise_order": 0,
                "verb": english_meaning,
                "answer": answer_text,
                "prompt": prompt,
            })
            continue

        if pronoun == "er, sie, es":
            # Separate er, sie, es into 3 separate questions with explicit note
            singular_pronouns = (
                ("er", "er (note: form works for er/sie/es)", 0),
                ("sie", "sie (note: form works for er/sie/es)", 1),
                ("es", "es (note: form works for er/sie/es)", 2),
            )
            for pronoun_sub, prompt_note, sub_idx in singular_pronouns:
                key = f"{stage}:{verb_order}:{index}:{pronoun_sub}:{prompt_name}"
                full_answer = f"{pronoun_sub} {answer_text}" if add_answer_pronoun else answer_text
                prompt = f"{stage_name} · Verb: {verb_label} · {prompt_note}"
                units.append({
                    "unit_key": key,
                    "stage": stage,
                    "stage_name": stage_name,
                    "verb_order": verb_order,
                    "pronoun_order": index * 10 + sub_idx,
                    "exercise_order": sub_idx,
                    "verb": verb,
                    "answer": full_answer,
                    "prompt": prompt,
                })
        elif pronoun == "sie, Sie":
            # Separate 3rd person plural and formal into 2 separate questions
            plural_pronouns = (
                ("sie", "sie (they)", 0),
                ("Sie", "Sie (you, formal)", 1),
            )
            for pronoun_sub, prompt_note, sub_idx in plural_pronouns:
                key = f"{stage}:{verb_order}:{index}:{pronoun_sub}:{prompt_name}"
                full_answer = f"{pronoun_sub} {answer_text}" if add_answer_pronoun else answer_text
                prompt = f"{stage_name} · Verb: {verb_label} · {prompt_note}"
                units.append({
                    "unit_key": key,
                    "stage": stage,
                    "stage_name": stage_name,
                    "verb_order": verb_order,
                    "pronoun_order": index * 10 + sub_idx,
                    "exercise_order": sub_idx,
                    "verb": verb,
                    "answer": full_answer,
                    "prompt": prompt,
                })
        else:
            key = f"{stage}:{verb_order}:{index}:{prompt_name}"
            full_answer = f"{pronoun} {answer_text}".strip() if (add_answer_pronoun and pronoun in PRONOUNS) else answer_text
            prompt_str = f"{stage_name} · Verb: {verb_label}"
            if pronoun:
                prompt_str += f" · {pronoun}"
            units.append({
                "unit_key": key,
                "stage": stage,
                "stage_name": stage_name,
                "verb_order": verb_order,
                "pronoun_order": index * 10 if pronouns else -1,
                "exercise_order": 0,
                "verb": verb,
                "answer": full_answer,
                "prompt": prompt_str,
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
                forms = [imperative.get(pronoun) for pronoun in IMPERATIVE_PRONOUNS]
                forms = [form for form in forms if form]
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "imperativ", IMPERATIVE_PRONOUNS[:len(forms)],
                           add_answer_pronoun=False)
                continue
            elif stage == 6:
                forms = [record.get("partizip2")]
            elif stage == 7:
                forms = indikativ.get("perfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "perfekt", PRONOUNS)
                continue
            elif stage == 8:
                forms = indikativ.get("praeteritum")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "praeteritum", PRONOUNS)
                continue
            elif stage == 9:
                forms = [record.get("zu_infinitiv")]
            elif stage == 10:
                forms = indikativ.get("plusquamperfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "plusquamperfekt", PRONOUNS)
                continue
            elif stage == 11:
                forms = indikativ.get("futur1")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "futur1", PRONOUNS)
                continue
            elif stage == 12:
                forms = konj2.get("praeteritum")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj2_praeteritum", PRONOUNS)
                continue
            elif stage == 13:
                if not passive or verb == "haben":
                    continue
                forms = list(passive.get("praesens") or []) + list(passive.get("praeteritum") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "passive_present_past", PRONOUNS * 2)
                continue
            elif stage == 14:
                forms = konj2.get("plusquamperfekt")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj2_plusquamperfekt", PRONOUNS)
                continue
            elif stage == 15:
                if not passive or verb == "haben":
                    continue
                forms = list(passive.get("perfekt") or []) + list(passive.get("plusquamperfekt") or []) + list(passive.get("futur1") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "passive_advanced", PRONOUNS * 3)
                continue
            elif stage == 16:
                forms = list(konj1.get("praesens") or []) + list(konj1.get("perfekt") or [])
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj1_present_perfect", PRONOUNS * 2)
                continue
            elif stage == 17:
                forms = indikativ.get("futur2")
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "futur2", PRONOUNS)
                continue
            elif stage == 18:
                forms = (list(konj1.get("futur1") or []) + list(konj1.get("futur2") or [])
                         + list(konj2.get("futur1") or []) + list(konj2.get("futur2") or []))
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "konj_future", PRONOUNS * 4)
                continue
            elif stage == 19:
                forms = [record.get("partizip1")]
            else:
                forms = [record.get("infinitiv")]
            if forms and forms[0]:
                _add_units(units, stage, stage_name, verb_order, verb, record,
                           forms, "infinitive" if stage in (2, 20) else stage_name, None)
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
        daily_pronoun_done INTEGER NOT NULL DEFAULT 0,
        last_pronoun_date DATE,
        last_decay_at DATE,
        last_practiced TEXT
    )''')
    existing_columns = {
        row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
    }
    if 'daily_pronoun_done' not in existing_columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN daily_pronoun_done INTEGER NOT NULL DEFAULT 0')
    if 'last_pronoun_date' not in existing_columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_pronoun_date DATE')

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
            daily_pronoun_done INTEGER NOT NULL DEFAULT 0,
            last_pronoun_date DATE,
            last_decay_at DATE,
            last_practiced TEXT
        )''')
        columns = (
            'unit_key, score, leitner_box, completed, attempts, incorrect, '
            'times_practiced, times_correct, times_incorrect, times_drilled, '
            'times_mastered, drill_pending, daily_pronoun_done, last_pronoun_date, last_decay_at, last_practiced'
        )
        conn.execute(
            f'INSERT INTO "{table}" ({columns}) '
            f'SELECT unit_key, score, CASE WHEN score >= 9 THEN MIN(leitner_box, 10) END, '
            f'completed, attempts, incorrect, times_practiced, times_correct, '
            f'times_incorrect, times_drilled, times_mastered, drill_pending, '
            f'0, NULL, last_decay_at, last_practiced FROM "{legacy}"'
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


def next_units(conn, user, limit=16, drill_mode=False, known_drill_mode=False, stage=None):
    """Select units for a SINGLE learning stage per session.

    Never mix multiple stages in a single session.
    Same-score verbs are randomized per session to prevent repetitive static ordering.
    Returns a list of (unit_dict, progress_dict) tuples.
    """
    table = ensure_table(conn, user)
    all_units_by_key = {unit['unit_key']: unit for unit in build_units()}
    user_progress_map = {
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
    today_date = date.today()
    today_iso = today_date.isoformat()

    default_progress = {
        'score': 0.0,
        'leitner_box': None,
        'completed': 0,
        'last_practiced': None,
        'incorrect': 0,
        'times_practiced': 0,
    }

    # Determine target stage for this session
    if stage is not None:
        target_stage = stage
    else:
        # Check if user has completed all Stage 1 pronouns today
        pronoun_count_today = conn.execute(
            f'SELECT COUNT(DISTINCT unit_key) FROM "{table}" WHERE unit_key LIKE "1:pronoun:%" AND last_practiced = ?',
            (today_iso,)
        ).fetchone()
        pronouns_done_today = bool(pronoun_count_today and pronoun_count_today[0] >= len(PERSONAL_PRONOUNS))

        if not pronouns_done_today:
            target_stage = 1
        else:
            # Find the lowest stage (>= 2) with incomplete units
            incomplete = [
                unit['stage'] for key, unit in all_units_by_key.items()
                if unit['stage'] != 1 and user_progress_map.get(key, default_progress)['score'] < 9
            ]
            target_stage = min(incomplete) if incomplete else 20

    # Filter units strictly to target_stage ONLY (no mixing of stages)
    stage_units_map = {
        key: unit for key, unit in all_units_by_key.items()
        if unit['stage'] == target_stage
    }

    # For empty progress, return unpracticed stage units
    if not user_progress_map:
        stage_units_list = list(stage_units_map.values())
        if target_stage == 1:
            stage_units_list.sort(key=lambda u: u['pronoun_order'])
        else:
            stage_units_list.sort(key=lambda u: (u['verb_order'], u['pronoun_order'], u['exercise_order']))
        return [(unit, default_progress.copy()) for unit in stage_units_list[:limit]]

    stage_candidates = [
        (unit, user_progress_map.get(unit_key, default_progress.copy()))
        for unit_key, unit in stage_units_map.items()
    ]

    if drill_mode:
        selected = [item for item in stage_candidates if item[1]['incorrect'] > 0]
        selected.sort(key=lambda item: (
            -item[1]['incorrect'], item[1]['last_practiced'] or '',
            item[0]['verb_order'], item[0]['pronoun_order'],
        ))
    elif known_drill_mode:
        selected = [
            item for item in stage_candidates
            if item[1]['score'] >= 9 and item[1]['times_practiced'] > 0
        ]
        selected.sort(key=lambda item: (
            item[1]['last_practiced'] or '',
            item[0]['verb_order'], item[0]['pronoun_order'],
        ))
    else:
        due_reviews = []
        for unit, progress_dict in stage_candidates:
            if progress_dict['score'] < 9 or not progress_dict['last_practiced']:
                continue
            last_day = date.fromisoformat(str(progress_dict['last_practiced'])[:10])
            interval = LEITNER_INTERVALS.get(progress_dict['leitner_box'] or 1, 1)
            if (today_date - last_day).days >= interval:
                due_reviews.append((unit, progress_dict))
        due_reviews.sort(key=lambda item: (
            item[1]['last_practiced'],
            item[0]['verb_order'], item[0]['pronoun_order'],
        ))

        incomplete = [item for item in stage_candidates if item[1]['score'] < 9]
        if target_stage == 1:
            incomplete.sort(key=lambda item: item[0]['pronoun_order'])
        else:
            # Sort incomplete items strictly by curriculum verb_order, then pronoun_order, then exercise_order
            incomplete.sort(key=lambda item: (
                item[0]['verb_order'],
                item[0]['pronoun_order'],
                item[0]['exercise_order'],
                item[0]['unit_key'],
            ))
        selected = due_reviews + incomplete

    return selected[:limit]


from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple, Any

# Shared scoring constants (mirrored from tartarus.py)
SCORE_DELTA = 0.5
MAX_SCORE = 9.0
MAX_BOX = 10
LEITNER_INTERVALS = {box: box for box in range(1, 11)}
RESULT_COUNTERS = {
    'correct': 'times_correct',
    'incorrect': 'times_incorrect',
    'drilled': 'times_drilled',
    'mastered': 'times_mastered',
    'flagged': 'times_flagged',
}


def score_band(score):
    """Return score band: 0=Learning, 1=Familiar, 2=Mastered."""
    if score < 4.0:
        return 0
    if score < 9.0:
        return 1
    return 2


def _corrects_to_mastery(score, sentence_mode=False):
    """Calculate remaining correct answers needed to reach 9.0."""
    if score >= 9.0:
        return 0
    remaining = 9.0 - score
    return int(round(remaining / SCORE_DELTA))


def corrects_to_mastery(score, sentence_mode=False):
    """Public version for external use."""
    return _corrects_to_mastery(score, sentence_mode)


def update_unit_score(conn, user, unit_key, result_status, current_score=None, current_box=None):
    """Apply the shared half-point score and ten-box learning contract for conjugation units."""
    table = table_name(user)
    max_box = MAX_BOX
    today_iso = date.today().isoformat()

    row = conn.execute(
        f'SELECT last_practiced, leitner_box FROM "{table}" WHERE unit_key = ?', (unit_key,)
    ).fetchone()
    stored_last_practiced = row[0] if row else None
    current_box = row[1] if row else current_box
    practiced_today = (stored_last_practiced == today_iso)

    preserve_box_timestamp = False

    if result_status == 'correct':
        current_score = float(current_score or 0)
        new_score = min(MAX_SCORE, current_score + SCORE_DELTA)
        just_mastered = (current_score < MAX_SCORE) and (new_score >= MAX_SCORE)
        if just_mastered:
            new_box = 1
        elif current_score >= MAX_SCORE:
            if practiced_today:
                new_box = current_box or 1
                preserve_box_timestamp = True
            else:
                new_box = min((current_box or 1) + 1, max_box)
        else:
            new_box = None
    elif result_status == 'incorrect':
        new_score = float(current_score)
        # A failed first attempt on a scheduled review preserves the score but
        # shortens the next interval by one box. Same-day drill attempts do
        # not keep pushing the item down.
        new_box = max(1, (current_box or 1) - 1) if (
            current_score >= MAX_SCORE and not practiced_today and (current_box or 1) > 1
        ) else (current_box if current_score >= MAX_SCORE else None)
    else:
        new_score = MAX_SCORE if result_status == 'mastered' else float(current_score or 0)
        new_box = {
            'mastered': 1,
            'flagged': current_box if current_score and current_score >= MAX_SCORE else None,
            'drilled': current_box if current_score and current_score >= MAX_SCORE else None,
        }[result_status]

    counter = RESULT_COUNTERS.get(result_status)
    if new_box is not None and not preserve_box_timestamp:
        set_clauses = ['score = ?', 'leitner_box = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, new_box, today_iso, today_iso]
    elif preserve_box_timestamp:
        # Same-day re-practice of an already-mastered unit: bump counters only.
        # Do NOT touch leitner_box, last_practiced or last_decay_at.
        set_clauses = ['score = ?', 'times_practiced = times_practiced + 1']
        params = [new_score]
    else:
        set_clauses = ['score = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, today_iso, today_iso]
    if counter:
        set_clauses.append(f'{counter} = {counter} + 1')
    if result_status == 'incorrect':
        set_clauses.append('drill_pending = 1')
    elif result_status == 'drilled':
        set_clauses.append('drill_pending = 0')
    # Conjugation-specific counters
    if result_status in {'correct', 'incorrect'}:
        set_clauses.append('attempts = attempts + 1')
        if result_status == 'incorrect':
            set_clauses.append('incorrect = incorrect + 1')
    params.append(unit_key)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE unit_key = ?', params)
    if new_score >= MAX_SCORE:
        conn.execute(f'UPDATE "{table}" SET completed = 1 WHERE unit_key = ?', (unit_key,))
    conn.commit()

    # Check if all pronouns completed today (for mandatory pronoun practice)
    if result_status == 'correct':
        _check_and_update_pronoun_completion(conn, user, today_iso)


def _check_and_update_pronoun_completion(conn, user, today_iso):
    """Check if all pronouns have been completed today, and if so, mark daily_pronoun_done."""
    table = table_name(user)
    cursor = conn.execute(
        f'SELECT COUNT(DISTINCT unit_key) FROM "{table}" WHERE unit_key LIKE "1:pronoun:%" AND last_practiced = ?',
        (today_iso,)
    )
    row = cursor.fetchone()
    practiced_count = row[0] if row else 0
    if practiced_count >= len(PERSONAL_PRONOUNS):
        conn.execute(
            f'UPDATE "{table}" SET daily_pronoun_done = 1, last_pronoun_date = ?',
            (today_iso,)
        )
        conn.commit()


def record_attempt(conn, user, unit_key, correct):
    table = ensure_table(conn, user)
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn.execute(f'''UPDATE "{table}" SET attempts = attempts + 1,
        incorrect = incorrect + ?,
        last_practiced = ? WHERE unit_key = ?''',
        (0 if correct else 1, now_iso, unit_key))
    conn.commit()


def mark_completed(conn, user, unit_key):
    table = ensure_table(conn, user)
    conn.execute(f'UPDATE "{table}" SET completed = 1 WHERE unit_key = ?', (unit_key,))
    conn.commit()


def progress(conn, user):
    table = ensure_table(conn, user)
    all_units = build_units()
    completed_unit_keys = {
        row[0] for row in conn.execute(f'SELECT unit_key FROM "{table}" WHERE completed = 1')
    }
    pending_stages = [
        unit['stage'] for unit in all_units
        if unit['stage'] != 1 and unit['unit_key'] not in completed_unit_keys
    ]
    current_stage = min(pending_stages) if pending_stages else 20
    return {
        "current_stage": current_stage,
        "total": len(all_units),
        "completed": len(completed_unit_keys),
        "stages": STAGES,
    }


def seed_stage_users(conn):
    """Create 20 dedicated stage test users (stage_01_user through stage_20_user)."""
    from tartarus import ensure_user
    today_iso = date.today().isoformat()
    all_units = build_units(conn=conn)
    
    for stage_number in range(1, 21):
        username = f"stage_{stage_number:02d}_user"
        ensure_user(conn, username)
        table = sync(conn, username)
        
        # Reset all units to initial unpracticed state
        conn.execute(
            f'UPDATE "{table}" SET score = 0.0, leitner_box = NULL, completed = 0, '
            f'attempts = 0, incorrect = 0, times_practiced = 0, times_correct = 0, '
            f'times_incorrect = 0, times_drilled = 0, times_mastered = 0, '
            f'drill_pending = 0, daily_pronoun_done = 0, last_pronoun_date = NULL, last_practiced = NULL'
        )
        
        if stage_number > 1:
            earlier_unit_keys = [unit['unit_key'] for unit in all_units if unit['stage'] < stage_number]
            if earlier_unit_keys:
                placeholders = ','.join('?' for _ in earlier_unit_keys)
                conn.execute(
                    f'UPDATE "{table}" SET score = 9.0, leitner_box = 1, completed = 1, '
                    f'last_practiced = ? WHERE unit_key IN ({placeholders})',
                    [today_iso] + earlier_unit_keys
                )
            conn.execute(
                f'UPDATE "{table}" SET daily_pronoun_done = 1, last_pronoun_date = ?',
                (today_iso,)
            )
        conn.commit()
