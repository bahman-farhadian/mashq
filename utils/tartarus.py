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
NAME_PATTERN = re.compile(r'^[a-z0-9_\-\.!]+$')

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
            f"Invalid {label} '{name}': only lowercase letters, digits, underscores, hyphens, periods, and exclamation marks are allowed."
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
            # Generated IDs are anchored to stable source coordinates rather than
            # editable JSON content.  Definition/example/frequency edits therefore do
            # not silently create a second learner-progress identity.
            absolute_path = os.path.abspath(path)
            try:
                source_key = os.path.relpath(absolute_path, os.path.abspath(WORD_LISTS_DIR))
            except ValueError:
                source_key = os.path.basename(absolute_path)
            source_key = os.path.normcase(source_key).replace(os.sep, '/')
            digest = hashlib.sha256(f"{source_key}:{index}:{word}".encode('utf-8')).hexdigest()[:24]
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


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


SCHEMA_VERSION = 3


def ensure_word_table(conn, user, lang):
    table = words_table_name(user, lang)
    schema = f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0.0,
            last_practiced DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
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
    migrate_obsolete = 'last_decay_at' in columns or 'stage_reached' in columns or 'drill_pending' in columns or 'times_flagged' in columns

    if migrate_legacy or migrate_leitner or migrate_last_known or migrate_obsolete:
        legacy_table = f'{table}_legacy'
        conn.execute('SAVEPOINT word_table_migration')
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{legacy_table}"')
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy_table}"')
            conn.execute(schema)
            shared = [
                'score', 'last_practiced', 'active', 'times_practiced', 'times_correct',
                'times_incorrect', 'times_drilled', 'times_mastered',
                'leitner_box', 'last_known_review_at',
            ]
            available = {row[1] for row in conn.execute(f'PRAGMA table_info("{legacy_table}")')}
            shared = [column for column in shared if column in available]
            content_id = "'legacy:' || id" if migrate_legacy else 'content_id'
            columns_sql = ', '.join(['content_id', *shared])
            values_sql = ', '.join([
                content_id,
                *shared,
            ])
            conn.execute(
                f'INSERT INTO "{table}" ({columns_sql}) '
                f'SELECT {values_sql} FROM "{legacy_table}"'
            )
            conn.execute(f'DROP TABLE "{legacy_table}"')
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
            conn.execute('RELEASE SAVEPOINT word_table_migration')
        except Exception:
            conn.execute('ROLLBACK TO SAVEPOINT word_table_migration')
            conn.execute('RELEASE SAVEPOINT word_table_migration')
            raise
    elif 'content_id' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN content_id TEXT')
        conn.execute(f"UPDATE \"{table}\" SET content_id = 'legacy:' || id WHERE content_id IS NULL")
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_content_id" ON "{table}" (content_id)')
    current_version = conn.execute('PRAGMA user_version').fetchone()[0]
    if current_version < SCHEMA_VERSION:
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
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


# The learning engine has two explicit paths: Tartarus score progression and
# Leitner box progression. There is intentionally no calendar due scheduler.

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


def canonical_material_metadata(metadata=None, *, name=None, language=None, kind='vocabulary', level='all'):
    """Return Master Schema metadata using canonical ``kind``/``level`` keys.

    Unknown metadata is preserved, while legacy aliases are normalized on new
    personal files.  This keeps readers backward compatible without writing new
    ``type`` / ``cefr_level`` variants.
    """
    result = dict(metadata or {})
    resolved_kind = str(result.pop('type', result.get('kind', kind)) or kind).lower()
    resolved_level = str(result.pop('cefr_level', result.get('level', level)) or level).lower()
    result['name'] = str(result.get('name') or name or 'Untitled')
    result['language'] = str(result.get('language') or language or 'unknown').lower()
    result['kind'] = 'sentences' if resolved_kind == 'sentences' else 'vocabulary'
    result['level'] = resolved_level
    return result


def retire_sample_progress(user):
    """Retire bundled sample progress for one user after personal adoption.

    Shared sample JSON is never touched. Only this user's progress/session
    rows are removed, so another local user keeps their own history.
    """
    user_s = sanitize_name(user, 'user')
    samples = sample_list_ids()
    if not samples:
        return 0
    conn = get_connection()
    removed = 0
    try:
        for sample in samples:
            table = words_table_name(user_s, sample)
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
            ).fetchone():
                conn.execute(f'DROP TABLE "{table}"')
                removed += 1
        session_table = sessions_table_name(user_s)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (session_table,)
        ).fetchone():
            placeholders = ','.join('?' for _ in samples)
            conn.execute(
                f'DELETE FROM "{session_table}" WHERE language IN ({placeholders})',
                tuple(sorted(samples)),
            )
        conn.commit()
    finally:
        conn.close()
    return removed


def sync_word_list(user, lang):
    """Synchronize JSON material IDs to user progress rows only."""
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


def load_practice_items(path):
    """Load validated material."""
    raw_data = read_word_list(path)
    records = validate_word_list_items(raw_data['items'], path)
    items = []
    for position, record in enumerate(records):
        word = record['word']
        definition = normalize_definition(record.get('definition', record.get('translation', word)))
        frequency = normalize_word_frequency(record.get('word_frequency', 0))
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
                         sentence_mode=False, fast_mode=False, drill_all=False, known_drill_mode=False):
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

    if drill_all or known_drill_mode:
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


def complete_drill(user, lang, word_id, known_review=False):
    """Record one completed drill exactly once.

    Normal corrective/manual drills count as one practice event and grant the
    accepted +0.5 score progression.  Known-review drills remain score/Leitner
    neutral and only update their review marker plus drill counter.
    """
    if not known_review:
        return update_word_score(user, lang, word_id, 'drilled')

    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_word_table(conn, user, lang)
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET times_drilled = times_drilled + 1, '
        'last_known_review_at = ? WHERE id = ?',
        (now, word_id),
    )
    conn.commit()
    conn.close()


def record_as_drilled(user, lang, word_id, known_review=False):
    """Compatibility wrapper for the single-source drill completion operation."""
    return complete_drill(user, lang, word_id, known_review)


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
        f'last_practiced = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, now, word_id)
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
        f'last_practiced = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, now, word_id)
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
    table = words_table_name(user, lang)
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
            if result_status == 'drilled' or practiced_today:
                # Drills never advance an already-mastered Leitner item.
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
        set_clauses = ['score = ?', 'leitner_box = ?', 'last_practiced = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, new_box, today]
    elif preserve_box_timestamp:
        # Same-day re-practice of an already-mastered word: bump counters only.
        # Do NOT touch the Leitner timestamp during a same-day review.
        set_clauses = ['score = ?', 'times_practiced = times_practiced + 1']
        params = [new_score]
    else:
        set_clauses = ['score = ?', 'last_practiced = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, today]
    if counter:
        set_clauses.append(f'{counter} = {counter} + 1')
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE {key_column} = ?', params)
    conn.commit()
    conn.close()
    log_event("SCORE_UPDATED", user=user, lang=lang, word_id=word_id, status=result_status, new_score=new_score, new_box=new_box)
    return new_score


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


def _shuffle_score_groups(selected):
    """Keep descending score priority while randomizing only equal-score items."""
    result = []
    index = 0
    while index < len(selected):
        score = selected[index][3]
        end = index + 1
        while end < len(selected) and selected[end][3] == score:
            end += 1
        bucket = selected[index:end]
        random.shuffle(bucket)
        result.extend(bucket)
        index = end
    return result


def get_words_for_practice(user, lang, num_words=MAX_QUESTIONS, known_drill_mode=False, drill_all=False):
    """Return one focused Tartarus session.

    Normal learning follows one deliberately narrow rule:
    1. mastered items (score 9) are excluded from the Tartarus path;
    2. candidates are ranked by score descending, then by JSON position;
    3. the first ``num_words`` (normally 16) become the session pool;
    4. presentation still runs score-high to score-low, but equal-score items
       are shuffled for this session.

    A new file therefore takes the first 16 JSON items (the material files are
    frequency ordered) and randomizes only their presentation order. Across
    later sessions the highest-progress items stay in focus until they reach
    score 9, at which point the next JSON-priority item enters the pool.
    """
    sync_word_list(user, lang)
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box, last_practiced,
                   times_incorrect, times_practiced, last_known_review_at,
                   times_drilled
            FROM "{table}" WHERE active = 1'''
    ).fetchall()
    conn.close()

    candidates = []
    for row in rows:
        row_id, content_id, score, box, last, incorrect, practiced, known_at, drilled = row
        item = material.get(content_id)
        if item is None:
            continue
        if drill_all:
            eligible = True
            order = (item['position'], row_id)
        elif known_drill_mode:
            eligible = score >= 9 and practiced > 0
            order = (known_at is not None, known_at or last or '', item['position'], row_id)
        else:
            eligible = score < 9
            order = (-float(score), item['position'], row_id)
        if eligible:
            candidates.append((order, row_id, item, float(score), box))

    if not candidates:
        if known_drill_mode:
            raise ValueError(
                "No known practiced words to review. Master some words first, then try this mode again."
            )
        if drill_all:
            raise ValueError("No active words are available to drill in this file.")
        if rows:
            raise ValueError(
                "The Tartarus path is complete for this file. All active items have reached score 9.0. "
                "Continue with the Leitner path to finish the file."
            )
        raise ValueError(
            "No active words found for this list. Add words to your word list file and try again."
        )

    candidates.sort(key=lambda candidate: candidate[0])
    selected = candidates[:num_words]
    if not (known_drill_mode or drill_all):
        selected = _shuffle_score_groups(selected)
    return [(row_id, item['word'], item['definition'], score, box, item['word_frequency'])
            for _, row_id, item, score, box in selected]


def get_words_for_leitner(user, lang, num_words=MAX_QUESTIONS):
    """Return one focused Leitner session without calendar due-date gating.

    Only Tartarus-mastered items participate. Box 10 is complete and excluded.
    Higher boxes are kept in focus first so a small set can finish before the
    learner context-switches to lower-box material. JSON order determines
    membership among equal boxes; presentation randomizes only equal-box items.
    """
    sync_word_list(user, lang)
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    rows = conn.execute(
        f'''SELECT id, content_id, score, leitner_box
            FROM "{table}"
            WHERE active = 1 AND score >= 9.0 AND COALESCE(leitner_box, 1) < 10'''
    ).fetchall()
    conn.close()

    candidates = []
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item is None:
            continue
        box = int(box or 1)
        candidates.append(((-box, item['position'], row_id), row_id, item, float(score), box))
    if not candidates:
        raise ValueError(
            "No Leitner items are available. Master Tartarus items first, or this file is already complete."
        )

    candidates.sort(key=lambda candidate: candidate[0])
    selected = candidates[:num_words]
    result = []
    index = 0
    while index < len(selected):
        box = selected[index][4]
        end = index + 1
        while end < len(selected) and selected[end][4] == box:
            end += 1
        bucket = selected[index:end]
        random.shuffle(bucket)
        result.extend(bucket)
        index = end
    return [(row_id, item['word'], item['definition'], score, box, item['word_frequency'])
            for _, row_id, item, score, box in result]


def update_leitner_result(user, lang, word_id, correct):
    """Record one active Leitner-path answer while keeping Tartarus score at 9.

    Correct answers move exactly one box (up to box 10). Incorrect answers keep
    the current box. There is deliberately no calendar due-date gate: the two
    learning paths are explicit user choices rather than a hidden scheduler.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    row = conn.execute(
        f'SELECT score, leitner_box FROM "{table}" WHERE id = ?', (word_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f'Unknown practice item id: {word_id}')
    score, box = row
    if float(score) < 9.0:
        conn.close()
        raise ValueError('Leitner practice requires a Tartarus-mastered item.')
    box = int(box or 1)
    new_box = min(10, box + 1) if correct else box
    counter = 'times_correct' if correct else 'times_incorrect'
    conn.execute(
        f'''UPDATE "{table}" SET score = 9.0, leitner_box = ?, last_practiced = ?,
            last_known_review_at = ?, times_practiced = times_practiced + 1,
            {counter} = {counter} + 1 WHERE id = ?''',
        (new_box, today, now, word_id),
    )
    conn.commit()
    conn.close()
    log_event('LEITNER_UPDATED', user=user, lang=lang, word_id=word_id,
              correct=correct, old_box=box, new_box=new_box)
    return new_box


def get_learning_progress(user, lang):
    """Return compact progress for the two explicit completion paths."""
    sync_word_list(user, lang)
    table = words_table_name(user, lang)
    conn = get_connection()
    row = conn.execute(
        f'''SELECT COUNT(*),
            SUM(CASE WHEN score < 9.0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN score >= 9.0 AND COALESCE(leitner_box, 1) < 10 THEN 1 ELSE 0 END),
            SUM(CASE WHEN score >= 9.0 AND COALESCE(leitner_box, 1) >= 10 THEN 1 ELSE 0 END)
            FROM "{table}" WHERE active = 1'''
    ).fetchone()
    conn.close()
    total, tartarus_remaining, mastered, leitner_remaining, finished = [int(v or 0) for v in row]
    return {
        'total': total,
        'tartarus_remaining': tartarus_remaining,
        'tartarus_mastered': mastered,
        'leitner_remaining': leitner_remaining,
        'leitner_finished': finished,
        'complete': total > 0 and tartarus_remaining == 0 and finished == total,
    }

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
                           item['word_frequency']))
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
        new_score = update_word_score(user, lang, word_id, 'drilled')
        print(f"Score advanced to {new_score:.1f}.")
    time.sleep(1)


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


ERASE_LINE = "\r\033[K"
SESSION_HELP_SENTENCE = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '?' (reveal before mastery), '+' (replay audio), '$' (drill)."
SESSION_HELP = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '$' (drill), '?' (reveal before mastery), '+' (replay audio)."


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


def ask_production(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, sentence_mode=False, wpm=128):
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

    correct = answer_matches(answer, word_text, sentence_mode=sentence_mode)
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


def start_leitner_practice_session(user, lang, audio, audio_lang=None, wpm=128):
    '''Run Leitner only after the Tartarus path for the file is complete.'''
    progress = get_learning_progress(user, lang)
    if progress['tartarus_remaining'] > 0:
        raise ValueError(
            "Tartarus has priority for this file. Master every active item to score 9.0 before continuing with Leitner."
        )
    sentence_mode = is_sentence_list(lang)
    rows = get_words_for_leitner(user, lang, MAX_QUESTIONS)
    start_time = time.time()
    practiced = correct_count = drilled_count = 0
    incorrect_list = []
    try:
        for index, row in enumerate(rows, 1):
            word_id, word_text, definition, score, current_box = row[:5]
            clear_screen()
            print(f"--- Leitner | Q{index}/{len(rows)} | Box {current_box or 1}/10 ---")
            print("Type the mastered item from its definition. Correct moves one box; wrong keeps the box and drills immediately.")
            prompt_definition = english_definition_only(definition)
            if prompt_definition:
                show_definition(prompt_definition)
            if audio:
                speak(word_text, audio_lang or lang, wpm=wpm)
            while True:
                answer = input("Answer: ").strip()
                if answer == '!!':
                    raise KeyboardInterrupt
                if answer == '+':
                    if audio:
                        speak(word_text, audio_lang or lang, wpm=wpm)
                    continue
                if answer == '?':
                    print("Reveal is unavailable on the Leitner path.")
                    continue
                break
            correct = answer_matches(answer, word_text, sentence_mode=sentence_mode)
            practiced += 1
            if correct:
                update_leitner_result(user, lang, word_id, True)
                correct_count += 1
                print("Correct. Advanced one Leitner box.")
            else:
                update_leitner_result(user, lang, word_id, False)
                incorrect_list.append((word_text, answer))
                print("Incorrect. Box preserved; complete the drill.")
                drill_word(
                    user, lang, word_text, word_id, definition,
                    f"--- Leitner Drill | Box {current_box or 1}/10 ---",
                    audio, audio_lang=audio_lang, update_score=False, wpm=wpm,
                )
                complete_drill(user, lang, word_id, known_review=True)
                drilled_count += 1
    except KeyboardInterrupt:
        print("\n\nLeitner session ended early. Saving progress...")

    if practiced == 0:
        print("No Leitner items were completed. Nothing to save.")
        return
    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, practiced, correct_count, len(incorrect_list), drilled_count)
    minutes, seconds = divmod(elapsed_seconds, 60)
    print("\n--- Leitner Session Summary ---")
    print(f"Items completed:     {practiced}")
    print(f"Correct first tries: {correct_count}")
    print(f"Corrective drills:   {drilled_count}")
    print(f"Session time:        {minutes} min {seconds} sec")


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
            clear_screen()
            print(f"--- Fast Mode | Q{index}/{len(queue)} ---")
            print("The word is shown. Type it from memory; mistakes retry the same word.")
            print(f"  {word_text}")
            prompt_definition = english_definition_only(definition)
            if prompt_definition:
                show_definition(prompt_definition)
            if audio:
                speak(word_text, audio_lang or lang, wpm=wpm)

            while True:
                answer = input("Answer: ").strip()
                if answer == '!!':
                    raise KeyboardInterrupt
                if answer == '?':
                    print("Reveal is unavailable for mastered Fast mode material.")
                    continue
                if answer == '+':
                    if audio:
                        speak(word_text, audio_lang or lang, wpm=wpm)
                    continue
                correct = answer_matches(answer, word_text, sentence_mode=sentence_mode)
                if correct:
                    record_fast_review(user, lang, word_id)
                    correct_count += 1
                    print("Correct.")
                    break
                incorrect_list.append((word_text, answer))
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



def start_practice_session(user, lang, audio, audio_lang=None, drill_all=False, instant_drill=False, known_drill_mode=False, wpm=128):
    """Run one CLI practice session over ordinary Master Schema items."""
    sentence_mode = is_sentence_list(lang)
    words = get_words_for_practice(
        user, lang, DRILL_WORDS if drill_all else MAX_QUESTIONS,
        known_drill_mode=known_drill_mode, drill_all=drill_all,
    )
    queue = [
        {'id': r[0], 'word': r[1], 'def': r[2], 'score': r[3], 'box': r[4]}
        for r in words
    ]

    correct_count = 0
    questions_count = 0
    drilled_words_count = 0
    incorrect_list = []
    start_time = time.time()
    total = len(queue)
    mode_label = " [DRILL ALL]" if drill_all else ""
    help_text = SESSION_HELP_SENTENCE if sentence_mode else SESSION_HELP

    def header_text():
        return (
            f"--- Practice{mode_label} | "
            f"Q{questions_count}/{total} | "
            f"Correct: {correct_count} ---\n{help_text}"
        )

    try:
        for entry in queue:
            word_id, word_text, definition, score, current_box = (
                entry['id'], entry['word'], entry['def'], entry['score'], entry['box']
            )
            word_header = f"{score_gauge(score)} (score: {score:.1f}):"
            band = score_band(score)

            if drill_all:
                drill_word(
                    user, lang, word_text, word_id, definition,
                    header_text(), audio, audio_lang=audio_lang,
                    update_score=False, wpm=wpm,
                )
                record_as_drilled(user, lang, word_id)
                status, message, attempt = 'drilled', None, None
            elif known_drill_mode:
                drill_word(
                    user, lang, word_text, word_id, definition,
                    header_text(), audio, audio_lang=audio_lang,
                    update_score=False, wpm=wpm, show_word=False,
                )
                record_as_drilled(user, lang, word_id, known_review=True)
                status, message, attempt = 'drilled', None, None
            elif band < 8:
                status, message, attempt = ask_learning(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    current_box=current_box, sentence_mode=sentence_mode, wpm=wpm,
                )
            else:
                status, message, attempt = ask_production(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    update_score=True, current_box=current_box,
                    sentence_mode=sentence_mode, wpm=wpm,
                )

            if status == 'end':
                print("\n\nSession ended early. Saving progress...")
                break

            questions_count += 1
            if status == 'drilled':
                drilled_words_count += 1
            elif status == 'correct':
                correct_count += 1
            elif status == 'incorrect':
                incorrect_list.append((word_text, attempt))
                if instant_drill:
                    drill_word(
                        user, lang, word_text, word_id, definition,
                        header_text(), audio, audio_lang=audio_lang,
                        update_score=False, wpm=wpm,
                    )
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
    log_session(
        user, lang, elapsed_seconds, questions_count, correct_count,
        len(incorrect_list), drilled_words_count,
    )
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


def print_leitner_summary(conn, user, lang):
    '''Print the explicit Leitner path distribution for one word list.'''
    table = words_table_name(user, lang)
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return
    rows = conn.execute(
        f'''SELECT COALESCE(leitner_box, 1), COUNT(*)
            FROM "{table}" WHERE active = 1 AND score >= 9.0
            GROUP BY COALESCE(leitner_box, 1) ORDER BY COALESCE(leitner_box, 1)'''
    ).fetchall()
    if not rows:
        return
    total_words = sum(row[1] for row in rows)
    finished = sum(row[1] for row in rows if int(row[0]) >= 10)
    box_str = '  '.join(f"Box {row[0]}: {row[1]}" for row in rows)
    print(f"\nLeitner Path  Active: {total_words}  Finished: {finished}")
    print(f"  {box_str}")


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
                print_leitner_summary(conn, user_s, language)
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
    try:
        shared_path = word_list_path(args.user, args.lang)
    except FileNotFoundError:
        shared_path = None
    path = shared_path if shared_path and os.path.exists(shared_path) and shared_path != user_path else user_path
    created = not os.path.exists(path)
    if created and path == user_path:
        write_word_list_atomic(path, {
            'metadata': canonical_material_metadata(
                {'pos': 'all'}, name=args.lang, language='unknown', kind='vocabulary', level='all'
            ),
            'items': [],
        })
    conn = get_connection()
    ensure_word_table(conn, args.user, args.lang)
    ensure_sessions_table(conn, args.user)
    conn.commit()
    conn.close()
    if path == user_path:
        retire_sample_progress(args.user)
    action = 'created' if created else 'already exists'
    print(f"JSON list {action}: {path}")
    if is_read_only_sample_list(args.user, args.lang):
        print("Tartarus sample lists are read-only; use them for evaluation or create a personal list.")
    else:
        print("Add material through the web editor or by editing the JSON file, then run practice.")


def cmd_practice(args):
    audio = sys.platform == 'darwin'
    if args.leitner:
        if args.fast or args.drill or args.instant_drill:
            raise ValueError("Leitner mode cannot be combined with Fast or drill modes.")
        sync_word_list(args.user, args.lang)
        start_leitner_practice_session(
            args.user, args.lang, audio, audio_lang=args.audio_lang or None, wpm=args.wpm,
        )
        return
    if args.fast:
        if args.drill or args.instant_drill:
            raise ValueError("Fast mode cannot be combined with drill modes.")
        start_fast_practice_session(args.user, args.lang, audio,
                                     audio_lang=args.audio_lang or None,
                                     wpm=args.wpm)
        return
    sync_word_list(args.user, args.lang)
    if not args.drill and not args.instant_drill:
        progress = get_learning_progress(args.user, args.lang)
        if progress['tartarus_remaining'] == 0:
            if progress['leitner_remaining'] > 0:
                start_leitner_practice_session(
                    args.user, args.lang, audio, audio_lang=args.audio_lang or None, wpm=args.wpm,
                )
                return
            if progress['complete']:
                print("This file is complete: Tartarus mastery and Leitner progression are both finished.")
                return
    start_practice_session(args.user, args.lang, audio,
                           audio_lang=args.audio_lang or None,
                           drill_all=args.drill,
                           instant_drill=args.instant_drill,
                           known_drill_mode=False,
                           wpm=args.wpm)


def cmd_report(args):
    if args.lang:
        sync_word_list(args.user, args.lang)
    generate_report(args.user, args.lang)



BACKUP_FORMAT = 'tartarus-progress'
BACKUP_VERSION = 2


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
        return {
            'format': BACKUP_FORMAT,
            'version': BACKUP_VERSION,
            'user': {'name': user_row[0], 'created_at': user_row[1]},
            'word_progress': word_progress,
            'sessions': sessions,
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
    """Atomically restore one user's logical progress from a strict backup.

    Restore semantics are replacement, not merge: sessions and per-list progress for this user become exactly the state represented by the
    backup.  Validation and replacement occur inside one transaction so any
    failure leaves the pre-import database unchanged.
    """
    user_s = sanitize_name(user, 'user')
    if not isinstance(data, dict):
        raise ValueError('Backup data must be an object.')
    if data.get('format') != BACKUP_FORMAT or data.get('version') not in (1, BACKUP_VERSION):
        raise ValueError('Unsupported backup format or version.')
    backup_user = data.get('user')
    if not isinstance(backup_user, dict) or backup_user.get('name') != user_s:
        raise ValueError('Backup user does not match the requested user.')
    word_progress = data.get('word_progress')
    sessions = data.get('sessions')
    if not isinstance(word_progress, dict) or not isinstance(sessions, list):
        raise ValueError('Backup must include word_progress and sessions.')

    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        ensure_user(conn, user_s)
        session_table = ensure_sessions_table(conn, user_s)

        session_columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{session_table}")')]
        session_rows = _validate_backup_rows(sessions, session_columns, 'sessions')
        validated_progress = {}
        for lang, rows in word_progress.items():
            lang_s = sanitize_name(str(lang), 'language')
            table = ensure_word_table(conn, user_s, lang_s)
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            validated_progress[table] = (
                columns,
                _validate_backup_rows(rows, columns, f'word_progress.{lang_s}'),
            )

        # Validation is complete. Replacement begins here and remains covered by
        # the same transaction.
        if backup_user.get('created_at') is not None:
            conn.execute('UPDATE users SET created_at = ? WHERE name = ?', (backup_user['created_at'], user_s))

        prefix = f'words_{user_s}_'
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                (prefix + '%',),
            )
        }
        for table in existing_tables - set(validated_progress):
            conn.execute(f'DROP TABLE "{table}"')

        for table, (columns, rows) in validated_progress.items():
            conn.execute(f'DELETE FROM "{table}"')
            if rows:
                quoted = ', '.join(f'"{column}"' for column in columns)
                placeholders = ', '.join('?' for _ in columns)
                conn.executemany(
                    f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                    [[row[column] for column in columns] for row in rows],
                )

        conn.execute(f'DELETE FROM "{session_table}"')
        if session_rows:
            quoted = ', '.join(f'"{column}"' for column in session_columns)
            placeholders = ', '.join('?' for _ in session_columns)
            conn.executemany(
                f'INSERT INTO "{session_table}" ({quoted}) VALUES ({placeholders})',
                [[row[column] for column in session_columns] for row in session_rows],
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
            'metadata': canonical_material_metadata(name=list_name_s),
            'items': items,
        }
    else:
        raise ValueError('Custom list must be a JSON object or item array.')
    if not isinstance(data.get('metadata'), dict) or not isinstance(data.get('items'), list):
        raise ValueError('Custom list must contain metadata and an items array.')
    data = {'metadata': canonical_material_metadata(data['metadata'], name=list_name_s), 'items': validate_word_list_items(data['items'], '<custom import>', require_explicit_ids=True)}
    file_path = word_list_path_user_specific(user_s, list_name_s)
    write_word_list_atomic(file_path, data)
    sync_word_list(user_s, list_name_s)
    retire_sample_progress(user_s)
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
  make practice user=learner list=german_noun_a1_part01

  # View progress report
  make report user=learner list=german_noun_a1_part01

How practice works:
  Every item has a score from 0.0 (new) to 9.0 (mastered). Correct answers
  add 0.5. Scores below 8.0 progressively mask random letters; scores from
  8.0 to 9.0 use a fully masked answer with definition and audio. A mistake
  preserves the score and starts a strict 9-correct drill. Mastered items
  enter Leitner box 1; the separate Leitner path advances them explicitly to box 10.

Special Commands (during a session):
  !! or Ctrl+C  -> End session early and save progress.
  ?             -> Reveal the answer before it has reached mastery.
  +             -> Replay the current word's audio.
  !word         -> Flag word as difficult without changing its score.
  @word         -> Mark word as known (score becomes 9.0).
  $word         -> Start a strict 9-repetition drill for the current word;
                    completing it advances the score by 0.5 (up to 9.0).

"""
    )
    subparsers = parser.add_subparsers(dest='command')

    practice_parser = subparsers.add_parser('practice', help="Start a practice session.")
    practice_parser.add_argument('--user', required=True, help="Username (lowercase letters, digits, _, -, ., !).")
    practice_parser.add_argument('--lang', required=True, help="Word list / language to practice.")
    practice_parser.add_argument('--audio-lang',
                                  help="Override the language used for voice/audio selection.\n"
                                       "Useful when --lang is a sub-list name (e.g. 'german_home') that doesn't\n"
                                       "auto-detect as a language: pass --audio-lang german to still use the\n"
                                       "German 'say' voice. Accepts the same values as --lang (e.g. 'german', 'de').")
    practice_parser.add_argument('--leitner', action='store_true',
                                  help="Leitner path: practice score-9 items and advance boxes 1 through 10.")
    practice_parser.add_argument('--fast', action='store_true',
                                  help="Fast mode: auxiliary mastered-word review; scores and Leitner boxes unchanged.")
    practice_parser.add_argument('--drill', action='store_true',
                                  help="Drill-mode: every word in the session is put through the 9-repetition\n"
                                       "drill automatically, regardless of its score band.")
    practice_parser.add_argument('--instant-drill', action='store_true',
                                  help="Instant drill: after any incorrect answer, immediately start a\n"
                                       "9-repetition drill; completion advances the score by 0.5.")
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
