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
import logging
import logging.handlers
import shutil
import hashlib
import tempfile
import uuid
from datetime import date, datetime, timedelta

# --- Configuration ---
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
DATABASE_FILE = os.environ.get('TARTARUS_DB', os.path.join(DATA_DIR, 'tartarus.db'))
WORD_LISTS_DIR = os.environ.get('TARTARUS_WORD_LISTS_DIR', os.path.join(DATA_DIR, 'word_lists'))
LOG_FILE_PATH = os.path.join(PROJECT_DIR, 'tartarus.log')
NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')

# Logging is configured by executable entry points, not at import time. This keeps
# library calls and isolated tests free of project-log side effects.
logger = logging.getLogger('tartarus')
logger.addHandler(logging.NullHandler())


def configure_logging():
    """Configure bounded, redacted application logging once per process."""
    if getattr(logger, '_tartarus_configured', False):
        return
    level = getattr(logging, os.environ.get('TARTARUS_LOG_LEVEL', 'INFO').upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    log_path = os.environ.get('TARTARUS_LOG_FILE', LOG_FILE_PATH)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger._tartarus_configured = True


def log_event(event_type, **kwargs):
    # Learner answers and correct targets are deliberately excluded from logs.
    sensitive = {'answer', 'typed', 'target', 'word_text'}
    details = ' | '.join(f"{key}: {value}" for key, value in kwargs.items() if key not in sensitive)
    logger.debug(f"{event_type} | {details}")


def tts_available():
    """Return whether this host can provide the supported macOS speech engine."""
    return sys.platform == 'darwin' and shutil.which('say') is not None


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
    if not tts_available():
        return False
    text = str(text).strip()
    if not text:
        return False
    if len(text) > 2_000:
        raise ValueError('Speech text exceeds the 2000-character limit.')
    try:
        rate = max(80, min(320, int(wpm)))
    except (TypeError, ValueError):
        rate = 128
    cmd = ['say', '-r', str(rate)]
    if lang:
        voice = voice_for_language(lang)
        if voice:
            cmd += ['-v', voice]
    cmd.append(text)
    try:
        if block:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(60, max(5, len(text) * 0.3)))
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def sanitize_name(name, label):
    """Validates a user/language name for safe use in table and file names."""
    name = name.lower()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid {label} '{name}': only lowercase letters, digits, and underscores are allowed."
        )
    return name


def read_word_list(path):
    """Read and validate one material file without changing its shape."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Word list not found: {path}")
    with open(path, encoding='utf-8') as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid word-list schema in {path}: expected an object.")
    if not isinstance(data.get('metadata'), dict):
        raise ValueError(f"Invalid word-list schema in {path}: metadata must be an object.")
    if not isinstance(data.get('items'), list):
        raise ValueError(f"Invalid word-list schema in {path}: items must be an array.")
    return data


def validate_word_list_items(items, path='<word list>', require_explicit_ids=False):
    """Validate stable IDs and practice fields before material is persisted."""
    if not isinstance(items, list):
        raise ValueError(f"Invalid word-list schema in {path}: items must be an array.")
    seen_ids = set()
    normalized = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid item {index} in {path}: expected an object.")
        word = str(item.get('word', item.get('text', ''))).strip()
        if not word:
            raise ValueError(f"Invalid item {index} in {path}: missing word.")
        content_id = str(item.get('id', '')).strip()
        if not content_id:
            if require_explicit_ids:
                raise ValueError(f"Invalid item {index} in {path}: missing stable id.")
            seed = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(f"{os.path.basename(path)}:{index}:{seed}".encode('utf-8')).hexdigest()[:24]
            content_id = f'legacy-{digest}'
        if content_id in seen_ids:
            raise ValueError(f"Invalid word list in {path}: duplicate id '{content_id}'.")
        frequency = normalize_word_frequency(item.get('word_frequency', item.get('frequency', 0)))
        if frequency is None:
            raise ValueError(f"Invalid item {index} in {path}: word_frequency must be a non-negative integer.")
        record = dict(item)
        record['id'] = content_id
        record['word'] = word
        if 'word_frequency' in record or 'frequency' in record:
            record['word_frequency'] = frequency
            record.pop('frequency', None)
        seen_ids.add(content_id)
        normalized.append(record)
    return normalized


def write_word_list_atomic(path, data):
    """Persist validated JSON through a same-directory atomic replacement."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix='.tartarus-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


# --- Database Helpers ---
def get_connection():
    db_dir = os.path.dirname(DATABASE_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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


def ensure_progress_table(conn, user, lang):
    """Return the progress table for practice."""
    return words_table_name(user, lang)


def practice_table_name(user, lang):
    """Return the progress table for practice."""
    return words_table_name(user, lang)


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


def ensure_dataset_progress_table(conn):
    """Create dataset_progress table that tracks the 10-day gauntlet state per (user, lang)."""
    conn.execute('''CREATE TABLE IF NOT EXISTS dataset_progress (
        user TEXT NOT NULL,
        lang TEXT NOT NULL,
        current_stage INTEGER NOT NULL DEFAULT 0,
        current_day INTEGER NOT NULL DEFAULT 0,
        sessions_done_today INTEGER NOT NULL DEFAULT 0,
        last_practice_date DATE,
        PRIMARY KEY (user, lang)
    )''')


def ensure_word_table(conn, user, lang):
    ensure_dataset_progress_table(conn)
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
            times_flagged INTEGER NOT NULL DEFAULT 0,
            drill_pending INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            stage_reached INTEGER NOT NULL DEFAULT 0,
            last_known_review_at TEXT
        )
    '''
    conn.execute(schema)
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    migrate_legacy = 'text' in columns
    migrate_leitner = not migrate_legacy and any(
        row[1] == 'leitner_box' and row[3] for row in conn.execute(f'PRAGMA table_info("{table}")')
    )
    migrate_last_known = 'last_known_review_at' not in columns

    if migrate_legacy or migrate_leitner or migrate_last_known:
        legacy_table = f'{table}_legacy'
        conn.execute(f'DROP TABLE IF EXISTS "{legacy_table}"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy_table}"')
        conn.execute(schema)
        shared = [
            'score', 'last_practiced', 'last_decay_at', 'active',
            'times_practiced', 'times_correct', 'times_incorrect',
            'times_drilled', 'times_mastered', 'times_flagged', 'drill_pending', 'leitner_box',
            'last_known_review_at',
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


# ---------------------------------------------------------------------------
# Gauntlet (10-Day Descent) constants and helpers
# ---------------------------------------------------------------------------

# (stage, day_min, day_max, stage_name, session_mode)
GAUNTLET_STAGE_MAP = [
    (0,  0,  0,  'The Forging',  'forging'),
    (1,  1,  2,  'The Crucible', 'crucible'),
    (2,  3,  4,  'The Shadows',  'shadows'),
    (3,  5,  6,  'The Depths',   'depths'),
    (4,  7,  8,  'The Void',     'void'),
    (5,  9,  10, 'Ascension',    'ascension'),
]

GAUNTLET_MAX_DAY = 10
# The hardcoded sessions limit is removed in favor of task-completion logic.


def gauntlet_stage_for_day(day):
    """Return (stage_num, stage_name, session_mode) for a given gauntlet day 0-10."""
    for stage, day_min, day_max, name, mode in GAUNTLET_STAGE_MAP:
        if day_min <= day <= day_max:
            return stage, name, mode
    return 5, 'Ascension', 'ascension'


def get_dataset_progress(user, lang, conn=None):
    """Return gauntlet progress dict for (user, lang). Defaults to Day 0 if not started."""
    close = conn is None
    if close:
        conn = get_connection()
    ensure_dataset_progress_table(conn)
    row = conn.execute(
        'SELECT current_stage, current_day, sessions_done_today, last_practice_date '
        'FROM dataset_progress WHERE user = ? AND lang = ?',
        (user, lang)
    ).fetchone()
    if close:
        conn.close()
    if not row:
        return {'current_stage': 0, 'current_day': 0, 'sessions_done_today': 0, 'last_practice_date': None}
    return {
        'current_stage': row[0],
        'current_day': row[1],
        'sessions_done_today': row[2],
        'last_practice_date': row[3],
    }


def update_dataset_progress(user, lang, **kwargs):
    """Upsert gauntlet progress for (user, lang) with given field values."""
    conn = get_connection()
    ensure_dataset_progress_table(conn)
    exists = conn.execute(
        'SELECT 1 FROM dataset_progress WHERE user = ? AND lang = ?', (user, lang)
    ).fetchone()
    if not exists:
        conn.execute('INSERT INTO dataset_progress (user, lang) VALUES (?, ?)', (user, lang))
    allowed = {'current_stage', 'current_day', 'sessions_done_today', 'last_practice_date'}
    unknown = set(kwargs) - allowed
    if unknown:
        conn.close()
        raise ValueError(f"Unsupported dataset progress fields: {', '.join(sorted(unknown))}")
    if kwargs:
        set_parts = ', '.join(f'{key} = ?' for key in kwargs)
        params = list(kwargs.values()) + [user, lang]
        conn.execute(f'UPDATE dataset_progress SET {set_parts} WHERE user = ? AND lang = ?', params)
    conn.commit()
    conn.close()


def _gauntlet_tasks_remaining(conn, user, lang, current_day, practice_date):
    """Count unfinished work for a specific calendar date using one connection."""
    table = words_table_name(user, lang)
    stage, _, _ = gauntlet_stage_for_day(current_day)
    if stage == 0:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE active = 1 AND score < 9.0'
        ).fetchone()
    else:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE active = 1 '
            f'AND (last_practiced IS NULL OR last_practiced < ?)', (practice_date,)
        ).fetchone()
    return row[0] if row else 0


def transition_gauntlet_day(user, lang, today=None):
    """Advance at most once when the prior calendar day was completed."""
    today = today or date.today().isoformat()
    conn = get_connection()
    ensure_dataset_progress_table(conn)
    ensure_word_table(conn, user, lang)
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute(
        'SELECT current_stage, current_day, sessions_done_today, last_practice_date '
        'FROM dataset_progress WHERE user = ? AND lang = ?', (user, lang)
    ).fetchone()
    if row is None:
        conn.execute('INSERT INTO dataset_progress (user, lang) VALUES (?, ?)', (user, lang))
        progress = {'current_stage': 0, 'current_day': 0, 'sessions_done_today': 0, 'last_practice_date': None}
    else:
        current_stage, current_day, sessions_done_today, last_practice_date = row
        progress = {
            'current_stage': current_stage, 'current_day': current_day,
            'sessions_done_today': sessions_done_today, 'last_practice_date': last_practice_date,
        }
        if last_practice_date and last_practice_date < today:
            if _gauntlet_tasks_remaining(conn, user, lang, current_day, last_practice_date) == 0:
                progress['current_day'] = min(current_day + 1, GAUNTLET_MAX_DAY)
            progress['sessions_done_today'] = 0
            progress['current_stage'] = gauntlet_stage_for_day(progress['current_day'])[0]
            # Persist the transition date so repeated status/start calls cannot advance again.
            progress['last_practice_date'] = today
            conn.execute(
                'UPDATE dataset_progress SET current_stage = ?, current_day = ?, '
                'sessions_done_today = ?, last_practice_date = ? WHERE user = ? AND lang = ?',
                (progress['current_stage'], progress['current_day'], progress['sessions_done_today'],
                 progress['last_practice_date'], user, lang),
            )
    conn.commit()
    conn.close()
    return progress


def advance_gauntlet_session(user, lang, today=None):
    """Record one completed session after the day transition has already occurred."""
    today = today or date.today().isoformat()
    conn = get_connection()
    ensure_dataset_progress_table(conn)
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute(
        'SELECT current_stage, current_day, sessions_done_today, last_practice_date '
        'FROM dataset_progress WHERE user = ? AND lang = ?', (user, lang)
    ).fetchone()
    if row is None:
        stage, _, _ = gauntlet_stage_for_day(0)
        conn.execute(
            'INSERT INTO dataset_progress '
            '(user, lang, current_stage, current_day, sessions_done_today, last_practice_date) '
            'VALUES (?, ?, ?, 0, 1, ?)', (user, lang, stage, today),
        )
    else:
        current_stage, current_day, sessions_done_today, last_practice_date = row
        if last_practice_date != today:
            # A caller that finalizes without a prior start still transitions safely.
            if last_practice_date and _gauntlet_tasks_remaining(conn, user, lang, current_day, last_practice_date) == 0:
                current_day = min(current_day + 1, GAUNTLET_MAX_DAY)
            current_stage = gauntlet_stage_for_day(current_day)[0]
            sessions_done_today = 0
        conn.execute(
            'UPDATE dataset_progress SET current_stage = ?, current_day = ?, '
            'sessions_done_today = ?, last_practice_date = ? WHERE user = ? AND lang = ?',
            (current_stage, current_day, sessions_done_today + 1, today, user, lang),
        )
    conn.commit()
    conn.close()


def get_gauntlet_tasks_remaining(user, lang, current_day, practice_date=None):
    """Return unfinished tasks for the requested Gauntlet day and date."""
    conn = get_connection()
    try:
        return _gauntlet_tasks_remaining(
            conn, user, lang, current_day, practice_date or date.today().isoformat()
        )
    finally:
        conn.close()

def is_gauntlet_locked_today(user, lang):
    """Return True if this dataset's daily tasks are fully completed and it's still the same day."""
    progress = get_dataset_progress(user, lang)
    remaining = get_gauntlet_tasks_remaining(user, lang, progress['current_day'])
    return remaining == 0

def get_words_for_gauntlet_stage(user, lang, stage, num_words=None):
    """Select words for the given gauntlet stage.

    Stage 0 (Forging): unmastered words (score < 9.0), ordered by score ascending.
    Stages 1-5: all active words (mastered), randomly shuffled each session.
    """
    if num_words is None:
        import importlib
        num_words = MAX_QUESTIONS
    sync_word_list(user, lang)
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()

    today = date.today().isoformat()
    if stage == 0:
        rows = conn.execute(
            f'SELECT id, content_id, score, leitner_box FROM "{table}" '
            f'WHERE active = 1 AND score < 9.0 ORDER BY score ASC, id ASC'
        ).fetchall()
    else:
        # Pull words that haven't been practiced today
        rows = conn.execute(
            f'SELECT id, content_id, score, leitner_box FROM "{table}" '
            f'WHERE active = 1 AND (last_practiced IS NULL OR last_practiced < ?) '
            f'ORDER BY id ASC', (today,)
        ).fetchall()

    conn.close()

    candidates = []
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item:
            candidates.append((row_id, item['word'], item['definition'], score, box,
                               item['word_frequency'], item.get('noun_forms')))

    if not candidates:
        if stage == 0:
            raise ValueError('All words are already mastered for this list! The Forging is complete.')
        raise ValueError('No active words found for this list.')

    if stage == 0:
        selected = candidates[:num_words]
    else:
        random.shuffle(candidates)
        selected = candidates[:num_words]

    return selected


def check_leitner_due_words(user, lang, num_words=None):
    """Return mastered words that are due for lifetime Leitner maintenance review.
    Returns an empty list if none are due."""
    if num_words is None:
        num_words = MAX_QUESTIONS
    sync_word_list(user, lang)
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    due_case = leitner_interval_case()
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box
            FROM "{table}" WHERE active = 1 AND score >= 9.0 AND leitner_box IS NOT NULL AND (
                last_practiced IS NULL OR
                julianday('now', 'localtime') - julianday(last_practiced) >= {due_case}
            )
            ORDER BY last_practiced ASC
            LIMIT ?''',
        (num_words,)
    ).fetchall()
    conn.close()
    result = []
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item:
            result.append((row_id, item['word'], item['definition'], score, box,
                           item['word_frequency'], item.get('noun_forms')))
    return result


def get_due_review_words(user, lang, num_words=None):
    """Return only mastered Leitner items that are currently due, without mutation."""
    return check_leitner_due_words(user, lang, num_words)


# --- Word List Sync ---
def word_list_path(user, lang):
    """Resolve a personal override or one unambiguous shared material file."""
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    user_specific = os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")
    if os.path.isfile(user_specific):
        return user_specific

    matches = []
    for root, _, names in os.walk(WORD_LISTS_DIR):
        if f'{lang}.json' in names:
            candidate = os.path.join(root, f'{lang}.json')
            if candidate != user_specific:
                matches.append(candidate)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"Word list '{lang}' was not found.")
    locations = ', '.join(os.path.relpath(candidate, WORD_LISTS_DIR) for candidate in matches)
    raise ValueError(f"Word list id '{lang}' is ambiguous: {locations}")


def personal_list_owner(stem, users):
    """Return the longest matching user prefix for ``owner_list`` names."""
    matches = [user for user in users if stem.startswith(f'{user}_')]
    return max(matches, key=len) if matches else None


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
    """Ensure all sample and user datasets remain accessible at all times."""
    pass


def retire_sample_material(user):
    """Retain sample history; discovery hides samples after personal material exists."""
    sanitize_name(user, 'user')


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


def _noun_case_forms(record, case_name, path):
    noun_forms = record.get('noun_forms')
    if not isinstance(noun_forms, dict):
        raise ValueError(f"Invalid noun '{record['id']}' in {path}: missing noun_forms.")
    case_forms = noun_forms.get(case_name)
    if not isinstance(case_forms, dict):
        raise ValueError(f"Invalid noun '{record['id']}' in {path}: missing {case_name} forms.")
    result = {'case': case_name}
    for number in ('singular', 'plural'):
        form = case_forms.get(number)
        if not isinstance(form, dict):
            raise ValueError(f"Invalid noun '{record['id']}' in {path}: missing {case_name} {number} form.")
        text = str(form.get('form', '')).strip()
        sentence = str(form.get('sentence', '')).strip()
        translation = str(form.get('translation', '')).strip()
        if not text or not sentence or not translation:
            raise ValueError(f"Invalid noun '{record['id']}' in {path}: incomplete {case_name} {number} data.")
        result[number] = text
        result[f'{number}_sentence'] = sentence
        result[f'{number}_translation'] = translation
    return result


def load_practice_items(path):
    """Load validated material, expanding one German noun into four case items."""
    raw_data = read_word_list(path)
    records = validate_word_list_items(raw_data['items'], path)
    items = []
    for position, record in enumerate(records):
        word = record['word']
        definition = normalize_definition(record.get('definition', record.get('translation', word)))
        frequency = normalize_word_frequency(record.get('word_frequency', 0))
        if record.get('kind') == 'noun':
            for case_name in NOUN_CASES:
                forms = _noun_case_forms(record, case_name, path)
                items.append({
                    'content_id': f"{record['id']}:{case_name}",
                    'word': word,
                    'definition': definition,
                    'word_frequency': frequency,
                    'position': position,
                    'kind': 'noun',
                    'noun_forms': forms,
                    'record': record,
                })
            continue
        items.append({
            'content_id': record['id'],
            'word': word,
            'definition': definition,
            'word_frequency': frequency,
            'position': position,
            'kind': record.get('kind', 'item'),
            'record': record,
        })
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
    'flagged': 'times_flagged',
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


def record_drill_debt(user, lang, word_id):
    """Persist the corrective drill obligation created by a wrong answer."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_word_table(conn, user, lang)
    conn.execute(f'UPDATE "{table}" SET drill_pending = 1 WHERE id = ?', (word_id,))
    conn.commit()
    conn.close()


def complete_drill(user, lang, word_id, known_review=False):
    """Clear one persisted drill debt without granting a normal score increment."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_word_table(conn, user, lang)
    now = datetime.now().isoformat(timespec='microseconds')
    clauses = ['drill_pending = 0', 'times_drilled = times_drilled + 1']
    params = []
    if known_review:
        clauses.append('last_known_review_at = ?')
        params.append(now)
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(clauses)} WHERE id = ?', params)
    conn.commit()
    conn.close()


def record_as_drilled(user, lang, word_id, known_review=False):
    """Compatibility wrapper for the single-source drill completion operation."""
    complete_drill(user, lang, word_id, known_review)


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
        if 'last_known_review_at' not in columns:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_known_review_at TEXT')
    return table


def record_fast_review(user, lang, word_id):
    """Mark a completed Fast mode item without changing score or counters."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_fast_review_column(conn, user, lang)
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET last_known_review_at = ? WHERE id = ?',
        (now, word_id)
    )
    conn.commit()
    conn.close()


def update_word_score(user, lang, word_id, result_status, current_score=None, current_box=None):
    """Apply the shared half-point score and ten-box learning contract."""
    table = practice_table_name(user, lang)
    max_box = 10
    key_column = 'id'
    conn = get_connection()
    today = date.today().isoformat()

    row = conn.execute(
        f'SELECT score, last_practiced, leitner_box FROM "{table}" WHERE {key_column} = ?', (word_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f'Unknown practice item id: {word_id}')
    stored_score, stored_last_practiced, stored_box = row
    current_score = stored_score if current_score is None else current_score
    current_box = stored_box if current_box is None else current_box
    practiced_today = (stored_last_practiced == today)

    preserve_box_timestamp = False

    if result_status in ('correct', 'drilled'):
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
        # A failed first attempt on a scheduled review preserves the score and 
        # retains the current box (like a Kubernetes crash loop backoff).
        new_box = current_box if current_score >= 9.0 else None
    else:
        new_score = 9.0 if result_status == 'mastered' else float(current_score or 0)
        new_box = {
            'mastered': 1,
            'flagged': current_box if current_score and current_score >= 9.0 else None,
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
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE {key_column} = ?', params)
    conn.commit()
    conn.close()
    log_event("SCORE_UPDATED", user=user, lang=lang, word_id=word_id, status=result_status, new_score=new_score, new_box=new_box)


def update_sentence_score(user, lang, word_id, correct, current_score=None, current_box=None):
    """Compatibility wrapper: sentence items use the shared score engine."""
    return update_word_score(
        user, lang, word_id, 'correct' if correct else 'incorrect',
        current_score, current_box
    )


def is_list_ordered(path):
    """Return True if the JSON file specifies "ordered": true in its metadata."""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding='utf-8') as source:
            raw_data = json.load(source)
        if isinstance(raw_data, dict):
            return bool(raw_data.get('metadata', {}).get('ordered', False))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return False


def get_words_for_practice(user, lang, num_words=MAX_QUESTIONS, drill_mode=False, known_drill_mode=False, drill_all=False):
    """Select JSON-backed material using progress-only SQLite rows."""
    sync_word_list(user, lang)
    wpath = word_list_path(user, lang)
    is_ordered = is_list_ordered(wpath)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box, last_practiced,
                   times_incorrect, times_practiced, last_known_review_at,
                   times_drilled, drill_pending
            FROM "{table}" WHERE active = 1'''
    ).fetchall()
    conn.close()
    today = date.today()
    candidates = []
    for row in rows:
        row_id, content_id, score, box, last, incorrect, practiced, known_at, drilled, drill_pending = row
        item = material.get(content_id)
        if item is None:
            continue
        last_day = date.fromisoformat(last) if last else None
        due = last_day is None or (today - last_day).days >= LEITNER_INTERVALS.get(box or 1, 10)
        if drill_all:
            eligible = True
            if is_ordered:
                order = (item['position'], row_id)
            else:
                order = (-item['word_frequency'], len(item['word']), item['position'], row_id)
        elif known_drill_mode:
            eligible = score >= 9 and practiced > 0
            order = (known_at is not None, known_at or last or '', item['position'], row_id)
        elif drill_mode:
            eligible = bool(drill_pending)
            order = (item['position'], row_id) if is_ordered else (item['position'], row_id)
        else:
            eligible = bool(drill_pending) or score < 9 or (score >= 9 and last_day != today and due)
            if is_ordered:
                order = (0 if drill_pending else 1, item['position'], row_id)
            else:
                order = (0 if drill_pending else 1, -item['word_frequency'], len(item['word']), item['position'], row_id)
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
    pending_candidates = [candidate for candidate in candidates if candidate[0][0] == 0]
    if pending_candidates and not (known_drill_mode or drill_mode or drill_all):
        candidates = pending_candidates
    selected = candidates[:num_words]
    if not (known_drill_mode or drill_mode):
        if is_ordered:
            selected.sort(key=lambda candidate: candidate[2]['position'])
        else:
            random.shuffle(selected)
    pending_ids = {candidate[1] for candidate in pending_candidates}
    return [(row_id, item['word'], item['definition'], score, box, item['word_frequency'], item.get('noun_forms'),
             row_id in pending_ids)
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
              CASE WHEN last_known_review_at IS NULL THEN 0 ELSE 1 END,
              datetime(last_known_review_at) ASC,
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



def start_practice_session(user, lang, audio, audio_lang=None, drill_all=False, drill_mode=False, instant_drill=False, known_drill_mode=False, wpm=128):
    """
    Up to MAX_QUESTIONS unique words per session using Leitner spaced repetition.
    Due words (box interval elapsed) come first; each word is asked exactly once.
    Correct → advance one Leitner box. Incorrect → reset to box 1.

    Vocabulary and sentence items use the same score, masking, and drill flow.
    """
    sentence_mode = is_sentence_list(lang)
    words = get_words_for_practice(user, lang, DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS, drill_mode=drill_mode, known_drill_mode=known_drill_mode, drill_all=drill_all)
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
        write_word_list_atomic(path, {
            'metadata': {
                'name': args.lang, 'language': 'unknown', 'type': 'vocabulary',
                'cefr_level': 'all', 'pos': 'all',
            },
            'items': [],
        })
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
    audio = sys.platform == 'darwin'
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



BACKUP_FORMAT = 'tartarus-progress'
BACKUP_VERSION = 1


def _table_rows(conn, table):
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return [dict(zip(columns, row)) for row in rows]


def export_user_data(user):
    """Export one user's progress in a versioned logical backup schema."""
    user_s = sanitize_name(user, 'user')
    conn = get_connection()
    try:
        user_row = conn.execute('SELECT name, created_at FROM users WHERE name = ?', (user_s,)).fetchone()
        if user_row is None:
            raise ValueError(f"Unknown user '{user_s}'.")
        prefix = f'words_{user_s}_'
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? ORDER BY name", (prefix + '%',)
        )]
        word_progress = {}
        for table in tables:
            lang = table[len(prefix):]
            word_progress[lang] = _table_rows(conn, table)
        session_table = sessions_table_name(user_s)
        sessions = _table_rows(conn, session_table) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (session_table,)
        ).fetchone() else []
        ensure_dataset_progress_table(conn)
        gauntlet = [dict(zip(('user', 'lang', 'current_stage', 'current_day', 'sessions_done_today', 'last_practice_date'), row))
                    for row in conn.execute(
                        'SELECT user, lang, current_stage, current_day, sessions_done_today, last_practice_date '
                        'FROM dataset_progress WHERE user = ? ORDER BY lang', (user_s,)
                    )]
        return {
            'format': BACKUP_FORMAT,
            'version': BACKUP_VERSION,
            'user': {'name': user_row[0], 'created_at': user_row[1]},
            'word_progress': word_progress,
            'sessions': sessions,
            'gauntlet_progress': gauntlet,
        }
    finally:
        conn.close()


def _validate_backup_rows(rows, allowed_columns, label):
    if not isinstance(rows, list):
        raise ValueError(f'{label} must be an array.')
    validated = []
    required = set(allowed_columns)
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f'{label} row {number} must be an object.')
        keys = set(row)
        unknown = keys - required
        missing = required - keys
        if unknown or missing:
            detail = []
            if unknown:
                detail.append(f"unknown: {', '.join(sorted(unknown))}")
            if missing:
                detail.append(f"missing: {', '.join(sorted(missing))}")
            raise ValueError(f"{label} row {number} has invalid columns ({'; '.join(detail)}).")
        validated.append({column: row[column] for column in allowed_columns})
    return validated


def import_user_data(user, data):
    """Atomically merge a strict version-1 backup into the requested user's progress."""
    user_s = sanitize_name(user, 'user')
    if not isinstance(data, dict):
        raise ValueError('Backup data must be an object.')
    if data.get('format') != BACKUP_FORMAT or data.get('version') != BACKUP_VERSION:
        raise ValueError('Unsupported backup format or version.')
    backup_user = data.get('user')
    if not isinstance(backup_user, dict) or backup_user.get('name') != user_s:
        raise ValueError('Backup user does not match the requested user.')
    word_progress = data.get('word_progress')
    sessions = data.get('sessions')
    gauntlet = data.get('gauntlet_progress')
    if not isinstance(word_progress, dict) or not isinstance(sessions, list) or not isinstance(gauntlet, list):
        raise ValueError('Backup must include word_progress, sessions, and gauntlet_progress arrays.')

    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        ensure_user(conn, user_s)
        if backup_user.get('created_at') is not None:
            conn.execute('UPDATE users SET created_at = ? WHERE name = ?', (backup_user['created_at'], user_s))
        session_table = ensure_sessions_table(conn, user_s)
        ensure_dataset_progress_table(conn)
        session_columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{session_table}")')]
        session_rows = _validate_backup_rows(sessions, session_columns, 'sessions')
        gauntlet_columns = ['user', 'lang', 'current_stage', 'current_day', 'sessions_done_today', 'last_practice_date']
        gauntlet_rows = _validate_backup_rows(gauntlet, gauntlet_columns, 'gauntlet_progress')
        for row in gauntlet_rows:
            if row['user'] != user_s:
                raise ValueError('Gauntlet progress belongs to another user.')
            sanitize_name(str(row['lang']), 'language')

        validated_progress = {}
        for lang, rows in word_progress.items():
            lang_s = sanitize_name(str(lang), 'language')
            table = ensure_word_table(conn, user_s, lang_s)
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            validated_progress[table] = _validate_backup_rows(rows, columns, f'word_progress.{lang_s}')

        for table, rows in validated_progress.items():
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            if rows:
                quoted = ', '.join(f'"{column}"' for column in columns)
                placeholders = ', '.join('?' for _ in columns)
                conn.executemany(
                    f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})',
                    [[row[column] for column in columns] for row in rows],
                )
        if session_rows:
            quoted = ', '.join(f'"{column}"' for column in session_columns)
            placeholders = ', '.join('?' for _ in session_columns)
            conn.executemany(
                f'INSERT OR REPLACE INTO "{session_table}" ({quoted}) VALUES ({placeholders})',
                [[row[column] for column in session_columns] for row in session_rows],
            )
        if gauntlet_rows:
            conn.executemany(
                'INSERT OR REPLACE INTO dataset_progress '
                '(user, lang, current_stage, current_day, sessions_done_today, last_practice_date) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [[row[column] for column in gauntlet_columns] for row in gauntlet_rows],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def save_custom_list(user, list_name, items):
    """Validate and atomically save a personal imported material list."""
    user_s = sanitize_name(user, 'user')
    list_name_s = sanitize_name(list_name, 'list')
    if isinstance(items, dict):
        data = items
    elif isinstance(items, list):
        data = {
            'metadata': {'language': 'unknown', 'type': 'vocabulary', 'cefr_level': 'all'},
            'items': items,
        }
    else:
        raise ValueError('Custom list must be a JSON object or item array.')
    if not isinstance(data.get('metadata'), dict) or not isinstance(data.get('items'), list):
        raise ValueError('Custom list must contain metadata and an items array.')
    data = {'metadata': dict(data['metadata']), 'items': validate_word_list_items(data['items'], '<custom import>', require_explicit_ids=True)}
    file_path = word_list_path_user_specific(user_s, list_name_s)
    write_word_list_atomic(file_path, data)
    sync_word_list(user_s, list_name_s)
    return list_name_s

def build_parser():
    parser = argparse.ArgumentParser(
        prog='tartarus',
        description="An interactive CLI tool for vocabulary practice with multi-user, multi-language word lists.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Usage Examples:
  # First time setup for a user
  make init user=learner

  # Start a practice session; macOS uses local say speech when available
  make practice user=learner list=tartarus_sample_english_a1

  # View progress report
  make report user=learner list=tartarus_sample_english_a1

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
  !word         -> Flag word as difficult without changing its score.
  @word         -> Mark word as known (score becomes 9.0).
  $word         -> Start a strict 9-repetition drill for the current word
                    without changing its score.

"""
    )
    subparsers = parser.add_subparsers(dest='command')

    practice_parser = subparsers.add_parser('practice', help="Start a practice session.")
    practice_parser.add_argument('--user', required=True, help="Username (lowercase letters, digits, underscores).")
    practice_parser.add_argument('--lang', required=True, help="Word list / language to practice.")
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
    configure_logging()
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
