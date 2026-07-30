# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import time
import random
import sqlite3
import argparse
import subprocess
from datetime import date, datetime, timedelta
import conjugation

# --- Configuration ---
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
DATABASE_FILE = os.environ.get('TARTARUS_DB', os.path.join(DATA_DIR, 'tartarus.db'))
WORD_LISTS_DIR = os.path.join(DATA_DIR, 'word_lists')
NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')


class Colors:
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    CYAN = '\033[96m'
    BLUE = '\033[94m'  # Masculine (der)
    ENDC = '\033[0m'


def split_word_forms(word_text):
    """Splits a word entry on commas into its accepted forms (e.g. singular
    and plural), stripping surrounding whitespace from each form so that
    "a, b", "a,b" and "a , b" are all equivalent."""
    return [form.strip() for form in word_text.split(',') if form.strip()]


def answer_matches(answer, word_text, sentence_mode=False):
    """Checks a typed answer against every accepted form of a word,
    case-sensitively (comma-separated forms like "das Haus, die Häuser").
    Also accepts the full text with all forms typed out, e.g.
    "das Haus, die Häuser", however the commas/spacing are written.

    In sentence_mode, commas are part of the sentence and must NOT be treated
    as form separators — a simple case-sensitive full-string comparison is
    used instead."""
    if sentence_mode:
        return answer.strip() == word_text.strip()
    forms = [form.strip() for form in split_word_forms(word_text)]
    answer_forms = [form.strip() for form in split_word_forms(answer)]
    if len(answer_forms) == 1 and answer_forms[0] in forms:
        return True
    return sorted(answer_forms) == sorted(forms)


def mask_sentence(sentence, score):
    """Mask an answer progressively; score 0 is visible and score 8 is hidden."""
    if score <= 0:
        return sentence
    if score >= 8:
        visible_ratio = 0.0
    else:
        visible_ratio = max(0.15, 1.0 - (float(score) / 8.0))
    
    # Find all letter positions (a-z, A-Z, and unicode letters)
    letter_indices = [i for i, ch in enumerate(sentence) if ch.isalpha()]
    if not letter_indices:
        return sentence
    
    # Calculate how many letters to keep visible
    num_visible = 0 if visible_ratio == 0 else max(1, int(len(letter_indices) * visible_ratio))
    # A fresh sample prevents learners from memorizing one fixed mask.
    visible_indices = set(random.sample(letter_indices, num_visible))
    
    # Build masked sentence
    result = []
    for i, ch in enumerate(sentence):
        if ch.isalpha():
            result.append(ch if i in visible_indices else '_')
        else:
            result.append(ch)
    return ''.join(result)


def noun_form_hints(forms, score):
    """Mask both noun case forms with the current practice score."""
    if not forms:
        return None
    return {
        number: mask_sentence(forms[number], score)
        for number in ('singular', 'plural')
    }


def noun_answers_match(answers, forms):
    """Check the singular and plural answers for one German noun case."""
    return isinstance(answers, dict) and bool(forms) and all(
        str(answers.get(number, '')).strip() == forms[number]
        for number in ('singular', 'plural')
    )


def noun_audio_text(forms):
    """Build the German audio prompt for one noun case pair."""
    return '. '.join(forms[number] for number in ('singular', 'plural'))


def get_gender_color(word_text):
    """Returns ANSI color for a word based on its German article:
    der (masculine) -> blue, die (feminine) -> red, das (neuter) -> green.
    Words without an article (verbs, adjectives, other languages) -> green."""
    text_lower = word_text.lower()
    if text_lower.startswith("der "):
        return Colors.BLUE
    if text_lower.startswith("die "):
        return Colors.RED
    if text_lower.startswith("das "):
        return Colors.GREEN
    return Colors.GREEN


# Maps common --lang names/codes to the locale prefix 'say' voices use
# (e.g. "german" / "de" -> "de", matching voices like "de_DE").
LANGUAGE_LOCALES = {
    'english': 'en', 'en': 'en',
    'german': 'de', 'deutsch': 'de', 'de': 'de',
}

# Preferred 'say' voices per locale prefix, in order of quality. The first
# one found installed (via 'say -v ?') is used; if none are installed, falls
# back to the first voice matching the locale prefix (see voice_for_language).
VOICE_PREFERENCES = {
    'de': ['Anna (Premium)', 'Anna (Enhanced)', 'Anna'],
    'ja': ['Otoya (Enhanced)', 'Kyoko (Enhanced)', 'Otoya', 'Kyoko'],
}

_VOICE_CACHE = {}


# --- Helper Functions ---
def clear_screen():
    """Clears the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def voice_for_language(lang):
    """Finds an installed macOS 'say' voice for lang, if any.

    English uses the system default voice (no '-v' flag). Other languages
    prefer a voice from VOICE_PREFERENCES if one is installed, otherwise the
    first installed voice matching the locale prefix (e.g. "de_DE")."""
    lang_lower = lang.lower()
    locale_prefix = LANGUAGE_LOCALES.get(lang_lower) or LANGUAGE_LOCALES.get(lang_lower.split('_')[0])
    if not locale_prefix or locale_prefix == 'en':
        return None
    if locale_prefix not in _VOICE_CACHE:
        voice = None
        try:
            output = subprocess.run(['say', '-v', '?'], capture_output=True, text=True, timeout=5).stdout
            installed = []
            for line in output.splitlines():
                match = re.match(r'^(.+?)\s+([a-zA-Z]{2}_[a-zA-Z]{2})\s+#', line)
                if match:
                    installed.append((match.group(1).strip(), match.group(2).lower()))
            for preferred in VOICE_PREFERENCES.get(locale_prefix, []):
                if any(name == preferred for name, _ in installed):
                    voice = preferred
                    break
            if not voice:
                for name, locale in installed:
                    if locale.startswith(locale_prefix):
                        voice = name
                        break
        except Exception:
            voice = None
        _VOICE_CACHE[locale_prefix] = voice
    return _VOICE_CACHE[locale_prefix]


def speak(text, lang=None, block=True, wpm=128):
    """Pipes text to the macOS 'say' command, using a voice matching lang's
    locale if one is installed. block=True waits for speech to finish.
    wpm sets the speech rate in words per minute (default 128, clear
    for language learners)."""
    rate = str(int(wpm)) if wpm else '128'
    cmd = ['say', '-r', rate]
    if lang:
        voice = voice_for_language(lang)
        if voice:
            cmd += ['-v', voice]
    cmd.append(text)
    try:
        if block:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def sanitize_name(name, label):
    """Validates a user/language name for safe use in table and file names."""
    name = name.lower()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid {label} '{name}': only lowercase letters, digits, and underscores are allowed."
        )
    return name


# --- Database Helpers ---
def get_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    return conn


def ensure_user(conn, user):
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    user = sanitize_name(user, 'user')
    conn.execute('INSERT OR IGNORE INTO users(name) VALUES (?)', (user,))
    return user


def words_table_name(user, lang):
    return f"words_{sanitize_name(user, 'user')}_{sanitize_name(lang, 'language')}"


def practice_table_name(user, lang):
    """Return the progress table for ordinary or conjugation practice."""
    return conjugation.table_name(user) if conjugation.is_conjugation_list(lang) else words_table_name(user, lang)


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


def ensure_word_table(conn, user, lang):
    table = words_table_name(user, lang)
    schema = f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0.0,
            last_practiced DATE,
            last_decay_at DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            drill_pending INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            last_fast_review_at TEXT,
            last_known_review_at TEXT
        )
    '''
    conn.execute(schema)
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    migrate_legacy = 'text' in columns
    migrate_leitner = not migrate_legacy and any(
        row[1] == 'leitner_box' and row[3] for row in conn.execute(f'PRAGMA table_info("{table}")')
    )
    if migrate_legacy or migrate_leitner:
        legacy_table = f'{table}_legacy'
        conn.execute(f'DROP TABLE IF EXISTS "{legacy_table}"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy_table}"')
        conn.execute(schema)
        shared = [
            'score', 'last_practiced', 'last_decay_at', 'active',
            'times_practiced', 'times_correct', 'times_incorrect',
            'times_drilled', 'times_mastered', 'drill_pending', 'leitner_box',
            'last_fast_review_at', 'last_known_review_at',
        ]
        available = {row[1] for row in conn.execute(f'PRAGMA table_info("{legacy_table}")')}
        shared = [column for column in shared if column in available]
        content_id = "'legacy:' || id" if migrate_legacy else 'content_id'
        columns_sql = ', '.join(['content_id', *shared])
        values_sql = ', '.join([
            content_id,
            *('CASE WHEN score >= 9.0 THEN leitner_box ELSE NULL END' if column == 'leitner_box' else column for column in shared),
        ])
        conn.execute(
            f'INSERT INTO "{table}" ({columns_sql}) '
            f'SELECT {values_sql} FROM "{legacy_table}"'
        )
        conn.execute(f'DROP TABLE "{legacy_table}"')
    elif 'content_id' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN content_id TEXT')
        conn.execute(f"UPDATE \"{table}\" SET content_id = 'legacy:' || id WHERE content_id IS NULL")
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_content_id" ON "{table}" (content_id)')
    return table


def ensure_sessions_table(conn, user):
    table = sessions_table_name(user)
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            language TEXT NOT NULL,
            session_date DATE NOT NULL,
            duration_seconds INTEGER NOT NULL,
            words_practiced INTEGER NOT NULL,
            correct_count INTEGER NOT NULL,
            incorrect_count INTEGER NOT NULL,
            drilled_count INTEGER NOT NULL DEFAULT 0
        )
    ''')
    return table


# --- Word List Sync ---
def word_list_path(user, lang):
    """Resolve a user's list, then the categorized shared list.

    Shared lists live under ``data/word_lists/<language>/<kind>/`` while
    user-created lists remain at the word-list root for compatibility.
    """
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    if conjugation.is_conjugation_list(lang):
        return conjugation.SOURCE_PATH
    user_specific = os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")
    if os.path.isfile(user_specific):
        return user_specific

    legacy = os.path.join(WORD_LISTS_DIR, f"{lang}.json")
    if os.path.isfile(legacy):
        return legacy

    matches = []
    for root, _, names in os.walk(WORD_LISTS_DIR):
        if f'{lang}.json' in names:
            matches.append(os.path.join(root, f'{lang}.json'))
    if len(matches) == 1:
        return matches[0]
    return os.path.join(WORD_LISTS_DIR, f'{lang}.json')


def word_list_path_user_specific(user, lang):
    """Returns the user-specific word list path (for creating new lists)."""
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    return os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")


def is_read_only_sample_list(user, lang):
    """Return whether ``lang`` resolves to a bundled Tartarus sample."""
    path = word_list_path(user, lang)
    return os.path.basename(path).startswith('tartarus_sample_')


def sample_list_ids():
    """Return every bundled sample identifier."""
    return {
        os.path.splitext(name)[0]
        for _, _, names in os.walk(WORD_LISTS_DIR)
        for name in names
        if name.startswith('tartarus_sample_') and name.endswith('.json')
    }


def user_has_personal_material(user):
    """Return whether a user owns at least one vocabulary or sentence list."""
    user = sanitize_name(user, 'user')
    prefix = f'{user}_'
    try:
        names = os.listdir(WORD_LISTS_DIR)
    except FileNotFoundError:
        return False
    return any(name.startswith(prefix) and name.endswith('.json') for name in names)


def ensure_list_available(user, lang):
    """Reject bundled samples after the user creates personal material."""
    lang = sanitize_name(lang, 'language')
    if lang in sample_list_ids() and user_has_personal_material(user):
        raise ValueError(
            'Tartarus samples are disabled after personal material is created.'
        )


def retire_sample_material(user):
    """Remove a user's sample progress once personal material exists."""
    user = sanitize_name(user, 'user')
    if not user_has_personal_material(user):
        return
    samples = sorted(sample_list_ids())
    conn = get_connection()
    for lang in samples:
        conn.execute(f'DROP TABLE IF EXISTS "{words_table_name(user, lang)}"')
    conn.execute(f'DROP TABLE IF EXISTS "{conjugation.table_name(user)}"')
    sessions = ensure_sessions_table(conn, user)
    if samples:
        placeholders = ', '.join('?' for _ in samples)
        conn.execute(
            f'DELETE FROM "{sessions}" WHERE language IN ({placeholders})',
            samples,
        )
    conn.commit()
    conn.close()


def normalize_definition(definition):
    """Normalizes a definition (string, list of strings, or None) into newline-joined text."""
    if not definition:
        return ''
    if isinstance(definition, list):
        return '\n'.join(str(item).strip() for item in definition if str(item).strip())
    return str(definition).strip()


def normalize_word_frequency(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else 0


def apply_decay(conn, table):
    """
    Applies time-based decay: any active word not practiced for one or more
    days loses 1.0 score per idle day (floored at 1.0). This pulls neglected
    words back into easier question bands automatically.

    Mastered words (score >= 9.0) are exempt: they are governed by the Leitner
    spaced-repetition schedule, not by decay. Decaying them while they wait for
    their scheduled review would pull them back into easier bands before the
    review interval has elapsed, defeating the purpose of the box system.

    Leitner box integrity: any word with score < 9 must be in box 1. The box
    only advances on mastery (score reaching 9) and resets on an incorrect
    answer (which drops the score below 9). Since decay now only affects
    words already below 9, the box should already be 1 — but we enforce it
    here as a safety net to repair any stale boxes left over from the old
    decay code that lowered scores without resetting boxes.
    """
    # Scores are intentionally stable between answers.  Review eligibility is
    # governed only by the Leitner due date; idle time must not erase learning.
    return


def sync_word_list(user, lang, apply_score_decay=True):
    """Synchronize JSON material IDs to user progress rows only."""
    ensure_list_available(user, lang)
    if conjugation.is_conjugation_list(lang):
        conn = get_connection()
        ensure_sessions_table(conn, user)
        conjugation.sync(conn, user)
        conn.close()
        return
    path = word_list_path(user, lang)
    entries = load_practice_items(path)
    conn = get_connection()
    table = ensure_word_table(conn, user, lang)
    ensure_user(conn, user)
    ensure_sessions_table(conn, user)
    seen_ids = {entry['content_id'] for entry in entries}
    for entry in entries:
        conn.execute(
            f'INSERT OR IGNORE INTO "{table}" (content_id) VALUES (?)', (entry['content_id'],)
        )
    rows = conn.execute(f'SELECT id, content_id FROM "{table}"').fetchall()
    for row_id, content_id in rows:
        conn.execute(f'UPDATE "{table}" SET active = ? WHERE id = ?',
                     (int(content_id in seen_ids), row_id))

    conn.commit()
    conn.close()


NOUN_CASES = ('nominative', 'accusative', 'dative', 'genitive')


def load_practice_items(path):
    """Read JSON material and derive four case-pair items for every noun."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Word list not found: {path}")
    with open(path, encoding='utf-8') as source:
        records = json.load(source)
    if not isinstance(records, list):
        raise ValueError(f"Word list must be a JSON array: {path}")
    items = []
    seen_ids = set()
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Invalid record at {path}#{position + 1}")
        base_id = str(record.get('id', '')).strip()
        if not base_id:
            raise ValueError(f"Missing stable id at {path}#{position + 1}")
        frequency = normalize_word_frequency(record.get('word_frequency', record.get('frequency', 0)))
        definition = normalize_definition(record.get('definition', record.get('translation')))
        if all(f'{case}_singular' in record or f'{case}_plural' in record for case in NOUN_CASES):
            meanings = {
                'singular': normalize_definition(record.get('singular_definition', definition)),
                'plural': normalize_definition(record.get('plural_definition', definition)),
            }
            for case_index, case_name in enumerate(NOUN_CASES):
                forms = {}
                examples = []
                for number in ('singular', 'plural'):
                    form = str(record.get(f'{case_name}_{number}', '')).strip()
                    if not form:
                        raise ValueError(f"Missing {case_name} {number} form for {base_id}")
                    forms[number] = form
                    sentence = str(record.get(f'{case_name}_{number}_sentence', '')).strip()
                    translation = str(record.get(f'{case_name}_{number}_translation', '')).strip()
                    if sentence and translation:
                        examples.append(f'{sentence}\n{translation}')
                forms['case'] = case_name
                forms['meanings'] = meanings
                content_id = f'{base_id}:{case_name}'
                if content_id in seen_ids:
                    raise ValueError(f"Duplicate id '{content_id}' in {path}")
                items.append({'content_id': content_id, 'word': record.get('noun', base_id),
                              'definition': '\n'.join(part for part in (definition, *examples) if part),
                              'word_frequency': frequency, 'position': position * len(NOUN_CASES) + case_index,
                              'kind': 'noun', 'noun_case': case_name, 'noun_forms': forms})
                seen_ids.add(content_id)
            continue
        word = str(record.get('word', record.get('text', ''))).strip()
        if not word:
            raise ValueError(f"Missing word at {path}#{position + 1}")
        if base_id in seen_ids:
            raise ValueError(f"Duplicate id '{base_id}' in {path}")
        items.append({'content_id': base_id, 'word': word, 'definition': definition,
                      'word_frequency': frequency, 'position': position, 'kind': 'item'})
        seen_ids.add(base_id)
    return items


# --- Practice / Scoring Logic ---
# The lower an item's score, the more of its answer remains visible.
MAX_QUESTIONS = 16   # unique words per session (each asked exactly once)
DRILL_WORDS = 10     # top-N most-incorrect words shown in drill mode

LEITNER_INTERVALS = {box: box for box in range(1, 11)}  # box -> days until review

SCORE_DELTA = 0.5
RESULT_COUNTERS = {
    'correct': 'times_correct',
    'incorrect': 'times_incorrect',
    'mastered': 'times_mastered',
    'drilled': 'times_drilled',
}

# Vocabulary and sentences share the same score progression.
SENTENCE_MIN_SCORE = 0
SENTENCE_MAX_SCORE = 9
SENTENCE_CORRECT_DELTA = SCORE_DELTA


def leitner_interval_case(column='leitner_box'):
    cases = ' '.join(
        f'WHEN {box} THEN {days}'
        for box, days in LEITNER_INTERVALS.items()
    )
    return f'CASE {column} {cases} ELSE {LEITNER_INTERVALS[10]} END'


def _corrects_to_mastery(score, sentence_mode=False):
    """Number of half-point correct answers needed to reach score 9."""
    return max(0, int(round((9.0 - float(score)) / SCORE_DELTA)))


def corrects_to_mastery(score, sentence_mode=False):
    """Public version of _corrects_to_mastery for external use (e.g., web dashboard)."""
    return _corrects_to_mastery(score, sentence_mode)


def is_sentence_list(lang):
    """Returns True if the lang name identifies a sentence practice list."""
    return 'sentences' in (lang or '').lower()


def score_band(score):
    """Return the integer score band for a 0.0-9.0, half-point scale."""
    return min(9, max(0, int(float(score))))


def score_gauge(score, ansi=True):
    """Returns a 3-dot growth gauge for a word's score.
    If ansi=True (default), includes ANSI color codes for terminal.
    If ansi=False, returns plain Unicode dots for web."""
    if score >= 9:
        return '●●●' if not ansi else f"{Colors.GREEN}●●●{Colors.ENDC}"
    if score >= 8:
        return '●●○' if not ansi else f"{Colors.GREEN}●●○{Colors.ENDC}"
    if score >= 4:
        return '●○○' if not ansi else f"{Colors.YELLOW}●○○{Colors.ENDC}"
    return '○○○' if not ansi else f"{Colors.RED}○○○{Colors.ENDC}"


def get_gender_style(word_text):
    """Returns gender styling for a word based on German article.
    Returns tuple: (ansi_color, css_class) where ansi_color is for terminal,
    css_class is for web ('masc', 'fem', 'neut', 'none')."""
    text_lower = word_text.lower()
    if text_lower.startswith("der "):
        return Colors.BLUE, 'masc'
    if text_lower.startswith("die "):
        return Colors.RED, 'fem'
    if text_lower.startswith("das "):
        return Colors.GREEN, 'neut'
    return Colors.GREEN, 'none'


DRILL_TARGET = 9


def build_question_data(word_id, word_text, definition, score, leitner_box=1,
                         sentence_mode=False, fast_mode=False, drill_mode=False, known_drill_mode=False):
    """Builds the question data dict used by both CLI and web UI."""
    band = score_band(score)
    has_def = bool(definition)

    # Vocabulary and sentence items use the same progressive recall support.
    display_word = word_text if fast_mode else mask_sentence(word_text, score)

    if fast_mode:
        question_type = 'fast'
    elif band < 8:
        question_type = 'learning' if has_def else 'spelling'
    else:
        question_type = 'production'

    if known_drill_mode:
        question_type = 'known_review'

    full_definition_lines = definition.split('\n') if definition else []
    primary_definition = english_definition_only(definition)
    prompt_definition_lines = [primary_definition] if primary_definition else []
    definition_lines = full_definition_lines if question_type == 'learning' else prompt_definition_lines

    ansi_color, css_class = get_gender_style(word_text)

    question = {
        'word_id': word_id,
        'word': display_word,
        'word_unmasked': word_text,
        'definition': definition_lines,
        'score': round(score, 1),
        'gauge': score_gauge(score, ansi=False),
        'band': band,
        'gender': css_class,
        'type': question_type,
        'sentence_mode': sentence_mode,
        'fast_mode': fast_mode,
        'can_reveal': score < 9,
    }
    initial_drill = None

    if drill_mode or known_drill_mode:
        definition_lines = prompt_definition_lines
        question['definition'] = definition_lines
        question['type'] = 'drill'
        question_type = 'drill'
        initial_drill = {'correct_in_a_row': 0, 'repetition': 1}
        question['drill_start'] = {
            'word': word_text,
            'definition': definition_lines,
            'repetition': 1,
            'correct_in_a_row': 0,
            'target': DRILL_TARGET,
            'show_word': True,
        }

    return question, initial_drill


def record_as_drilled(user, lang, word_id, known_review=False):
    """Record a completed drill: increment times_drilled and erase one incorrect mark."""
    table = practice_table_name(user, lang)
    key_column = 'unit_key' if conjugation.is_conjugation_list(lang) else 'id'
    conn = get_connection()
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    set_clauses = [
        'times_drilled = times_drilled + 1',
        'times_practiced = times_practiced + 1',
        'drill_pending = 0',
        'last_practiced = ?',
        'last_decay_at = ?',
    ]
    params = [today, today]
    if known_review and not conjugation.is_conjugation_list(lang):
        set_clauses.append('last_known_review_at = ?')
        params.append(now)
    params.append(word_id)
    conn.execute(
        f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE {key_column} = ?',
        params
    )
    conn.commit()
    conn.close()


def record_review_result(user, lang, word_id, correct):
    """Record a review-only answer without changing score or Leitner state."""
    table = words_table_name(user, lang)
    conn = get_connection()
    counter = 'times_correct' if correct else 'times_incorrect'
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET '
        f'times_practiced = times_practiced + 1, '
        f'{counter} = {counter} + 1, '
        f'last_practiced = ?, last_decay_at = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, today, now, word_id)
    )
    conn.commit()
    conn.close()


def record_known_review_seen(user, lang, word_id):
    """Mark a known-review word as seen without changing score or answer counters."""
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET '
        f'times_practiced = times_practiced + 1, '
        f'last_practiced = ?, last_decay_at = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, today, now, word_id)
    )
    conn.commit()
    conn.close()


def ensure_fast_review_column(conn, user, lang):
    """Add the Fast mode review marker without changing word progress."""
    table = words_table_name(user, lang)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    if exists:
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if 'last_fast_review_at' not in columns:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_fast_review_at TEXT')
    return table


def record_fast_review(user, lang, word_id):
    """Mark a completed Fast mode item without changing score or counters."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_fast_review_column(conn, user, lang)
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET last_fast_review_at = ? WHERE id = ?',
        (now, word_id)
    )
    conn.commit()
    conn.close()


def update_word_score(user, lang, word_id, result_status, current_score=None, current_box=None):
    """Apply the shared half-point score and ten-box learning contract."""
    table = practice_table_name(user, lang)
    conjugation_mode = conjugation.is_conjugation_list(lang)
    max_box = 10
    key_column = 'unit_key' if conjugation.is_conjugation_list(lang) else 'id'
    conn = get_connection()
    today = date.today().isoformat()

    row = conn.execute(
        f'SELECT last_practiced, leitner_box FROM "{table}" WHERE {key_column} = ?', (word_id,)
    ).fetchone()
    stored_last_practiced = row[0] if row else None
    current_box = row[1] if row else current_box
    practiced_today = (stored_last_practiced == today)

    preserve_box_timestamp = False

    if result_status == 'correct':
        current_score = float(current_score or 0)
        new_score = min(9.0, current_score + SCORE_DELTA)
        just_mastered = (current_score < 9.0) and (new_score >= 9.0)
        if just_mastered:
            new_box = 1
        elif current_score >= 9.0:
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
            current_score >= 9.0 and not practiced_today and (current_box or 1) > 1
        ) else (current_box if current_score >= 9.0 else None)
    else:
        new_score = 9.0 if result_status == 'mastered' else float(current_score or 0)
        new_box = {
            'mastered': 1,
            'flagged': current_box if current_score and current_score >= 9.0 else None,
            'drilled': current_box if current_score and current_score >= 9.0 else None,
        }[result_status]

    counter = RESULT_COUNTERS.get(result_status)
    if new_box is not None and not preserve_box_timestamp:
        set_clauses = ['score = ?', 'leitner_box = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, new_box, today, today]
    elif preserve_box_timestamp:
        # Same-day re-practice of an already-mastered word: bump counters only.
        # Do NOT touch leitner_box, last_practiced or last_decay_at.
        set_clauses = ['score = ?', 'times_practiced = times_practiced + 1']
        params = [new_score]
    else:
        set_clauses = ['score = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, today, today]
    if counter:
        set_clauses.append(f'{counter} = {counter} + 1')
    if result_status == 'incorrect':
        set_clauses.append('drill_pending = 1')
    elif result_status == 'drilled':
        set_clauses.append('drill_pending = 0')
    if conjugation.is_conjugation_list(lang) and result_status in {'correct', 'incorrect'}:
        set_clauses.append('attempts = attempts + 1')
        if result_status == 'incorrect':
            set_clauses.append('incorrect = incorrect + 1')
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE {key_column} = ?', params)
    if conjugation.is_conjugation_list(lang):
        conn.execute(
            f'UPDATE "{table}" SET completed = CASE WHEN score >= 9.0 THEN 1 ELSE 0 END '
            f'WHERE {key_column} = ?', (word_id,)
        )
    conn.commit()
    conn.close()


def update_sentence_score(user, lang, word_id, correct, current_score=None, current_box=None):
    """Compatibility wrapper: sentence items use the shared score engine."""
    return update_word_score(
        user, lang, word_id, 'correct' if correct else 'incorrect',
        current_score, current_box
    )


def get_words_for_practice(user, lang, num_words=MAX_QUESTIONS, drill_mode=False, known_drill_mode=False):
    """Select JSON-backed material using progress-only SQLite rows."""
    sync_word_list(user, lang)
    material = {item['content_id']: item for item in load_practice_items(word_list_path(user, lang))}
    table = words_table_name(user, lang)
    conn = get_connection()
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box, last_practiced,
                   times_incorrect, times_practiced, last_known_review_at
            FROM "{table}" WHERE active = 1'''
    ).fetchall()
    conn.close()
    today = date.today()
    candidates = []
    for row in rows:
        row_id, content_id, score, box, last, incorrect, practiced, known_at = row
        item = material.get(content_id)
        if item is None:
            continue
        last_day = date.fromisoformat(last) if last else None
        due = last_day is None or (today - last_day).days >= LEITNER_INTERVALS.get(box or 1, 10)
        if known_drill_mode:
            eligible = score >= 9 and practiced > 0
            order = (known_at is not None, known_at or last or '', row_id)
        elif drill_mode:
            eligible = incorrect > 0
            order = (-incorrect, last or '', row_id)
        else:
            eligible = score < 9 or (score >= 9 and last_day != today and due)
            order = (-item['word_frequency'], len(item['word']), item['position'], row_id)
        if eligible:
            candidates.append((order, row_id, item, score, box))
    if not candidates:
        if known_drill_mode:
            raise ValueError(
                "No known practiced words to review. Master some words first, then try this mode again."
            )
        if drill_mode:
            raise ValueError(
                "No words with mistakes to drill. Keep practicing and errors will show up here."
            )
        if rows:
            raise ValueError(
                "All words in this list are mastered for today.\n"
                "Come back tomorrow to review them, or use drill mode / known-drill\n"
                "to keep practicing. You can also switch to another word list."
            )
        raise ValueError(
            "No active words found for this list. Add words to your word list file and try again."
        )
    candidates.sort(key=lambda candidate: candidate[0])
    selected = candidates[:num_words]
    if not (known_drill_mode or drill_mode):
        # Higher score prompts come first to achieve mastery, then global priority order
        selected.sort(key=lambda candidate: (-candidate[3], candidate[0]))
    return [(row_id, item['word'], item['definition'], score, box, item['word_frequency'], item.get('noun_forms'))
            for _, row_id, item, score, box in selected]


def get_mastered_words_for_fast(user, lang):
    """Return mastered words in Fast mode order, oldest review first."""
    sync_word_list(user, lang)
    material = {item['content_id']: item for item in load_practice_items(word_list_path(user, lang))}
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_fast_review_column(conn, user, lang)
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box
            FROM "{table}"
            WHERE active = 1 AND score >= 9.0
            ORDER BY
              CASE WHEN last_fast_review_at IS NULL THEN 0 ELSE 1 END,
              datetime(last_fast_review_at) ASC,
              id ASC'''
    ).fetchall()
    conn.close()
    result = []
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item:
            result.append((row_id, item['word'], item['definition'], score, box,
                           item['word_frequency'], item.get('noun_forms')))
    if not result:
        raise ValueError("No mastered words are available for fast mode.")
    return result


def show_definition(definition):
    """Prints each line of a (possibly multi-line) definition, indented and highlighted."""
    if not definition:
        return
    for line in definition.split('\n'):
        print(f"  {Colors.CYAN}{line}{Colors.ENDC}")


def english_definition_only(definition):
    """
    Returns the primary English prompt line, excluding sample sentences.
    Generated vocabulary lists store the core definition first and examples
    later; lines with " — " keep only the English side.
    """
    if not definition:
        return ''
    for line in definition.split('\n'):
        line = line.strip()
        if not line:
            continue
        if ' — ' in line:
            return line.rsplit(' — ', 1)[1].strip()
        return line
    return ''


def drill_word(user, lang, word_to_drill, word_id, definition, header_text, audio, audio_lang=None, update_score=True, wpm=128, show_word=True):
    """Initiates a strict 9-repetition drill with a consistent single-line UI."""
    clear_screen()
    print(header_text)
    if show_word:
        print(f"--- Drill Mode: '{get_gender_color(word_to_drill)}{word_to_drill}{Colors.ENDC}' ---")
    else:
        print("--- Known Drill Mode ---")
    prompt_definition = english_definition_only(definition)
    if prompt_definition:
        show_definition(prompt_definition)
    print("")
    correct_in_a_row = 0
    while correct_in_a_row < 9:
        sys.stdout.write('\033[A')
        erase_line = "\r\033[K"
        drill_header = f"Repetition {correct_in_a_row + 1}/9: "
        sys.stdout.write(f"{erase_line}{drill_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_to_drill, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + erase_line)
        if answer_matches(answer, word_to_drill):
            correct_in_a_row += 1
            print(f"{drill_header} Correct! ({correct_in_a_row}/9)")
        else:
            correct_in_a_row = 0
            print(f"{drill_header} Incorrect. Drill resetting.")
    print("\n--- Drill Complete. ---")
    if update_score:
        update_word_score(user, lang, word_id, 'drilled')
        print("Score set to 5.0.")
    time.sleep(1)


def drill_noun_case(user, lang, word_text, word_id, definition, forms, header_text,
                    audio, audio_lang=None, update_score=True, wpm=128):
    """Require nine consecutive correct singular/plural answers for one case."""
    clear_screen()
    print(header_text)
    print(f"--- Drill: {forms['case']} ---")
    prompt_definition = english_definition_only(definition)
    if prompt_definition:
        show_definition(prompt_definition)
    streak = 0
    while streak < DRILL_TARGET:
        if audio:
            speak(noun_audio_text(forms), audio_lang or lang, wpm=wpm)
        singular = input("Singular: ").strip()
        plural = input("Plural: ").strip()
        if noun_answers_match({'singular': singular, 'plural': plural}, forms):
            streak += 1
            print(f"Correct ({streak}/{DRILL_TARGET}).")
        else:
            streak = 0
            print("Incorrect. Drill reset.")
    if update_score:
        record_as_drilled(user, lang, word_id)


def ask_noun_case(user, lang, word_id, word_text, definition, forms, score, audio,
                  header_text, audio_lang=None, current_box=None, wpm=128):
    """Practice one German noun case pair through the shared score contract."""
    while True:
        clear_screen()
        print(header_text)
        print(f"\n{get_gender_color(word_text)}{mask_sentence(word_text, score)}{Colors.ENDC}")
        prompt = english_definition_only(definition)
        if prompt:
            show_definition(prompt)
        hints = noun_form_hints(forms, score)
        if hints:
            print(f"\n{forms['case'].title()} singular: {hints['singular']}")
            print(f"{forms['case'].title()} plural:   {hints['plural']}")
        if audio:
            speak(noun_audio_text(forms), audio_lang or lang, wpm=wpm)
        singular = input("Singular: ").strip()
        if singular == '!!':
            return 'end', None, None
        if singular == '?':
            if score < 9:
                print(f"{word_text}\n{forms['singular']}\n{forms['plural']}")
                time.sleep(1.2)
            else:
                print("Reveal is unavailable after mastery.")
                time.sleep(1.0)
            continue
        if singular == '+':
            continue
        if singular.startswith('@'):
            update_word_score(user, lang, word_id, 'mastered', score, current_box)
            return 'mastered', f"Marked '{word_text}' as known.", None
        if singular.startswith('!'):
            update_word_score(user, lang, word_id, 'flagged', score, current_box)
            return 'flagged', f"Flagged '{word_text}' for more practice.", None
        if singular.startswith('$'):
            drill_noun_case(user, lang, word_text, word_id, definition, forms,
                            header_text, audio, audio_lang, update_score=False, wpm=wpm)
            record_as_drilled(user, lang, word_id)
            return 'drilled', 'Drill complete.', None
        plural = input("Plural: ").strip()
        correct = noun_answers_match({'singular': singular, 'plural': plural}, forms)
        update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
        if correct:
            return 'correct', f"{Colors.GREEN}Correct.{Colors.ENDC}", None
        drill_noun_case(user, lang, word_text, word_id, definition, forms,
                        header_text, audio, audio_lang, update_score=False, wpm=wpm)
        record_as_drilled(user, lang, word_id)
        return 'drilled', f"{Colors.RED}Incorrect. Drill complete.{Colors.ENDC}", f'{singular} | {plural}'


ERASE_LINE = "\r\033[K"

SESSION_HELP_SENTENCE = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '?' (reveal before mastery), '+' (replay audio), '$' (drill)."
SESSION_HELP = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '$' (drill), '?' (reveal before mastery), '+' (replay audio)."



def handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=None, sentence_mode=False):
    """
    Checks an answer for the session-level special commands. Returns
    (status, message) if one matched ('end'/'drilled'/'mastered'/'flagged'),
    or None if the answer should be checked normally for correctness.

    Every practice material uses the same manual drill command.
    """
    if answer == '!!':
        return 'end', None, None
    if answer.startswith('$'):
        drill_word(user, lang, word_text, word_id, definition, header_text, audio, audio_lang=audio_lang)
        return 'drilled', None, None
    if answer.startswith('@'):
        update_word_score(user, lang, word_id, 'mastered')
        return 'mastered', f"Marked '{word_text}' as known.", None
    if answer.startswith('!'):
        update_word_score(user, lang, word_id, 'flagged')
        return 'flagged', f"Flagged '{word_text}' for more practice.", None
    return None


def ask_learning(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, sentence_mode=False, wpm=128):
    """
    Guided practice for any item below score 8. Correct answers add 0.5;
    wrong answers preserve score and start a nine-repetition drill.
    """
    while True:
        clear_screen()
        print(header_text)
        print("")
        has_def = bool(definition)
        if has_def:
            display_text = mask_sentence(word_text, score)
            print(f"{get_gender_color(display_text)}{display_text}{Colors.ENDC}")
            show_definition(definition)
            print("")
            while True:
                sys.stdout.write(f"{ERASE_LINE}{word_header} ")
                sys.stdout.flush()
                if audio:
                    speak(word_text, audio_lang or lang, wpm=wpm)
                answer = input("").strip()
                sys.stdout.write('\033[A' + ERASE_LINE)
                if answer == '?':
                    reveal_text = word_text if score < 9 else "Reveal unavailable after mastery."
                    sys.stdout.write(f"{word_header} {get_gender_color(reveal_text)}{reveal_text}{Colors.ENDC}")
                    sys.stdout.flush()
                    time.sleep(1.0)
                    sys.stdout.write(ERASE_LINE)
                    continue
                if answer == '+':
                    continue
                break
        else:
            while True:
                display_text = mask_sentence(word_text, score)
                sys.stdout.write(f"{ERASE_LINE}{word_header} {get_gender_color(display_text)}{display_text}{Colors.ENDC}")
                sys.stdout.flush()
                if audio:
                    speak(word_text, audio_lang or lang, wpm=wpm)
                time.sleep(0.6)
                sys.stdout.write(f"{ERASE_LINE}{word_header} ")
                sys.stdout.flush()
                answer = input("").strip()
                sys.stdout.write('\033[A' + ERASE_LINE)
                if answer == '?':
                    reveal_text = word_text if score < 9 else "Reveal unavailable after mastery."
                    sys.stdout.write(f"{word_header} {get_gender_color(reveal_text)}{reveal_text}{Colors.ENDC}")
                    sys.stdout.flush()
                    time.sleep(1.0)
                    sys.stdout.write(ERASE_LINE)
                    continue
                if answer == '+':
                    continue
                break

        special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang, sentence_mode=sentence_mode)
        if special:
            return special

        correct = answer_matches(answer, word_text, sentence_mode=sentence_mode)
        if update_score:
            if sentence_mode:
                update_sentence_score(user, lang, word_id, correct, score, current_box)
            else:
                update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        if correct:
            return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
        drill_word(user, lang, word_text, word_id, definition, header_text,
                   audio, audio_lang=audio_lang, update_score=False, wpm=wpm)
        record_as_drilled(user, lang, word_id)
        return 'drilled', f"{Colors.RED}Incorrect. Drill complete.{Colors.ENDC}", answer


def ask_audio(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=128):
    """Ask a fully masked audio question near mastery."""
    clear_screen()
    print(header_text)
    print("")
    print(f"{Colors.YELLOW}Listen and type the word you hear.{Colors.ENDC} ('?' reveals before mastery; '+' replays audio)\n")
    while True:
        sys.stdout.write(f"{ERASE_LINE}{word_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + ERASE_LINE)
        if answer == '?':
            reveal_text = word_text if score < 9 else "Reveal unavailable after mastery."
            sys.stdout.write(f"{word_header} {get_gender_color(reveal_text)}{reveal_text}{Colors.ENDC}")
            sys.stdout.flush()
            time.sleep(1.0)
            sys.stdout.write(ERASE_LINE)
            continue
        if answer == '+':
            continue
        break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang)
    if special:
        return special

    correct = answer_matches(answer, word_text)
    if update_score:
        update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
    if audio:
        speak(word_text, audio_lang or lang, wpm=wpm)
    if correct:
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    drill_word(user, lang, word_text, word_id, definition, header_text,
               audio, audio_lang=audio_lang, update_score=False, wpm=wpm)
    record_as_drilled(user, lang, word_id)
    return 'drilled', f"Incorrect. Drill complete.", answer


def ask_production(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=128):
    """
    Band 3 / drill-mode question: definition is shown and audio plays; the
    user must type the word from memory (case-sensitive). When update_score
    is False the caller is responsible for recording the attempt (drill mode).
    """
    clear_screen()
    print(header_text)
    print(f"\n{Colors.YELLOW}Type the word from the definition and audio.{Colors.ENDC} ('?' to replay)\n")
    prompt_definition = english_definition_only(definition)
    if prompt_definition:
        show_definition(prompt_definition)
    print("")

    while True:
        sys.stdout.write(f"{ERASE_LINE}{word_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + ERASE_LINE)
        if answer == '?':
            reveal_text = word_text if score < 9 else "Reveal unavailable after mastery."
            sys.stdout.write(f"{word_header} {get_gender_color(reveal_text)}{reveal_text}{Colors.ENDC}")
            sys.stdout.flush()
            time.sleep(1.0)
            sys.stdout.write(ERASE_LINE)
            continue
        if answer == '+':
            continue
        break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang)
    if special:
        return special

    correct = answer_matches(answer, word_text)
    if update_score:
        update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
    if audio:
        speak(word_text, audio_lang or lang, wpm=wpm)  # replay after answer
    if correct:
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    drill_word(user, lang, word_text, word_id, definition, header_text,
               audio, audio_lang=audio_lang, update_score=False, wpm=wpm)
    record_as_drilled(user, lang, word_id)
    return 'drilled', "Incorrect. Drill complete.", answer


def start_fast_practice_session(user, lang, audio, audio_lang=None, wpm=128):
    """Run the CLI Fast mode without changing word scores or practice counters."""
    sync_word_list(user, lang)
    sentence_mode = is_sentence_list(lang)
    rows = get_mastered_words_for_fast(user, lang)
    start_time = time.time()
    correct_count = 0
    incorrect_list = []
    queue = list(rows)

    try:
        for index, row in enumerate(queue, 1):
            word_id, word_text, definition, score, current_box = row[:5]
            noun_forms = row[6] if len(row) > 6 else None
            clear_screen()
            print(f"--- Fast Mode | Q{index}/{len(queue)} ---")
            print("The word is shown. Type it from memory; mistakes retry the same word.")
            print(f"  {word_text}")
            prompt_definition = english_definition_only(definition)
            if prompt_definition:
                show_definition(prompt_definition)
            if audio:
                speak(noun_audio_text(noun_forms) if noun_forms else word_text, audio_lang or lang, wpm=wpm)

            while True:
                singular = input("Singular: " if noun_forms else "Answer: ").strip()
                if singular == '!!':
                    raise KeyboardInterrupt
                if singular == '?':
                    print("Reveal is unavailable for mastered Fast mode material.")
                    continue
                if singular == '+':
                    if audio:
                        speak(noun_audio_text(noun_forms) if noun_forms else word_text, audio_lang or lang, wpm=wpm)
                    continue
                plural = input("Plural: ").strip() if noun_forms else None
                correct = noun_answers_match({'singular': singular, 'plural': plural}, noun_forms) if noun_forms else \
                    answer_matches(singular, word_text, sentence_mode=sentence_mode)
                if correct:
                    record_fast_review(user, lang, word_id)
                    correct_count += 1
                    print("Correct.")
                    break
                incorrect_list.append((word_text, f'{singular} | {plural}' if noun_forms else singular))
                print("Incorrect. Try again.")
    except KeyboardInterrupt:
        print("\n\nFast session ended early. Saving progress...")

    if correct_count == 0:
        print("No words were completed. Nothing to save.")
        return

    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, correct_count, correct_count,
                len(incorrect_list), 0)
    clear_screen()
    attempts = correct_count + len(incorrect_list)
    minutes, seconds = divmod(elapsed_seconds, 60)
    print("\n--- Fast Session Summary ---")
    print(f"Words completed:     {correct_count}")
    print(f"Incorrect answers:   {len(incorrect_list)}")
    print(f"Accuracy:            {100 * correct_count / attempts:.1f}%")
    print(f"Session time:        {minutes} min {seconds} sec")
    print("\nFast session finished. Progress saved.")


def ask_conjugation_unit(user, unit, score, current_box, audio, header_text, wpm=128):
    """Run one conjugation form through the normal vocabulary question flow."""
    question, _ = build_question_data(
        unit['unit_key'], unit['answer'], unit['prompt'], score, current_box
    )
    common = dict(
        user=user, lang=conjugation.LIST_ID, word_id=unit['unit_key'],
        word_text=unit['answer'], definition=unit['prompt'], score=score,
        audio=audio, header_text=header_text,
        word_header=f"{unit['stage_name']} · {unit['verb']}",
        audio_lang='german', update_score=True, current_box=current_box,
        wpm=wpm,
    )
    if question['type'] == 'learning':
        return ask_learning(**common)
    if question['type'] == 'audio':
        return ask_audio(**common)
    return ask_production(**common)


def start_conjugation_session(user, audio, audio_lang=None, wpm=128,
                              drill_all=False, drill_mode=False,
                              known_drill_mode=False, instant_drill=False):
    """Practice deterministic conjugation units through core scoring flow."""
    sync_word_list(user, conjugation.LIST_ID)
    conn = get_connection()
    queue = conjugation.next_units(
        conn, user, MAX_QUESTIONS,
        drill_mode=drill_mode,
        known_drill_mode=known_drill_mode,
    )
    conn.close()
    if (drill_mode or known_drill_mode) and not any(unit['stage'] != 1 for unit in queue):
        label = "mistakes" if drill_mode else "mastered conjugations"
        print(f"No {label} are available to drill.")
        return
    if not queue:
        print("No conjugation units are available.")
        return
    correct_count = 0
    incorrect_count = 0
    drilled_count = 0
    start_time = time.time()
    for index, unit in enumerate(queue, 1):
        conn = get_connection()
        row = conn.execute(
            f'SELECT score, leitner_box FROM "{conjugation.table_name(user)}" WHERE unit_key = ?',
            (unit['unit_key'],)
        ).fetchone()
        conn.close()
        score, current_box = row or (1.0, 1)
        header = f"German conjugations · {unit['stage_name']} · Q{index}/{len(queue)}"
        if unit['stage'] != 1 and (drill_all or drill_mode or known_drill_mode):
            drill_word(
                user, conjugation.LIST_ID, unit['answer'], unit['unit_key'],
                unit['prompt'], header, audio, audio_lang='german',
                update_score=False, wpm=wpm, show_word=True,
            )
            record_as_drilled(user, conjugation.LIST_ID, unit['unit_key'])
            drilled_count += 1
            continue
        status, _, attempt = ask_conjugation_unit(
            user, unit, score, current_box, audio, header, wpm=wpm
        )
        if status == 'end':
            elapsed = int(time.time() - start_time)
            log_session(user, conjugation.LIST_ID, elapsed, correct_count,
                        correct_count, incorrect_count, drilled_count)
            print("\nConjugation session ended early.")
            return
        if status == 'correct':
            correct_count += 1
        elif status == 'incorrect':
            incorrect_count += 1
        elif status == 'drilled':
            drilled_count += 1
        elif status in {'mastered', 'flagged'}:
            correct_count += 1
    elapsed = int(time.time() - start_time)
    log_session(user, conjugation.LIST_ID, elapsed, correct_count,
                correct_count, incorrect_count, drilled_count)
    print(f"\nConjugation session complete: {correct_count} correct, {incorrect_count} incorrect.")


def start_practice_session(user, lang, audio, audio_lang=None, drill_all=False, drill_mode=False, instant_drill=False, known_drill_mode=False, wpm=128):
    """
    Up to MAX_QUESTIONS unique words per session using Leitner spaced repetition.
    Due words (box interval elapsed) come first; each word is asked exactly once.
    Correct → advance one Leitner box. Incorrect → reset to box 1.

    Vocabulary and sentence items use the same score, masking, and drill flow.
    """
    sentence_mode = is_sentence_list(lang)
    words = get_words_for_practice(user, lang, DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS, drill_mode=drill_mode, known_drill_mode=known_drill_mode)
    queue = [{'id': r[0], 'word': r[1], 'def': r[2], 'score': r[3], 'box': r[4],
              'noun_forms': r[6] if len(r) > 6 else None}
             for r in words]

    correct_count = 0
    questions_count = 0
    drilled_words_count = 0
    incorrect_list = []
    start_time = time.time()
    total = len(queue)
    mode_label = " [DRILL ALL]" if drill_all else ""
    help_text = SESSION_HELP

    def header_text():
        return (
            f"--- Practice{mode_label} | "
            f"Q{questions_count}/{total} | "
            f"Correct: {correct_count} ---\n{help_text}"
        )

    try:
        for entry in queue:
            word_id, word_text, definition, score, current_box, noun_forms = (
                entry['id'], entry['word'], entry['def'], entry['score'], entry['box'], entry['noun_forms']
            )
            display_score = score
            word_header = f"{score_gauge(score)} (score: {display_score:.1f}):"
            if noun_forms:
                word_header = f"{noun_forms['case'].title()} {word_header}"
            band = score_band(score)

            if drill_all:
                if noun_forms:
                    drill_noun_case(user, lang, word_text, word_id, definition, noun_forms,
                                    header_text(), audio, audio_lang, update_score=False, wpm=wpm)
                else:
                    drill_word(user, lang, word_text, word_id, definition,
                               header_text(), audio, audio_lang=audio_lang,
                               update_score=False, wpm=wpm)
                record_as_drilled(user, lang, word_id)
                status, message, attempt = 'drilled', None, None
            elif drill_mode:
                if noun_forms:
                    drill_noun_case(user, lang, word_text, word_id, definition, noun_forms,
                                    header_text(), audio, audio_lang, update_score=False, wpm=wpm)
                else:
                    drill_word(user, lang, word_text, word_id, definition,
                               header_text(), audio, audio_lang=audio_lang,
                               update_score=False, wpm=wpm)
                status, message, attempt = 'drilled', None, None
            elif known_drill_mode:
                drill_word(user, lang, word_text, word_id, definition,
                           header_text(), audio, audio_lang=audio_lang,
                           update_score=False, wpm=wpm, show_word=False)
                record_as_drilled(user, lang, word_id, known_review=True)
                status, message, attempt = 'drilled', None, None
            elif noun_forms:
                status, message, attempt = ask_noun_case(
                    user, lang, word_id, word_text, definition, noun_forms, score,
                    audio, header_text(), audio_lang=audio_lang,
                    current_box=current_box, wpm=wpm)
            elif band < 8:
                status, message, attempt = ask_learning(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    current_box=current_box, wpm=wpm)
            else:
                status, message, attempt = ask_production(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    update_score=True, current_box=current_box, wpm=wpm)

            if status == 'end':
                print("\n\nSession ended early. Saving progress...")
                break

            questions_count += 1

            if drill_mode:
                record_as_drilled(user, lang, word_id)
                drilled_words_count += 1
                if message:
                    print(f"{word_header} {message}")
                    time.sleep(1.2)
                continue

            if status == 'drilled':
                drilled_words_count += 1
            elif status == 'correct':
                correct_count += 1
            elif status == 'incorrect':
                incorrect_list.append((word_text, attempt))
                if instant_drill:
                    drill_word(user, lang, word_text, word_id, definition,
                               header_text(), audio, audio_lang=audio_lang,
                               update_score=False, wpm=wpm)
                    record_as_drilled(user, lang, word_id)
                    drilled_words_count += 1

            if message:
                print(f"{word_header} {message}")
                time.sleep(1.2)

    except KeyboardInterrupt:
        print("\n\nSession ended early (Ctrl+C). Saving progress...")

    if questions_count == 0:
        clear_screen()
        print("No words were practiced. Nothing to save.")
        return

    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, questions_count, correct_count,
                len(incorrect_list), drilled_words_count)
    clear_screen()
    print("\n--- Session Summary ---")
    minutes, seconds = divmod(elapsed_seconds, 60)
    print(f"Questions answered:  {questions_count}")
    print(f"Correct answers:     {correct_count}")
    print(f"Incorrect answers:   {len(incorrect_list)}")
    print(f"Words drilled:       {drilled_words_count}")
    print(f"Session time:        {minutes} min {seconds} sec")
    if incorrect_list:
        print("\nWords you got wrong:")
        for word, attempt in incorrect_list:
            print(f"  - You wrote: '{attempt}', correct: '{word}'")
    print("\nSession finished. Progress saved.")


# --- Reporting ---
def log_session(user, lang, duration, practiced, correct, incorrect, drilled):
    conn = get_connection()
    table = ensure_sessions_table(conn, user)
    conn.execute(
        f'INSERT INTO "{table}" (language, session_date, duration_seconds, words_practiced, '
        f'correct_count, incorrect_count, drilled_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (lang, date.today().isoformat(), duration, practiced, correct, incorrect, drilled)
    )
    conn.commit()
    conn.close()


def print_language_report(conn, table, language):
    where_clause, params = "WHERE language = ?", [language]

    query = (
        f'SELECT session_date, COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
        f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
        f'FROM "{table}" {where_clause} GROUP BY session_date ORDER BY session_date DESC'
    )
    cursor = conn.execute(query, params)
    report_data = cursor.fetchall()
    if not report_data:
        return False

    print(f"\n--- Daily Practice Report ({language}) ---")
    header_format = "{:<12} | {:<10} | {:<12} | {:<15} | {:<15} | {:<15} | {:<15} | {:<15}"
    header = header_format.format(
        "Date", "Sessions", "Spent Time", "Practiced Words", "Correct Words",
        "Wrong Words", "Drilled Words", "Avg Time/Word"
    )
    print(header)
    print("-" * len(header))
    for row in report_data:
        s_date, sessions, seconds, practiced, correct, incorrect, drilled = row
        minutes, sec = divmod(seconds, 60)
        time_str = f"{minutes}m {sec}s"
        avg_time_str = f"{(seconds / practiced):.1f}s" if practiced > 0 else "N/A"
        print(header_format.format(s_date, sessions, time_str, practiced, correct, incorrect or 0, drilled or 0, avg_time_str))

    total_query = (
        f'SELECT COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
        f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
        f'FROM "{table}" {where_clause}'
    )
    cursor = conn.execute(total_query, params)
    t_sessions, t_seconds, t_practiced, t_correct, t_incorrect, t_drilled = cursor.fetchone()
    print("-" * len(header))
    if t_seconds is not None:
        t_hours, rem = divmod(t_seconds, 3600)
        t_minutes, _ = divmod(rem, 60)
        total_time_str = f"{t_hours}h {t_minutes}m"
        total_avg_time_str = f"{(t_seconds / t_practiced):.1f}s" if t_practiced > 0 else "N/A"
        print(header_format.format("Total", t_sessions, total_time_str, t_practiced, t_correct, t_incorrect or 0, t_drilled or 0, total_avg_time_str))
    return True


def compute_streak(date_strings):
    """Return (current_streak, best_streak) from a list of ISO date strings."""
    if not date_strings:
        return 0, 0
    parsed = sorted({date.fromisoformat(d) for d in date_strings})
    today = date.today()
    yesterday = today - timedelta(days=1)
    date_set = set(parsed)

    # Current streak: walk backwards from today (or yesterday if today has none)
    start = today if today in date_set else (yesterday if yesterday in date_set else None)
    current = 0
    if start:
        check = start
        while check in date_set:
            current += 1
            check -= timedelta(days=1)

    # Best streak: scan sorted dates for longest consecutive run
    best, run, prev = 0, 0, None
    for d in parsed:
        run = run + 1 if (prev is not None and d == prev + timedelta(days=1)) else 1
        best = max(best, run)
        prev = d

    return current, best


def print_user_report(conn, table, user):
    """Print an aggregate daily report across all languages for the user."""
    rows = conn.execute(
        f'SELECT session_date, COUNT(id), COUNT(DISTINCT language), '
        f'SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}" GROUP BY session_date ORDER BY session_date DESC'
    ).fetchall()
    if not rows:
        return False

    all_dates = conn.execute(f'SELECT session_date FROM "{table}"').fetchall()
    current_streak, best_streak = compute_streak([r[0] for r in all_dates])

    totals = conn.execute(
        f'SELECT COUNT(id), COUNT(DISTINCT language), SUM(duration_seconds), '
        f'SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}"'
    ).fetchone()

    print(f"\n{'=' * 72}")
    print(f"  User Report: {user}")
    print(f"{'=' * 72}")
    print(f"  Streak  ›  Current: {current_streak} day{'s' if current_streak != 1 else ''}   "
          f"Best: {best_streak} day{'s' if best_streak != 1 else ''}")

    hfmt = "{:<12} | {:<8} | {:<9} | {:<10} | {:<8} | {:<8} | {:<7} | {:<9} | {:<9}"
    header = hfmt.format("Date", "Sessions", "Languages", "Time", "Words", "Correct", "Wrong", "Accuracy", "Avg/Word")
    print(f"\n--- Daily Summary (All Languages) ---")
    print(header)
    print("-" * len(header))
    for s_date, sessions, langs, seconds, practiced, correct, incorrect in rows:
        minutes, sec = divmod(seconds or 0, 60)
        time_str = f"{minutes}m {sec}s"
        total_ans = (correct or 0) + (incorrect or 0)
        accuracy = f"{100 * correct / total_ans:.0f}%" if total_ans > 0 else "N/A"
        avg = f"{seconds / practiced:.1f}s" if practiced else "N/A"
        print(hfmt.format(s_date, sessions, langs, time_str, practiced or 0, correct or 0, incorrect or 0, accuracy, avg))

    t_sessions, t_langs, t_seconds, t_practiced, t_correct, t_incorrect = totals
    print("-" * len(header))
    t_h, t_rem = divmod(t_seconds or 0, 3600)
    t_m, _ = divmod(t_rem, 60)
    t_time = f"{t_h}h {t_m}m"
    t_total_ans = (t_correct or 0) + (t_incorrect or 0)
    t_accuracy = f"{100 * t_correct / t_total_ans:.0f}%" if t_total_ans > 0 else "N/A"
    t_avg = f"{t_seconds / t_practiced:.1f}s" if t_practiced else "N/A"
    print(hfmt.format("Total", t_sessions, t_langs, t_time, t_practiced or 0, t_correct or 0, t_incorrect or 0, t_accuracy, t_avg))
    return True


def print_due_summary(conn, user, lang):
    """Print Leitner box distribution and due-today count for a word list."""
    table = words_table_name(user, lang)
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return
    rows = conn.execute(
        f'''SELECT leitner_box, COUNT(*) AS total,
            SUM(CASE WHEN last_practiced IS NULL
                     OR date(last_practiced) = date('now', 'localtime')
                     OR julianday('now', 'localtime') - julianday(last_practiced) >=
                        {leitner_interval_case()}
                THEN 1 ELSE 0 END) AS due
            FROM "{table}" WHERE active = 1 AND score >= 9.0 AND leitner_box IS NOT NULL
            GROUP BY leitner_box ORDER BY leitner_box''',
        ()
    ).fetchall()
    if not rows:
        return
    total_due = sum(r[2] or 0 for r in rows)
    total_words = sum(r[1] for r in rows)
    box_str = '  '.join(f"Box {r[0]}: {r[2] or 0}/{r[1]}" for r in rows)
    print(f"\nReview Status  Active: {total_words}  Due today: {total_due}")
    print(f"  {box_str}  (due/total per box)")


def generate_report(user, lang=None):
    user_s = sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        print("No practice sessions found.")
        conn.close()
        return

    if lang:
        languages = [sanitize_name(lang, 'language')]
    else:
        print_user_report(conn, table, user_s)
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]

    any_data = False
    for language in languages:
        if print_language_report(conn, table, language):
            any_data = True
            if lang:
                print_due_summary(conn, user_s, language)
    if not any_data:
        print("No practice sessions found.")
    conn.close()


# --- CLI ---
def cmd_init(args):
    conn = get_connection()
    ensure_user(conn, args.user)
    ensure_sessions_table(conn, args.user)
    conn.commit()
    conn.close()

    if not args.lang:
        print(f"User ready: {sanitize_name(args.user, 'user')}")
        print("Select a shared list in the web UI, or pass --lang to create a personal JSON list.")
        return

    user_path = word_list_path_user_specific(args.user, args.lang)
    shared_path = word_list_path(args.user, args.lang)
    path = shared_path if os.path.exists(shared_path) and shared_path != user_path else user_path
    created = not os.path.exists(path)
    if created and path == user_path:
        os.makedirs(WORD_LISTS_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as target:
            json.dump([], target, indent=2)
        retire_sample_material(args.user)
    conn = get_connection()
    ensure_word_table(conn, args.user, args.lang)
    ensure_sessions_table(conn, args.user)
    conn.commit()
    conn.close()
    action = 'created' if created else 'already exists'
    print(f"JSON list {action}: {path}")
    if is_read_only_sample_list(args.user, args.lang):
        print("Tartarus sample lists are read-only; use them for evaluation or create a personal list.")
    else:
        print("Add material through the web editor or by editing the JSON file, then run practice.")


def cmd_practice(args):
    audio = sys.platform == 'darwin' and not args.no_audio
    if conjugation.is_conjugation_list(args.lang):
        if args.fast:
            raise ValueError("Conjugation practice cannot be combined with Fast mode.")
        start_conjugation_session(
            args.user, audio, audio_lang=args.audio_lang or None, wpm=args.wpm,
            drill_all=args.drill, drill_mode=args.drill_mode,
            known_drill_mode=args.known_drill_mode,
            instant_drill=args.instant_drill,
        )
        return
    if args.fast:
        if args.drill or args.drill_mode or args.instant_drill or args.known_drill_mode:
            raise ValueError("Fast mode cannot be combined with drill modes.")
        start_fast_practice_session(args.user, args.lang, audio,
                                     audio_lang=args.audio_lang or None,
                                     wpm=args.wpm)
        return
    sync_word_list(args.user, args.lang)
    start_practice_session(args.user, args.lang, audio,
                           audio_lang=args.audio_lang or None,
                           drill_all=args.drill,
                           drill_mode=args.drill_mode,
                           instant_drill=args.instant_drill,
                           known_drill_mode=args.known_drill_mode,
                           wpm=args.wpm)


def cmd_report(args):
    if args.lang:
        sync_word_list(args.user, args.lang)
    generate_report(args.user, args.lang)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='tartarus',
        description="An interactive CLI tool for vocabulary practice with multi-user, multi-language word lists.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Usage Examples:
  # First time setup for a user
  make init user=bahman

  # Start a practice session (4 words, 16 questions); audio on by default on macOS
  make practice user=bahman list=german

  # Same, but without audio
  make practice user=bahman list=german opts="--no-audio"

  # View progress report
  make report user=bahman list=german

How practice works:
  Every item has a score from 0.0 (new) to 9.0 (mastered). Correct answers
  add 0.5. Scores below 8.0 progressively mask random letters; scores from
  8.0 to 9.0 use a fully masked answer with definition and audio. A mistake
  preserves the score and starts a strict 9-correct drill. Mastered items
  enter Leitner box 1; boxes 1 through 10 are due after 1 through 10 days.

Special Commands (during a session):
  !! or Ctrl+C  -> End session early and save progress.
  ?             -> Reveal the answer before it has reached mastery.
  +             -> Replay the current word's audio.
  !word         -> Flag word as difficult (score becomes 1.0).
  @word         -> Mark word as known (score becomes 9.0).
  $word         -> Start a strict 9-repetition drill for the current word
                    without changing its score.

Developed by Bahman Farhadian.
"""
    )
    subparsers = parser.add_subparsers(dest='command')

    practice_parser = subparsers.add_parser('practice', help="Start a practice session.")
    practice_parser.add_argument('--user', required=True, help="Username (lowercase letters, digits, underscores).")
    practice_parser.add_argument('--lang', required=True, help="Word list / language to practice.")
    practice_parser.add_argument('--no-audio', action='store_true',
                                  help="Disable speaking each word aloud (audio is on by default on macOS, via 'say';\n"
                                       "has no effect on other platforms). Tartarus tries to use a 'say' voice that\n"
                                       "matches --lang (e.g. a German voice for --lang german).")
    practice_parser.add_argument('--audio-lang',
                                  help="Override the language used for voice/audio selection.\n"
                                       "Useful when --lang is a sub-list name (e.g. 'german_home') that doesn't\n"
                                       "auto-detect as a language: pass --audio-lang german to still use the\n"
                                       "German 'say' voice. Accepts the same values as --lang (e.g. 'german', 'de').")
    practice_parser.add_argument('--fast', action='store_true',
                                  help="Fast mode: review mastered words in oldest-fast-review order; scores unchanged.")
    practice_parser.add_argument('--drill', action='store_true',
                                  help="Drill-mode: every word in the session is put through the 9-repetition\n"
                                       "drill automatically, regardless of its score band.")
    practice_parser.add_argument('--drill-mode', action='store_true',
                                  help="Review drill: practice your high-mistake words without changing\n"
                                       "their scores. Completing a drill reduces that word's mistake count.")
    practice_parser.add_argument('--instant-drill', action='store_true',
                                  help="Instant drill: after any incorrect answer, immediately start a\n"
                                       "9-repetition drill for that word (score unchanged).")
    practice_parser.add_argument('--known-drill-mode', action='store_true',
                                  help="Known drill: review mastered words that were never reviewed,\n"
                                       "then oldest review first. Completing a drill reduces mistake count.")
    practice_parser.add_argument('--wpm', type=int, default=128,
                                  help="Speech rate in words per minute for macOS 'say' (default 128;\n"
                                       "clear for language learners; lower = slower, higher = faster).")

    report_parser = subparsers.add_parser('report', help="Show practice history.")
    report_parser.add_argument('--user', required=True, help="Username.")
    report_parser.add_argument('--lang', help="Limit the report to a single language (default: all languages).")

    init_parser = subparsers.add_parser('init', help="Create a user; optionally create a personal word list.")
    init_parser.add_argument('--user', required=True, help="Username.")
    init_parser.add_argument('--lang', help="Optional personal language / word list name.")

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    try:
        if args.command == 'practice':
            cmd_practice(args)
        elif args.command == 'report':
            cmd_report(args)
        elif args.command == 'init':
            cmd_init(args)
        else:
            parser.print_help()
    except Exception as e:
        print(f"\n{Colors.RED}An error occurred: {e}{Colors.ENDC}")
        sys.exit(1)


if __name__ == "__main__":
    main()
