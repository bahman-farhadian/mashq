# -*- coding: utf-8 -*-
"""
Tartarus web server: a localhost-only JSON API + static frontend that wraps
the same SQLite-backed scoring logic as the tartarus.py CLI. Standard
library only - no extra packages needed.

Run via: make web   (serves http://127.0.0.1:9999)
"""
import os
import sys
import errno
import json
import time
import urllib.parse
import http.server
import uuid
import re
import threading
from pathlib import Path

from datetime import date, timedelta
import tartarus as ll

HOST = os.environ.get('TARTARUS_HOST', '127.0.0.1')
PORT = int(os.environ.get('TARTARUS_PORT', '9999'))

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT_DIR, 'web')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}


# In-memory practice sessions, keyed by a random session id. Lost on
# restart, which is fine - sessions are short-lived and progress is only
# persisted to the database when a word is answered or the session ends.
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()


def positive_environment_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


SESSION_TTL_SECONDS = positive_environment_int('TARTARUS_SESSION_TTL_SECONDS', 30 * 60)
MAX_ACTIVE_SESSIONS = positive_environment_int('TARTARUS_MAX_ACTIVE_SESSIONS', 100)
MAX_REQUEST_BYTES = positive_environment_int('TARTARUS_MAX_REQUEST_BYTES', 1_000_000)


def cleanup_sessions(now=None):
    """Drop expired ephemeral sessions. Corrective drills are session-local only."""
    now = time.time() if now is None else now
    with SESSIONS_LOCK:
        expired = [session_id for session_id, session in SESSIONS.items()
                   if session.get('expires_at', 0) <= now]
        for session_id in expired:
            SESSIONS.pop(session_id, None)
    return len(expired)


def register_session(session_id, session):
    cleanup_sessions()
    with SESSIONS_LOCK:
        if len(SESSIONS) >= MAX_ACTIVE_SESSIONS:
            raise ValueError('Too many active sessions. End an existing session and try again.')
        session['last_activity'] = time.time()
        session['expires_at'] = session['last_activity'] + SESSION_TTL_SECONDS
        SESSIONS[session_id] = session


MAX_QUESTIONS = ll.MAX_QUESTIONS
DRILL_WORDS = ll.DRILL_WORDS
DRILL_TARGET = 9


def drill_definition_lines(current):
    """Return the definition shown while a word is being drilled."""
    prompt = (
        current.get('drill_definition')
        or current.get('prompt_definition')
        or current.get('definition')
        or ''
    )
    return prompt.split('\n') if prompt else []


def gauge_dots(score):
    """Return the compact score gauge used by word-list API responses."""
    if score >= 9:
        return '●●●'
    if score >= 8:
        return '●●○'
    if score >= 4:
        return '●○○'
    return '○○○'


# Practice sessions use the explicit Tartarus or Leitner path only.

def start_session(user, lang, audio_lang=None, drill_all=False, known_drill_mode=False,
                  instant_drill=False, fast_mode=False, wpm=128, level_mode=False,
                  category=None, level=None, leitner_mode=False):
    """Create one explicit Tartarus or Leitner practice session."""
    sentence_mode = ll.is_sentence_list(lang)
    selected_drill_modes = sum(bool(value) for value in (drill_all, known_drill_mode))
    if leitner_mode:
        if level_mode or fast_mode or selected_drill_modes:
            raise ValueError("Leitner mode cannot be combined with other practice modes.")
        if not lang:
            raise ValueError("Select a word list file before starting Leitner practice.")
        ll.sync_word_list(user, lang)
        words = ll.get_words_for_leitner(user, lang, MAX_QUESTIONS)
    elif level_mode:
        if not category or not level:
            raise ValueError("A language and level are required for level practice.")
        if lang:
            raise ValueError("Clear the word list file selection before practicing the whole level.")
        words = level_words(
            user, category, level,
            known_drill_mode=known_drill_mode,
            fast_mode=fast_mode,
            drill_all=drill_all
        )
        if not words:
            raise ValueError("No words are available for this language and level.")
        lang = f'{category}_{level}'
        sentence_mode = category.endswith('_sentences')
    elif not lang:
        raise ValueError("Select a word list file before starting a practice session.")
    elif fast_mode:
        if selected_drill_modes:
            raise ValueError("Fast mode cannot be combined with drill modes.")
        words = mastered_words(user, lang)
        if not words:
            raise ValueError("No mastered words are available for fast mode.")
    else:
        ll.sync_word_list(user, lang)
        words = ll.get_words_for_practice(
            user, lang,
            DRILL_WORDS if drill_all else MAX_QUESTIONS,
            known_drill_mode=known_drill_mode,
            drill_all=drill_all
        )

    source_language = (category or lang or '').split('_', 1)[0].lower()
    default_voice = source_language if source_language in {'english', 'german'} else lang
    voice_lang = audio_lang or default_voice
    queue = words if level_mode else [
        {
            'lang': lang,
            'word_id': word_row[0],
            'word_text': word_row[1],
            'definition': word_row[2],
            'score': word_row[3],
            'leitner_box': word_row[4],
        }
        for word_row in words
    ]

    session_id = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'voice_lang': voice_lang,
        'wpm': wpm,
        'queue': queue,
        'total': len(queue),
        'practiced': 0,
        'max_questions': len(queue) if (fast_mode or level_mode) else (DRILL_WORDS if drill_all else MAX_QUESTIONS),
        'known_drill_mode': known_drill_mode,
        'instant_drill': bool(instant_drill or leitner_mode),
        'fast_mode': fast_mode,
        'leitner_mode': leitner_mode,
        'drill_all': drill_all,
        'sentence_mode': sentence_mode,
        'level_mode': level_mode,
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'file_stats': {},
        'start_time': time.time(),
        'current': None,
        'drill_target': DRILL_TARGET,
        'lock': threading.RLock(),
        'question_sequence': 0,
        'answer_results': {},
    }
    register_session(session_id, session)
    ll.log_event(
        "SESSION_STARTED",
        session_id=session_id,
        user=user,
        lang=lang,
        total=len(queue),
        max_questions=session['max_questions'],
        known_drill_mode=known_drill_mode,
        instant_drill=session['instant_drill'],
        fast_mode=fast_mode,
        leitner_mode=leitner_mode,
    )
    return session_id, session

def next_question(session):
    """Pop and render the next immutable question in the current session queue."""
    queue = session['queue']
    if not queue:
        return None

    entry = queue.pop(0)
    question, drill = ll.build_question_data(
        entry['word_id'], entry['word_text'], entry['definition'], entry['score'], entry['leitner_box'],
        sentence_mode=session.get('sentence_mode', False),
        fast_mode=session.get('fast_mode', False),
        drill_all=session.get('drill_all', False),
        known_drill_mode=session.get('known_drill_mode', False),
    )

    if drill is not None:
        drill['target'] = session.get('drill_target', DRILL_TARGET)
        question['drill_start']['target'] = drill['target']

    if session.get('leitner_mode'):
        # Leitner is an independent mastered-word path. It uses production
        # recall, keeps the answer hidden, and advances only the box number.
        question['type'] = 'leitner'
        question['word'] = ''
        question['word_unmasked'] = entry['word_text']
        primary = ll.english_definition_only(entry['definition'])
        question['definition'] = [primary] if primary else []
        question['can_reveal'] = False
        question['leitner_box'] = entry['leitner_box'] or 1

    if session.get('known_drill_mode'):
        question['word'] = ''
        question['word_unmasked'] = ''

    session['current'] = {
        'lang': entry.get('lang', session['lang']),
        'word_id': entry['word_id'],
        'word_text': entry['word_text'],
        'definition': entry['definition'],
        'prompt_definition': '\n'.join(question['definition']),
        'drill_definition': '\n'.join(question['definition']),
        'score': entry['score'],
        'leitner_box': entry['leitner_box'],
        'type': question['type'],
        'drill': drill,
        'started_at': time.time(),
    }
    session['question_sequence'] += 1
    question_id = uuid.uuid4().hex
    session['current']['question_id'] = question_id
    session['current']['sequence'] = session['question_sequence']
    question['question_id'] = question_id
    question['sequence'] = session['question_sequence']
    ll.log_event(
        "QUESTION_PROMPTED",
        user=session['user'],
        lang=session['lang'],
        word_id=entry['word_id'],
        word_text=entry['word_text'],
        score=entry['score'],
        box=entry['leitner_box'],
        type=question['type'],
    )
    return question

def record_current_time(session):
    """Assign the current word's elapsed time to its source file."""
    current = session.get('current')
    if not current:
        return
    now = time.time()
    started_at = current.get('started_at', now)
    elapsed = max(0.0, now - started_at)
    lang = current.get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['seconds'] += elapsed
    current['started_at'] = now


def record_file_result(session, status, lang=None):
    """Record a completed result against the word's source file."""
    lang = lang or session['current'].get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['practiced'] += 1
    if status == 'correct':
        stats['correct'] += 1
    elif status == 'incorrect':
        stats['incorrect'] += 1
    elif status == 'drilled':
        stats['drilled'] += 1


def record_file_incorrect(session, lang=None):
    """Record a wrong attempt without marking the word completed."""
    lang = lang or session['current'].get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['incorrect'] += 1


def finalize_session(session, ended_early=False):
    record_current_time(session)
    elapsed = int(time.time() - session['start_time'])
    if session.get('level_mode'):
        for lang, stats in session['file_stats'].items():
            if stats['practiced'] > 0:
                ll.log_session(
                    session['user'], lang, round(stats['seconds']),
                    stats['practiced'], stats['correct'], stats['incorrect'],
                    stats['drilled']
                )
    elif session['practiced'] > 0:
        ll.log_session(
            session['user'], session['lang'], elapsed, session['practiced'],
            session['correct'], len(session['incorrect']), session['drilled']
        )

    practiced = session['practiced']
    attempts = practiced + len(session['incorrect']) if session.get('fast_mode') else practiced
    return {
        'practiced': practiced,
        'correct': session['correct'],
        'incorrect': session['incorrect'],
        'drilled': session['drilled'],
        'elapsed_seconds': elapsed,
        'ended_early': ended_early,
        'fast_mode': session.get('fast_mode', False),
        'leitner_mode': session.get('leitner_mode', False),
        'accuracy': round(100 * session['correct'] / attempts, 1) if attempts else None,
        'avg_seconds_per_item': round(elapsed / practiced, 1) if practiced else None,
    }

def advance_fast(session, correct, attempt):
    cur = session['current']
    word_text = cur['word_text']
    if not correct:
        session['incorrect'].append({'word': word_text, 'attempt': attempt})
        record_file_incorrect(session)
        return {
            'result': 'incorrect',
            'message': f"Incorrect. Try again. Mistakes: {len(session['incorrect'])}",
            'word': word_text,
            'fast_mode': True,
            'fast_retry': True,
            'done': False,
            'incorrect_count': len(session['incorrect']),
        }

    session['practiced'] += 1
    session['correct'] += 1
    record_file_result(session, 'correct')
    status = 'correct'
    message = 'Correct.'

    result = {'result': status, 'message': message, 'word': word_text, 'fast_mode': True}
    if session['practiced'] >= session['max_questions']:
        result['done'] = True
        result['session'] = finalize_session(session)
    else:
        result['done'] = False
        result['question'] = next_question(session)
        result['progress'] = {
            'correct': session['correct'],
            'drilled': 0,
            'total': session['total'],
            'questions': session['practiced'],
            'max_questions': session['max_questions'],
        }
    return result


def advance(session, status, message, attempt=None):
    cur = session['current']
    word_text = cur['word_text']
    session['practiced'] += 1
    if status == 'correct':
        session['correct'] += 1
    elif status == 'incorrect':
        session['incorrect'].append({'word': word_text, 'attempt': attempt})
    elif status == 'drilled':
        session['drilled'] += 1
    record_file_result(session, status)

    result = {'result': status, 'message': message, 'word': word_text}
    limit_reached = session['practiced'] >= session['max_questions']
    nxt = None if limit_reached else next_question(session)
    if nxt is None:
        result['done'] = True
        result['session'] = finalize_session(session)
    else:
        result['done'] = False
        result['question'] = nxt
        result['progress'] = {
            'correct': session['correct'],
            'drilled': session['drilled'],
            'total': session['total'],
            'questions': session['practiced'],
            'max_questions': session['max_questions'],
        }
    return result


def process_drill_answer(session, answer):
    cur = session['current']
    lang = cur.get('lang', session['lang'])
    drill = cur['drill']
    target = drill.get('target', DRILL_TARGET)
    if ll.answer_matches(answer, cur['word_text'], sentence_mode=session.get('sentence_mode', False)):
        drill['correct_in_a_row'] += 1
        if drill['correct_in_a_row'] >= target:
            cur['drill'] = None
            ll.complete_drill(
                session['user'], lang, cur['word_id'],
                known_review=(session.get('known_drill_mode', False) or session.get('leitner_mode', False))
            )
            ll.log_event("DRILL_COMPLETED", user=session['user'], lang=lang, word_id=cur['word_id'], word_text=cur['word_text'])
            result = advance(session, 'drilled', "Drill complete.")
            result['drill'] = {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'repetition': target,
                'correct_in_a_row': target,
                'target': target,
                'correct': True,
                'show_word': True,
            }
            return result
        correct = True
    else:
        drill['correct_in_a_row'] = 0
        correct = False

    drill['repetition'] += 1
    ll.log_event(
        "DRILL_PROGRESS",
        user=session['user'],
        lang=lang,
        word_id=cur['word_id'],
        repetition=drill['repetition'],
        streak=drill['correct_in_a_row'],
        target=target,
        correct=correct,
    )
    return {
        'result': 'drill_progress',
        'done': False,
        'drill': {
            'word': cur['word_text'],
            'definition': drill_definition_lines(cur),
            'repetition': drill['repetition'],
            'correct_in_a_row': drill['correct_in_a_row'],
            'target': target,
            'correct': correct,
            'show_word': True,
        },
    }


def process_answer(session, answer):
    answer = (answer or '').strip()
    cur = session['current']
    lang = cur.get('lang', session['lang'])
    sentence_mode = session.get('sentence_mode', False)
    record_current_time(session)

    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if session.get('fast_mode'):
        correct = ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)
        if correct:
            ll.record_fast_review(session['user'], lang, cur['word_id'])
        return advance_fast(session, correct, answer)

    if cur['drill'] is not None:
        return process_drill_answer(session, answer)

    if answer.startswith('$'):
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'show_word': True,
            },
        }

    # Master/flag commands stay available on the Tartarus path. Leitner items
    # are already mastered, so '@' is a harmless no-op there; flagging keeps
    # score/box state unchanged through the shared scoring function.
    if answer.startswith('@'):
        if not session.get('leitner_mode'):
            if not session.get('known_drill_mode'):
                ll.update_word_score(session['user'], lang, cur['word_id'], 'mastered')
            else:
                ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!'):
        if session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        else:
            ll.update_word_score(
                session['user'], lang, cur['word_id'], 'flagged', cur['score'], cur['leitner_box']
            )
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    correct = ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)

    if session.get('leitner_mode'):
        ll.update_leitner_result(session['user'], lang, cur['word_id'], correct)
    elif correct:
        ll.update_word_score(session['user'], lang, cur['word_id'],
                             'correct', cur['score'], cur['leitner_box'])
    else:
        ll.update_word_score(session['user'], lang, cur['word_id'],
                             'incorrect', cur['score'], cur['leitner_box'])

    ll.log_event(
        "ANSWER_SUBMITTED",
        user=session['user'],
        word_id=cur['word_id'],
        result='CORRECT' if correct else 'INCORRECT',
        score=cur.get('score', 0.0),
        box=cur.get('leitner_box'),
        leitner_mode=session.get('leitner_mode', False),
    )

    if correct:
        return advance(session, 'correct', None, attempt=answer)

    session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
    record_file_incorrect(session)
    if session.get('instant_drill'):
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1, 'instant': True}
        return {
            'result': 'drill_start',
            'done': False,
            'message': 'Incorrect. Complete the drill before continuing.',
            'drill': {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'correct': False,
                'show_word': True,
            },
        }

    return advance(
        session, 'incorrect',
        f"Incorrect. Correct answer was '{cur['word_text']}'.",
        attempt=answer,
    )


# --- Word lists / report ---
def _list_descriptor(user, lang, path, data, shared, owner=None):
    """Build the one list-descriptor shape consumed by every API view."""
    metadata = data['metadata']
    language = str(metadata.get('language', 'unknown')).lower()
    kind = str(metadata.get('type', metadata.get('kind', 'vocabulary'))).lower()
    kind = 'sentences' if kind == 'sentences' else 'vocabulary'
    level = str(metadata.get('cefr_level', metadata.get('level', 'all'))).lower()
    pos = str(metadata.get('pos', '')).lower()
    if not pos:
        parts = Path(path).stem.split('_')
        known_pos = {'noun', 'verb', 'adjective', 'adverb', 'pronoun', 'preposition', 'conjunction', 'interjection'}
        pos = next((part for part in parts if part in known_pos), 'all')
    return {
        'user': user,
        'owner': owner,
        'lang': lang,
        'language': language,
        'kind': kind,
        'category': f'{language}_{kind}',
        'cefr_level': level,
        'pos': pos,
        'name': str(metadata.get('name', lang)),
        'word_count': len(data['items']),
        'ordered': bool(metadata.get('ordered', False)),
        'shared': shared,
    }


def list_word_lists():
    """Discover shared JSON material and user-owned root lists without leakage."""
    root = Path(ll.WORD_LISTS_DIR)
    if not root.is_dir():
        return []
    conn = ll.get_connection()
    users = [row[0] for row in conn.execute(
        "SELECT name FROM users WHERE name != 'system' ORDER BY name"
    )]
    conn.close()

    personal = []
    shared_paths = []
    for path in sorted(root.rglob('*.json')):
        owner = ll.personal_list_owner(path.stem, users) if path.parent == root else None
        if owner:
            personal.append((path, owner, path.stem[len(owner) + 1:]))
        else:
            shared_paths.append(path)

    personal_by_owner = {}
    for path, owner, lang in personal:
        try:
            data = ll.read_word_list(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        personal_by_owner.setdefault(owner, {})[lang] = _list_descriptor(
            owner, lang, path, data, shared=False, owner=owner,
        )

    descriptors = []
    for user in users:
        overrides = personal_by_owner.get(user, {})
        hide_samples = bool(overrides)
        for path in shared_paths:
            try:
                data = ll.read_word_list(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            lang = path.stem
            if hide_samples and path.name.startswith('tartarus_sample_'):
                continue
            if lang not in overrides:
                descriptors.append(_list_descriptor(user, lang, path, data, shared=True))
        descriptors.extend(overrides.values())
    return sorted(descriptors, key=lambda item: (item['user'], item['category'], item['cefr_level'], item['pos'], item['lang']))

def report_data(user, lang=None):
    user_s = ll.sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return []

    if lang:
        languages = [ll.sanitize_name(lang, 'language')]
    else:
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]

    reports = []
    for language in languages:
        where_clause, params = "WHERE language = ?", [language]
        query = (
            f'SELECT session_date, COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
            f'FROM "{table}" {where_clause} GROUP BY session_date ORDER BY session_date DESC'
        )
        rows = conn.execute(query, params).fetchall()
        if not rows:
            continue

        days = []
        for s_date, sessions, seconds, practiced, correct, incorrect, drilled in rows:
            days.append({
                'date': s_date, 'sessions': sessions, 'seconds': seconds,
                'practiced': practiced, 'correct': correct,
                'incorrect': incorrect or 0, 'drilled': drilled or 0,
                'avg_time': round(seconds / practiced, 1) if practiced else None,
            })

        total_query = (
            f'SELECT COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
            f'FROM "{table}" {where_clause}'
        )
        t_sessions, t_seconds, t_practiced, t_correct, t_incorrect, t_drilled = conn.execute(total_query, params).fetchone()
        reports.append({
            'language': language,
            'days': days,
            'total': {
                'sessions': t_sessions, 'seconds': t_seconds, 'practiced': t_practiced,
                'correct': t_correct, 'incorrect': t_incorrect or 0, 'drilled': t_drilled or 0,
                'avg_time': round(t_seconds / t_practiced, 1) if t_practiced else None,
            },
        })
    conn.close()
    return reports


def user_summary_data(user):
    """Return aggregate daily stats across all languages for the user."""
    user_s = ll.sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None

    rows = conn.execute(
        f'SELECT session_date, COUNT(id), COUNT(DISTINCT language), '
        f'SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}" GROUP BY session_date ORDER BY session_date DESC'
    ).fetchall()

    all_dates = [r[0] for r in conn.execute(f'SELECT session_date FROM "{table}"').fetchall()]
    current_streak, best_streak = ll.compute_streak(all_dates)

    totals = conn.execute(
        f'SELECT COUNT(id), COUNT(DISTINCT language), SUM(duration_seconds), '
        f'SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}"'
    ).fetchone()
    conn.close()

    days = []
    for s_date, sessions, langs, seconds, practiced, correct, incorrect in rows:
        total_ans = (correct or 0) + (incorrect or 0)
        days.append({
            'date': s_date,
            'sessions': sessions,
            'languages': langs,
            'seconds': seconds or 0,
            'practiced': practiced or 0,
            'correct': correct or 0,
            'incorrect': incorrect or 0,
            'accuracy': round(100 * correct / total_ans, 1) if total_ans > 0 else None,
            'avg_time': round(seconds / practiced, 1) if practiced else None,
        })

    t_sessions, t_langs, t_seconds, t_practiced, t_correct, t_incorrect = totals
    t_total_ans = (t_correct or 0) + (t_incorrect or 0)
    return {
        'user': user_s,
        'streak': {'current': current_streak, 'best': best_streak},
        'days': days,
        'total': {
            'sessions': t_sessions,
            'languages': t_langs,
            'seconds': t_seconds or 0,
            'practiced': t_practiced or 0,
            'correct': t_correct or 0,
            'incorrect': t_incorrect or 0,
            'accuracy': round(100 * t_correct / t_total_ans, 1) if t_total_ans > 0 else None,
            'avg_time': round(t_seconds / t_practiced, 1) if t_practiced else None,
        },
    }


def user_progress_data(user, category=None, level=None):
    '''Return the two explicit learning-path progress values for selectable lists.'''
    user_s = ll.sanitize_name(user, 'user')
    prefix = f"words_{user_s}_"
    conn = ll.get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
        (f"{prefix}%",),
    ).fetchall()
    selectable_langs = {
        item['lang'] for item in list_word_lists()
        if item['user'] == user_s
        and (not category or item.get('category') == category)
        and (not level or item.get('cefr_level') == level)
    }
    lists = []
    for (table_name,) in tables:
        lang = table_name[len(prefix):]
        if lang not in selectable_langs:
            continue
        total, tartarus_mastered, leitner_finished = conn.execute(
            f'''SELECT COUNT(*),
                SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN score >= 9.0 AND COALESCE(leitner_box, 1) >= 10 THEN 1 ELSE 0 END)
                FROM "{table_name}" WHERE active = 1'''
        ).fetchone()
        total = total or 0
        tartarus_mastered = tartarus_mastered or 0
        leitner_finished = leitner_finished or 0
        lists.append({
            'lang': lang,
            'total': total,
            'learned': tartarus_mastered,
            'tartarus_mastered': tartarus_mastered,
            'leitner_finished': leitner_finished,
            'progress': round(100 * tartarus_mastered / total, 1) if total else 0.0,
            'tartarus_progress': round(100 * tartarus_mastered / total, 1) if total else 0.0,
            'leitner_progress': round(100 * leitner_finished / total, 1) if total else 0.0,
            'complete': bool(total and tartarus_mastered == total and leitner_finished == total),
        })
    conn.close()
    return lists


def leitner_stats_data(user, lang):
    '''Return box distribution for the explicit, non-calendar-gated Leitner path.'''
    table = ll.words_table_name(user, lang)
    ll.sync_word_list(user, lang)
    conn = ll.get_connection()
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        conn.close()
        return None
    total, eligible, finished = conn.execute(
        f'''SELECT COUNT(*),
            SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN score >= 9.0 AND COALESCE(leitner_box, 1) >= 10 THEN 1 ELSE 0 END)
            FROM "{table}" WHERE active = 1'''
    ).fetchone()
    rows = conn.execute(
        f'''SELECT COALESCE(leitner_box, 1), COUNT(*)
            FROM "{table}"
            WHERE active = 1 AND score >= 9.0
            GROUP BY COALESCE(leitner_box, 1) ORDER BY COALESCE(leitner_box, 1)'''
    ).fetchall()
    conn.close()
    counts = {int(box): count for box, count in rows}
    return {
        'total': total or 0,
        'eligible': eligible or 0,
        'finished': finished or 0,
        'boxes': [{'box': box, 'total': counts.get(box, 0)} for box in range(1, 11)],
    }


def _corrects_to_mastery(score, sentence_mode=False):
    """Return the shared engine's remaining correct-answer count."""
    return ll.corrects_to_mastery(score, sentence_mode)


def dashboard_data(user, lang=None):
    '''Analytics derived from the two explicit paths, with no calendar due scheduler.'''
    user_s = ll.sanitize_name(user, 'user')
    lang_s = ll.sanitize_name(lang, 'language') if lang else None
    sessions_table = f"sessions_{user_s}"
    conn = ll.get_connection()
    has_sessions = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (sessions_table,)
    ).fetchone() is not None
    s_where = 'WHERE language = ?' if lang_s else ''
    s_params = (lang_s,) if lang_s else ()
    s_and_lang = 'AND language = ?' if lang_s else ''

    total_seconds = total_practiced = total_correct = total_incorrect = 0
    current_streak = best_streak = 0
    avg_seconds_per_word = avg_words_7d = avg_seconds_7d = 0.0
    session_count = 0
    if has_sessions:
        row = conn.execute(
            f'''SELECT COALESCE(SUM(duration_seconds),0), COALESCE(SUM(words_practiced),0),
                       COALESCE(SUM(correct_count),0), COALESCE(SUM(incorrect_count),0)
                FROM "{sessions_table}" {s_where}''', s_params
        ).fetchone()
        total_seconds, total_practiced, total_correct, total_incorrect = row
        all_dates = [r[0] for r in conn.execute(
            f'SELECT session_date FROM "{sessions_table}" {s_where}', s_params
        ).fetchall()]
        current_streak, best_streak = ll.compute_streak(all_dates)
        session_count = len(all_dates)
        last_7 = conn.execute(
            f'''SELECT COALESCE(SUM(words_practiced),0), COALESCE(SUM(duration_seconds),0)
                FROM "{sessions_table}"
                WHERE session_date >= date('now', '-6 days', 'localtime') {s_and_lang}''',
            (lang_s,) if lang_s else (),
        ).fetchone()
        avg_words_7d = last_7[0] / 7.0
        avg_seconds_7d = last_7[1] / 7.0
        if total_practiced:
            avg_seconds_per_word = total_seconds / total_practiced

    total_answers = total_correct + total_incorrect
    overall_accuracy = round(100 * total_correct / total_answers, 1) if total_answers else None
    if avg_words_7d >= 40:
        benchmark = 'Hyper-Learner'
    elif avg_words_7d >= 20:
        benchmark = 'On Track'
    elif avg_words_7d >= 10:
        benchmark = 'Building Momentum'
    elif avg_words_7d > 0:
        benchmark = 'Getting Started'
    else:
        benchmark = None

    result = {
        'overview': {
            'streak': {'current': current_streak, 'best': best_streak},
            'total_seconds': total_seconds,
            'overall_accuracy': overall_accuracy,
            'leitner_finished': 0,
        },
        'velocity': {
            'avg_seconds_per_word': round(avg_seconds_per_word, 1) if avg_seconds_per_word else None,
            'avg_words_per_day_7d': round(avg_words_7d, 1),
            'avg_minutes_per_day_7d': round(avg_seconds_7d / 60, 1),
            'benchmark': benchmark,
            'enough_data': session_count >= 3,
        },
        'mastery': None,
        'nemesis': None,
        'prediction': None,
        'roadmap': None,
    }

    if lang_s:
        ll.sync_word_list(user_s, lang_s)
        wtable = ll.words_table_name(user_s, lang_s)
        has_wtable = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (wtable,)
        ).fetchone() is not None
        if has_wtable:
            learning, familiar, mastered, total_words, leitner_finished = conn.execute(
                f'''SELECT
                    SUM(CASE WHEN score < 4.0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score >= 4.0 AND score < 9.0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END),
                    COUNT(*),
                    SUM(CASE WHEN score >= 9.0 AND COALESCE(leitner_box,1) >= 10 THEN 1 ELSE 0 END)
                    FROM "{wtable}" WHERE active=1'''
            ).fetchone()
            learning, familiar, mastered = learning or 0, familiar or 0, mastered or 0
            total_words, leitner_finished = total_words or 0, leitner_finished or 0
            result['overview']['leitner_finished'] = leitner_finished
            result['mastery'] = {
                'learning': learning, 'familiar': familiar, 'mastered': mastered, 'total': total_words,
            }
            try:
                material = {
                    item['content_id']: item
                    for item in ll.load_practice_items(ll.word_list_path(user_s, lang_s))
                }
            except (OSError, ValueError):
                material = {}
            result['nemesis'] = [
                {'word': material.get(r[0], {}).get('word', r[0]),
                 'times_incorrect': r[1], 'times_correct': r[2], 'score': round(r[3], 1)}
                for r in conn.execute(
                    f'''SELECT content_id, times_incorrect, times_correct, score
                        FROM "{wtable}" WHERE active=1 AND times_incorrect > 0
                        ORDER BY times_incorrect DESC, score ASC LIMIT 10'''
                ).fetchall()
            ]
            word_rows = conn.execute(
                f'SELECT score, COALESCE(leitner_box,1) FROM "{wtable}" WHERE active=1'
            ).fetchall()
            tartarus_corrects = sum(ll.corrects_to_mastery(score) for score, _ in word_rows)
            leitner_steps = sum(
                (9 if float(score) < 9.0 else max(0, 10 - int(box or 1)))
                for score, box in word_rows
            )
            enough_data = session_count >= 3 and avg_seconds_per_word > 0
            result['prediction'] = {
                'grind_hours': round((tartarus_corrects + leitner_steps) * avg_seconds_per_word / 3600, 1)
                if enough_data else None,
                'leitner_steps': leitner_steps,
                'enough_data': enough_data,
                'sessions_needed': max(0, 3 - session_count),
            }
            distribution = {str(i): 0 for i in range(1, 11)}
            for box, count in conn.execute(
                f'''SELECT COALESCE(leitner_box,1), COUNT(*) FROM "{wtable}"
                    WHERE active=1 AND score >= 9.0
                    GROUP BY COALESCE(leitner_box,1)'''
            ).fetchall():
                distribution[str(int(box))] = count
            score_distribution = {str(i): 0 for i in range(10)}
            for score, _ in word_rows:
                score_distribution[str(ll.score_band(score))] += 1
            result['roadmap'] = {
                'learning': {
                    'total': total_words,
                    'tartarus_mastered': mastered,
                    'tartarus_remaining': total_words - mastered,
                    'leitner_finished': leitner_finished,
                    'leitner_remaining': total_words - leitner_finished,
                    'complete': bool(total_words and mastered == total_words and leitner_finished == total_words),
                },
                'score_distribution': score_distribution,
                'leitner_distribution': distribution,
            }
    conn.close()
    return result


def word_list_stats(user, lang):
    '''Return current progress rows; no calendar-based due projection.'''
    table = ll.words_table_name(user, lang)
    ll.sync_word_list(user, lang)
    material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user, lang))}
    conn = ll.get_connection()
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        conn.close()
        return None
    rows = conn.execute(
        f'''SELECT content_id, score, active, times_practiced, times_correct,
                   times_incorrect, times_drilled, times_mastered, last_practiced,
                   leitner_box, last_known_review_at
            FROM "{table}" ORDER BY active DESC, score DESC, id ASC'''
    ).fetchall()
    conn.close()
    words = []
    for (content_id, score, active, practiced, correct, incorrect, drilled,
         mastered, last_practiced, leitner_box, last_known_review_at) in rows:
        item = material.get(content_id, {})
        words.append({
            'word': item.get('word', content_id),
            'score': round(score, 1),
            'gauge': gauge_dots(score),
            'band': ll.score_band(score),
            'active': bool(active),
            'leitner_box': leitner_box,
            'times_practiced': practiced,
            'times_correct': correct,
            'times_incorrect': incorrect,
            'times_drilled': drilled,
            'times_mastered': mastered,
            'last_practiced': last_practiced,
            'last_known_review_at': last_known_review_at,
        })
    return words


def load_word_list(user, lang):
    """Return editable material records without discarding any source fields."""
    path = ll.word_list_path(user, lang)
    data = ll.read_word_list(path)
    items = []
    for entry in ll.validate_word_list_items(data['items'], path):
        definition = entry.get('definition', '')
        if isinstance(definition, list):
            lines = [str(line) for line in definition]
        else:
            lines = str(definition).split('\n') if definition else []
        items.append({
            'id': entry['id'],
            'word': entry['word'],
            'definition': lines,
            'record': entry,
        })
    return {'metadata': data['metadata'], 'items': items}


def _editor_definition(item, original):
    if isinstance(item.get('definition'), list):
        return [str(line) for line in item['definition']]
    if 'definition' in item:
        return str(item['definition'])
    source = original.get('definition', '')
    if isinstance(source, list):
        lines = list(source)
        if 'def1' in item:
            if lines:
                lines[0] = str(item['def1'])
            else:
                lines.append(str(item['def1']))
        if 'def2' in item:
            while len(lines) < 2:
                lines.append('')
            lines[1] = str(item['def2'])
        return lines
    lines = str(source).split('\n') if source else []
    if 'def1' in item:
        if lines:
            lines[0] = str(item['def1'])
        else:
            lines.append(str(item['def1']))
    if 'def2' in item:
        while len(lines) < 2:
            lines.append('')
        lines[1] = str(item['def2'])
    return '\n'.join(lines)


def save_word_list(user, lang, items):
    """Losslessly save one user-owned list with stable IDs and atomic writes."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    if ll.is_read_only_sample_list(user, lang):
        raise ValueError('Tartarus sample lists are read-only. Create a personal list to edit material.')
    target_path = ll.word_list_path_user_specific(user, lang)
    if os.path.isfile(target_path):
        source_path = target_path
        source = ll.read_word_list(source_path)
    else:
        try:
            source_path = ll.word_list_path(user, lang)
            source = ll.read_word_list(source_path)
        except FileNotFoundError:
            source_path = target_path
            source = {'metadata': ll.canonical_material_metadata(name=lang), 'items': []}
    originals = {
        entry['id']: entry
        for entry in ll.validate_word_list_items(source['items'], source_path)
    }
    saved = []
    ids = set()
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'Invalid editor row {position}.')
        record = dict(item.get('record', {}))
        content_id = str(item.get('id', record.get('id', ''))).strip() or uuid.uuid4().hex
        if content_id in ids:
            raise ValueError(f"Each word list item needs a unique id; duplicate '{content_id}'.")
        original = originals.get(content_id, record)
        record = dict(original)
        record['id'] = content_id
        record['word'] = str(item.get('word', record.get('word', ''))).strip()
        if not record['word']:
            raise ValueError(f'Invalid editor row {position}: missing word.')
        record['definition'] = _editor_definition(item, original)
        record.setdefault('word_frequency', 0)
        ids.add(content_id)
        saved.append(record)
    saved = ll.validate_word_list_items(saved, target_path)
    metadata = ll.canonical_material_metadata(source['metadata'], name=lang)
    ll.write_word_list_atomic(target_path, {'metadata': metadata, 'items': saved})
    ll.sync_word_list(user, lang)
    ll.retire_sample_progress(user)
    return target_path, len(saved)

def init_word_list(user, lang, material_type='vocabulary'):
    """Create a user-owned canonical Master Schema vocabulary list."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    material_type = str(material_type).strip().lower()
    if material_type != 'vocabulary':
        raise ValueError("List type must be 'vocabulary'.")
    path = ll.word_list_path_user_specific(user, lang)
    created = not os.path.exists(path)
    if created:
        ll.write_word_list_atomic(path, {
            'metadata': ll.canonical_material_metadata(
                name=lang.replace('_', ' ').title(), language='unknown',
                kind='vocabulary', level='all',
            ),
            'items': [],
        })
    conn = ll.get_connection()
    ll.ensure_user(conn, user)
    ll.ensure_word_table(conn, user, lang)
    ll.ensure_sessions_table(conn, user)
    conn.commit()
    conn.close()
    ll.retire_sample_progress(user)
    return created, path


# --- HTTP server ---
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TartarusWeb/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename, content_type):
        path = os.path.join(WEB_DIR, filename)
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            return self._send_json({'error': 'not found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type != 'application/json':
            raise ValueError('Content-Type must be application/json')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as error:
            raise ValueError('invalid Content-Length') from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f'request body exceeds {MAX_REQUEST_BYTES} bytes')
        if not length:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError('incomplete request body')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('JSON body must be an object')
        return payload

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.endswith(('.css', '.js', '.ico')):
            ll.log_event("HTTP_GET", path=parsed.path, query=parsed.query)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            return self._send_static(filename, content_type)

        if parsed.path == '/api/wordlists':
            conn = ll.get_connection()
            all_users = [row[0] for row in conn.execute("SELECT name FROM users WHERE name != 'system' ORDER BY name")]
            conn.close()
            return self._send_json({'wordlists': list_word_lists(), 'users': all_users})

        if parsed.path == '/api/report':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [None])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                response = {'reports': report_data(user, lang)}
                if lang:
                    summary = dashboard_data(user, lang)
                    if 'roadmap' in summary:
                        response['roadmap'] = summary['roadmap']
                return self._send_json(response)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/report/summary':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                summary = user_summary_data(user)
                if summary is None:
                    return self._send_json({'summary': None})
                return self._send_json({'summary': summary})
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/user/progress':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            category = qs.get('category', [''])[0] or None
            level = qs.get('level', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json({'lists': user_progress_data(user, category, level)})

        if parsed.path == '/api/wordlist':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                return self._send_json({
                    **load_word_list(user, lang),
                    'read_only': ll.is_read_only_sample_list(user, lang),
                })
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/wordlist/stats':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                words = word_list_stats(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            if words is None:
                return self._send_json({'error': 'no such word list'}, 404)
            return self._send_json({'words': words})

        if parsed.path == '/api/dashboard':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                return self._send_json(dashboard_data(user, lang))
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        
        if parsed.path == '/api/export':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json(ll.export_user_data(user))

        if parsed.path == '/api/wordlist/leitner':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                stats = leitner_stats_data(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            if stats is None:
                return self._send_json({'error': 'no such word list'}, 404)
            return self._send_json({'leitner': stats})

        if parsed.path == '/api/learning/progress':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                progress = ll.get_learning_progress(user, lang)
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'progress': progress})

        return self._send_json({'error': 'not found'}, 404)


    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        ll.log_event("HTTP_POST", path=parsed.path)
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self._send_json({'error': 'invalid JSON body'}, 400)

        if parsed.path == '/api/tts':
            text = str(payload.get('text', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            wpm = payload.get('wpm', 128)
            try:
                wpm = int(wpm)
            except (TypeError, ValueError):
                wpm = 128
            # Browser integration tests can emulate the blocking macOS `say`
            # process with a bounded sleep. Production leaves this unset.
            try:
                simulated_delay_ms = max(0, int(os.environ.get('TARTARUS_TTS_TEST_DELAY_MS', '0')))
            except (TypeError, ValueError):
                simulated_delay_ms = 0
            if simulated_delay_ms:
                time.sleep(simulated_delay_ms / 1000.0)
                return self._send_json({'supported': True, 'spoken': True, 'simulated': True})
            if not ll.tts_available():
                return self._send_json({'supported': False, 'spoken': False, 'error': 'Speech is supported only on macOS with say installed.'}, 501)
            try:
                spoken = ll.speak(text, lang or None, block=True, wpm=wpm)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({'supported': True, 'spoken': spoken})

        
        if parsed.path == '/api/import':
            user = str(payload.get('user', '')).strip()
            data = payload.get('data', {})
            if not user or not data:
                return self._send_json({'error': "'user' and 'data' are required"}, 400)
            try:
                ll.import_user_data(user, data)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/user/create':
            user = str(payload.get('user', '')).strip()
            if not user:
                return self._send_json({'error': "user required"}, 400)
            conn = ll.get_connection()
            ll.ensure_user(conn, user)
            ll.ensure_sessions_table(conn, user)
            conn.commit()
            conn.close()
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/wordlist/custom':
            user = str(payload.get('user', '')).strip()
            list_name = str(payload.get('list_name', '')).strip()
            items = payload.get('items', [])
            if not user or not list_name or not items:
                return self._send_json({'error': "user, list_name, and items required"}, 400)
            path = ll.save_custom_list(user, list_name, items)
            return self._send_json({'status': 'ok', 'path': path})

        if parsed.path == '/api/init':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            try:
                created, path = init_word_list(user, lang, payload.get('type', 'vocabulary'))
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'created': created, 'path': path})

        if parsed.path == '/api/practice/start':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            requested_mode = str(payload.get('mode', 'auto')).strip().lower()
            try:
                wpm = int(payload.get('wpm', 128))
            except (TypeError, ValueError):
                wpm = 128
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            if requested_mode not in {'auto', 'tartarus', 'leitner'}:
                return self._send_json({'error': "mode must be 'auto', 'tartarus', or 'leitner'"}, 400)

            try:
                learning = ll.get_learning_progress(user, lang)
                if not learning['total']:
                    raise ValueError('No active words are available in this file.')
                if learning['tartarus_remaining'] > 0:
                    if requested_mode == 'leitner':
                        raise ValueError('Complete the Tartarus path before starting Leitner practice.')
                    mode = 'tartarus'
                elif learning['leitner_finished'] < learning['total']:
                    mode = 'leitner'
                else:
                    raise ValueError('This file is complete: Tartarus and Leitner are both finished.')

                session_id, session = start_session(
                    user, lang, audio_lang=audio_lang, wpm=wpm,
                    leitner_mode=(mode == 'leitner'),
                    instant_drill=True,
                )
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)

            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang': session['lang'],
                'audio_lang': session['voice_lang'],
                'fast_mode': session['fast_mode'],
                'leitner_mode': session['leitner_mode'],
                'learning_mode': mode,
                'learning_progress': learning,
                'progress': {
                    'correct': 0,
                    'drilled': 0,
                    'total': session['total'],
                    'questions': 0,
                    'max_questions': session['max_questions'],
                },
                'question': question,
            })

        if parsed.path == '/api/wordlist':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            items = payload.get('items', payload.get('words', []))
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                path, count = save_word_list(user, lang, items)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'saved': True, 'path': path, 'count': count})

        if parsed.path == '/api/practice/cancel':
            cleanup_sessions()
            session_id = str(payload.get('session_id', '')).strip()
            with SESSIONS_LOCK:
                session = SESSIONS.pop(session_id, None)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            if session.get('practiced', 0):
                finalize_session(session, ended_early=True)
            return self._send_json({'cancelled': True})

        if parsed.path == '/api/practice/answer':
            cleanup_sessions()
            session_id = payload.get('session_id')
            with SESSIONS_LOCK:
                session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            question_id = str(payload.get('question_id', '')).strip()
            sequence = payload.get('sequence')
            attempt_id = str(payload.get('attempt_id', '')).strip()
            with session['lock']:
                session['last_activity'] = time.time()
                session['expires_at'] = session['last_activity'] + SESSION_TTL_SECONDS
                cached = session['answer_results'].get(attempt_id) if attempt_id else None
                if cached is not None:
                    return self._send_json(cached)
                current = session.get('current') or {}
                if not question_id or question_id != current.get('question_id'):
                    return self._send_json({'error': 'stale or missing question id'}, 409)
                if sequence != current.get('sequence'):
                    return self._send_json({'error': 'stale or missing question sequence'}, 409)
                if not attempt_id:
                    return self._send_json({'error': 'missing answer idempotency key'}, 400)
                try:
                    result = process_answer(session, payload.get('answer', ''))
                except Exception as e:
                    import traceback; traceback.print_exc()
                    with SESSIONS_LOCK:
                        SESSIONS.pop(session_id, None)
                    if session.get('practiced', 0) > 0:
                        finalize_session(session, ended_early=True)
                    return self._send_json({'error': f'Internal error processing answer: {str(e)}'}, 500)
                session['answer_results'][attempt_id] = result
                if result.get('done'):
                    with SESSIONS_LOCK:
                        SESSIONS.pop(session_id, None)
                return self._send_json(result)

        return self._send_json({'error': 'not found'}, 404)


class TartarusHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import traceback
        ll.log_event("SERVER_CRASH", client=str(client_address), error=traceback.format_exc())
        super().handle_error(request, client_address)

def main():
    ll.configure_logging()
    try:
        httpd = TartarusHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"Error: port {PORT} is already in use.")
            print(f"  Another Tartarus web server (or another process) is "
                  f"probably already listening on http://{HOST}:{PORT}/.")
            print(f"  Find it with: lsof -i :{PORT}")
            print(f"  Stop it with: kill <PID>")
            sys.exit(1)
        raise
    db_path = os.path.abspath(ll.DATABASE_FILE)
    print("Tartarus web server starting...")
    print(f"  Listening on : http://{HOST}:{PORT}/")
    print(f"  Database     : {db_path}")
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()
        print("Server stopped.")


if __name__ == '__main__':
    main()
