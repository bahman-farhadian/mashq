"""Deterministic German conjugation curriculum and progress storage."""

import json
import os
from datetime import datetime


PRONOUNS = ("ich", "du", "er, sie, es", "wir", "ihr", "sie, Sie")
IMPERATIVE_PRONOUNS = ("du", "ihr", "Sie", "wir")
# Conjugation units have one Leitner box per curriculum stage. The intervals
# are deterministic and intentionally separate from Tartarus's five-box lists.
LEITNER_INTERVALS = {
    box: interval for box, interval in enumerate(
        (1, 2, 4, 7, 14, 21, 30, 45, 60, 90,
         120, 150, 180, 240, 300, 365, 450, 540, 630, 730), 1
    )
}
SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "word_lists", "german", "conjugations.json"
)

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
    return (lang or "").lower() == "german_conjugations"


def table_name(user):
    from tartarus import sanitize_name
    return f"conjugations_{sanitize_name(user, 'user')}"


def due_interval_case(column='leitner_box'):
    """Return the SQL CASE expression for the twenty conjugation boxes."""
    cases = ' '.join(
        f'WHEN {box} THEN {days}'
        for box, days in LEITNER_INTERVALS.items()
    )
    return f'CASE {column} {cases} ELSE {LEITNER_INTERVALS[20]} END'


def load_source():
    with open(SOURCE_PATH, encoding="utf-8") as source:
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
               prompt_name, pronouns=None):
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
        if pronoun in PRONOUNS:
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


def build_units(data=None):
    """Create stable units from the source object without random selection.

    Core verbs have an explicit, reviewable order. The remaining source
    inventory is appended in its committed source order, which is stable and
    never selected by score, database IDs, or randomness.
    """
    data = data or load_source()
    core = [(verb, data[verb]) for verb in CORE_VERBS if verb in data]
    core_names = {verb for verb, _ in core}
    expansion = [(verb, record) for verb, record in data.items() if verb not in core_names]
    verbs = core + expansion
    units = []
    for stage, stage_name in STAGES:
        if stage == 1:
            # Pronouns provide context for person-dependent forms; they are
            # not standalone questions in the conjugation track.
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
                           forms, "imperativ", IMPERATIVE_PRONOUNS[:len(forms)])
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
                if not passive:
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
                if not passive:
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
        stage INTEGER NOT NULL,
        stage_name TEXT NOT NULL,
        verb_order INTEGER NOT NULL,
        pronoun_order INTEGER NOT NULL,
        exercise_order INTEGER NOT NULL,
        verb TEXT NOT NULL,
        answer TEXT NOT NULL,
        prompt TEXT NOT NULL,
        score REAL NOT NULL DEFAULT 1.0,
        leitner_box INTEGER NOT NULL DEFAULT 1,
        completed INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        incorrect INTEGER NOT NULL DEFAULT 0,
        times_practiced INTEGER NOT NULL DEFAULT 0,
        times_correct INTEGER NOT NULL DEFAULT 0,
        times_incorrect INTEGER NOT NULL DEFAULT 0,
        times_drilled INTEGER NOT NULL DEFAULT 0,
        times_mastered INTEGER NOT NULL DEFAULT 0,
        last_decay_at DATE,
        last_practiced TEXT
    )''')
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    additions = {
        'score': 'REAL NOT NULL DEFAULT 1.0',
        'leitner_box': 'INTEGER NOT NULL DEFAULT 1',
        'times_practiced': 'INTEGER NOT NULL DEFAULT 0',
        'times_correct': 'INTEGER NOT NULL DEFAULT 0',
        'times_incorrect': 'INTEGER NOT NULL DEFAULT 0',
        'times_drilled': 'INTEGER NOT NULL DEFAULT 0',
        'times_mastered': 'INTEGER NOT NULL DEFAULT 0',
        'last_decay_at': 'DATE',
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {name} {definition}')
    return table


def sync(conn, user):
    table = ensure_table(conn, user)
    units = build_units()
    seen = set()
    for unit in units:
        seen.add(unit["unit_key"])
        conn.execute(f'''INSERT INTO "{table}"
            (unit_key, stage, stage_name, verb_order, pronoun_order,
             exercise_order, verb, answer, prompt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_key) DO UPDATE SET
              stage_name=excluded.stage_name, verb=excluded.verb,
              answer=excluded.answer, prompt=excluded.prompt''',
            (unit["unit_key"], unit["stage"], unit["stage_name"],
             unit["verb_order"], unit["pronoun_order"], unit["exercise_order"],
             unit["verb"], unit["answer"], unit["prompt"]))
    if seen:
        placeholders = ",".join("?" for _ in seen)
        # Remove units retired from the curriculum, such as the old
        # standalone-pronoun stage. Valid units retain their progress.
        conn.execute(f'DELETE FROM "{table}" WHERE unit_key NOT IN ({placeholders})', tuple(seen))
    conn.commit()
    return table


def next_units(conn, user, limit=16):
    table = ensure_table(conn, user)
    due_query = f'''SELECT unit_key, stage, stage_name, verb_order,
            pronoun_order, verb, answer, prompt, score, leitner_box, completed
            FROM "{table}" WHERE completed = 1 AND (
            last_practiced IS NULL OR
              julianday('now', 'localtime') - julianday(last_practiced) >=
              {due_interval_case()}
            ) ORDER BY last_practiced, stage, verb_order, pronoun_order,
                     exercise_order, unit_key LIMIT ?'''
    due_rows = conn.execute(due_query, (limit,)).fetchall()
    if due_rows:
        rows = due_rows
        return [dict(zip(("unit_key", "stage", "stage_name", "verb_order",
                          "pronoun_order", "verb", "answer", "prompt", "score",
                          "leitner_box", "completed"), row)) for row in rows]
    current = conn.execute(f'SELECT MIN(stage) FROM "{table}" WHERE completed = 0').fetchone()[0]
    if current is not None:
        # Person-dependent stages unlock one pronoun at a time. Completed
        # earlier pronouns remain eligible through the due-review query above,
        # but new material must stay on the first incomplete pronoun.
        current_pronoun = conn.execute(
            f'''SELECT MIN(pronoun_order) FROM "{table}"
                WHERE completed = 0 AND stage = ?''', (current,)
        ).fetchone()[0]
        rows = conn.execute(f'''SELECT unit_key, stage, stage_name, verb_order,
                pronoun_order, verb, answer, prompt, score, leitner_box, completed
                FROM "{table}" WHERE completed = 0 AND stage = ?
                AND pronoun_order = ?
                ORDER BY stage, verb_order, pronoun_order, exercise_order, unit_key
                LIMIT ?''', (current, current_pronoun, limit)).fetchall()
    else:
        rows = conn.execute(f'''SELECT unit_key, stage, stage_name, verb_order,
                pronoun_order, verb, answer, prompt, score, leitner_box, completed
                FROM "{table}" WHERE completed = 1
                ORDER BY COALESCE(last_practiced, '9999'), stage, verb_order,
                         pronoun_order, exercise_order, unit_key LIMIT ?''', (limit,)).fetchall()
    return [dict(zip(("unit_key", "stage", "stage_name", "verb_order",
                      "pronoun_order", "verb", "answer", "prompt", "score",
                      "leitner_box", "completed"), row))
            for row in rows]


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
    row = conn.execute(f'''SELECT MIN(stage), COUNT(*),
        SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END) FROM "{table}"''').fetchone()
    return {"current_stage": row[0] or 20, "total": row[1] or 0, "completed": row[2] or 0,
            "stages": STAGES}
