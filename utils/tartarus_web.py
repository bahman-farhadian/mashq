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
from pathlib import Path

from datetime import date, timedelta
import tartarus as ll
import conjugation

HOST = '127.0.0.1'
PORT = 9999

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


# --- Session lifecycle ---
def mastered_words(user, lang):
    """Read all mastered entries, ordered by their last Fast review."""
    return ll.get_mastered_words_for_fast(user, lang)


def level_words(user, category, level, drill_mode=False, known_drill_mode=False,
                fast_mode=False):
    """Return mode-appropriate candidates across all files in a CEFR level."""
    files = [item for item in list_word_lists()
             if item['user'] == user and item['category'] == category and item['level'] == level]
    candidates = []
    for item in files:
        ll.sync_word_list(user, item['lang'])
        try:
            if fast_mode:
                rows = mastered_words(user, item['lang'])
            else:
                rows = ll.get_words_for_practice(
                    user, item['lang'],
                    DRILL_WORDS if drill_mode else MAX_QUESTIONS,
                    drill_mode=drill_mode,
                    known_drill_mode=known_drill_mode,
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


def start_session(user, lang, audio_lang=None, drill_all=False, drill_mode=False, known_drill_mode=False, instant_drill=False, fast_mode=False, wpm=128, level_mode=False, category=None, level=None, review_mode=False):
    sentence_mode = ll.is_sentence_list(lang)
    conjugation_mode = conjugation.is_conjugation_list(lang)
    selected_drill_modes = sum(bool(value) for value in (drill_all, drill_mode, known_drill_mode, instant_drill))
    if selected_drill_modes > 1:
        raise ValueError("Choose only one drill mode per session.")
    if conjugation_mode:
        if review_mode or level_mode or fast_mode:
            raise ValueError("Conjugation practice cannot be combined with review, level, or Fast mode.")
        ll.sync_word_list(user, lang)
        conn = ll.get_connection()
        words = conjugation.next_units(
            conn, user, MAX_QUESTIONS,
            drill_mode=drill_mode,
            known_drill_mode=known_drill_mode,
        )
        conn.close()
        if (drill_mode or known_drill_mode) and not any(row['stage'] != 1 for row in words):
            label = "mistakes" if drill_mode else "mastered conjugations"
            raise ValueError(f"No {label} are available to drill.")
        if not words:
            raise ValueError("No conjugation units are available.")
    elif review_mode:
        if level_mode or fast_mode or selected_drill_modes:
            raise ValueError("Review mode cannot be combined with practice modes.")
        if not lang:
            raise ValueError("Select a word list file before starting a review.")
        ll.sync_word_list(user, lang, apply_score_decay=False)
        words = ll.get_words_for_practice(user, lang, MAX_QUESTIONS)
        if not words:
            raise ValueError("No due words are available for review in this file.")
    elif level_mode:
        if not category or not level:
            raise ValueError("A language and level are required for level practice.")
        if lang:
            raise ValueError("Clear the word list file selection before practicing the whole level.")
        words = level_words(
            user, category, level,
            drill_mode=drill_mode or drill_all,
            known_drill_mode=known_drill_mode,
            fast_mode=fast_mode,
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
        )
    source_language = (category or lang or '').split('_', 1)[0].lower()
    default_voice = source_language if source_language in {'english', 'german'} else lang
    voice_lang = 'german' if conjugation_mode else (audio_lang or default_voice)

    if conjugation_mode:
        queue = [
            {'lang': lang, 'word_id': row['unit_key'], 'word_text': row['answer'],
             'definition': row['prompt'], 'score': row['score'],
             'leitner_box': row['leitner_box'],
             'conjugation': row}
            for row in words
        ]
    else:
        queue = words if level_mode else [
        {'lang': lang, 'word_id': r[0], 'word_text': r[1], 'definition': r[2],
         'score': r[3], 'leitner_box': r[4], 'noun_forms': r[6] if len(r) > 6 else None,
         'noun_case': r[6].get('case') if len(r) > 6 and isinstance(r[6], dict) else None}
        for r in words
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
        'max_questions': len(queue) if (conjugation_mode or fast_mode or level_mode) else (DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS),
        'drill_mode': drill_mode,
        'known_drill_mode': known_drill_mode,
        'instant_drill': instant_drill,
        'fast_mode': fast_mode,
        'review_mode': review_mode,
        'conjugation_mode': conjugation_mode,
        'conjugation_completed': 0,
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
    }
    SESSIONS[session_id] = session
    return session_id, session


def next_question(session):
    queue = session['queue']
    if not queue:
        return None
    if session.get('review_mode'):
        index = session.get('review_index', 0)
        if index >= len(queue):
            return None
        entry = queue[index]
        question = {
            'word_id': entry['word_id'],
            'word': entry['word_text'],
            'word_unmasked': entry['word_text'],
            'definition': [],
            'score': round(entry['score'], 1),
            'gauge': 'Review',
            'band': ll.score_band(entry['score']),
            'gender': ll.get_gender_style(entry['word_text'])[1],
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
    elif session.get('conjugation_mode'):
        row = entry.get('conjugation', {})
        daily_pronoun = row.get('stage') == 1
        question, drill = ll.build_question_data(
            entry['word_id'], entry['word_text'], entry['definition'],
            entry['score'], entry['leitner_box'], sentence_mode=False,
            fast_mode=False,
            drill_mode=(
                (session.get('drill_mode', False) or session.get('drill_all', False))
                and not daily_pronoun
            ),
            known_drill_mode=(
                session.get('known_drill_mode', False) and not daily_pronoun
            ))
        question['conjugation'] = {
            'stage': row.get('stage'), 'stage_name': row.get('stage_name'),
            'verb': row.get('verb'), 'verb_order': row.get('verb_order'),
            'pronoun_order': row.get('pronoun_order'),
            'daily_pronoun': daily_pronoun,
        }
    else:
        question_definition = entry['definition']
        if entry.get('noun_forms'):
            question_definition = ll.english_definition_only(question_definition)
        question, drill = ll.build_question_data(
            entry['word_id'], entry['word_text'], question_definition, entry['score'], entry['leitner_box'],
            sentence_mode=session.get('sentence_mode', False), fast_mode=session.get('fast_mode', False),
            drill_mode=(session.get('drill_mode', False) or session.get('drill_all', False)),
            known_drill_mode=session.get('known_drill_mode', False))
        if entry.get('noun_forms'):
            question['noun_forms'] = ll.noun_form_hints(entry['noun_forms'], entry['score'])
            question['noun_forms_unmasked'] = ll.noun_form_hints(entry['noun_forms'], 0)
            question['noun_meanings'] = entry['noun_forms'].get('meanings', {})
            question['noun_case'] = entry.get('noun_case')
            question['noun_grid'] = True
            question['audio_text'] = ll.noun_audio_text(entry['noun_forms'])
            if question.get('drill_start'):
                question['drill_start']['word'] = entry['word_text']
                question['drill_start']['noun_forms'] = ll.noun_form_hints(entry['noun_forms'], 0)
    if session.get('known_drill_mode') and not question.get('conjugation', {}).get('daily_pronoun'):
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
        'conjugation': entry.get('conjugation'),
        'noun_forms': entry.get('noun_forms'),
        'noun_case': entry.get('noun_case'),
    }
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
    practiced = session['practiced']
    attempts = practiced + len(session['incorrect']) if session.get('fast_mode') else practiced
    return {
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
    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if ll.noun_answers_match(noun_answers, cur.get('noun_forms')) if cur.get('noun_forms') else ll.answer_matches(
        answer, cur['word_text'], sentence_mode=session.get('sentence_mode', False)
    ):
        drill['correct_in_a_row'] += 1
        if drill['correct_in_a_row'] >= DRILL_TARGET:
            cur['drill'] = None
            ll.record_as_drilled(
                session['user'], lang, cur['word_id'],
                known_review=session.get('known_drill_mode', False)
            )
            result = advance(session, 'drilled', "Drill complete.")
            result['drill'] = {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'noun_forms': ll.noun_form_hints(cur.get('noun_forms'), 0),
                'repetition': DRILL_TARGET,
                'correct_in_a_row': DRILL_TARGET,
                'target': DRILL_TARGET,
                'correct': True,
                'show_word': True,
            }
            return result
        correct = True
    else:
        drill['correct_in_a_row'] = 0
        correct = False

    drill['repetition'] += 1
    return {
        'result': 'drill_progress',
        'done': False,
        'drill': {
            'word': cur['word_text'],
            'definition': drill_definition_lines(cur),
            'noun_forms': ll.noun_form_hints(cur.get('noun_forms'), 0),
            'repetition': drill['repetition'],
            'correct_in_a_row': drill['correct_in_a_row'],
            'target': DRILL_TARGET,
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
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if session.get('review_mode'):
        return advance_review(session, answer)

    if session.get('fast_mode'):
        correct = ll.noun_answers_match(noun_answers, cur.get('noun_forms')) if cur.get('noun_forms') else \
            ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)
        if correct:
            ll.record_fast_review(session['user'], lang, cur['word_id'])
        return advance_fast(session, correct, answer)

    if answer.startswith('@'):
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(session['user'], lang, cur['word_id'], 'mastered')
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!'):
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(session['user'], lang, cur['word_id'], 'flagged')
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], lang, cur['word_id'])
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    if cur['drill'] is not None:
        return process_drill_answer(session, answer, noun_answers)

    if answer.startswith('$'):
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'noun_forms': ll.noun_form_hints(cur.get('noun_forms'), 0),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'show_word': True,
            },
        }

    correct = ll.noun_answers_match(noun_answers, cur.get('noun_forms')) if cur.get('noun_forms') else \
        ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)

    if correct:
        ll.update_word_score(session['user'], lang, cur['word_id'],
                             'correct', cur['score'], cur['leitner_box'])
        return advance(session, 'correct', None, attempt=answer)

    ll.update_word_score(session['user'], lang, cur['word_id'],
                         'incorrect', cur['score'], cur['leitner_box'])
    session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
    record_file_incorrect(session)
    cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1, 'instant': True}
    return {
        'result': 'drill_start',
        'done': False,
        'message': 'Incorrect. Complete the drill before continuing.',
        'drill': {
            'word': cur['word_text'],
            'definition': drill_definition_lines(cur),
            'noun_forms': ll.noun_form_hints(cur.get('noun_forms'), 0),
            'repetition': 1,
            'correct_in_a_row': 0,
            'target': DRILL_TARGET,
            'correct': False,
            'show_word': True,
        },
    }


# --- Word lists / report ---
def list_word_lists():
    """Return shared lists grouped by language/type for cascading selectors.

    Each entry keeps ``lang`` as the database/list identifier, while
    ``category`` is one of ``english_vocabulary``, ``english_sentences``,
    ``german_vocabulary``, or ``german_sentences``.
    """
    if not os.path.isdir(ll.WORD_LISTS_DIR):
        return []
    conn = ll.get_connection()
    users = [row[0] for row in conn.execute(
        "SELECT name FROM users WHERE name != 'system' ORDER BY name"
    )]
    conn.close()
    personal_users = {
        user for user in users if ll.user_has_personal_material(user)
    }
    result = []
    for path in sorted(Path(ll.WORD_LISTS_DIR).rglob('*.json')):
        relative = path.relative_to(ll.WORD_LISTS_DIR)
        parts = relative.parts
        if os.path.abspath(path) == os.path.abspath(conjugation.SOURCE_PATH):
            count = len(conjugation.load_source())
            for user in users:
                if user in personal_users:
                    continue
                result.append({
                    'user': user, 'lang': conjugation.LIST_ID,
                    'language': 'german', 'kind': 'conjugations',
                    'level': 'a1', 'category': 'german_conjugations',
                    'word_count': count, 'shared': True,
                })
            continue
        try:
            ll.load_practice_items(str(path))
            with open(path, encoding='utf-8') as source:
                count = len(json.load(source))
        except (OSError, ValueError, json.JSONDecodeError):
            count = 0
        if len(parts) >= 4 and parts[0] in {'english', 'german'} and parts[1] in {'vocabulary', 'sentences'}:
            language, kind, level = parts[:3]
            if level not in {'a1', 'a2', 'b1', 'b2', 'c1', 'c2'}:
                continue
            for user in users:
                if user in personal_users:
                    continue
                result.append({'user': user, 'lang': path.stem, 'language': language,
                               'kind': kind, 'level': level,
                               'category': f'{language}_{kind}', 'word_count': count, 'shared': True})
        elif len(parts) == 1 and '_' in path.stem:
            owner, lang = path.stem.split('_', 1)
            if owner in users:
                language = lang.split('_', 1)[0]
                kind = 'sentences' if ll.is_sentence_list(lang) else 'vocabulary'
                level = next((part for part in lang.split('_') if part in {'a1', 'a2', 'b1', 'b2', 'c1', 'c2'}), 'all')
                result.append({'user': owner, 'lang': lang, 'language': language,
                               'kind': kind, 'level': level, 'category': f'{language}_{kind}',
                               'word_count': count, 'shared': False})
    unique = {}
    for item in result:
        key = (item['user'], item['lang'])
        if key not in unique or not item['shared']:
            unique[key] = item
    return [unique[key] for key in sorted(unique)]

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
        and (not level or item.get('level') == level)
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
            to_drill_expr = '0' if sentence_mode else 'SUM(CASE WHEN times_incorrect > 0 THEN 1 ELSE 0 END)'
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
            to_drill_expr = '0' if sentence_mode else 'SUM(CASE WHEN times_incorrect > 0 THEN 1 ELSE 0 END)'
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
    conjugation_mode = conjugation.is_conjugation_list(lang)
    table = ll.practice_table_name(user, lang)
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None

    active_clause = '1 = 1' if conjugation_mode else 'active = 1'
    box_clause = ' AND score >= 9.0 AND leitner_box IS NOT NULL'
    due_case = conjugation.due_interval_case() if conjugation_mode else ll.leitner_interval_case()
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
            SUM(CASE WHEN last_practiced IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN last_practiced IS NULL OR
                julianday('now', 'localtime') - julianday(last_practiced) >=
                {due_case}
                THEN 1 ELSE 0 END)
        FROM "{table}" WHERE {active_clause}{box_clause}
    ''').fetchone()
    conn.close()

    interval_values = conjugation.LEITNER_INTERVALS if conjugation_mode else ll.LEITNER_INTERVALS
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

    conn.close()
    return result


def word_list_stats(user, lang, due_today_only=False):
    conjugation_mode = conjugation.is_conjugation_list(lang)
    table = ll.practice_table_name(user, lang)
    if not conjugation_mode:
        ll.sync_word_list(user, lang)
        material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user, lang))}
    else:
        material = {
            unit['unit_key']: {'word': unit['answer']}
            for unit in conjugation.build_units()
        }
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None
    active_clause = '1 = 1' if conjugation_mode else 'active = 1'
    due_case = conjugation.due_interval_case() if conjugation_mode else ll.leitner_interval_case()
    select_columns = (
        'unit_key, score, 1, times_practiced, times_correct, times_incorrect, '
        'times_drilled, times_mastered, last_practiced, leitner_box, NULL'
        if conjugation_mode else
        'content_id, score, active, times_practiced, times_correct, times_incorrect, '
        'times_drilled, times_mastered, last_practiced, leitner_box, last_known_review_at'
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
            ) ORDER BY score DESC, {'unit_key' if conjugation_mode else 'content_id'} ASC
        '''
        rows = conn.execute(query, (today.isoformat(),)).fetchall()
    else:
        order = 'score DESC, unit_key ASC' if conjugation_mode else 'active DESC, score DESC, content_id ASC'
        rows = conn.execute(
            f'SELECT {select_columns} FROM "{table}" ORDER BY {order}'
        ).fetchall()
    
    conn.close()
    words = []
    for (text, score, active, practiced, correct, incorrect,
         drilled, mastered, last_practiced, leitner_box, last_known_review_at) in rows:
        box = leitner_box
        if last_practiced and box:
            intervals = conjugation.LEITNER_INTERVALS if conjugation_mode else ll.LEITNER_INTERVALS
            interval = intervals.get(box, 1)
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
            'times_mastered': mastered,
            'last_practiced': last_practiced,
            'last_known_review_at': last_known_review_at,
        })
    return words


def load_word_list(user, lang):
    ll.ensure_list_available(user, lang)
    path = ll.word_list_path(user, lang)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as source:
        data = json.load(source)
    words = []
    for entry in data:
        definition = ll.normalize_definition(entry.get('definition')).split('\n')
        words.append({
            'id': entry.get('id', ''),
            'word': entry.get('word', ''),
            'def1': definition[0] if len(definition) > 0 else '',
            'def2': definition[1] if len(definition) > 1 else '',
        })
    return words


def save_word_list(user, lang, items):
    if ll.is_read_only_sample_list(user, lang):
        raise ValueError('Tartarus sample lists are read-only. Create a personal list to edit material.')
    data = []
    for position, item in enumerate(items):
        word = str(item.get('word', '')).strip()
        if not word:
            continue
        defs = [str(item.get(f, '')).strip() for f in ('def1', 'def2')]
        defs = [d for d in defs if d]
        content_id = str(item.get('id', '')).strip() or f'{lang}-{position + 1}'
        data.append({'id': content_id, 'word': word, 'definition': defs, 'word_frequency': 0})
    ids = [entry['id'] for entry in data]
    if len(ids) != len(set(ids)):
        raise ValueError('Each word list item needs a unique id.')
    path = ll.word_list_path_user_specific(user, lang)
    os.makedirs(ll.WORD_LISTS_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as target:
        json.dump(data, target, ensure_ascii=False, indent=2)
    ll.retire_sample_material(user)
    ll.sync_word_list(user, lang)
    return path, len(data)


def init_word_list(user, lang):
    user_path = ll.word_list_path_user_specific(user, lang)
    shared_path = ll.word_list_path(user, lang)
    path = shared_path if os.path.exists(shared_path) and shared_path != user_path else user_path
    created = not os.path.exists(path)
    if created and path == user_path:
        os.makedirs(ll.WORD_LISTS_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as target:
            json.dump([], target, indent=2)
        ll.retire_sample_material(user)
    conn = ll.get_connection()
    ll.ensure_user(conn, user)
    ll.ensure_word_table(conn, user, lang)
    ll.ensure_sessions_table(conn, user)
    conn.commit()
    conn.close()
    return created, path


def save_noun(user, slug, noun, translation, forms):
    """Store one German noun in the user-owned JSON source file."""
    if ll.is_read_only_sample_list(user, slug):
        raise ValueError('Tartarus sample lists are read-only. Create a personal list to add nouns.')
    required = {(case, number) for case in ('nominative', 'accusative', 'dative', 'genitive')
                for number in ('singular', 'plural')}
    if set(forms) != required:
        raise ValueError('A German noun requires singular and plural forms for all four cases.')
    path = ll.word_list_path_user_specific(user, slug)
    data = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as source:
            data = json.load(source)
    content_id = f'{slug}-{re.sub(r"[^a-z0-9]+", "-", noun.lower()).strip("-")}'
    entry = {'id': content_id, 'kind': 'noun', 'definition': translation, 'word_frequency': 0}
    for case_name, number in required:
        form_data = forms[(case_name, number)]
        form = str(form_data.get('form', '')).strip()
        sentence = str(form_data.get('sentence', '')).strip()
        sentence_translation = str(form_data.get('translation', '')).strip()
        if not form:
            raise ValueError(f'Missing {case_name} {number} noun form.')
        if not sentence or not sentence_translation:
            raise ValueError(f'Missing {case_name} {number} example or translation.')
        entry[f'{case_name}_{number}'] = form
        entry[f'{case_name}_{number}_sentence'] = sentence
        entry[f'{case_name}_{number}_translation'] = sentence_translation
    data = [old for old in data if old.get('id') != content_id] + [entry]
    os.makedirs(ll.WORD_LISTS_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as target:
        json.dump(data, target, ensure_ascii=False, indent=2)
    ll.retire_sample_material(user)
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
        self.send_header('Cache-Control', 'no-store, max-age=0')
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
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            return self._send_static(filename, content_type)

        if parsed.path == '/api/wordlists':
            return self._send_json({'wordlists': list_word_lists()})

        if parsed.path == '/api/report':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [None])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                return self._send_json({'reports': report_data(user, lang)})
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
                    'words': load_word_list(user, lang),
                    'read_only': ll.is_read_only_sample_list(user, lang),
                })
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/noun':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', ['tartarus_sample_german_nouns_a1'])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            path = ll.word_list_path(user, lang)
            rows = []
            if os.path.exists(path):
                with open(path, encoding='utf-8') as source:
                    rows = json.load(source)
            return self._send_json({
                'rows': rows,
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

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
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
            if text:
                ll.speak(text, lang or None, block=True, wpm=wpm)
            return self._send_json({})

        if parsed.path == '/api/init':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            try:
                created, path = init_word_list(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'created': created, 'path': path})

        if parsed.path == '/api/noun':
            user = str(payload.get('user', '')).strip()
            slug = str(payload.get('lang', 'tartarus_sample_german_nouns_a1')).strip()
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
            category = str(payload.get('category', '')).strip() or None
            level = str(payload.get('level', '')).strip() or None
            level_mode = bool(payload.get('level_mode', False))
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            drill_all = bool(payload.get('drill_all', False))
            drill_mode = bool(payload.get('drill_mode', False))
            known_drill_mode = bool(payload.get('known_drill_mode', False))
            instant_drill = bool(payload.get('instant_drill', False))
            fast_mode = bool(payload.get('fast_mode', False))
            review_mode = bool(payload.get('review_mode', False))
            try:
                wpm = int(payload.get('wpm', 128))
            except (TypeError, ValueError):
                wpm = 128
            try:
                session_id, session = start_session(
                    user, lang,
                    audio_lang=audio_lang,
                    drill_all=drill_all,
                    drill_mode=drill_mode,
                    known_drill_mode=known_drill_mode,
                    instant_drill=instant_drill,
                    fast_mode=fast_mode,
                    wpm=wpm,
                    level_mode=level_mode,
                    category=category,
                    level=level,
                    review_mode=review_mode,
                )
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang': session['lang'],
                'audio_lang': 'german' if session.get('conjugation_mode') else session['voice_lang'],
                'conjugation_mode': session.get('conjugation_mode', False),
                'fast_mode': session['fast_mode'],
                'review_mode': session['review_mode'],
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
            words = payload.get('words', [])
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                path, count = save_word_list(user, lang, words)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'saved': True, 'path': path, 'count': count})

        if parsed.path == '/api/practice/answer':
            session_id = payload.get('session_id')
            session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            try:
                result = process_answer(session, payload.get('answer', ''), payload.get('noun_answers'))
            except Exception:
                SESSIONS.pop(session_id, None)
                if session.get('practiced', 0) > 0:
                    finalize_session(session, ended_early=True)
                return self._send_json({'error': 'Internal error processing answer'}, 500)
            if result.get('done'):
                SESSIONS.pop(session_id, None)
            return self._send_json(result)

        self.send_error(404)


def main():
    try:
        httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
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
