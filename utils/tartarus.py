# -*- coding: utf-8 -*-
import contextlib
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
import threading
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
    logger.info(f"{event_type} | {details}")


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


def answer_matches(answer, word_text):
    """Return True only for an exact dataset-target match.

    Learning content is deliberately strict: no trimming, case-folding,
    comma-form splitting/reordering, Unicode normalization, or fuzzy matching.
    Transport controls are parsed outside this function.
    """
    return str(answer) == str(word_text)



def mask_sentence(sentence, score):
    """Mask only learnable letters/digits while preserving text structure.

    Whitespace and punctuation are never replaced by underscores: they remain
    literal separators in visible, faded, and fully masked states.  This keeps
    sentences readable while exact answer checking still requires the learner to
    type every space and punctuation mark correctly.
    """
    sentence = str(sentence)
    if score <= 0:
        return sentence
    visible_ratio = 0.0 if score >= 8 else max(0.15, 1.0 - (float(score) / 8.0))
    positions = [i for i, ch in enumerate(sentence) if ch.isalnum()]
    if not positions:
        return sentence
    num_visible = 0 if visible_ratio == 0 else max(1, int(len(positions) * visible_ratio))
    visible_indices = set(random.sample(positions, num_visible))
    return ''.join(
        ch if (not ch.isalnum() or i in visible_indices) else '_'
        for i, ch in enumerate(sentence)
    )


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
        word = str(item.get('word', item.get('text', '')))
        if not word.strip():
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
    """Open the configured progress database without implicit schema writes."""
    return sqlite3.connect(DATABASE_FILE)


def ensure_users_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')


def ensure_user(conn, user):
    ensure_users_table(conn)
    user = sanitize_name(user, 'user')
    cursor = conn.execute('INSERT OR IGNORE INTO users(name) VALUES (?)', (user,))
    if cursor.rowcount:
        log_event('USER_CREATED', user=user)
    return user


def words_table_name(user, lang):
    return f"words_{sanitize_name(user, 'user')}_{sanitize_name(lang, 'language')}"


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


MASTERY_EVENT_TYPES = ('mastered', 'box10')
MASTERY_EVENT_BACKUP_COLUMNS = ['lang', 'word_id', 'event_type', 'mastered_date']


def ensure_mastery_events_table(conn):
    """Create the append-only reporting ledger without altering progress state."""
    conn.execute('''CREATE TABLE IF NOT EXISTS mastery_events (
        id INTEGER PRIMARY KEY,
        user TEXT NOT NULL,
        lang TEXT NOT NULL,
        word_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('mastered', 'box10')),
        mastered_date TEXT NOT NULL,
        UNIQUE(user, lang, word_id, event_type)
    )''')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_mastery_events_user_lang_type_date '
        'ON mastery_events(user, lang, event_type, mastered_date)'
    )


def record_mastery_event(conn, user, lang, word_id, event_type, event_date):
    """Append one transition event; retries and repeated answers stay idempotent."""
    if event_type not in MASTERY_EVENT_TYPES:
        raise ValueError(f'Unsupported mastery event type: {event_type}')
    ensure_mastery_events_table(conn)
    conn.execute(
        'INSERT OR IGNORE INTO mastery_events(user,lang,word_id,event_type,mastered_date) '
        'VALUES(?,?,?,?,?)',
        (sanitize_name(user, 'user'), sanitize_name(lang, 'language'), int(word_id), event_type, str(event_date)[:10]),
    )

SCHEMA_VERSION = 5


WORD_TABLE_COLUMNS = [
    'id', 'content_id', 'score', 'last_practiced', 'last_tartarus_completed',
    'active', 'times_practiced', 'times_correct', 'times_incorrect',
    'times_drilled', 'times_mastered', 'leitner_box', 'leitner_last_reviewed',
]


def table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone() is not None


def word_table_schema(table):
    return f"""
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0.0,
            last_practiced TEXT,
            last_tartarus_completed TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            leitner_last_reviewed TEXT
        )
    """


def _word_table_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _create_v4_word_table(conn, table):
    conn.execute(word_table_schema(table))


def _copy_word_table_to_v4(conn, source, target):
    columns = set(_word_table_columns(conn, source))
    if not columns:
        raise ValueError(f'Cannot migrate missing table {source}.')
    if 'content_id' in columns:
        content_expr = 'content_id'
    elif 'id' in columns:
        content_expr = "'legacy:' || id"
    else:
        raise ValueError(f'Cannot migrate {source}: no content identity column.')

    def col(name, default='NULL'):
        return name if name in columns else default

    id_expr = col('id', 'NULL')
    score = col('score', '0.0')
    last = col('last_practiced')
    box = col('leitner_box')
    tartarus = col('last_tartarus_completed', last)
    leitner_last = col(
        'leitner_last_reviewed',
        f"CASE WHEN COALESCE({score},0) >= 9.0 AND {box} IS NOT NULL THEN {last} ELSE NULL END",
    )
    select_exprs = [
        id_expr, content_expr, score, last, tartarus, col('active', '1'),
        col('times_practiced', '0'), col('times_correct', '0'),
        col('times_incorrect', '0'), col('times_drilled', '0'),
        col('times_mastered', '0'), box, leitner_last,
    ]
    quoted = ', '.join(f'"{c}"' for c in WORD_TABLE_COLUMNS)
    conn.execute(
        f'INSERT INTO "{target}" ({quoted}) SELECT {", ".join(select_exprs)} FROM "{source}"'
    )


def _audit_word_table(conn, table):
    """Return a preservation manifest using only columns present in ``table``."""
    columns = set(_word_table_columns(conn, table))
    def sum_expr(name):
        return f'COALESCE(SUM({name}),0)' if name in columns else '0'
    distinct = 'COUNT(DISTINCT content_id)' if 'content_id' in columns else 'COUNT(*)'
    row = conn.execute(
        f'SELECT COUNT(*), {distinct}, {sum_expr("score")}, {sum_expr("times_practiced")}, '
        f'{sum_expr("times_correct")}, {sum_expr("times_incorrect")}, {sum_expr("times_drilled")}, '
        f'{sum_expr("times_mastered")}, {sum_expr("leitner_box")} FROM "{table}"'
    ).fetchone()
    return tuple(row)


def _preserved_word_rows(conn, table):
    """Snapshot values that v4 promises to preserve exactly, keyed by row id."""
    columns = set(_word_table_columns(conn, table))
    names = [name for name in (
        'id','content_id','score','last_practiced','active','times_practiced',
        'times_correct','times_incorrect','times_drilled','times_mastered','leitner_box'
    ) if name in columns]
    if not names:
        return []
    quoted = ','.join(f'"{name}"' for name in names)
    return (tuple(names), conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY id').fetchall())


def verified_database_backup(database_file=None, label='snapshot'):
    """Create and fsync a SQLite-consistent backup after checking both databases."""
    db_path=os.path.abspath(database_file or DATABASE_FILE)
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    safe_label=re.sub(r'[^a-z0-9-]+','-',str(label).lower()).strip('-') or 'snapshot'
    stamp=datetime.now().strftime('%Y%m%d%H%M%S%f')
    backup_path=f'{db_path}.pre-{safe_label}.{stamp}.sqlite'
    source=sqlite3.connect(f'file:{db_path}?mode=ro',uri=True)
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Database integrity check failed before backup.')
        target=sqlite3.connect(backup_path)
        try:
            source.backup(target); target.commit()
        finally: target.close()
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(backup_path)
        raise
    finally: source.close()
    with open(backup_path,'rb+') as handle:
        handle.flush(); os.fsync(handle.fileno())
    check=sqlite3.connect(f'file:{backup_path}?mode=ro',uri=True)
    try:
        if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Backup integrity check failed.')
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(backup_path)
        raise
    finally: check.close()
    return backup_path

def _database_audit_manifest(conn):
    result = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'words_%' ORDER BY name"
    ):
        if table.endswith('_legacy') or '__v4_' in table:
            raise ValueError(f'Unexpected migration scratch table: {table}')
        result[table] = _audit_word_table(conn, table)
    result['mastery_event_rows'] = (
        conn.execute('SELECT COUNT(*) FROM mastery_events').fetchone()[0]
        if table_exists(conn, 'mastery_events') else 0
    )
    result['session_rows'] = sum(
        conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions_%'"
        )
    )
    return result


def migrate_database(database_file=None, *, create_backup=True, fail_after_tables=None):
    """Atomically migrate progress storage to the current schema.

    A SQLite-consistent verified backup is created before mutation unless
    ``create_backup`` is false. The whole database migration is one transaction.
    ``fail_after_tables`` exists only for rollback tests.
    """
    db_path = database_file or DATABASE_FILE
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    if not os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        ensure_mastery_events_table(conn)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit(); conn.close()
        return None

    source = sqlite3.connect(db_path)
    backup_path = None
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed before migration.')
        version = source.execute('PRAGMA user_version').fetchone()[0]
        tables = [r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'words_%' ORDER BY name"
        )]
        already_current = version >= SCHEMA_VERSION and not table_exists(source, 'dataset_progress') and all(
            _word_table_columns(source, table) == WORD_TABLE_COLUMNS for table in tables
        )
        if already_current:
            return None
        before = _database_audit_manifest(source)
        if create_backup:
            backup_path = verified_database_backup(db_path, f'v{SCHEMA_VERSION}')

        source.execute('BEGIN IMMEDIATE')
        ensure_mastery_events_table(source)
        migrated = 0
        for table in tables:
            columns = _word_table_columns(source, table)
            if columns == WORD_TABLE_COLUMNS:
                continue
            scratch = f'{table}__v4_{uuid.uuid4().hex[:8]}'
            _create_v4_word_table(source, scratch)
            src_count = source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            preserved = _preserved_word_rows(source, table)
            _copy_word_table_to_v4(source, table, scratch)
            dst_count = source.execute(f'SELECT COUNT(*) FROM "{scratch}"').fetchone()[0]
            if src_count != dst_count:
                raise ValueError(f'Row-count mismatch while migrating {table}.')
            if preserved:
                names, expected_rows = preserved
                quoted = ','.join(f'"{name}"' for name in names)
                actual_rows = source.execute(f'SELECT {quoted} FROM "{scratch}" ORDER BY id').fetchall()
                if actual_rows != expected_rows:
                    raise ValueError(f'Preserved row values changed while migrating {table}.')
            source.execute(f'DROP TABLE "{table}"')
            source.execute(f'ALTER TABLE "{scratch}" RENAME TO "{table}"')
            migrated += 1
            if fail_after_tables is not None and migrated >= fail_after_tables:
                raise RuntimeError('Injected migration failure')

        if table_exists(source, 'dataset_progress'):
            source.execute('DROP TABLE dataset_progress')
        source.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        after = _database_audit_manifest(source)
        for table, audit in before.items():
            if table.startswith('words_') and table in after and after[table] != audit:
                raise ValueError(f'Progress audit mismatch after migrating {table}.')
        source.commit()
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed after migration.')
        return backup_path
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()


def initialize_database(*, create_backup=True):
    """Initialize/migrate the configured progress DB at an explicit boundary."""
    migrate_database(DATABASE_FILE, create_backup=create_backup)
    conn = get_connection()
    try:
        ensure_users_table(conn)
        ensure_mastery_events_table(conn)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    finally:
        conn.close()


def ensure_word_table(conn, user, lang):
    """Create a fresh v4 word table or verify an already-migrated one."""
    ensure_mastery_events_table(conn)
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        _create_v4_word_table(conn, table)
    columns = _word_table_columns(conn, table)
    if columns != WORD_TABLE_COLUMNS:
        raise RuntimeError(
            f'Progress table {table} is not schema v4. Run initialize_database() before use.'
        )
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
GAUNTLET_COMPLETE_DAY = 11
# Day 11 is a terminal Tartarus state; Leitner maintenance may continue.


def gauntlet_stage_for_day(day):
    """Return (stage_num, stage_name, session_mode) for Gauntlet day 0..11."""
    day = int(day or 0)
    for stage, day_min, day_max, name, mode in GAUNTLET_STAGE_MAP:
        if day_min <= day <= day_max:
            return stage, name, mode
    if day >= GAUNTLET_COMPLETE_DAY:
        return 5, 'Ascension', 'complete'
    return 0, 'The Forging', 'forging'



def word_gauntlet_day(mastered_date, today):
    """Return the word's reinforcement day, clamped to the 10-day track."""
    days = (
        date.fromisoformat(str(today)[:10])
        - date.fromisoformat(str(mastered_date)[:10])
    ).days
    return min(max(days, 1), GAUNTLET_MAX_DAY)


def _reinforcement_rows(conn, user, lang, today, *, due_only=False):
    """Return score-9 rows still inside their independent 10-day tracks."""
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        return []
    today = str(today)[:10]
    due_clause = (
        'AND (w.last_tartarus_completed IS NULL OR w.last_tartarus_completed < ?)'
        if due_only else ''
    )
    params = [user, lang]
    if due_only:
        params.append(today)
    rows = conn.execute(
        f'SELECT w.id,w.content_id,w.score,w.leitner_box,w.last_tartarus_completed,e.mastered_date '
        f'FROM "{table}" AS w LEFT JOIN mastery_events AS e '
        'ON e.user=? AND e.lang=? AND e.word_id=w.id AND e.event_type=\'mastered\' '
        f'WHERE w.active=1 AND w.score>=9.0 {due_clause} ORDER BY w.id',
        params,
    ).fetchall()
    result = []
    today_date = date.fromisoformat(today)
    for row_id, content_id, score, box, last_completed, mastered_date in rows:
        if mastered_date:
            age = (today_date - date.fromisoformat(str(mastered_date)[:10])).days
            if age > GAUNTLET_MAX_DAY:
                continue
            day = word_gauntlet_day(mastered_date, today)
        else:
            # Schema-v5 databases normally have an event for every score-9
            # transition. An old or manually edited row is still surfaced as
            # Day 1 rather than silently disappearing from reinforcement.
            day = 1
        stage, stage_name, mode = gauntlet_stage_for_day(day)
        result.append({
            'id': row_id,
            'content_id': content_id,
            'score': score,
            'leitner_box': box,
            'last_tartarus_completed': last_completed,
            'mastered_date': mastered_date,
            'day': day,
            'stage': stage,
            'stage_name': stage_name,
            'mode': mode,
        })
    return result


def _gauntlet_tasks_remaining(conn, user, lang, practice_date):
    """Count due reinforcement tasks across every active mastery cohort."""
    return len(_reinforcement_rows(
        conn, user, lang, practice_date, due_only=True
    ))


def get_gauntlet_tasks_remaining(user, lang, practice_date=None):
    """Return due per-word reinforcement tasks for one calendar date."""
    conn = get_connection()
    try:
        return _gauntlet_tasks_remaining(
            conn, user, lang, practice_date or date.today().isoformat()
        )
    finally:
        conn.close()


def get_words_for_gauntlet_stage(user, lang, stage, num_words=None, today=None):
    """Select Forging work; reinforcement is selected per word separately."""
    if int(stage or 0) != 0:
        raise ValueError('Reinforcement stages are selected per word.')
    num_words = MAX_QUESTIONS if num_words is None else num_words
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            raise ValueError('No progress table exists for this list.')
        rows = conn.execute(
            f'SELECT id,content_id,score,leitner_box FROM "{table}" '
            'WHERE active=1 AND score < 9.0'
        ).fetchall()
    finally:
        conn.close()
    candidates = []
    positions = {}
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item:
            positions[row_id] = item['position']
            candidates.append((
                row_id, item['word'], item['definition'], score, box,
                item['word_frequency'],
            ))
    if not candidates:
        raise ValueError('The Forging is complete for this list.')
    candidates.sort(key=lambda row: (-row[3], positions[row[0]], row[0]))
    selected = candidates[:num_words]
    ordered = []
    index = 0
    while index < len(selected):
        score = selected[index][3]
        end = index + 1
        while end < len(selected) and selected[end][3] == score:
            end += 1
        group = selected[index:end]
        random.shuffle(group)
        ordered.extend(group)
        index = end
    return ordered


def get_words_for_reinforcement(user, lang, num_words=None, today=None):
    """Select due words from all independent mastery cohorts."""
    num_words = MAX_QUESTIONS if num_words is None else num_words
    today = today or date.today().isoformat()
    material = {
        item['content_id']: item
        for item in load_practice_items(word_list_path(user, lang))
    }
    conn = get_connection()
    try:
        rows = _reinforcement_rows(conn, user, lang, today, due_only=True)
    finally:
        conn.close()
    candidates = []
    for row in rows:
        item = material.get(row['content_id'])
        if not item:
            continue
        candidates.append((
            row['id'], item['word'], item['definition'], row['score'],
            row['leitner_box'], item['word_frequency'], row['mode'],
            row['stage'], row['stage_name'], row['day'],
        ))
    random.shuffle(candidates)
    return candidates[:num_words]


def gauntlet_state_breakdown(user, lang, today=None, conn=None):
    """Return cohort counts without creating or advancing mutable state."""
    today = str(today or date.today().isoformat())[:10]
    close = conn is None
    if close:
        conn = get_connection()
    try:
        table = words_table_name(user, lang)
        if not table_exists(conn, table):
            total = forging = mastered = 0
        else:
            total, forging, mastered = conn.execute(
                f'SELECT COUNT(*),'
                'SUM(CASE WHEN score<9.0 THEN 1 ELSE 0 END),'
                'SUM(CASE WHEN score>=9.0 THEN 1 ELSE 0 END) '
                f'FROM "{table}" WHERE active=1'
            ).fetchone()
            forging = int(forging or 0)
            mastered = int(mastered or 0)
        track_rows = _reinforcement_rows(conn, user, lang, today)
        due = _gauntlet_tasks_remaining(conn, user, lang, today)
        stage_counts = {stage: 0 for stage in range(1, 6)}
        for row in track_rows:
            stage_counts[row['stage']] += 1
        stages = []
        for stage, day_min, day_max, name, mode in GAUNTLET_STAGE_MAP[1:]:
            stages.append({
                'stage': stage,
                'name': name,
                'mode': mode,
                'days': f'{day_min}-{day_max}',
                'count': stage_counts[stage],
            })
        reinforcement = len(track_rows)
        long_term = max(0, mastered - reinforcement)
        available = due if due else forging
        complete = bool(total and forging == 0 and reinforcement == 0)
        return {
            'total_tasks': int(total or 0),
            'forging': forging,
            'mastered_total': mastered,
            'reinforcement_total': reinforcement,
            'reinforcement_stages': stages,
            'long_term_review': long_term,
            'due_reinforcement': due,
            'available_tasks': available,
            'complete': complete,
            'locked_today': bool(
                forging == 0 and reinforcement > 0 and due == 0
            ),
        }
    finally:
        if close:
            conn.close()


def maintenance_ready_words(user, lang, num_words=None, today=None):
    """Return score-9 items ready for Leitner maintenance, without mutation."""
    num_words = MAX_QUESTIONS if num_words is None else num_words
    today_date = date.fromisoformat(today or date.today().isoformat())
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            return []
        rows = conn.execute(
            f'SELECT id,content_id,score,leitner_box,leitner_last_reviewed FROM "{table}" '
            f'WHERE active=1 AND score >= 9.0 AND leitner_box IS NOT NULL ORDER BY id'
        ).fetchall()
    finally:
        conn.close()
    ready = []
    for row_id, content_id, score, box, last_reviewed in rows:
        box = int(box or 1)
        interval = LEITNER_INTERVALS.get(box, 10)
        is_ready = last_reviewed is None
        if last_reviewed:
            reviewed = date.fromisoformat(str(last_reviewed)[:10])
            is_ready = (today_date - reviewed).days >= interval
        if is_ready and content_id in material:
            item = material[content_id]
            ready.append((
                row_id, item['word'], item['definition'], score, box,
                item['word_frequency'],
            ))
    return ready[:num_words]


def maintenance_next_date(leitner_box, leitner_last_reviewed):
    if not leitner_box or not leitner_last_reviewed:
        return None
    reviewed = date.fromisoformat(str(leitner_last_reviewed)[:10])
    return (
        reviewed + timedelta(days=LEITNER_INTERVALS.get(int(leitner_box), 10))
    ).isoformat()


def _with_stage(rows, mode, stage, stage_name, day):
    return [
        (*row, mode, stage, stage_name, day)
        for row in rows
    ]


def select_practice_words(user, lang, today=None):
    """Choose due Leitner, then due reinforcement, then Forging work."""
    today = today or date.today().isoformat()
    state = gauntlet_state_breakdown(user, lang, today)

    words = maintenance_ready_words(user, lang, today=today)
    if words:
        words = _with_stage(
            words, 'maintenance', 5, 'Leitner Maintenance', 0
        )
        return (
            words, 'maintenance', 'maintenance', 5,
            'Leitner Maintenance', 0, state,
        )

    words = get_words_for_reinforcement(user, lang, today=today)
    if words:
        first = words[0]
        return (
            words, 'tartarus', first[6], first[7], first[8], first[9], state,
        )

    if state['forging']:
        words = _with_stage(
            get_words_for_gauntlet_stage(user, lang, 0, today=today),
            'forging', 0, 'The Forging', 0,
        )
        return words, 'tartarus', 'forging', 0, 'The Forging', 0, state

    return [], 'tartarus', 'complete', 5, 'Complete', GAUNTLET_COMPLETE_DAY, state


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


def sync_word_list(user, lang):
    """Synchronize JSON identities at an explicit mutation boundary."""
    initialize_database(create_backup=False)
    path = word_list_path(user, lang)
    entries = load_practice_items(path)
    conn = get_connection()
    try:
        table = ensure_word_table(conn, user, lang)
        ensure_user(conn, user); ensure_sessions_table(conn, user)
        seen_ids={entry['content_id'] for entry in entries}
        previously_active={cid for (cid,) in conn.execute(f'SELECT content_id FROM "{table}" WHERE active=1')}
        before_count=conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.executemany(
            f'INSERT OR IGNORE INTO "{table}" (content_id) VALUES (?)',
            ((entry['content_id'],) for entry in entries),
        )
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS tartarus_seen_ids (content_id TEXT PRIMARY KEY)')
        conn.execute('DELETE FROM tartarus_seen_ids')
        conn.executemany(
            'INSERT INTO tartarus_seen_ids(content_id) VALUES (?)',
            ((content_id,) for content_id in seen_ids),
        )
        conn.execute(
            f'UPDATE "{table}" SET active=0 WHERE active=1 AND NOT EXISTS '
            f'(SELECT 1 FROM tartarus_seen_ids WHERE tartarus_seen_ids.content_id="{table}".content_id)'
        )
        conn.execute(
            f'UPDATE "{table}" SET active=1 WHERE active=0 AND EXISTS '
            f'(SELECT 1 FROM tartarus_seen_ids WHERE tartarus_seen_ids.content_id="{table}".content_id)'
        )
        added=conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]-before_count
        conn.commit()
    finally:
        conn.close()
    deactivated=len(previously_active-seen_ids)
    if added or deactivated:
        log_event('WORD_LIST_SYNCED', user=user, lang=lang, added=added, deactivated=deactivated, total=len(entries))


def reset_word_list_progress(user, lang):
    """Restart one list while preserving factual session history.

    Scores, completion markers, Leitner state, and milestone events are cleared.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            raise ValueError(f"No progress exists yet for '{lang}'.")
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            f'UPDATE "{table}" SET score=0.0, last_practiced=NULL, last_tartarus_completed=NULL, '
            'times_practiced=0, times_correct=0, times_incorrect=0, times_drilled=0, times_mastered=0, '
            'leitner_box=NULL, leitner_last_reviewed=NULL'
        )
        ensure_mastery_events_table(conn)
        conn.execute('DELETE FROM mastery_events WHERE user=? AND lang=?', (user, lang))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_event('PROGRESS_RESET', user=user, lang=lang)


_PRACTICE_ITEM_CACHE = {}
_PRACTICE_ITEM_CACHE_LOCK = threading.RLock()


def load_practice_items(path):
    """Load validated material, invalidating the process cache on file change."""
    path = os.path.abspath(os.fspath(path))
    stat = os.stat(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _PRACTICE_ITEM_CACHE_LOCK:
        cached = _PRACTICE_ITEM_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
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
    with _PRACTICE_ITEM_CACHE_LOCK:
        _PRACTICE_ITEM_CACHE[path] = (signature, items)
    return items


# --- Practice / Scoring Logic ---
# The lower an item's score, the more of its answer remains visible.
MAX_QUESTIONS = 16   # unique words per session (each asked exactly once)

LEITNER_INTERVALS = {box: box for box in range(1, 11)}  # box -> days until review

SCORE_DELTA = 0.5

def score_band(score):
    """Return the integer score band for a 0.0-9.0, half-point scale."""
    return min(9, max(0, int(float(score))))


def score_color_band(score):
    """Return the 3-way visual color band a score falls into: 1=red (<4),
    2=yellow (4-7.9), 3=green (>=8). This is a coarser grouping than
    score_band's 0-9 mastery band and is the one both score_gauge and any
    web gauge-color rendering must derive from, so there is exactly one
    definition of "what color is this score" in the codebase."""
    if score >= 8:
        return 3
    if score >= 4:
        return 2
    return 1


def score_gauge(score, ansi=True):
    """Returns a 3-dot growth gauge for a word's score.
    If ansi=True (default), includes ANSI color codes for terminal.
    If ansi=False, returns plain Unicode dots for web."""
    color = {1: Colors.RED, 2: Colors.YELLOW, 3: Colors.GREEN}[score_color_band(score)]
    if score >= 9:
        dots = '●●●'
    elif score >= 8:
        dots = '●●○'
    elif score >= 4:
        dots = '●○○'
    else:
        dots = '○○○'
    return dots if not ansi else f"{color}{dots}{Colors.ENDC}"


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

SHADOWS_DRILL_TARGET = 2

def build_question_data(word_id, word_text, definition, score):
    """Build the ordinary question payload. Stage-specific presentation is added by Web."""
    band=score_band(score)
    question_type='learning' if band < 8 else 'production'
    full_lines=definition.split('\n') if definition else []
    primary=english_definition_only(definition)
    prompt=[primary] if primary else []
    lines=full_lines if question_type=='learning' else prompt
    return {
        'word_id':word_id,'word':mask_sentence(word_text,score),'word_unmasked':word_text,
        'definition':lines,'score':round(score,1),'gauge':score_gauge(score,ansi=False),
        'band':band,'gender':get_gender_style(word_text)[1],'type':question_type,
    }



def _load_progress_row(conn, table, word_id):
    row=conn.execute(
        f'SELECT score,leitner_box,last_tartarus_completed,leitner_last_reviewed FROM "{table}" WHERE id=?',
        (word_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f'Unknown practice item id: {word_id}')
    return row


def record_tartarus_answer(user, lang, word_id, correct, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,leitner_last=_load_progress_row(conn,table,word_id); score=float(score or 0)
        if correct:
            new_score=min(9.0,score+SCORE_DELTA) if score<9 else 9.0
            new_box=box
            new_leitner_last=leitner_last
            if score < 9 <= new_score and box is None:
                new_box=1; new_leitner_last=today
            completed = today if new_score >= 9.0 else None
            conn.execute(
                f'UPDATE "{table}" SET score=?,leitner_box=?,leitner_last_reviewed=?,last_practiced=?,last_tartarus_completed=COALESCE(?,last_tartarus_completed), '
                'times_practiced=times_practiced+1,times_correct=times_correct+1 WHERE id=?',
                (new_score,new_box,new_leitner_last,today,completed,word_id),
            )
            if score < 9.0 <= new_score:
                record_mastery_event(conn, user, lang, word_id, 'mastered', today)
        else:
            new_score=score
            conn.execute(
                f'UPDATE "{table}" SET last_practiced=?,times_practiced=times_practiced+1,times_incorrect=times_incorrect+1 WHERE id=?',
                (today,word_id),
            )
        conn.commit()
    finally: conn.close()
    if correct and score < 9.0 <= new_score:
        log_event('WORD_MASTERED', user=user, lang=lang, word_id=word_id)
    log_event('TARTARUS_ANSWER', user=user, lang=lang, word_id=word_id, correct=correct, score=new_score)
    return new_score


def complete_tartarus_drill(user, lang, word_id, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,leitner_last=_load_progress_row(conn,table,word_id); score=float(score or 0)
        new_score=min(9.0,score+SCORE_DELTA) if score<9 else 9.0
        new_box=box; new_leitner_last=leitner_last
        if score < 9 <= new_score and box is None:
            new_box=1; new_leitner_last=today
        completed = today if new_score >= 9.0 else None
        conn.execute(
            f'UPDATE "{table}" SET score=?,leitner_box=?,leitner_last_reviewed=?,last_practiced=?,last_tartarus_completed=COALESCE(?,last_tartarus_completed), '
            'times_practiced=times_practiced+1,times_drilled=times_drilled+1 WHERE id=?',
            (new_score,new_box,new_leitner_last,today,completed,word_id),
        )
        if score < 9.0 <= new_score:
            record_mastery_event(conn, user, lang, word_id, 'mastered', today)
        conn.commit()
    finally: conn.close()
    if score < 9.0 <= new_score:
        log_event('WORD_MASTERED', user=user, lang=lang, word_id=word_id)
    log_event('TARTARUS_DRILL_COMPLETED', user=user, lang=lang, word_id=word_id, score=new_score)
    return new_score


def record_maintenance_answer(user, lang, word_id, correct, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,_= _load_progress_row(conn,table,word_id)
        if float(score or 0) < 9:
            raise ValueError('Only score-9 items may enter Leitner maintenance.')
        if correct:
            new_box=min(int(box or 1)+1,10)
            conn.execute(
                f'UPDATE "{table}" SET leitner_box=?,leitner_last_reviewed=?,last_practiced=?, '
                'times_practiced=times_practiced+1,times_correct=times_correct+1 WHERE id=?',
                (new_box,today,today,word_id),
            )
            if int(box or 1) < 10 <= new_box:
                record_mastery_event(conn, user, lang, word_id, 'box10', today)
        else:
            new_box=int(box or 1)
            conn.execute(
                f'UPDATE "{table}" SET last_practiced=?,times_practiced=times_practiced+1,times_incorrect=times_incorrect+1 WHERE id=?',
                (today,word_id),
            )
        conn.commit()
    finally: conn.close()
    log_event('LEITNER_ANSWER', user=user, lang=lang, word_id=word_id, correct=correct, box=new_box)
    return 9.0


def complete_maintenance_drill(user, lang, word_id, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,_=_load_progress_row(conn,table,word_id)
        if float(score or 0) < 9: raise ValueError('Maintenance drill requires a score-9 item.')
        new_box=min(int(box or 1)+1,10)
        conn.execute(
            f'UPDATE "{table}" SET leitner_box=?,leitner_last_reviewed=?,last_practiced=?,times_practiced=times_practiced+1,times_drilled=times_drilled+1 WHERE id=?',
            (new_box,today,today,word_id),
        )
        if int(box or 1) < 10 <= new_box:
            record_mastery_event(conn, user, lang, word_id, 'box10', today)
        conn.commit()
    finally: conn.close()
    log_event('LEITNER_DRILL_COMPLETED', user=user, lang=lang, word_id=word_id, box=new_box)
    return 9.0


def show_definition(definition):
    """Print a possibly multi-line definition for the CLI."""
    if not definition:
        return
    for line in str(definition).split('\n'):
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


def _cli_read(prompt, *, allow_quit=True):
    try:
        return input(prompt)
    except KeyboardInterrupt:
        if allow_quit:
            raise
        print("\nA mandatory corrective drill is active; complete it before exiting.")
        return None


def drill_word(user, lang, word_to_drill, word_id, definition, header_text, audio, audio_lang=None, update_score=True, wpm=128, show_word=True, maintenance=False, target=DRILL_TARGET, auto_audio=True, escalate_on_wrong=False):
    """Required repetition loop; a stage target escalates to nine after a mistake."""
    correct_in_a_row=0; first_mistake=None
    while correct_in_a_row < target:
        clear_screen(); print(header_text); print(f"\n--- Mandatory drill {correct_in_a_row + 1}/{target} ---")
        if show_word: print(f"{get_gender_color(word_to_drill)}{word_to_drill}{Colors.ENDC}")
        prompt=english_definition_only(definition)
        if prompt: show_definition(prompt)
        if audio and auto_audio: speak(word_to_drill,audio_lang or lang,wpm=wpm)
        answer=_cli_read('Answer: ',allow_quit=False)
        if answer is None: continue
        if answer == '/replay':
            if audio: speak(word_to_drill,audio_lang or lang,wpm=wpm)
            continue
        if answer == '/quit':
            print('Complete the mandatory drill before exiting.')
            time.sleep(0.4); continue
        if answer_matches(answer,word_to_drill): correct_in_a_row += 1
        else:
            correct_in_a_row = 0
            if escalate_on_wrong and target < DRILL_TARGET:
                target = DRILL_TARGET; first_mistake = answer
                if update_score: record_tartarus_answer(user,lang,word_id,False)
    if update_score:
        result=complete_maintenance_drill(user,lang,word_id) if maintenance else complete_tartarus_drill(user,lang,word_id)
        return first_mistake if escalate_on_wrong else result
    return first_mistake



SESSION_HELP_SENTENCE = "Controls: /replay (audio), /quit or Ctrl+C (end outside a drill)."
SESSION_HELP = SESSION_HELP_SENTENCE


def _cli_stage_prompt(mode, word_text, definition, score):
    """Render the current guided stage without exposing optional pedagogy."""
    primary = english_definition_only(definition)
    if mode == 'forging':
        return mask_sentence(word_text, score), definition
    if mode == 'crucible':
        vowels = 'aeiouAEIOUäöüÄÖÜ'
        return ''.join('_' if ch in vowels else ch for ch in word_text), definition
    if mode in {'shadows', 'depths', 'void', 'ascension', 'maintenance'}:
        return '', primary
    return mask_sentence(word_text, score), definition


def _cli_audio_allowed(mode):
    return mode in {'forging', 'crucible', 'shadows', 'depths', 'maintenance'}


def _cli_audio_automatic(mode):
    return mode in {'forging', 'crucible', 'shadows', 'maintenance'}


def _cli_answer_once(user, lang, row, mode, context, audio, audio_lang, wpm, header):
    word_id, word_text, definition, score, current_box, *_ = row
    shown_word, shown_definition = _cli_stage_prompt(mode, word_text, definition, score)
    clear_screen(); print(header); print('')
    if shown_word:
        print(f"{get_gender_color(word_text)}{shown_word}{Colors.ENDC}")
    if shown_definition:
        show_definition(shown_definition)
    if audio and _cli_audio_automatic(mode):
        speak(word_text, audio_lang or lang, wpm=wpm)
    while True:
        try:
            answer = _cli_read('Answer: ', allow_quit=True)
        except KeyboardInterrupt:
            return 'end', None
        if answer == '/replay':
            if audio and _cli_audio_allowed(mode):
                speak(word_text, audio_lang or lang, wpm=wpm)
            else:
                print('Replay is unavailable in this stage.')
            continue
        if answer == '/quit':
            return 'end', None
        break
    correct = answer_matches(answer, word_text)
    if context == 'maintenance':
        record_maintenance_answer(user, lang, word_id, correct)
    else:
        record_tartarus_answer(user, lang, word_id, correct)
    if correct:
        return 'correct', None
    drill_word(
        user, lang, word_text, word_id, definition, header, audio and _cli_audio_allowed(mode),
        audio_lang=audio_lang, update_score=True, wpm=wpm,
        show_word=(mode != 'shadows'), maintenance=(context == 'maintenance'),
        auto_audio=_cli_audio_automatic(mode),
    )
    return 'drilled', answer


def start_practice_session(user, lang, audio, audio_lang=None, wpm=128):
    """Run the same due-practice-first, per-word-stage engine as the Web UI."""
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    sync_word_list(user, lang)
    words, context, *_ = select_practice_words(user, lang)
    if not words:
        print('No learning work is ready for this list right now.')
        return

    correct_count = drilled = answered = 0
    incorrect = []
    started = time.time()
    total = len(words)
    for row in words:
        (
            word_id, word_text, definition, score, current_box, _,
            mode, stage, stage_name, day,
        ) = row
        header = (
            f"--- {stage_name} | Q{answered + 1}/{total} | "
            f"Correct: {correct_count} ---\n{SESSION_HELP}"
        )
        if mode == 'shadows' and context == 'tartarus':
            attempt = drill_word(
                user, lang, word_text, word_id, definition, header, audio,
                audio_lang=audio_lang, update_score=True, wpm=wpm,
                show_word=False, maintenance=False,
                target=SHADOWS_DRILL_TARGET, auto_audio=True,
                escalate_on_wrong=True,
            )
            status = 'drilled'
        else:
            status, attempt = _cli_answer_once(
                user, lang, row, mode, context, audio, audio_lang, wpm, header
            )
        if status == 'end':
            break
        answered += 1
        if status == 'correct':
            correct_count += 1
        elif status == 'drilled':
            drilled += 1
            if attempt is not None:
                incorrect.append((word_text, attempt))

    if answered:
        elapsed = int(time.time() - started)
        log_session(
            user, lang, elapsed, answered, correct_count,
            len(incorrect), drilled,
        )
        print(
            f"\nSession summary: {answered} practiced, "
            f"{correct_count} correct, {len(incorrect)} incorrect, "
            f"{drilled} drilled."
        )


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
    log_event('SESSION_LOGGED', user=user, lang=lang, duration=duration, practiced=practiced,
              correct=correct, incorrect=incorrect, drilled=drilled)


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
    if not any_data:
        print("No practice sessions found.")
    conn.close()


# --- CLI ---
def cmd_init(args):
    log_event('CLI_INIT', user=args.user, lang=args.lang)
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
    action = 'created' if created else 'already exists'
    print(f"JSON list {action}: {path}")
    print("Add material through the web editor or by editing the JSON file, then run practice.")


def cmd_practice(args):
    log_event('CLI_PRACTICE', user=args.user, lang=args.lang)
    initialize_database()
    sync_word_list(args.user,args.lang)
    start_practice_session(args.user,args.lang,sys.platform=='darwin',audio_lang=args.audio_lang or None,wpm=args.wpm)



def cmd_report(args):
    log_event('CLI_REPORT', user=args.user, lang=args.lang)
    initialize_database()
    generate_report(args.user,args.lang)




BACKUP_FORMAT = 'tartarus-progress'
BACKUP_VERSION = 4


def _table_rows(conn, table):
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return [dict(zip(columns, row)) for row in rows]


def export_user_data(user):
    """Read-only v4 logical backup export."""
    user_s=sanitize_name(user,'user'); conn=get_connection()
    try:
        row=conn.execute('SELECT name,created_at FROM users WHERE name=?',(user_s,)).fetchone()
        if row is None: raise ValueError(f"Unknown user '{user_s}'.")
        prefix=f'words_{user_s}_'; word_progress={}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",(prefix+'%',)):
            word_progress[table[len(prefix):]]=_table_rows(conn,table)
        stable=sessions_table_name(user_s)
        sessions=_table_rows(conn,stable) if table_exists(conn,stable) else []
        mastery_events=[]
        if table_exists(conn,'mastery_events'):
            mastery_events=[dict(zip(MASTERY_EVENT_BACKUP_COLUMNS,r)) for r in conn.execute(
                'SELECT lang,word_id,event_type,mastered_date FROM mastery_events '
                'WHERE user=? ORDER BY mastered_date,id',
                (user_s,),
            )]
        result={'format':BACKUP_FORMAT,'version':BACKUP_VERSION,'user':{'name':row[0],'created_at':row[1]},'word_progress':word_progress,'sessions':sessions,'mastery_events':mastery_events}
    finally: conn.close()
    log_event('USER_DATA_EXPORTED', user=user_s, lists=len(word_progress), sessions=len(sessions))
    return result



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
    """Atomically import logical backup versions 1 through 4."""
    user_s=sanitize_name(user,'user')
    version=data.get('version') if isinstance(data,dict) else None
    if not isinstance(data,dict) or data.get('format')!=BACKUP_FORMAT or version not in (1,2,3,4):
        raise ValueError('Unsupported backup format or version.')
    backup_user=data.get('user')
    if not isinstance(backup_user,dict) or backup_user.get('name')!=user_s: raise ValueError('Backup user does not match the requested user.')
    wp=data.get('word_progress'); sessions=data.get('sessions')
    events=data.get('mastery_events',[]) if version<3 else data.get('mastery_events')
    if not isinstance(wp,dict) or not isinstance(sessions,list) or not isinstance(events,list):
        raise ValueError('Backup must include word_progress, sessions, and mastery_events arrays.')
    initialize_database(create_backup=False)
    conn=get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE'); ensure_user(conn,user_s); st=ensure_sessions_table(conn,user_s); ensure_mastery_events_table(conn)
        session_cols=_word_table_columns(conn,st); session_rows=_validate_backup_rows(sessions,session_cols,'sessions')
        prepared={}
        event_rows=_validate_backup_rows(events,MASTERY_EVENT_BACKUP_COLUMNS,'mastery_events')
        for lang,rows in wp.items():
            lang_s=sanitize_name(str(lang),'language'); table=ensure_word_table(conn,user_s,lang_s)
            converted=[]
            for raw in rows:
                if not isinstance(raw,dict): raise ValueError(f'word_progress.{lang_s} rows must be objects.')
                if data.get('version')==1:
                    score=float(raw.get('score',0) or 0); box=raw.get('leitner_box'); last=raw.get('last_practiced')
                    converted.append({
                        'id':raw.get('id'),'content_id':raw.get('content_id'),'score':score,'last_practiced':last,
                        'last_tartarus_completed':last,'active':raw.get('active',1),'times_practiced':raw.get('times_practiced',0),
                        'times_correct':raw.get('times_correct',0),'times_incorrect':raw.get('times_incorrect',0),'times_drilled':raw.get('times_drilled',0),
                        'times_mastered':raw.get('times_mastered',0),'leitner_box':box,
                        'leitner_last_reviewed':last if score>=9 and box is not None else None,
                    })
                else: converted.append(raw)
            prepared[table]=_validate_backup_rows(converted,WORD_TABLE_COLUMNS,f'word_progress.{lang_s}')
        prefix=f'words_{user_s}_'
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",(prefix+'%',)).fetchall():
            if table not in prepared: conn.execute(f'DROP TABLE "{table}"')
        for table,rows in prepared.items():
            conn.execute(f'DELETE FROM "{table}"')
            if rows:
                q=', '.join(f'"{c}"' for c in WORD_TABLE_COLUMNS); ph=', '.join('?' for _ in WORD_TABLE_COLUMNS)
                conn.executemany(f'INSERT INTO "{table}" ({q}) VALUES ({ph})',[[r[c] for c in WORD_TABLE_COLUMNS] for r in rows])
        conn.execute(f'DELETE FROM "{st}"')
        if session_rows:
            q=', '.join(f'"{c}"' for c in session_cols); ph=', '.join('?' for _ in session_cols)
            conn.executemany(f'INSERT INTO "{st}" ({q}) VALUES ({ph})',[[r[c] for c in session_cols] for r in session_rows])
        conn.execute('DELETE FROM mastery_events WHERE user=?',(user_s,))
        if event_rows:
            conn.executemany(
                'INSERT INTO mastery_events(user,lang,word_id,event_type,mastered_date) VALUES(?,?,?,?,?)',
                [(user_s,r['lang'],r['word_id'],r['event_type'],r['mastered_date']) for r in event_rows],
            )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    log_event('USER_DATA_IMPORTED', user=user_s, lists=len(prepared), sessions=len(session_rows))



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
    file_path = word_list_path_user_specific(user_s, list_name_s)
    # Personal imports follow the same Master Schema contract as bundled material:
    # source items may omit IDs.  Persist generated IDs into the personal copy on
    # first import so later edits keep the same progress identity.
    validated_items = validate_word_list_items(data['items'], file_path)
    data = {
        'metadata': canonical_material_metadata(data['metadata'], name=list_name_s),
        'items': validated_items,
    }
    write_word_list_atomic(file_path, data)
    sync_word_list(user_s, list_name_s)
    log_event('CUSTOM_LIST_SAVED', user=user_s, lang=list_name_s, items=len(validated_items))
    return list_name_s

def build_parser():
    parser=argparse.ArgumentParser(prog='tartarus',description='Focused Tartarus + Leitner language practice.')
    sub=parser.add_subparsers(dest='command')
    practice=sub.add_parser('practice',help='Start the guided learning session.')
    practice.add_argument('--user',required=True); practice.add_argument('--lang',required=True)
    practice.add_argument('--audio-lang'); practice.add_argument('--wpm',type=int,default=128)
    report=sub.add_parser('report',help='Show factual practice history.'); report.add_argument('--user',required=True); report.add_argument('--lang')
    init=sub.add_parser('init',help='Create a user and optionally a personal list.'); init.add_argument('--user',required=True); init.add_argument('--lang')
    return parser



def main():
    configure_logging(); initialize_database()
    parser=build_parser()
    if len(sys.argv)==1: parser.print_help(sys.stderr); sys.exit(1)
    args=parser.parse_args()
    try:
        if args.command=='practice': cmd_practice(args)
        elif args.command=='report': cmd_report(args)
        elif args.command=='init': cmd_init(args)
        else: parser.print_help()
    except Exception as exc:
        print(f"\n{Colors.RED}An error occurred: {exc}{Colors.ENDC}"); sys.exit(1)



if __name__ == "__main__":
    main()
