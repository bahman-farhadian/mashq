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

# ── TEST 2: The Forging (Day 0) - Complete 4 Sessions ──
log("\n[2] The Forging (Day 0) - Complete 4 Sessions")
# To speed up, we'll master words using the '@' cheat which forces mastery and bypasses the rest of the question queue,
# but we just want to end the session successfully. Actually, ending early with '!!' means the session is voided.
# We must complete a session legitimately to increment sessions_done_today.
# But completing 16 questions normally takes a lot of API calls.
# Let's adjust DB directly to master words, or just answer them correctly.
# Wait, if we '!!' (end early), tartarus_web.py says: if not ended_early and practiced > 0: advance.
# So we MUST answer all 16 questions?
# The fastest way is to answer '@' for each.

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

# Complete sessions 2, 3, 4
complete_full_session()
complete_full_session()
s4 = complete_full_session()
prog3 = api(f'/api/gauntlet/progress?user={USER}&lang={LANG}')
check("sessions_done_today = 4", prog3['progress']['sessions_done_today'] == 4)
check("Day remains 0 until tomorrow", prog3['progress']['current_day'] == 0)
check("List is locked for today", prog3['progress']['locked_today'] == True)

# Try to start 5th session
res_locked = api('/api/practice/start', {'user': USER, 'lang': LANG})
check("5th session blocked (sleep lockout)", 'error' in res_locked and 'quota for this list is complete' in res_locked['error'])

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
check("Crucible mode forces word hidden", q_day1['word'] == '' and q_day1['type'] == 'fast')
check("Crucible mode sends word_unmasked", q_day1.get('word_unmasked') != '')
check("Crucible mode hides definition", q_day1['definition'] == [])

# Abandon this session
api('/api/practice/answer', {'session_id': res_day1['session_id'], 'word_id': q_day1['word_id'], 'answer': '!!'})

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
