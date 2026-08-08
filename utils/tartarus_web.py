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
import threading
import ipaddress
from pathlib import Path

from datetime import date
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


def gauge_color_band(score):
    """Map the three visual gauge states to the project's red/yellow/green palette."""
    if score >= 9:
        return 3
    if score >= 4:
        return 2
    return 1


# ---------------------------------------------------------------------------
# Gauntlet session builder
# ---------------------------------------------------------------------------

def gauntlet_start_session(user, lang, wpm=128, audio_lang=None):
    """Build the one guided session: Tartarus first, then Leitner maintenance."""
    today=date.today().isoformat(); user=ll.sanitize_name(user,'user'); lang=ll.sanitize_name(lang,'language')
    ll.sync_word_list(user,lang)
    progress=ll.reconcile_gauntlet_progress(user,lang,today)
    day=int(progress['current_day']); stage,stage_name,mode=ll.gauntlet_stage_for_day(day)
    words=[]; context='tartarus'
    if day < ll.GAUNTLET_COMPLETE_DAY:
        try:
            words=ll.get_words_for_gauntlet_stage(user,lang,stage,today=today)
        except ValueError:
            words=[]
    if not words:
        words=ll.maintenance_ready_words(user,lang,today=today)
        if words:
            context='maintenance'; mode='maintenance'; stage_name='Leitner Maintenance'
    if not words:
        if day >= ll.GAUNTLET_COMPLETE_DAY:
            raise ValueError('The Tartarus track is complete and no Leitner maintenance is ready.')
        raise ValueError("Today's Tartarus work is complete and no Leitner maintenance is ready.")

    source_language=lang.split('_',1)[0].lower(); voice=audio_lang or (source_language if source_language in {'english','german'} else lang)
    queue=[{'lang':lang,'word_id':r[0],'word_text':r[1],'definition':r[2],'score':r[3],'leitner_box':r[4]} for r in words]
    sid=uuid.uuid4().hex
    session={
        'user':user,'lang':lang,'voice_lang':voice,'wpm':wpm,'queue':queue,'total':len(queue),
        'practiced':0,'max_questions':min(MAX_QUESTIONS,len(queue)),
        'correct':0,'drilled':0,'incorrect':[],'file_stats':{},'start_time':time.time(),'current':None,
        'gauntlet_mode':mode,'gauntlet_day':day,'gauntlet_stage':stage,'gauntlet_stage_name':stage_name,
        'gauntlet_sessions_done':progress['sessions_done_today'],'is_maintenance':context=='maintenance','is_gauntlet':True,
        'learning_context':context,'stage_drill_required':context=='tartarus' and mode=='shadows','drill_target':2 if mode=='shadows' else DRILL_TARGET,
        'lock':threading.RLock(),'question_sequence':0,'answer_results':{},
    }
    register_session(sid,session)
    meta={'mode':mode,'stage':stage,'stage_name':stage_name,'day':day,'sessions_done_today':progress['sessions_done_today'],'remaining_tasks':ll.get_gauntlet_tasks_remaining(user,lang,day,today),'is_maintenance':context=='maintenance'}
    ll.log_event('GAUNTLET_SESSION_STARTED',user=user,lang=lang,mode=mode,day=day,stage=stage)
    return sid,session,meta



# --- Session lifecycle ---
def next_question(session):
    if not session['queue']:
        return None
    entry=session['queue'].pop(0)
    question=ll.build_question_data(entry['word_id'],entry['word_text'],entry['definition'],entry['score'])
    mode=session.get('gauntlet_mode','forging')
    drill=None
    if session.get('stage_drill_required'):
        drill={'correct_in_a_row':0,'repetition':1,'target':session.get('drill_target',DRILL_TARGET),'show_word':mode!='shadows'}
        question['drill_start']=dict(drill)
    if mode in ('crucible','shadows','depths','void','ascension'):
        question['type']=mode; question['word_unmasked']=entry['word_text']; question['definition']=entry['definition'].split('\n') if isinstance(entry['definition'],str) else entry['definition']
        if mode=='crucible':
            vowels='aeiouAEIOUäöüÄÖÜ'; question['word']=''.join('_' if c in vowels else c for c in entry['word_text'])
        else: question['word']=''
    elif mode=='maintenance':
        question['type']='maintenance'; question['word']=''; question['word_unmasked']=entry['word_text']; question['definition']=entry['definition'].split('\n') if isinstance(entry['definition'],str) else entry['definition']
    question['gauntlet']={'mode':mode,'stage':session.get('gauntlet_stage',0),'stage_name':session.get('gauntlet_stage_name',''),'day':session.get('gauntlet_day',0),'sessions_done':session.get('gauntlet_sessions_done',0)}
    session['current']={
        'lang':entry.get('lang',session['lang']),'word_id':entry['word_id'],'word_text':entry['word_text'],'definition':entry['definition'],
        'prompt_definition':'\n'.join(question.get('definition',[])),'drill_definition':'\n'.join(question.get('definition',[])),
        'score':entry['score'],'leitner_box':entry['leitner_box'],'type':question['type'],'drill':drill,'started_at':time.time(),
    }
    session['question_sequence']+=1; qid=uuid.uuid4().hex; session['current']['question_id']=qid; session['current']['sequence']=session['question_sequence']; question['question_id']=qid; question['sequence']=session['question_sequence']
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
    record_current_time(session); elapsed=int(time.time()-session['start_time'])
    if session['practiced']>0:
        ll.log_session(session['user'],session['lang'],elapsed,session['practiced'],session['correct'],len(session['incorrect']),session['drilled'])
        if session.get('learning_context') == 'tartarus' and not ended_early:
            ll.advance_gauntlet_session(session['user'], session['lang'])
            ll.reconcile_gauntlet_progress(session['user'], session['lang'])
    practiced=session['practiced']
    return {
        'practiced':practiced,'correct':session['correct'],'incorrect':session['incorrect'],'drilled':session['drilled'],
        'elapsed_seconds':elapsed,'ended_early':ended_early,'accuracy':round(100*session['correct']/practiced,1) if practiced else None,
        'avg_seconds_per_item':round(elapsed/practiced,1) if practiced else None,
        'gauntlet':{'mode':session.get('gauntlet_mode'),'stage':session.get('gauntlet_stage'),'stage_name':session.get('gauntlet_stage_name'),'day':session.get('gauntlet_day'),'sessions_done':session.get('gauntlet_sessions_done',0)+(0 if ended_early else 1),'voided':ended_early},
    }




def advance(session, status, message, attempt=None):
    cur=session['current']; session['practiced']+=1
    if status=='correct': session['correct']+=1
    elif status=='drilled': session['drilled']+=1
    record_file_result(session,status)
    result={'result':status,'message':message,'word':cur['word_text']}
    nxt=None if session['practiced']>=session['max_questions'] else next_question(session)
    if nxt is None:
        result['done']=True; result['session']=finalize_session(session)
    else:
        result['done']=False; result['question']=nxt; result['progress']={'correct':session['correct'],'drilled':session['drilled'],'total':session['total'],'questions':session['practiced'],'max_questions':session['max_questions']}
    return result



def process_drill_answer(session, answer):
    cur=session['current']; drill=cur['drill']; target=drill.get('target',DRILL_TARGET)
    correct=ll.answer_matches(answer,cur['word_text'])
    drill['correct_in_a_row']=drill['correct_in_a_row']+1 if correct else 0
    if drill['correct_in_a_row']>=target:
        cur['drill']=None
        if session.get('learning_context')=='maintenance': ll.complete_maintenance_drill(session['user'],session['lang'],cur['word_id'])
        else: ll.complete_tartarus_drill(session['user'],session['lang'],cur['word_id'])
        if session.get('gauntlet_day')==ll.GAUNTLET_MAX_DAY and session.get('learning_context')=='tartarus':
            ll.reconcile_gauntlet_progress(session['user'],session['lang'])
        result=advance(session,'drilled','Drill complete.')
        result['drill']={'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':target,'correct_in_a_row':target,'target':target,'correct':True,'show_word':True}
        return result
    drill['repetition']+=1
    return {'result':'drill_progress','done':False,'drill':{'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':drill['repetition'],'correct_in_a_row':drill['correct_in_a_row'],'target':target,'correct':correct,'show_word':session.get('gauntlet_mode')!='shadows'}}



def process_answer(session, answer, *, timed_out=False):
    """Evaluate learning text; timer expiry is an explicit transport event."""
    cur=session['current']; answer='' if answer is None else str(answer); record_current_time(session)
    if cur['drill'] is not None:
        return process_drill_answer(session, answer)
    correct=False if timed_out else ll.answer_matches(answer,cur['word_text'])
    if session.get('learning_context')=='maintenance':
        ll.record_maintenance_answer(session['user'],session['lang'],cur['word_id'],correct)
    else:
        ll.record_tartarus_answer(session['user'],session['lang'],cur['word_id'],correct)
    if correct:
        if session.get('gauntlet_day')==ll.GAUNTLET_MAX_DAY and session.get('learning_context')=='tartarus': ll.reconcile_gauntlet_progress(session['user'],session['lang'])
        return advance(session,'correct',None,attempt=answer)
    session['incorrect'].append({'word':cur['word_text'],'attempt':answer}); record_file_incorrect(session)
    cur['drill']={'correct_in_a_row':0,'repetition':1,'target':session.get('drill_target',DRILL_TARGET),'show_word':True}
    return {'result':'drill_start','done':False,'message':'Incorrect. Complete the mandatory drill before continuing.','drill':{'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':1,'correct_in_a_row':0,'target':cur['drill']['target'],'correct':False,'show_word':True}}



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
        for path in shared_paths:
            try:
                data = ll.read_word_list(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            lang = path.stem
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


def user_progress_data(user, category=None, level=None, lang=None):
    user_s=ll.sanitize_name(user,'user'); conn=ll.get_connection(); results=[]
    lang_s=ll.sanitize_name(lang,'language') if lang else None
    try:
        for item in list_word_lists():
            if item['user']!=user_s: continue
            if category and item['category']!=category: continue
            if level and item['cefr_level']!=level: continue
            if lang_s and item['lang']!=lang_s: continue
            table=ll.words_table_name(user_s,item['lang'])
            if not ll.table_exists(conn,table):
                total=item['word_count']; tartarus_score9=box10=0
            else:
                total,tartarus_score9,box10=conn.execute(
                    f'SELECT COUNT(*),SUM(CASE WHEN score>=9 THEN 1 ELSE 0 END),SUM(CASE WHEN score>=9 AND leitner_box=10 THEN 1 ELSE 0 END) FROM "{table}" WHERE active=1'
                ).fetchone(); tartarus_score9=tartarus_score9 or 0; box10=box10 or 0
            progress=ll.get_dataset_progress(user_s,item['lang'],conn=conn); tartarus_complete=progress['current_day']>=ll.GAUNTLET_COMPLETE_DAY
            results.append({**item,'total':total or 0,'tartarus_score9':tartarus_score9,'leitner_box10':box10,'tartarus_track_complete':tartarus_complete,'learning_complete':bool(tartarus_complete and total and box10==total)})
        return results
    finally: conn.close()



def leitner_stats_data(user, lang):
    user_s=ll.sanitize_name(user,'user'); lang_s=ll.sanitize_name(lang,'language'); table=ll.words_table_name(user_s,lang_s); conn=ll.get_connection()
    try:
        if not ll.table_exists(conn,table): return None
        distribution={str(i):0 for i in range(1,11)}
        for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND score>=9 AND leitner_box IS NOT NULL GROUP BY leitner_box'):
            distribution[str(box)]=count
        return {'distribution':distribution,'ready':len(ll.maintenance_ready_words(user_s,lang_s)),'box10':distribution['10']}
    finally: conn.close()



def dashboard_data(user, lang=None):
    """Return factual, read-only session and track metrics."""
    user_s=ll.sanitize_name(user,'user'); lang_s=ll.sanitize_name(lang,'language') if lang else None; conn=ll.get_connection()
    try:
        stable=ll.sessions_table_name(user_s); total_seconds=total_practiced=total_correct=total_incorrect=0; dates=[]
        if ll.table_exists(conn,stable):
            where='WHERE language=?' if lang_s else ''; params=(lang_s,) if lang_s else ()
            row=conn.execute(f'SELECT COALESCE(SUM(duration_seconds),0),COALESCE(SUM(words_practiced),0),COALESCE(SUM(correct_count),0),COALESCE(SUM(incorrect_count),0) FROM "{stable}" {where}',params).fetchone(); total_seconds,total_practiced,total_correct,total_incorrect=row
            dates=[r[0] for r in conn.execute(f'SELECT session_date FROM "{stable}" {where}',params)]
        current_streak,best_streak=ll.compute_streak(dates); total_answers=total_correct+total_incorrect
        result={'overview':{'streak':{'current':current_streak,'best':best_streak},'total_seconds':total_seconds,'overall_accuracy':round(100*total_correct/total_answers,1) if total_answers else None},'velocity':{'avg_seconds_per_word':round(total_seconds/total_practiced,1) if total_practiced else None,'sessions':len(dates)},'tracks':None,'nemesis':None,'roadmap':None}
        if lang_s:
            table=ll.words_table_name(user_s,lang_s)
            if ll.table_exists(conn,table):
                total,tmaster,box10=conn.execute(f'SELECT COUNT(*),SUM(CASE WHEN score>=9 THEN 1 ELSE 0 END),SUM(CASE WHEN score>=9 AND leitner_box=10 THEN 1 ELSE 0 END) FROM "{table}" WHERE active=1').fetchone(); tmaster=tmaster or 0; box10=box10 or 0
                gp=ll.get_dataset_progress(user_s,lang_s,conn=conn); tcomplete=gp['current_day']>=ll.GAUNTLET_COMPLETE_DAY
                result['tracks']={'total':total or 0,'tartarus_score9':tmaster,'leitner_box10':box10,'tartarus_track_complete':tcomplete,'learning_complete':bool(tcomplete and total and box10==total)}
                try: material={i['content_id']:i for i in ll.load_practice_items(ll.word_list_path(user_s,lang_s))}
                except Exception: material={}
                result['nemesis']=[{'word':material.get(cid,{}).get('word',cid),'times_incorrect':wrong,'times_correct':right,'score':round(score,1)} for cid,wrong,right,score in conn.execute(f'SELECT content_id,times_incorrect,times_correct,score FROM "{table}" WHERE active=1 AND times_incorrect>0 ORDER BY times_incorrect DESC,score ASC LIMIT 10')]
                distribution={str(i):0 for i in range(1,11)}
                for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND leitner_box IS NOT NULL GROUP BY leitner_box'):
                    distribution[str(box)]=count
                stage,stage_name,_=ll.gauntlet_stage_for_day(gp['current_day'])
                result['roadmap']={'gauntlet':{'current_stage':stage,'current_day':gp['current_day'],'sessions_done_today':gp['sessions_done_today'],'stage_name':stage_name,'remaining_tasks':ll._gauntlet_tasks_remaining(conn,user_s,lang_s,gp['current_day'],date.today().isoformat()),'total_tasks':total or 0,'complete':tcomplete},'leitner_distribution':distribution,'maintenance_ready':len(ll.maintenance_ready_words(user_s,lang_s))}
        return result
    finally: conn.close()



def word_list_stats(user, lang):
    """Return factual per-item progress without synchronizing or mutating state."""
    user_s = ll.sanitize_name(user, 'user')
    lang_s = ll.sanitize_name(lang, 'language')
    table = ll.words_table_name(user_s, lang_s)
    conn = ll.get_connection()
    try:
        if not ll.table_exists(conn, table):
            return None
        material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user_s, lang_s))}
        ready_ids = {row[0] for row in ll.maintenance_ready_words(user_s, lang_s, num_words=10**9)}
        rows = conn.execute(
            f'SELECT id,content_id,score,active,times_practiced,times_correct,times_incorrect,times_drilled,'
            f'last_practiced,last_tartarus_completed,leitner_box,leitner_last_reviewed FROM "{table}" '
            f'ORDER BY active DESC,score DESC,id'
        ).fetchall()
        words = []
        for row_id,cid,score,active,practiced,correct,incorrect,drilled,last,last_tart,box,leitner_last in rows:
            words.append({
                'word': material.get(cid, {}).get('word', cid),
                'score': round(float(score or 0), 1),
                'gauge': gauge_dots(score),
                'band': ll.score_band(score),
                'gauge_band': gauge_color_band(score),
                'active': bool(active),
                'leitner_box': box,
                'next_maintenance': ll.maintenance_next_date(box, leitner_last),
                'maintenance_ready': row_id in ready_ids,
                'times_practiced': practiced,
                'times_correct': correct,
                'times_incorrect': incorrect,
                'times_drilled': drilled,
                'last_practiced': last,
                'last_tartarus_completed': last_tart,
                'leitner_last_reviewed': leitner_last,
            })
        return words
    finally:
        conn.close()



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
        record['word'] = str(item.get('word', record.get('word', '')))
        if not record['word'].strip():
            raise ValueError(f'Invalid editor row {position}: missing word.')
        record['definition'] = _editor_definition(item, original)
        record.setdefault('word_frequency', 0)
        ids.add(content_id)
        saved.append(record)
    saved = ll.validate_word_list_items(saved, target_path)
    metadata = ll.canonical_material_metadata(source['metadata'], name=lang)
    ll.write_word_list_atomic(target_path, {'metadata': metadata, 'items': saved})
    ll.sync_word_list(user, lang)
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
            lang = qs.get('lang', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json({'lists': user_progress_data(user, category, level, lang)})

        if parsed.path == '/api/wordlist':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                return self._send_json(load_word_list(user, lang))
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

        if parsed.path == '/api/gauntlet/progress':
            qs=urllib.parse.parse_qs(parsed.query); user=qs.get('user',[''])[0]; lang=qs.get('lang',[''])[0]
            if not user or not lang: return self._send_json({'error': "'user' and 'lang' are required"},400)
            try:
                user=ll.sanitize_name(user,'user'); lang=ll.sanitize_name(lang,'language'); conn=ll.get_connection()
                try:
                    progress=ll.get_dataset_progress(user,lang,conn=conn); stage,stage_name,mode=ll.gauntlet_stage_for_day(progress['current_day']); table=ll.words_table_name(user,lang)
                    distribution={str(i):0 for i in range(1,11)}; total=remaining=0
                    if ll.table_exists(conn,table):
                        total=conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE active=1').fetchone()[0]
                        remaining=ll._gauntlet_tasks_remaining(conn,user,lang,progress['current_day'],date.today().isoformat())
                        for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND leitner_box IS NOT NULL GROUP BY leitner_box'): distribution[str(box)]=count
                    complete=progress['current_day']>=ll.GAUNTLET_COMPLETE_DAY
                    today=date.today().isoformat()
                    locked_today=(not complete and remaining==0 and str(progress.get('last_practice_date') or '')[:10]==today)
                    payload={'progress':{**progress,'current_stage':stage,'stage_name':stage_name,'session_mode':mode,'remaining_tasks':remaining,'total_tasks':total,'max_day':ll.GAUNTLET_MAX_DAY,'complete':complete,'locked_today':locked_today},'roadmap':{'gauntlet':{'current_stage':stage,'current_day':progress['current_day'],'sessions_done_today':progress['sessions_done_today'],'stage_name':stage_name,'remaining_tasks':remaining,'total_tasks':total,'complete':complete,'locked_today':locked_today},'leitner_distribution':distribution,'maintenance_ready':len(ll.maintenance_ready_words(user,lang))}}
                    return self._send_json(payload)
                finally: conn.close()
            except (ValueError,FileNotFoundError) as e: return self._send_json({'error':str(e)},400)

        return self._send_json({'error': 'not found'}, 404)


    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        ll.log_event("HTTP_POST", path=parsed.path)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({'error': 'invalid JSON body'}, 400)
        except ValueError as error:
            return self._send_json({'error': str(error)}, 400)

        if parsed.path == '/api/tts':
            text = str(payload.get('text', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            wpm = payload.get('wpm', 128)
            try:
                wpm = int(wpm)
            except (TypeError, ValueError):
                wpm = 128
            # Deterministic non-macOS test double: emulate blocking `say`
            # without changing production behavior.
            try:
                test_delay_ms = max(0, int(os.environ.get('TARTARUS_TTS_TEST_DELAY_MS', '0')))
            except (TypeError, ValueError):
                test_delay_ms = 0
            if test_delay_ms:
                time.sleep(test_delay_ms / 1000.0)
                return self._send_json({'supported': True, 'spoken': bool(text), 'simulated': True})
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
            allowed = {'user', 'lang', 'audio_lang', 'wpm'}
            unknown = set(payload) - allowed
            if unknown:
                return self._send_json({'error': f"unsupported practice option(s): {', '.join(sorted(unknown))}"}, 400)
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            try:
                wpm = int(payload.get('wpm', 128))
            except (TypeError, ValueError):
                wpm = 128
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)

            try:
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
                session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            with session['lock']:
                if (session.get('current') or {}).get('drill') is not None:
                    return self._send_json({'error': 'Complete the mandatory drill before ending the session.'}, 409)
                with SESSIONS_LOCK:
                    SESSIONS.pop(session_id, None)
                summary = finalize_session(session, ended_early=True)
            return self._send_json({'cancelled': True, 'session': summary})

        if parsed.path in {'/api/practice/answer', '/api/practice/timeout'}:
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
                    result = process_answer(
                        session,
                        payload.get('answer', ''),
                        timed_out=(parsed.path == '/api/practice/timeout'),
                    )
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

def _loopback_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower()=='localhost'


def main():
    ll.configure_logging()
    if not _loopback_host(HOST):
        raise ValueError('Tartarus release mode is localhost-only; use 127.0.0.1, ::1, or localhost.')
    ll.initialize_database()
    try: httpd=TartarusHTTPServer((HOST,PORT),Handler)
    except OSError as e:
        if e.errno==errno.EADDRINUSE:
            print(f'Error: port {PORT} is already in use.'); sys.exit(1)
        raise
    print(f'Tartarus web server: http://{HOST}:{PORT}/')
    try: httpd.serve_forever()
    except KeyboardInterrupt: print('\nShutting down...')
    finally: httpd.server_close()



if __name__ == '__main__':
    main()
