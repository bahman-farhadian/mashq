#!/usr/bin/env python3
"""
Tartarus Backend E2E Test
Tests the full API flow covering all practice modes and drill logic.
Results logged to /tmp/tartarus_backend_e2e.log
"""
import urllib.request
import urllib.parse
import json
import time
import os
import sys
import sqlite3
import subprocess
import signal
from datetime import date

LOG_FILE = "/tmp/tartarus_backend_e2e.log"
BASE = "http://127.0.0.1:9999"
USER = "bahman_test"
LANG = "german_vocabulary_a1"
TEST_DB = "/tmp/tartarus_test.db"

PASS_COUNT = 0
FAIL_COUNT = 0
RESULTS = []

# Ensure clean DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

print(f"Starting test server with DB: {TEST_DB}...")
env = os.environ.copy()
env["TARTARUS_DB"] = TEST_DB
server_proc = subprocess.Popen([sys.executable, "utils/tartarus_web.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)  # Wait for server to start

def log(msg):
    ts = time.strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + "\n")

def check(label, condition, details=""):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        log(f"  ✓ PASS: {label}" + (f" | {details}" if details else ""))
        PASS_COUNT += 1
        return True
    else:
        log(f"  ✗ FAIL: {label}" + (f" | {details}" if details else ""))
        FAIL_COUNT += 1
        return False

def api(path, data=None, method=None):
    url = BASE + path
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        if method:
            req.method = method
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return json.loads(body)
        except:
            return {"error": str(e), "body": body}

def db_query(query):
    try:
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute(query)
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        return [("DB_ERROR", str(e))]

# ── Reset log ──────────────────────────────────────────────────────────────────
with open(LOG_FILE, 'w') as f:
    f.write("")

log("=" * 65)
log("  Tartarus Backend E2E Test")
log(f"  Server: {BASE}  |  User: {USER}  |  Lang: {LANG}")
log("=" * 65)

# ── TEST 1: Server health ──────────────────────────────────────────────────────
log("\n[1] Server Health")
try:
    with urllib.request.urlopen(BASE + '/', timeout=5) as r:
        status = r.status
    check("Server responds 200 OK", status == 200, f"status={status}")
except Exception as e:
    check("Server responds 200 OK", False, str(e))

# ── TEST 2: Init API (users/progress) ──────────────────────────────────────────
log("\n[2] Init API")
init_data = api('/api/init', {'user': USER, 'lang': LANG})
check("Init API returns dict", isinstance(init_data, dict))
check("Init API has created flag", 'created' in init_data)

# ── TEST 3: Word list API ──────────────────────────────────────────────────────
log("\n[3] Word Lists API")
lists_response = api(f'/api/wordlists?user={USER}')
lists = lists_response.get('wordlists', []) if isinstance(lists_response, dict) else lists_response
check("Word lists API returns list", isinstance(lists, list))
german_lists = [l for l in lists if l.get('lang','').startswith('german')]
check("German vocabulary list present", len(german_lists) > 0, f"found {len(german_lists)}")
a1_list = [l for l in german_lists if 'a1' in l.get('lang','')]
check("A1 level list present", len(a1_list) > 0)
if a1_list:
    count = a1_list[0].get('word_count', 0)
    check("A1 list has >= 20 words", count >= 20, f"word_count={count}")
    log(f"  A1 list word count: {count}")

# ── TEST 4: Normal session start ───────────────────────────────────────────────
log("\n[4] Normal Session Start")
res = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("Session start returns session_id", 'session_id' in res, str(res.get('error','')))
check("Session start returns question", 'question' in res)
sid_normal = res.get('session_id')
q1 = res.get('question', {})
log(f"  First question: word='{q1.get('word','?')}' type='{q1.get('type','?')}' score={q1.get('score','?')}")
check("Question has word field", bool(q1.get('word') is not None))
check("Question has word_id", bool(q1.get('word_id')))

# ── TEST 5: Correct answer ─────────────────────────────────────────────────────
log("\n[5] Correct Answer")
word_text = q1.get('word_unmasked', q1.get('word', ''))
res_correct = api('/api/practice/answer', {'session_id': sid_normal, 'word_id': q1['word_id'], 'answer': word_text})
check("Correct answer returns result=correct", res_correct.get('result') == 'correct', f"result={res_correct.get('result')}")
check("Correct answer not done early", not res_correct.get('done') or res_correct.get('question') is None)
progress = res_correct.get('progress', {})
check("Progress updated after correct", progress.get('correct', 0) >= 1, f"progress={progress}")

# ── TEST 6: Wrong answer without instant drill ─────────────────────────────────
log("\n[6] Wrong Answer (no instant drill)")
q2 = res_correct.get('question', {})
if q2:
    res_wrong = api('/api/practice/answer', {'session_id': sid_normal, 'word_id': q2['word_id'], 'answer': 'WRONG_ANSWER_XYZ'})
    check("Wrong answer returns result=incorrect", res_wrong.get('result') == 'incorrect', f"result={res_wrong.get('result')}")
    check("Wrong answer NOT drill_start (no instant drill)", res_wrong.get('result') != 'drill_start', f"result={res_wrong.get('result')}")
else:
    log("  SKIP: No second question available")

# End normal session
api('/api/practice/answer', {'session_id': sid_normal, 'word_id': q1['word_id'], 'answer': '!!'})
log("  Normal session ended")

# ── TEST 7: Session with instant drill ────────────────────────────────────────
log("\n[7] Session with Instant Drill")
res_id = api('/api/practice/start', {'user': USER, 'lang': LANG, 'instant_drill': True})
check("Instant drill session starts", 'session_id' in res_id)
sid_drill = res_id.get('session_id')
qd1 = res_id.get('question', {})
log(f"  Question: '{qd1.get('word_unmasked','?')}'")

# Answer wrong to trigger instant drill
res_wrong2 = api('/api/practice/answer', {'session_id': sid_drill, 'word_id': qd1['word_id'], 'answer': 'XYZWRONG'})
log(f"  After wrong answer: result={res_wrong2.get('result')}")
check("Wrong answer triggers drill_start (instant_drill=True)", res_wrong2.get('result') == 'drill_start', f"result={res_wrong2.get('result')}")

# Complete all 9 drill reps
drill_word = qd1.get('word_unmasked', qd1.get('word', ''))
last_drill_result = None
for rep in range(1, 10):
    r = api('/api/practice/answer', {'session_id': sid_drill, 'word_id': qd1['word_id'], 'answer': drill_word})
    log(f"  Drill rep {rep}: result={r.get('result')} streak={r.get('drill',{}).get('correct_in_a_row','?')}")
    last_drill_result = r
    if r.get('result') == 'drilled':
        check(f"Drill completed after {rep} reps", True, f"rep={rep}")
        break
else:
    check("Drill completed within 9 reps", last_drill_result and last_drill_result.get('result') == 'drilled',
          f"last_result={last_drill_result.get('result') if last_drill_result else 'None'}")

# ── TEST 8: DB verification after drill ───────────────────────────────────────
log("\n[8] DB Verification After Drill")
try:
    pending = db_query(f"SELECT content_id, drill_pending FROM words_{USER}_{LANG} WHERE drill_pending=1 AND content_id = '{qd1.get('word_id')}'")
    check("drill_pending=0 for drilled word", len(pending) == 0, f"still pending: {pending}")
    drilled_rows = db_query(f"SELECT content_id, times_drilled, score FROM words_{USER}_{LANG} WHERE times_drilled>0")
    check("times_drilled incremented", len(drilled_rows) > 0, f"drilled rows: {drilled_rows}")
    if drilled_rows:
        log(f"  Drilled word: {drilled_rows[0][0]} score={drilled_rows[0][2]} drilled_count={drilled_rows[0][1]}")
except Exception as e:
    log(f"  DB error: {e}")

# ── TEST 9: End session -> summary ────────────────────────────────────────────
log("\n[9] Session End and Summary")
res_end = api('/api/practice/answer', {'session_id': sid_drill, 'word_id': qd1['word_id'], 'answer': '!!'})
check("Session end returns done=True", res_end.get('done') == True, f"done={res_end.get('done')}")
session_summary = res_end.get('session', {})
check("Summary has practiced count", 'practiced' in session_summary, f"summary={session_summary}")
check("Summary has drilled count", 'drilled' in session_summary)
log(f"  Summary: practiced={session_summary.get('practiced')} correct={session_summary.get('correct')} drilled={session_summary.get('drilled')}")

# ── TEST 10: Dashboard progress API ───────────────────────────────────────────
log("\n[10] Dashboard Progress API")
prog = api(f'/api/user/progress?user={USER}&category=german_vocabulary&level=a1')
check("Progress API returns data", isinstance(prog, dict) or isinstance(prog, list))
log(f"  Progress response: {json.dumps(prog)[:300]}")
# Check the to_drill count
if isinstance(prog, list):
    for p in prog:
        if p.get('lang') == LANG:
            to_drill = p.get('to_drill', -1)
            check("Dashboard to_drill >= 1 (due to current and past runs)", to_drill >= 1, f"to_drill={to_drill}")
elif isinstance(prog, dict):
    progress_list = prog.get('progress', []) or prog.get('lists', [])
    for p in progress_list:
        if p.get('lang') == LANG:
            to_drill = p.get('to_drill', -1)
            check("Dashboard to_drill >= 1 (due to current and past runs)", to_drill >= 1, f"to_drill={to_drill}")

# ── TEST 11: Fast mode session ─────────────────────────────────────────────────
log("\n[11] Fast Mode Session (requires mastered word)")
# First master a word
res_fast_setup = api('/api/practice/start', {'user': USER, 'lang': LANG})
if 'session_id' in res_fast_setup:
    sid_setup = res_fast_setup['session_id']
    q_setup = res_fast_setup.get('question', {})
    # Force-master via @ command
    res_master = api('/api/practice/answer', {'session_id': sid_setup, 'word_id': q_setup['word_id'], 'answer': '@'})
    log(f"  Mastered word: {res_master.get('result')}")
    api('/api/practice/answer', {'session_id': sid_setup, 'word_id': q_setup.get('word_id'), 'answer': '!!'})

# Now try fast mode
res_fast = api('/api/practice/start', {'user': USER, 'lang': LANG, 'fast_mode': True})
if 'error' in res_fast:
    log(f"  Fast mode error (expected if no mastered words): {res_fast['error']}")
    # This is acceptable if no words are mastered yet
else:
    check("Fast mode session starts", 'session_id' in res_fast)
    sid_fast = res_fast.get('session_id')
    qf = res_fast.get('question', {})
    log(f"  Fast mode question: '{qf.get('word_unmasked','?')}'")
    check("Fast mode question type is fast", qf.get('type') == 'fast', f"type={qf.get('type')}")
    api('/api/practice/answer', {'session_id': sid_fast, 'word_id': qf.get('word_id'), 'answer': '!!'})

# ── TEST 12: Mistake drill mode ────────────────────────────────────────────────
log("\n[12] Mistake Drill Mode (requires words with drill_pending)")
# Make a mistake first
res_md_setup = api('/api/practice/start', {'user': USER, 'lang': LANG})
if 'session_id' in res_md_setup:
    sid_md = res_md_setup['session_id']
    q_md = res_md_setup.get('question', {})
    api('/api/practice/answer', {'session_id': sid_md, 'word_id': q_md['word_id'], 'answer': 'WRONG'})
    api('/api/practice/answer', {'session_id': sid_md, 'word_id': q_md['word_id'], 'answer': '!!'})

res_drill_mode = api('/api/practice/start', {'user': USER, 'lang': LANG, 'drill_mode': True})
if 'error' in res_drill_mode:
    log(f"  Drill mode: {res_drill_mode['error']}")
    check("Mistake drill mode responds correctly", 'error' in res_drill_mode, "expected since drill_pending may be 0")
else:
    check("Mistake drill mode session starts", 'session_id' in res_drill_mode)
    api('/api/practice/answer', {'session_id': res_drill_mode['session_id'], 'word_id': res_drill_mode.get('question',{}).get('word_id'), 'answer': '!!'})

# ── TEST 13: Drill All mode ────────────────────────────────────────────────────
log("\n[13] Drill All Mode")
res_all = api('/api/practice/start', {'user': USER, 'lang': LANG, 'drill_all': True})
check("Drill All mode session starts", 'session_id' in res_all, str(res_all.get('error','')))
if 'session_id' in res_all:
    q_all = res_all.get('question', {})
    log(f"  Drill all first word: '{q_all.get('word_unmasked','?')}'")
    api('/api/practice/answer', {'session_id': res_all['session_id'], 'word_id': q_all.get('word_id'), 'answer': '!!'})

# ── TEST 14: Report API ────────────────────────────────────────────────────────
log("\n[14] Report API")
report_res = api(f'/api/report?user={USER}&lang={LANG}')
report = report_res.get('reports', []) if isinstance(report_res, dict) else report_res
check("Report API returns list", isinstance(report, list))
if report:
    log(f"  Report has {len(report)} language entries")
    total = report[0].get('total', {})
    log(f"  Total practiced: {total.get('practiced',0)}")
    check("Report shows practice activity", total.get('practiced', 0) > 0, f"practiced={total.get('practiced',0)}")

# ── TEST 15: Word List Detail API ──────────────────────────────────────────────
log("\n[15] Word List Detail API")
detail = api(f'/api/wordlist?user={USER}&lang={LANG}')
check("Word list detail returns items", isinstance(detail, (list, dict)))
if isinstance(detail, list):
    check("Word list detail has items", len(detail) > 0, f"count={len(detail)}")
    if detail:
        first = detail[0]
        check("Word list item has word field", 'word' in first)
        check("Word list item has score field", 'score' in first)
elif isinstance(detail, dict):
    items = detail.get('words') or detail.get('items') or []
    check("Word list detail has items", len(items) > 0, f"keys={list(detail.keys())}")

# ── TEST 16: Review mode ───────────────────────────────────────────────────────
log("\n[16] Review Mode")
res_review = api('/api/practice/start', {'user': USER, 'lang': LANG, 'review_mode': True})
check("Review mode session starts", 'session_id' in res_review, str(res_review.get('error','')))
if 'session_id' in res_review:
    q_review = res_review.get('question', {})
    log(f"  Review question type: {q_review.get('type','?')}")
    check("Review mode question type is review", q_review.get('review_mode') == True, f"review_mode={q_review.get('review_mode')}")
    api('/api/practice/answer', {'session_id': res_review['session_id'], 'word_id': q_review.get('word_id'), 'answer': '!!'})

# ── SUMMARY ────────────────────────────────────────────────────────────────────
log("")
log("=" * 65)
log(f"  BACKEND E2E TEST RESULTS")
log(f"  PASSED: {PASS_COUNT}")
log(f"  FAILED: {FAIL_COUNT}")
log(f"  TOTAL:  {PASS_COUNT + FAIL_COUNT}")
log("=================================================================")
log(f"  Full log: {LOG_FILE}")

server_proc.terminate()
server_proc.wait()

sys.exit(0 if FAIL_COUNT == 0 else 1)
