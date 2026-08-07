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
    """Drop expired ephemeral sessions; persisted drill debt remains in SQLite."""
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


# ---------------------------------------------------------------------------
# Gauntlet session builder
# ---------------------------------------------------------------------------

def gauntlet_start_session(user, lang, wpm=128, audio_lang=None):
    """Unified entry point for the automated 10-day Gauntlet.

    The backend is solely responsible for determining:
      1. Whether the Leitner Maintenance track has due words (highest priority).
      2. Which gauntlet stage and day the user is on.
      3. Which session mode / rendering style to apply.

    Returns (session_id, session, gauntlet_meta) tuple.
    """
    today = ll.date.today().isoformat()
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    ll.sync_word_list(user, lang)

    # Persist an idempotent day transition before reading the current stage.
    progress = ll.transition_gauntlet_day(user, lang, today)
    current_day = progress['current_day']
    sessions_done_today = progress['sessions_done_today']
    last_practice_date = progress['last_practice_date']

    # Corrective drill debt has priority over every other learning path and lockout.
    try:
        debt_words = ll.get_words_for_practice(user, lang, DRILL_WORDS, drill_mode=True)
    except ValueError as error:
        if not str(error).startswith('No words with mistakes to drill.'):
            raise
        debt_words = []
    remaining_today = ll.get_gauntlet_tasks_remaining(user, lang, current_day, today)
    if not debt_words and remaining_today == 0 and last_practice_date == today:
        raise ValueError(
            f'Today\'s tasks for this list are complete! '
            f'Come back tomorrow — neuroplasticity requires sleep.'
        )

    # --- Determine session type via corrective-debt, maintenance, then Gauntlet stage ---
    current_stage, stage_name, session_mode = ll.gauntlet_stage_for_day(current_day)
    is_maintenance = False
    is_debt = bool(debt_words)
    words = debt_words
    if is_debt:
        session_mode = 'drill'
        stage_name = 'Corrective Drill'

    # Priority 1: Leitner Maintenance track (only after forging begins)
    if not words and current_day > 0:
        try:
            leitner_words = ll.check_leitner_due_words(user, lang)
            if leitner_words:
                words = leitner_words
                is_maintenance = True
                session_mode = 'maintenance'
                stage_name = 'Leitner Review'
        except ValueError:
            raise

    # Priority 2: Gauntlet stage words
    if not words:
        try:
            words = ll.get_words_for_gauntlet_stage(user, lang, current_stage)
        except ValueError as e:
            if 'Forging is complete' in str(e) and current_stage == 0:
                # All words mastered: automatically jump to stage 1
                current_day = 1
                current_stage, stage_name, session_mode = ll.gauntlet_stage_for_day(1)
                words = ll.get_words_for_gauntlet_stage(user, lang, current_stage)
            else:
                raise

    # --- Build the in-memory session ---
    sentence_mode = ll.is_sentence_list(lang)
    source_language = lang.split('_', 1)[0].lower()
    default_voice = source_language if source_language in {'english', 'german'} else lang
    voice_lang = audio_lang or default_voice

    # Map gauntlet mode to existing fast/known_drill flags
    fast_mode = False  # Gauntlet and Leitner both need score updates to graduate boxes
    known_drill_mode = False  # Not used in gauntlet
    instant_drill = True       # ALWAYS enforced in gauntlet (Rule 5)

    queue = [
        {
            'lang': lang,
            'word_id': row[0],
            'word_text': row[1],
            'definition': row[2],
            'score': row[3],
            'leitner_box': row[4],
            'noun_forms': row[6] if len(row) > 6 else None,
            'noun_case': row[6].get('case') if len(row) > 6 and isinstance(row[6], dict) else None,
        }
        for row in words
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
        'max_questions': MAX_QUESTIONS,
        'fast_mode': fast_mode,
        'known_drill_mode': known_drill_mode,
        'instant_drill': True,   # Gauntlet always enforces instant drill
        'drill_mode': is_debt,
        'drill_all': (session_mode == 'shadows'),
        'drill_target': 2 if session_mode == 'shadows' else DRILL_TARGET,
        'review_mode': False,
        'sentence_mode': sentence_mode,
        'level_mode': False,
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'file_stats': {},
        'start_time': __import__('time').time(),
        'current': None,
        'review_index': 0,
        'reviewed_ids': set(),
        # Gauntlet metadata
        'gauntlet_mode': session_mode,
        'gauntlet_day': current_day,
        'gauntlet_stage': current_stage,
        'gauntlet_stage_name': stage_name,
        'gauntlet_sessions_done': sessions_done_today,
        'gauntlet_remaining_tasks': remaining_today,
        'is_maintenance': is_maintenance,
        'is_gauntlet': not is_debt,
        'is_debt_session': is_debt,
        'lock': threading.RLock(),
        'question_sequence': 0,
        'answer_results': {},
    }
    register_session(session_id, session)

    gauntlet_meta = {
        'mode': session_mode,
        'stage': current_stage,
        'stage_name': stage_name,
        'day': current_day,
        'sessions_done_today': sessions_done_today,
        'remaining_tasks': remaining_today,
        'is_maintenance': is_maintenance,
    }
    ll.log_event(
        'GAUNTLET_SESSION_STARTED',
        user=user, lang=lang, mode=session_mode, day=current_day,
        stage=current_stage, sessions_today=sessions_done_today,
    )
    return session_id, session, gauntlet_meta


# --- Session lifecycle ---
def mastered_words(user, lang):
    """Read all mastered entries, ordered by their last Fast review."""
    return ll.get_mastered_words_for_fast(user, lang)


def level_words(user, category, level, drill_mode=False, known_drill_mode=False,
                fast_mode=False, drill_all=False):
    """Return mode-appropriate candidates across all files in a CEFR level."""
    files = [item for item in list_word_lists()
             if item['user'] == user and item['category'] == category and item['cefr_level'] == level]
    candidates = []
    for item in files:
        ll.sync_word_list(user, item['lang'])
        try:
            if fast_mode:
                rows = mastered_words(user, item['lang'])
            else:
                rows = ll.get_words_for_practice(
                    user, item['lang'],
                    drill_mode=drill_mode,
                    known_drill_mode=known_drill_mode,
                    drill_all=drill_all
                )
        except ValueError:
            continue
        candidates.extend(
            {'lang': item['lang'], 'word_id': row[0], 'word_text': row[1],
             'definition': row[2], 'score': row[3], 'leitner_box': row[4],
             'word_frequency': row[5] if len(row) > 5 else 0,
             'noun_forms': row[6] if len(row) > 6 else None,
             'noun_case': row[6].get('case') if len(row) > 6 and isinstance(row[6], dict) else None,
             'fast_review_at': None,
             'random_order': row[0]}
            for row in rows
        )
    if fast_mode:
        candidates.sort(key=lambda item: (
            item['fast_review_at'] is not None,
            item['fast_review_at'] or '',
            item['random_order'],
        ))
    elif drill_mode or known_drill_mode:
        # Each source list already applies its mode-specific priority. Keep
        # that order rather than replacing mistake/known-review ordering.
        pass
    else:
        # Choose the level-wide pool by material priority first.
        candidates.sort(key=lambda item: (
            -item['word_frequency'],
            len(item['word_text']),
            item['random_order'],
        ))
    limit = DRILL_WORDS if (drill_mode or known_drill_mode) else MAX_QUESTIONS
    selected = candidates[:limit]
    if not (fast_mode or drill_mode or known_drill_mode):
        selected.sort(key=lambda item: (
            -item['score'],
            -item['word_frequency'],
            len(item['word_text']),
            item['random_order'],
        ))
    return selected


def start_session(user, lang, audio_lang=None, drill_all=False, drill_mode=False, known_drill_mode=False, instant_drill=False, fast_mode=False, wpm=128, level_mode=False, category=None, level=None, review_mode=False, stage=None):
    sentence_mode = ll.is_sentence_list(lang)
    selected_drill_modes = sum(bool(value) for value in (drill_all, drill_mode, known_drill_mode))
    if review_mode:
        if level_mode or fast_mode or selected_drill_modes:
            raise ValueError("Review mode cannot be combined with practice modes.")
        if not lang:
            raise ValueError("Select a word list file before starting a review.")
        ll.sync_word_list(user, lang)
        words = ll.get_due_review_words(user, lang, MAX_QUESTIONS)
        if not words:
            raise ValueError("No due words are available for review in this file.")
    elif level_mode:
        if not category or not level:
            raise ValueError("A language and level are required for level practice.")
        if lang:
            raise ValueError("Clear the word list file selection before practicing the whole level.")
        words = level_words(
            user, category, level,
            drill_mode=drill_mode,
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
            DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS,
            drill_mode=drill_mode,
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
            'noun_forms': word_row[6] if len(word_row) > 6 else None,
            'noun_case': word_row[6].get('case') if len(word_row) > 6 and isinstance(word_row[6], dict) else None,
            'drill_pending': bool(word_row[7]) if len(word_row) > 7 else False,
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
        'max_questions': len(queue) if (fast_mode or level_mode) else (DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS),
        'drill_mode': drill_mode,
        'known_drill_mode': known_drill_mode,
        'instant_drill': instant_drill,
        'fast_mode': fast_mode,
        'review_mode': review_mode,
        'drill_all': drill_all,
        'sentence_mode': sentence_mode,
        'level_mode': level_mode,
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'file_stats': {},
        'start_time': time.time(),
        'current': None,
        'review_index': 0,
        'reviewed_ids': set(),
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
        drill_mode=drill_mode,
        known_drill_mode=known_drill_mode,
        instant_drill=instant_drill,
        fast_mode=fast_mode,
    )
    return session_id, session


def next_question(session):
    queue = session['queue']
    if not queue and not session.get('review_mode'):
        return None

    if session.get('review_mode'):
        idx = session['review_index']
        if idx < 0 or idx >= len(queue):
            return None
        entry = queue[idx]
        question = {
            'word': entry['word_text'],
            'word_unmasked': entry['word_text'],
            'definition': entry['definition'].split('\n') if isinstance(entry['definition'], str) else entry['definition'],
            'audio_text': entry['word_text'],
            'score': entry['score'],
            'leitner_box': entry['leitner_box'],
            'gauge': gauge_dots(entry['score']),
            'band': 4,
            'gender': ll.get_gender_style(entry['word_text'])[1],
            'can_reveal': False,
            'type': 'review',
            'sentence_mode': session.get('sentence_mode', False),
            'review_mode': True,
        }
        session['reviewed_ids'].add(entry['word_id'])
        session['practiced'] = len(session['reviewed_ids'])
    else:
        entry = queue.pop(0)

    if session.get('review_mode'):
        drill = None
    else:
        question_definition = entry['definition']
        gauntlet_mode = session.get('gauntlet_mode', '')

        # For gauntlet stages: adjust flags per mode
        _fast_mode = session.get('fast_mode', False)
        _drill_mode = session.get('drill_mode', False) or session.get('drill_all', False)
        _known_drill = session.get('known_drill_mode', False)

        question, drill = ll.build_question_data(
            entry['word_id'], entry['word_text'], question_definition, entry['score'], entry['leitner_box'],
            sentence_mode=session.get('sentence_mode', False),
            fast_mode=_fast_mode,
            drill_mode=_drill_mode,
            known_drill_mode=_known_drill,
        )

        if drill is not None:
            drill['target'] = session.get('drill_target', DRILL_TARGET)
            question['drill_start']['target'] = drill['target']

        if entry.get('drill_pending'):
            drill = {'correct_in_a_row': 0, 'repetition': 1, 'persisted': True}
            question['type'] = 'drill'
            question['drill_start'] = {
                'word': entry['word_text'],
                'definition': question['definition'],
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'show_word': True,
            }

        # --- Gauntlet mode adjustments to the question ---
        if gauntlet_mode in ('crucible', 'shadows', 'depths', 'void', 'ascension'):
            question['type'] = gauntlet_mode
            question['word_unmasked'] = entry['word_text']
            
            # Ensure definition is always populated for all Gauntlet stages
            full_def = entry['definition']
            if full_def and isinstance(full_def, str):
                question['definition'] = full_def.split('\n')
            elif isinstance(full_def, list):
                question['definition'] = full_def

            if gauntlet_mode == 'crucible':
                # Target Word: Heavily Masked (vowels to underscores)
                vowels = "aeiouAEIOUäöüÄÖÜ"
                question['word'] = "".join("_" if c in vowels else c for c in entry['word_text'])
            else:
                # Target Word: Completely hidden for shadows, depths, void, ascension
                question['word'] = ''
        elif gauntlet_mode == 'maintenance':
            # Leitner maintenance: similar to standard production
            question['type'] = 'maintenance'
            question['word'] = ''
            question['word_unmasked'] = entry['word_text']
            full_def = entry['definition']
            if full_def and isinstance(full_def, str):
                question['definition'] = full_def.split('\n')
            elif isinstance(full_def, list):
                question['definition'] = full_def

        # Add gauntlet metadata to each question
        if session.get('is_gauntlet'):
            question['gauntlet'] = {
                'mode': gauntlet_mode,
                'stage': session.get('gauntlet_stage', 0),
                'stage_name': session.get('gauntlet_stage_name', ''),
                'day': session.get('gauntlet_day', 0),
                'sessions_done': session.get('gauntlet_sessions_done', 0),
            }

    if session.get('known_drill_mode'):
        # The known-drill prompt must not leak the answer through the API.
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
        'noun_case': entry.get('noun_case'),
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


def advance_review(session, direction):
    """Move through a read-only due-word review without changing word state."""
    if direction not in {'ArrowLeft', 'ArrowRight'}:
        return {'result': 'review_wait', 'done': False}

    index = session.get('review_index', 0)
    if direction == 'ArrowRight':
        if index >= len(session['queue']) - 1:
            return {'result': 'review_complete', 'done': True, 'session': finalize_session(session)}
        session['review_index'] = index + 1
    elif index > 0:
        session['review_index'] = index - 1

    question = next_question(session)
    return {
        'result': 'review_move',
        'done': False,
        'boundary': direction == 'ArrowLeft' and index == 0,
        'question': question,
        'progress': {
            'correct': 0,
            'drilled': 0,
            'total': session['total'],
            'questions': session['review_index'],
            'max_questions': session['max_questions'],
        },
    }


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

    # Advance gauntlet progress only if session completed fully (no rage quit)
    # Rage-quit (ended_early=True) gets NO credit — voided session rule.
    if session.get('is_gauntlet') and not ended_early and session['practiced'] > 0:
        try:
            ll.advance_gauntlet_session(session['user'], session['lang'])
        except Exception as exc:
            ll.log_event('GAUNTLET_ADVANCE_ERROR', user=session['user'], lang=session['lang'], error=str(exc))

    practiced = session['practiced']
    attempts = practiced + len(session['incorrect']) if session.get('fast_mode') else practiced
    result = {
        'practiced': session['practiced'],
        'correct': session['correct'],
        'incorrect': session['incorrect'],
        'drilled': session['drilled'],
        'elapsed_seconds': elapsed,
        'ended_early': ended_early,
        'fast_mode': session.get('fast_mode', False),
        'review_mode': session.get('review_mode', False),
        'accuracy': round(100 * session['correct'] / attempts, 1) if attempts else None,
        'avg_seconds_per_item': round(elapsed / practiced, 1) if practiced else None,
    }
    if session.get('is_gauntlet'):
        result['gauntlet'] = {
            'mode': session.get('gauntlet_mode'),
            'stage': session.get('gauntlet_stage'),
            'stage_name': session.get('gauntlet_stage_name'),
            'day': session.get('gauntlet_day'),
            'sessions_done': session.get('gauntlet_sessions_done', 0) + (0 if ended_early else 1),
            'voided': ended_early,
        }
    return result



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


def process_drill_answer(session, answer, noun_answers=None):
    cur = session['current']
    lang = cur.get('lang', session['lang'])
    drill = cur['drill']
    target = drill.get('target', DRILL_TARGET)
    if answer == '!!':
        return {'done': False, 'result': 'drill_required', 'message': 'Complete the drill before ending the session.'}

    if ll.noun_answers_match(noun_answers, cur.get('noun_forms')) if cur.get('noun_forms') else ll.answer_matches(
        answer, cur['word_text'], sentence_mode=session.get('sentence_mode', False)
    ):
        drill['correct_in_a_row'] += 1
        if drill['correct_in_a_row'] >= target:
            cur['drill'] = None
            ll.complete_drill(
                session['user'], lang, cur['word_id'],
                known_review=session.get('known_drill_mode', False)
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


def process_answer(session, answer, noun_answers=None):
    answer = (answer or '').strip()
    cur = session['current']
    lang = cur.get('lang', session['lang'])
    sentence_mode = session.get('sentence_mode', False)
    record_current_time(session)

    # Session-level commands are always honoured, even mid-drill.
    if answer == '!!':
        if cur.get('drill') is not None:
            return {'done': False, 'result': 'drill_required', 'message': 'Complete the drill before ending the session.'}
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if session.get('review_mode'):
        return advance_review(session, answer)

    if session.get('fast_mode'):
        correct = ll.noun_answers_match(noun_answers, cur.get('noun_forms')) if cur.get('noun_forms') else \
            ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)
        if correct:
            ll.record_fast_review(session['user'], lang, cur['word_id'])
        return advance_fast(session, correct, answer)

    timed_out = answer == '!!TIMEOUT!!'

    if answer.startswith('@') and not timed_out:
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(session['user'], lang, cur['word_id'], 'mastered')
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!') and not timed_out:
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(
                session['user'], lang, cur['word_id'], 'flagged', cur['score'], cur['leitner_box']
            )
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    if cur['drill'] is not None:
        return process_drill_answer(session, answer, noun_answers)

    if answer.startswith('$') and not timed_out:
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

    if timed_out:
        correct = False
    elif cur.get('noun_forms'):
        correct = ll.noun_answers_match(noun_answers, cur.get('noun_forms'))
    else:
        correct = ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)

    if correct:
        ll.update_word_score(session['user'], lang, cur['word_id'],
                             'correct', cur['score'], cur['leitner_box'])
    else:
        ll.update_word_score(session['user'], lang, cur['word_id'],
                             'incorrect', cur['score'], cur['leitner_box'])
        ll.record_drill_debt(session['user'], lang, cur['word_id'])

    target_ans = cur['word_text']
    ll.log_event(
        "ANSWER_SUBMITTED",
        user=session['user'],
        session_id=session.get('session_id', 'N/A'),
        word_id=cur['word_id'],
        typed=answer,
        target=target_ans,
        result='CORRECT' if correct else 'INCORRECT',
        new_score=cur.get('score', 0.0)
    )

    if correct:
        return advance(session, 'correct', None, attempt=answer)

    if not correct:
        session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
        record_file_incorrect(session)

        # In gauntlet, instant_drill is ALWAYS on — no choice.
        # For non-gauntlet sessions, check the flag.
        if session.get('instant_drill') or session.get('is_gauntlet'):
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

        return advance(session, 'incorrect', f"Incorrect. Correct answer was '{cur['word_text']}'." , attempt=answer)


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
    """Return progress for selectable lists, optionally filtered by category and level."""
    user_s = ll.sanitize_name(user, 'user')
    prefix = f"words_{user_s}_"
    conn = ll.get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
        (f"{prefix}%",)
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
        sentence_mode = ll.is_sentence_list(lang)
        has_leitner = 'leitner_box' in {
            r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        }
        if has_leitner:
            to_drill_expr = 'SUM(CASE WHEN drill_pending = 1 THEN 1 ELSE 0 END)'
            row = conn.execute(
                f'SELECT COUNT(*), '
                f'SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END), '
                f'{to_drill_expr}, '
                f'SUM(CASE WHEN score >= 9.0 AND leitner_box IS NOT NULL AND (last_practiced IS NULL OR '
                f'julianday(\'now\', \'localtime\') - julianday(last_practiced) >= '
                f'{ll.leitner_interval_case()} '
                f') THEN 1 ELSE 0 END) '
                f'FROM "{table_name}" WHERE active = 1'
            ).fetchone()
            total, learned, to_drill, due_today = row
        else:
            to_drill_expr = '0'
            row = conn.execute(
                f'SELECT COUNT(*), '
                f'SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END), '
                f'{to_drill_expr} '
                f'FROM "{table_name}" WHERE active = 1'
            ).fetchone()
            total, learned, to_drill = row
            due_today = 0
        total = total or 0
        learned = learned or 0
        to_drill = to_drill or 0
        due_today = due_today or 0
        lists.append({
            'lang': lang,
            'total': total,
            'learned': learned,
            'to_drill': to_drill,
            'due_today': due_today,
            'progress': round(100 * learned / total, 1) if total > 0 else 0.0,
        })
    conn.close()
    return lists


def leitner_stats_data(user, lang):
    """Per-box word counts and due-today totals for one word list."""
    table = ll.words_table_name(user, lang)
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None

    active_clause = 'active = 1'
    box_clause = ' AND score >= 9.0 AND leitner_box IS NOT NULL'
    due_case = ll.leitner_interval_case()
    rows = conn.execute(f'''
        SELECT leitner_box, COUNT(*) AS total,
            SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END) AS learned,
            SUM(CASE WHEN last_practiced IS NULL OR
                julianday('now', 'localtime') - julianday(last_practiced) >=
                {due_case}
                THEN 1 ELSE 0 END) AS due
        FROM "{table}" WHERE {active_clause}{box_clause}
        GROUP BY leitner_box ORDER BY leitner_box
    ''').fetchall()

    summary = conn.execute(f'''
        SELECT COUNT(*),
            SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN times_practiced = 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN score >= 9.0 AND leitner_box IS NOT NULL AND (
                last_practiced IS NULL OR
                julianday('now', 'localtime') - julianday(last_practiced) >=
                {due_case}
            ) THEN 1 ELSE 0 END)
        FROM "{table}" WHERE {active_clause}
    ''').fetchone()
    conn.close()

    interval_values = ll.LEITNER_INTERVALS
    INTERVALS = {
        box: f'{days} day' + ('' if days == 1 else 's')
        for box, days in interval_values.items()
    }
    counts = {
        b: {'total': t or 0, 'learned': l or 0, 'due': d or 0}
        for b, t, l, d in rows
    }
    max_box = 10
    boxes = [
        {'box': b, **counts.get(b, {'total': 0, 'learned': 0, 'due': 0}),
         'interval': INTERVALS.get(b, '?')}
        for b in range(1, max_box + 1)
    ]
    total, learned, never_practiced, due_today = summary
    return {
        'total': total or 0,
        'learned': learned or 0,
        'never_practiced': never_practiced or 0,
        'due_today': due_today or 0,
        'boxes': boxes,
    }


def _corrects_to_mastery(score, sentence_mode=False):
    """Return the shared engine's remaining correct-answer count."""
    return ll.corrects_to_mastery(score, sentence_mode)


def dashboard_data(user, lang=None):
    """All analytics data for the dashboard: overview, velocity, and (if lang
    given) mastery funnel, nemesis words, and per-list completion prediction."""
    user_s = ll.sanitize_name(user, 'user')
    lang_s = ll.sanitize_name(lang, 'language') if lang else None
    sessions_table = f"sessions_{user_s}"
    conn = ll.get_connection()

    has_sessions = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (sessions_table,)
    ).fetchone() is not None

    total_seconds = total_practiced = total_correct = total_incorrect = 0
    current_streak = best_streak = 0
    avg_seconds_per_word = avg_words_7d = avg_seconds_7d = 0.0
    session_count = distinct_days = 0

    # Scope session queries to the selected list when lang is given
    s_where = f'WHERE language = ?' if lang_s else ''
    s_params = (lang_s,) if lang_s else ()
    s_and_lang = f'AND language = ?' if lang_s else ''

    if has_sessions:
        t = conn.execute(
            f'SELECT SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count) FROM "{sessions_table}" {s_where}',
            s_params
        ).fetchone()
        total_seconds = t[0] or 0
        total_practiced = t[1] or 0
        total_correct = t[2] or 0
        total_incorrect = t[3] or 0

        all_dates = [r[0] for r in conn.execute(
            f'SELECT session_date FROM "{sessions_table}" {s_where}', s_params).fetchall()]
        current_streak, best_streak = ll.compute_streak(all_dates)
        distinct_days = len(set(all_dates))
        session_count = len(all_dates)

        last_7 = conn.execute(
            f"SELECT SUM(words_practiced), SUM(duration_seconds) FROM \"{sessions_table}\" "
            f"WHERE session_date >= date('now', '-6 days', 'localtime') {s_and_lang}",
            (lang_s,) if lang_s else ()
        ).fetchone()
        avg_words_7d = (last_7[0] or 0) / 7.0
        avg_seconds_7d = (last_7[1] or 0) / 7.0
        if total_practiced > 0:
            avg_seconds_per_word = total_seconds / total_practiced

    total_answers = total_correct + total_incorrect
    overall_accuracy = round(100 * total_correct / total_answers, 1) if total_answers > 0 else None

    # --- Due today: scoped to selected list, or all lists if no lang ---
    if lang_s:
        tname = f"words_{user_s}_{lang_s}"
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tname,)
        ).fetchone():
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tname}")').fetchall()}
            if 'leitner_box' in cols:
                due_today_total = conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1 AND score >= 9.0 AND leitner_box IS NOT NULL AND ("
                    f"last_practiced IS NULL OR "
                    f"julianday('now','localtime') - julianday(last_practiced) >= "
                    f"{ll.leitner_interval_case()})"
                ).fetchone()[0]
            else:
                due_today_total = conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1"
                ).fetchone()[0]
        else:
            due_today_total = 0
    else:
        prefix = f"words_{user_s}_"
        word_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (f"{prefix}%",)
        ).fetchall()
        due_today_total = 0
        for (tname,) in word_tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tname}")').fetchall()}
            if 'leitner_box' in cols:
                due_today_total += conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1 AND score >= 9.0 AND leitner_box IS NOT NULL AND ("
                    f"last_practiced IS NULL OR "
                    f"julianday('now','localtime') - julianday(last_practiced) >= "
                    f"{ll.leitner_interval_case()})"
                ).fetchone()[0]
            else:
                due_today_total += conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1"
                ).fetchone()[0]

    # Benchmark pace vs. 20 words/day standard
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
            'due_today': due_today_total,
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
    }

    # --- Per-list data (requires lang) ---
    if lang_s:
        ll.sync_word_list(user_s, lang_s)
        wtable = f"words_{user_s}_{lang_s}"
        has_wtable = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (wtable,)
        ).fetchone() is not None

        if has_wtable:
            wcols = {r[1] for r in conn.execute(f'PRAGMA table_info("{wtable}")').fetchall()}
            has_leitner = 'leitner_box' in wcols

            # Mastery funnel: Learning (1–3.9), Familiar (4–8.9), Mastered (9.0)
            f_row = conn.execute(
                f'SELECT SUM(CASE WHEN score < 4.0 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN score >= 4.0 AND score < 9.0 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END), COUNT(*) '
                f'FROM "{wtable}" WHERE active=1'
            ).fetchone()
            learning, familiar, mastered_count, total_words = f_row
            result['mastery'] = {
                'learning': learning or 0,
                'familiar': familiar or 0,
                'mastered': mastered_count or 0,
                'total': total_words or 0,
            }

            # Nemesis: top-10 hardest words by incorrect count
            try:
                material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user, lang_s))}
            except (OSError, ValueError):
                material = {}
            result['nemesis'] = [
                {'word': material.get(r[0], {}).get('word', r[0]), 'times_incorrect': r[1],
                 'times_correct': r[2], 'score': round(r[3], 1)}
                for r in conn.execute(
                    f'SELECT content_id, times_incorrect, times_correct, score FROM "{wtable}" '
                    f'WHERE active=1 AND times_incorrect > 0 '
                    f'ORDER BY times_incorrect DESC, score ASC LIMIT 10'
                ).fetchall()
            ]

            # Prediction: grind hours + calendar date when all words reach box 5
            enough_data = session_count >= 3 and avg_seconds_per_word and avg_seconds_per_word > 0
            sentence_mode = ll.is_sentence_list(lang_s)
            if enough_data:
                box_col = 'leitner_box' if has_leitner else '1'
                word_rows = conn.execute(
                    f'SELECT score, {box_col} FROM "{wtable}" WHERE active=1'
                ).fetchall()

                # Total corrects needed → grind hours
                total_corrects = sum(ll.corrects_to_mastery(s, sentence_mode=sentence_mode) for s, _ in word_rows)
                grind_hours = round(total_corrects * avg_seconds_per_word / 3600, 1)

                # Calendar date: today + max(grind_days + leitner_days) over all words
                avg_secs_per_day = avg_seconds_7d if avg_seconds_7d > 0 else (
                    total_seconds / distinct_days if distinct_days > 0 else 3600
                )
                max_days = 0.0
                for score, box in word_rows:
                    b = int(box) if box else 1
                    corrects = ll.corrects_to_mastery(score, sentence_mode=sentence_mode)
                    grind_days = corrects * avg_seconds_per_word / avg_secs_per_day
                    # After reaching score 9, words advance through remaining Leitner boxes
                    leitner_days = sum(
                        ll.LEITNER_INTERVALS.get(bb, 10) for bb in range(b, 11)
                    )
                    total_days = grind_days + leitner_days
                    if total_days > max_days:
                        max_days = total_days

                box5_date = (date.today() + timedelta(days=int(max_days))).isoformat()
                result['prediction'] = {
                    'grind_hours': grind_hours,
                    'box5_date': box5_date,
                    'enough_data': True,
                }
            else:
                result['prediction'] = {
                    'grind_hours': None,
                    'box5_date': None,
                    'enough_data': False,
                    'sessions_needed': max(0, 3 - session_count),
                }

        # Roadmap visualization data (Always runs if lang_s is provided)
        gauntlet_progress = ll.get_dataset_progress(user, lang_s, conn=conn)
        stage, stage_name, _ = ll.gauntlet_stage_for_day(gauntlet_progress['current_day'])
        
        leitner_distribution = {str(i): 0 for i in range(1, 11)}
        if has_wtable:
            wcols = {r[1] for r in conn.execute(f'PRAGMA table_info("{wtable}")').fetchall()}
            if 'leitner_box' in wcols:
                l_rows = conn.execute(
                    f'SELECT leitner_box, COUNT(*) FROM "{wtable}" '
                    f'WHERE active=1 AND leitner_box IS NOT NULL '
                    f'GROUP BY leitner_box'
                ).fetchall()
                for box, count in l_rows:
                    if box:
                        leitner_distribution[str(box)] = count
        
        total_active = 0
        remaining_tasks = 0
        if has_wtable:
            total_active = conn.execute(f'SELECT COUNT(*) FROM "{wtable}" WHERE active=1').fetchone()[0]
            remaining_tasks = ll.get_gauntlet_tasks_remaining(user, lang_s, gauntlet_progress['current_day'])

        result['roadmap'] = {
            'gauntlet': {
                'current_stage': stage,
                'current_day': gauntlet_progress['current_day'],
                'sessions_done_today': gauntlet_progress['sessions_done_today'],
                'stage_name': stage_name,
                'remaining_tasks': remaining_tasks,
                'total_tasks': total_active
            },
            'leitner_distribution': leitner_distribution
        }

    conn.close()
    return result


def word_list_stats(user, lang, due_today_only=False):
    table = ll.words_table_name(user, lang)
    ll.sync_word_list(user, lang)
    material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user, lang))}
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None
    active_clause = 'active = 1'
    due_case = ll.leitner_interval_case()
    select_columns = (
        'content_id, score, active, times_practiced, times_correct, times_incorrect, '
        'times_drilled, times_flagged, times_mastered, last_practiced, leitner_box, last_known_review_at'
    )
    
    today = date.today()
    if due_today_only:
        # Only select words that are due today (next review is today or earlier)
        query = f'''
            SELECT {select_columns}
            FROM "{table}" WHERE {active_clause} AND score >= 9.0 AND leitner_box IS NOT NULL AND (
                last_practiced IS NULL OR
                julianday(?, 'localtime') - julianday(last_practiced) >=
                {due_case}
            ) ORDER BY score DESC, content_id ASC
        '''
        rows = conn.execute(query, (today.isoformat(),)).fetchall()
    else:
        order = 'active DESC, score DESC, content_id ASC'
        rows = conn.execute(
            f'SELECT {select_columns} FROM "{table}" ORDER BY {order}'
        ).fetchall()
    
    conn.close()
    words = []
    for (text, score, active, practiced, correct, incorrect,
         drilled, flagged, mastered, last_practiced, leitner_box, last_known_review_at) in rows:
        box = leitner_box
        if last_practiced and box:
            interval = ll.LEITNER_INTERVALS.get(box, 1)
            next_review = (
                date.fromisoformat(str(last_practiced)[:10]) + timedelta(days=interval)
            ).isoformat()
        else:
            next_review = None
        item = material.get(text, {})
        words.append({
            'word': item.get('word', text),
            'score': round(score, 1),
            'gauge': gauge_dots(score),
            'band': ll.score_band(score),
            'active': bool(active),
            'leitner_box': box,
            'next_review': next_review,
            'times_practiced': practiced,
            'times_correct': correct,
            'times_incorrect': incorrect,
            'times_drilled': drilled,
            'times_flagged': flagged,
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
        source = ll.read_word_list(target_path)
    else:
        try:
            source = ll.read_word_list(ll.word_list_path(user, lang))
        except FileNotFoundError:
            source = {'metadata': {'language': 'unknown', 'type': 'vocabulary', 'cefr_level': 'all'}, 'items': []}
    originals = {entry['id']: entry for entry in ll.validate_word_list_items(source['items'], target_path)}
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
    ll.write_word_list_atomic(target_path, {'metadata': source['metadata'], 'items': saved})
    ll.sync_word_list(user, lang)
    return target_path, len(saved)

def init_word_list(user, lang, material_type='vocabulary'):
    """Create a user-owned, schema-valid list without mutating shared material."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    material_type = str(material_type).strip().lower()
    if material_type not in {'vocabulary', 'nouns'}:
        raise ValueError("List type must be 'vocabulary' or 'nouns'.")
    path = ll.word_list_path_user_specific(user, lang)
    created = not os.path.exists(path)
    if created:
        ll.write_word_list_atomic(path, {
            'metadata': {
                'language': 'german' if material_type == 'nouns' else 'unknown',
                'type': material_type, 'cefr_level': 'all',
                'category': 'german_vocabulary' if material_type == 'nouns' else 'all',
                'pos': 'noun' if material_type == 'nouns' else 'all'
            },
            'items': [],
        })
    conn = ll.get_connection()
    ll.ensure_user(conn, user)
    ll.ensure_word_table(conn, user, lang)
    ll.ensure_sessions_table(conn, user)
    conn.commit()
    conn.close()
    return created, path

def save_noun(user, slug, noun, translation, forms):
    """Create or update one user-owned German noun in the canonical noun schema."""
    user = ll.sanitize_name(user, 'user')
    slug = ll.sanitize_name(slug, 'language')
    noun = noun.strip()
    translation = translation.strip()
    if not noun or not translation:
        raise ValueError('A noun and its English translation are required.')
    required = {(case_name, number) for case_name in ll.NOUN_CASES for number in ('singular', 'plural')}
    if set(forms) != required:
        raise ValueError('A German noun requires singular and plural forms for all four cases.')
    path = ll.word_list_path_user_specific(user, slug)
    if os.path.exists(path):
        data = ll.read_word_list(path)
    else:
        data = {
            'metadata': {
                'name': slug.replace('_', ' ').title(), 'language': 'german',
                'type': 'nouns', 'cefr_level': 'all', 'pos': 'noun',
            },
            'items': [],
        }
    if data['metadata'].get('type') != 'nouns':
        raise ValueError('The selected list is not a German noun list.')
    base_id = re.sub(r'[^a-z0-9]+', '-', noun.lower()).strip('-')
    content_id = f'noun-{base_id}'
    noun_forms = {}
    for case_name in ll.NOUN_CASES:
        noun_forms[case_name] = {}
        for number in ('singular', 'plural'):
            form_data = forms[(case_name, number)]
            form = str(form_data.get('form', '')).strip()
            sentence = str(form_data.get('sentence', '')).strip()
            sentence_translation = str(form_data.get('translation', '')).strip()
            if not form or not sentence or not sentence_translation:
                raise ValueError(f'Missing {case_name} {number} form, example, or translation.')
            noun_forms[case_name][number] = {
                'form': form, 'sentence': sentence, 'translation': sentence_translation,
            }
    entry = {
        'id': content_id, 'kind': 'noun', 'word': noun, 'definition': translation,
        'word_frequency': 0, 'noun_forms': noun_forms,
    }
    items = [old for old in data['items'] if old.get('id') != content_id]
    items.append(entry)
    data['items'] = ll.validate_word_list_items(items, path)
    ll.write_word_list_atomic(path, data)
    ll.sync_word_list(user, slug)
    return path, content_id


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

        if parsed.path == '/api/noun':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                data = ll.read_word_list(ll.word_list_path(user, lang))
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            if data['metadata'].get('type') != 'nouns':
                return self._send_json({'error': 'The selected list is not a German noun list.'}, 400)
            return self._send_json({
                'metadata': data['metadata'],
                'items': data['items'],
                'read_only': ll.is_read_only_sample_list(user, lang),
            })

        if parsed.path == '/api/wordlist/stats':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            due_today = qs.get('due_today', ['false'])[0].lower() == 'true'
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                words = word_list_stats(user, lang, due_today_only=due_today)
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

        if parsed.path == '/api/gauntlet/progress':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                ll.sync_word_list(user, lang)
                progress = ll.transition_gauntlet_day(user, lang)
                stage, stage_name, session_mode = ll.gauntlet_stage_for_day(progress['current_day'])
                remaining_tasks = ll.get_gauntlet_tasks_remaining(user, lang, progress['current_day'])
                locked = (remaining_tasks == 0)
                
                leitner_distribution = {str(i): 0 for i in range(1, 11)}
                conn = ll.get_connection()
                wtable = ll.words_table_name(user, lang)
                has_wtable = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (wtable,)
                ).fetchone() is not None
                if has_wtable:
                    wcols = {r[1] for r in conn.execute(f'PRAGMA table_info("{wtable}")').fetchall()}
                    if 'leitner_box' in wcols:
                        l_rows = conn.execute(
                            f'SELECT leitner_box, COUNT(*) FROM "{wtable}" '
                            f'WHERE active=1 AND leitner_box IS NOT NULL '
                            f'GROUP BY leitner_box'
                        ).fetchall()
                        for box, count in l_rows:
                            if box:
                                leitner_distribution[str(box)] = count
                
                # Get total active words for progress bar
                total_active = 0
                if has_wtable:
                    total_active = conn.execute(f'SELECT COUNT(*) FROM "{wtable}" WHERE active=1').fetchone()[0]
                conn.close()

                return self._send_json({
                    'progress': {
                        **progress,
                        'current_stage': stage,
                        'stage_name': stage_name,
                        'session_mode': session_mode,
                        'remaining_tasks': remaining_tasks,
                        'total_tasks': total_active,
                        'max_day': ll.GAUNTLET_MAX_DAY,
                        'locked_today': locked
                    },
                    'roadmap': {
                        'gauntlet': {
                            'current_stage': stage,
                            'current_day': progress['current_day'],
                            'sessions_done_today': progress['sessions_done_today'],
                            'stage_name': stage_name,
                            'remaining_tasks': remaining_tasks,
                            'total_tasks': total_active
                        },
                        'leitner_distribution': leitner_distribution
                    }
                })
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

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

        if parsed.path == '/api/noun':
            user = str(payload.get('user', '')).strip()
            slug = str(payload.get('lang', '')).strip()
            noun = str(payload.get('noun', '')).strip()
            forms = {}
            for case_name in ('nominative', 'accusative', 'dative', 'genitive'):
                for number in ('singular', 'plural'):
                    value = payload.get(f'{case_name}_{number}', {})
                    forms[(case_name, number)] = value if isinstance(value, dict) else {}
            try:
                set_id, item_id = save_noun(
                    user, slug, noun, str(payload.get('translation', '')).strip(), forms
                )
            except (TypeError, ValueError) as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'saved': True, 'path': set_id, 'item_id': item_id})

        if parsed.path == '/api/practice/start':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            review_mode = bool(payload.get('review_mode', False))
            try:
                wpm = int(payload.get('wpm', 128))
            except (TypeError, ValueError):
                wpm = 128
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)

            try:
                if review_mode:
                    # Review mode is still available (read-only due-word review)
                    session_id, session = start_session(
                        user, lang, audio_lang=audio_lang, wpm=wpm, review_mode=True,
                    )
                    question = next_question(session)
                    return self._send_json({
                        'session_id': session_id,
                        'lang': session['lang'],
                        'audio_lang': session['voice_lang'],
                        'fast_mode': False,
                        'review_mode': True,
                        'gauntlet': None,
                        'progress': {
                            'correct': 0, 'drilled': 0,
                            'total': session['total'],
                            'questions': 0,
                            'max_questions': session['max_questions'],
                        },
                        'question': question,
                    })

                # === GAUNTLET MODE: backend decides everything ===
                session_id, session, gauntlet_meta = gauntlet_start_session(
                    user, lang, wpm=wpm, audio_lang=audio_lang,
                )
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)

            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang': session['lang'],
                'audio_lang': session['voice_lang'],
                'fast_mode': session['fast_mode'],
                'review_mode': False,
                'gauntlet': gauntlet_meta,
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
                    result = process_answer(session, payload.get('answer', ''), payload.get('noun_answers'))
                except Exception as e:
                    import traceback; traceback.print_exc()
                    SESSIONS.pop(session_id, None)
                    if session.get('practiced', 0) > 0:
                        finalize_session(session, ended_early=True)
                    return self._send_json({'error': f'Internal error processing answer: {str(e)}'}, 500)
                session['answer_results'][attempt_id] = result
                if result.get('done'):
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
