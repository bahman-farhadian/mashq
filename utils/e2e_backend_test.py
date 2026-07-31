#!/usr/bin/env python3
"""
Tartarus Backend E2E Test (Dual-Track Gauntlet)
Tests the strict 10-day gauntlet lifecycle, inescapable drills, and time-locks.
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
from datetime import date

LOG_FILE = "/tmp/tartarus_backend_e2e.log"
BASE = "http://127.0.0.1:9999"
USER = "bahman_test"
LANG = "german_vocabulary_a1"
TEST_DB = "/tmp/tartarus_test.db"

PASS_COUNT = 0
FAIL_COUNT = 0

# Ensure clean DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

print(f"Starting test server with DB: {TEST_DB}...")
env = os.environ.copy()
env["TARTARUS_DB"] = TEST_DB
server_log = open("/tmp/tartarus_server.log", "w")
server_proc = subprocess.Popen([sys.executable, "utils/tartarus_web.py"], env=env, stdout=server_log, stderr=subprocess.STDOUT)
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
    conn = sqlite3.connect(TEST_DB)
    c = conn.cursor()
    c.execute(query)
    rows = c.fetchall()
    conn.commit()
    conn.close()
    return rows

with open(LOG_FILE, 'w') as f:
    f.write("")

log("=" * 65)
log("  Tartarus Gauntlet E2E Test")
log(f"  Server: {BASE}  |  User: {USER}  |  Lang: {LANG}")
log("=" * 65)

# Init API
init_data = api('/api/init', {'user': USER, 'lang': LANG})
check("Init API responds", 'created' in init_data)

# ── TEST 1: The Forging (Day 0) - Aborted Session (Rage Quit) ──
log("\n[1] The Forging (Day 0) - Rage Quit Test")
res_rq = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("Session starts in Gauntlet mode", 'gauntlet' in res_rq)
check("Day is 0 (The Forging)", res_rq['gauntlet']['day'] == 0 and res_rq['gauntlet']['mode'] == 'forging', f"gauntlet={res_rq['gauntlet']}")
sid_rq = res_rq['session_id']
q_rq = res_rq['question']

# Answer incorrectly
res_wrong = api('/api/practice/answer', {'session_id': sid_rq, 'word_id': q_rq['word_id'], 'answer': 'WRONG'})
if res_wrong.get('result') != 'drill_start':
    log(f"DEBUG res_wrong: {res_wrong}")
check("Mistake triggers inescapable drill_start", res_wrong.get('result') == 'drill_start')

# ABORT session now (do not finish drill, do not send !!)
# Verify DB state: sessions_done_today should remain 0
prog1 = api(f'/api/gauntlet/progress?user={USER}&lang={LANG}')
check("Progress not saved after rage quit", prog1['progress']['sessions_done_today'] == 0)

# Verify drill debt is not saved in DB (no drill_pending column anymore)
try:
    cols = db_query(f"PRAGMA table_info('words_{USER}_{LANG}')")
    drill_pending_exists = any(c[1] == 'drill_pending' for c in cols)
    check("drill_pending column is removed from schema", not drill_pending_exists)
except Exception as e:
    check("Schema check", False, str(e))

# ── TEST 2: The Forging (Day 0) - Endless Practice until Mastered ──
log("\n[2] The Forging (Day 0) - Endless Practice until Mastered")
# In the new design, there is no 4-session limit.
# The user can practice endlessly until all words reach score >= 9.0.

def complete_full_session():
    res = api('/api/practice/start', {'user': USER, 'lang': LANG})
    if 'error' in res:
        return res
    sid = res['session_id']
    q = res.get('question')
    ans = res
    while q:
        ans = api('/api/practice/answer', {'session_id': sid, 'word_id': q['word_id'], 'answer': q.get('word_unmasked', q.get('word'))})
        q = ans.get('question')
    return ans

# Complete session 1
s1 = complete_full_session()
check("Session 1 completed", s1.get('done') == True)
prog2 = api(f'/api/gauntlet/progress?user={USER}&lang={LANG}')
check("sessions_done_today = 1", prog2['progress']['sessions_done_today'] == 1)

# Ensure they are NOT locked out yet (since words are still unmastered)
check("List is NOT locked yet", prog2['progress']['locked_today'] == False)

# Now, cheat: master all words in DB to simulate completing the Day 0 task
db_query(f"UPDATE words_{USER}_{LANG} SET score = 9.0 WHERE active = 1")

prog3 = api(f'/api/gauntlet/progress?user={USER}&lang={LANG}')
check("Remaining tasks = 0", prog3['progress']['remaining_tasks'] == 0)
check("List is locked for today", prog3['progress']['locked_today'] == True)
check("Day remains 0 until tomorrow", prog3['progress']['current_day'] == 0)

# Try to start another session
res_locked = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("Next session blocked (task complete / sleep lockout)", 'error' in res_locked)

# ── TEST 3: Time-Travel to Day 1 (The Crucible) ──
log("\n[3] Time-Travel to Day 1 (The Crucible)")
# Alter DB to simulate sleep
db_query(f"UPDATE dataset_progress SET last_practice_date = '2020-01-01' WHERE user = '{USER}' AND lang = '{LANG}'")


prog4 = api(f'/api/gauntlet/progress?user={USER}&lang={LANG}')
# Notice: the API dynamically evaluates current_day on start/progress check!
check("After sleep, current_day advanced to 1", prog4['progress']['current_day'] == 1)
check("Mode is now 'crucible'", prog4['progress']['session_mode'] == 'crucible')

res_day1 = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("Day 1 session starts successfully", 'session_id' in res_day1)
q_day1 = res_day1['question']
check("Crucible mode masks word vowels", '_' in q_day1['word'] and q_day1['type'] == 'crucible')
check("Crucible mode sends word_unmasked", q_day1.get('word_unmasked') != '')
check("Crucible mode keeps definition visible", len(q_day1['definition']) > 0)

# Abandon this session
api('/api/practice/answer', {'session_id': res_day1['session_id'], 'word_id': q_day1['word_id'], 'answer': '!!'})

# ── TEST 4: Roadmap API in Report Endpoint ──
log("\n[4] Roadmap API in Report Endpoint")
res_report = api(f'/api/report?user={USER}&lang={LANG}')
check("Report endpoint succeeds", 'reports' in res_report)
check("Report endpoint includes roadmap", 'roadmap' in res_report)
if 'roadmap' in res_report:
    check("Roadmap has gauntlet data", 'gauntlet' in res_report['roadmap'])
    check("Roadmap has leitner distribution", 'leitner_distribution' in res_report['roadmap'])

# ── TEST 5: Leitner Sisyphus Loop Penalty (Box retention on failure) ──
log("\n[5] Leitner Sisyphus Loop (Retain Box on Failure)")
# Update all words to be mastered and due (Box 10)
db_query(f"UPDATE words_{USER}_{LANG} SET score = 9.0, leitner_box = 10, active = 1, times_practiced = 10, last_practiced = '2020-01-01'")
res_leitner = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("Leitner session starts", 'session_id' in res_leitner)
q_leitner = res_leitner['question']
# Answer incorrectly
api('/api/practice/answer', {'session_id': res_leitner['session_id'], 'word_id': q_leitner['word_id'], 'answer': 'WRONG_INTENTIONAL'})
# Verify word retains its box and score
row = db_query(f"SELECT score, leitner_box FROM words_{USER}_{LANG} WHERE id = {q_leitner['word_id']}")[0]
check("Score retained on failure (9.0)", row[0] == 9.0)
check("Leitner Box retained on failure (10)", row[1] == 10)

# Clear Leitner queue so it doesn't override Gauntlet checks
db_query(f"UPDATE words_{USER}_{LANG} SET leitner_box = NULL")

# ── TEST 6: Time-Travel to Day 3 (The Shadows) ──
log("\n[6] Time-Travel to Day 3 (The Shadows)")
db_query(f"UPDATE dataset_progress SET current_day = 3, current_stage = 2, last_practice_date = '2020-01-01' WHERE user = '{USER}' AND lang = '{LANG}'")
res_day3 = api('/api/practice/start', {'user': USER, 'lang': LANG})
q_day3 = res_day3['question']
log(f"DEBUG q_day3: word='{q_day3.get('word')}', type='{q_day3.get('type')}'")
check("Shadows mode completely hides word", q_day3['word'] == '' and q_day3['type'] == 'shadows')
api('/api/practice/answer', {'session_id': res_day3['session_id'], 'word_id': q_day3['word_id'], 'answer': '!!'})

# ── TEST 7: Time-Travel to Day 5 (The Depths) ──
log("\n[7] Time-Travel to Day 5 (The Depths)")
db_query(f"UPDATE dataset_progress SET current_day = 5, current_stage = 3, last_practice_date = '2020-01-01' WHERE user = '{USER}' AND lang = '{LANG}'")
res_day5 = api('/api/practice/start', {'user': USER, 'lang': LANG})
q_day5 = res_day5['question']
log(f"DEBUG q_day5: word='{q_day5.get('word')}', type='{q_day5.get('type')}'")
check("Depths mode completely hides word", q_day5['word'] == '' and q_day5['type'] == 'depths')
api('/api/practice/answer', {'session_id': res_day5['session_id'], 'word_id': q_day5['word_id'], 'answer': '!!'})

# ── TEST 8: Time-Travel to Day 7 (The Void) ──
log("\n[8] Time-Travel to Day 7 (The Void)")
db_query(f"UPDATE dataset_progress SET current_day = 7, current_stage = 4, last_practice_date = '2020-01-01' WHERE user = '{USER}' AND lang = '{LANG}'")
res_day7 = api('/api/practice/start', {'user': USER, 'lang': LANG})
q_day7 = res_day7['question']
log(f"DEBUG q_day7: word='{q_day7.get('word')}', type='{q_day7.get('type')}'")
check("Void mode completely hides word", q_day7['word'] == '' and q_day7['type'] == 'void')
api('/api/practice/answer', {'session_id': res_day7['session_id'], 'word_id': q_day7['word_id'], 'answer': '!!'})

# ── TEST 9: Time-Travel to Day 9 (Ascension) ──
log("\n[9] Time-Travel to Day 9 (Ascension)")
db_query(f"UPDATE dataset_progress SET current_day = 9, current_stage = 5, last_practice_date = '2020-01-01' WHERE user = '{USER}' AND lang = '{LANG}'")
res_day9 = api('/api/practice/start', {'user': USER, 'lang': LANG})
q_day9 = res_day9['question']
log(f"DEBUG q_day9: word='{q_day9.get('word')}', type='{q_day9.get('type')}'")
check("Ascension mode completely hides word", q_day9['word'] == '' and q_day9['type'] == 'ascension')
api('/api/practice/answer', {'session_id': res_day9['session_id'], 'word_id': q_day9['word_id'], 'answer': '!!'})

# ── TEST 10: Leitner Graduation ──
log("\n[10] Leitner Graduation (Box 1 -> Box 2)")
# Complete the Gauntlet (lock it) so Leitner takes over
db_query(f"UPDATE dataset_progress SET current_day = 10, current_stage = 5, last_practice_date = '{date.today().isoformat()}' WHERE user = '{USER}' AND lang = '{LANG}'")
# Set a word to Box 1, due today
db_query(f"UPDATE words_{USER}_{LANG} SET score = 9.0, leitner_box = 1, active = 1, times_practiced = 10, last_practiced = '2020-01-01'")
res_leitner_grad = api('/api/practice/start', {'user': USER, 'lang': LANG})
q_leitner_grad = res_leitner_grad['question']
check("Leitner maintenance session started", q_leitner_grad['type'] == 'maintenance')
check("Leitner maintenance hides target word", q_leitner_grad['word'] == '')
check("Leitner maintenance keeps definition visible", len(q_leitner_grad['definition']) > 0)
# Answer correctly
ans = q_leitner_grad.get('word_unmasked')
res_ans = api('/api/practice/answer', {'session_id': res_leitner_grad['session_id'], 'word_id': q_leitner_grad['word_id'], 'answer': ans})
log(f"DEBUG res_ans: {res_ans}")
# Check if box graduated
row2 = db_query(f"SELECT leitner_box FROM words_{USER}_{LANG} WHERE id = {q_leitner_grad['word_id']}")[0]
log(f"DEBUG row2: {row2}")
check("Leitner Box graduated (1 -> 2)", row2[0] == 2)

# ── SUMMARY ──
log("")
log("=" * 65)
log(f"  BACKEND E2E TEST RESULTS")
log(f"  PASSED: {PASS_COUNT}")
log(f"  FAILED: {FAIL_COUNT}")
log(f"  TOTAL:  {PASS_COUNT + FAIL_COUNT}")
log("=================================================================")

server_proc.terminate()
server_proc.wait()

sys.exit(0 if FAIL_COUNT == 0 else 1)
