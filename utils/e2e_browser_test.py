#!/usr/bin/env python3
"""Deterministic browser release tests for Tartarus.

Default execution targets Safari through WebDriver at TARTARUS_WEBDRIVER_URL.
For local/CI characterization on non-macOS hosts, set TARTARUS_BROWSER=chromium;
the same assertions run against a temporary headless Chromium instance via CDP.

All state lives under a TemporaryDirectory and all stage timers/TTS requests are
instrumented in-page, so the suite never waits 5/7/10 real seconds and never
uses production data.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER_URL = os.environ.get('TARTARUS_WEBDRIVER_URL', 'http://127.0.0.1:4444')
BROWSER_MODE = os.environ.get('TARTARUS_BROWSER', 'safari').lower()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def http_json(base, path, method='GET', data=None, timeout=20):
    body = None if data is None else json.dumps(data).encode('utf-8')
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    req = urllib.request.Request(base + path, body, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'{}')


class SafariWebDriver:
    def __init__(self):
        self.base = DRIVER_URL.rstrip('/')
        payload = {'capabilities': {'alwaysMatch': {'browserName': 'safari'}}}
        result = self.call('POST', '/session', payload)
        self.session_id = result['value']['sessionId']

    def close(self):
        if getattr(self, 'session_id', None):
            try:
                self.call('DELETE', f'/session/{self.session_id}')
            finally:
                self.session_id = None

    def call(self, method, path, payload=None):
        parsed = DRIVER_URL.removeprefix('http://').removeprefix('https://')
        host, port = parsed.rsplit(':', 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=30)
        body = None if payload is None else json.dumps(payload)
        connection.request(method, path, body, {'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read().decode()
        connection.close()
        if response.status >= 400:
            raise AssertionError(f'WebDriver {method} {path}: {response.status} {raw}')
        return json.loads(raw) if raw else {'value': None}

    def open(self, url):
        self.call('POST', f'/session/{self.session_id}/url', {'url': url})

    def script(self, script, *args):
        return self.call(
            'POST', f'/session/{self.session_id}/execute/sync',
            {'script': script, 'args': list(args)},
        )['value']

    def viewport(self, width, height):
        self.call('POST', f'/session/{self.session_id}/window/rect', {'width': width, 'height': height})


class ChromiumCDP:
    """Minimal CDP adapter used only to exercise the Safari test contract locally."""
    def __init__(self):
        import websocket  # optional dependency; Safari mode does not require it

        self._websocket = websocket
        self.temp = tempfile.TemporaryDirectory(prefix='tartarus-chromium-')
        self.port = free_port()
        executable = os.environ.get('TARTARUS_CHROMIUM', 'chromium')
        self.process = subprocess.Popen([
            executable,
            '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
            '--remote-allow-origins=*', f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.temp.name}', 'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = None
        self._id = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/list', timeout=1) as response:
                    pages = json.load(response)
                if pages:
                    self.ws = websocket.create_connection(pages[0]['webSocketDebuggerUrl'], timeout=30)
                    break
            except Exception:
                if self.process.poll() is not None:
                    raise AssertionError('Chromium exited before CDP became ready.')
                time.sleep(0.05)
        if self.ws is None:
            self.close()
            raise AssertionError('Chromium CDP did not become ready.')
        self._call('Page.enable')
        self._call('Runtime.enable')

    def _call(self, method, params=None):
        self._id += 1
        call_id = self._id
        self.ws.send(json.dumps({'id': call_id, 'method': method, 'params': params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get('id') != call_id:
                continue
            if 'error' in message:
                raise AssertionError(f'CDP {method} failed: {message["error"]}')
            return message.get('result', {})

    def close(self):
        if getattr(self, 'ws', None) is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if getattr(self, 'process', None) is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if getattr(self, 'temp', None) is not None:
            try:
                self.temp.cleanup()
            except OSError:
                shutil.rmtree(self.temp.name, ignore_errors=True)

    def open(self, url):
        self._call('Page.navigate', {'url': url})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                href = self.script("return window.location.href;") or ''
                ready = self.script("return document.readyState === 'complete';")
                if href.startswith(url) and ready:
                    return
            except AssertionError:
                pass
            time.sleep(0.05)
        raise AssertionError(f'Page did not finish loading: {url}')

    def script(self, script, *args):
        args_json = json.dumps(args, ensure_ascii=False)
        expression = f"(function(){{{script}}}).apply(null,{args_json})"
        result = self._call('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
        })
        value = result.get('result', {})
        if value.get('subtype') == 'error' or result.get('exceptionDetails'):
            raise AssertionError(f'Browser script failed: {result}')
        return value.get('value')

    def viewport(self, width, height):
        self._call('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False,
        })


def make_driver():
    if BROWSER_MODE == 'chromium':
        return ChromiumCDP()
    return SafariWebDriver()


INSTRUMENTATION = r"""
if (!window.__tartarusTest) {
  const nativeFetch = window.fetch.bind(window);
  const nativeSetTimeout = window.setTimeout.bind(window);
  const nativeClearTimeout = window.clearTimeout.bind(window);
  const state = window.__tartarusTest = {
    ttsCalls: [], pendingTts: [], delayTts: Boolean(arguments[0]),
    stageTimers: [], nextTimerId: -1,
  };
  window.fetch = function(input, init = {}) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.includes('/api/tts')) {
      let payload = {};
      try { payload = JSON.parse(init.body || '{}'); } catch (_) {}
      state.ttsCalls.push(payload);
      const response = () => new Response(JSON.stringify({supported:true, spoken:true}), {
        status: 200, headers: {'Content-Type':'application/json'}
      });
      if (state.delayTts) {
        return new Promise((resolve) => state.pendingTts.push(() => resolve(response())));
      }
      return Promise.resolve(response());
    }
    return nativeFetch(input, init);
  };
  window.setTimeout = function(fn, ms, ...args) {
    const delay = Number(ms);
    if ([5000, 7000, 10000].includes(delay)) {
      const timer = {id: state.nextTimerId--, ms: delay, fn, args, active: true};
      state.stageTimers.push(timer);
      return timer.id;
    }
    return nativeSetTimeout(fn, ms, ...args);
  };
  window.clearTimeout = function(id) {
    const timer = state.stageTimers.find((entry) => entry.id === id);
    if (timer) { timer.active = false; return; }
    return nativeClearTimeout(id);
  };
  window.__releaseAllTts = function() {
    state.delayTts = false;
    const pending = state.pendingTts.splice(0);
    pending.forEach((resolve) => resolve());
    return pending.length;
  };
  window.__fireStageTimer = function(ms) {
    const timer = [...state.stageTimers].reverse().find((entry) => entry.active && entry.ms === ms);
    if (!timer) return false;
    timer.active = false;
    timer.fn(...timer.args);
    return true;
  };
  window.__activeStageTimers = function() {
    return state.stageTimers.filter((entry) => entry.active).map((entry) => entry.ms);
  };
}
return true;
"""


class BrowserReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tartarus-browser-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / 'tartarus.db'
        self.word_lists = root / 'word_lists'
        self.word_lists.mkdir()
        self.log = root / 'tartarus.log'
        self.write_personal_fixture()
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        env = {
            **os.environ,
            'TARTARUS_DB': str(self.db),
            'TARTARUS_WORD_LISTS_DIR': str(self.word_lists),
            'TARTARUS_PORT': str(self.port),
            'TARTARUS_LOG_FILE': str(self.log),
            'PYTHONDONTWRITEBYTECODE': '1',
        }
        self.server = subprocess.Popen(
            [sys.executable, 'utils/tartarus_web.py'], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_server)
        self.wait_for_server()
        self.api('/api/user/create', {'user': 'alice'})
        # Synchronize fixture and create dataset_progress before direct stage setup.
        self.api('/api/gauntlet/progress?user=alice&lang=personal', method='GET')
        self.driver = make_driver()
        self.addCleanup(self.driver.close)
        self.driver.viewport(1280, 900)
        self.driver.open(self.base)
        self.wait_until("document.getElementById('practice-user').options.length > 1", 'setup did not load')
        self.driver.script(INSTRUMENTATION, False)

    def write_personal_fixture(self):
        (self.word_lists / 'alice_personal.json').write_text(json.dumps({
            'metadata': {
                'name': 'Personal', 'language': 'german', 'kind': 'vocabulary',
                'level': 'a1', 'pos': 'noun',
            },
            'items': [
                {'id': 'one', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
            ],
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    def stop_server(self):
        server = getattr(self, 'server', None)
        if server is None:
            return
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if server.stderr and not server.stderr.closed:
            server.stderr.close()

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise AssertionError(self.server.stderr.read())
            try:
                status, _ = http_json(self.base, '/api/wordlists')
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.05)
        raise AssertionError('isolated web server did not become ready')

    def api(self, path, data=None, *, method=None, expected=200):
        method = method or ('POST' if data is not None else 'GET')
        status, payload = http_json(self.base, path, method, data)
        self.assertEqual(status, expected, (path, status, payload))
        return payload

    def wait_until(self, predicate, message, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self.driver.script(
                    'try { return Boolean(' + predicate + '); } catch (_) { return false; }'
                )
            except AssertionError:
                value = False
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(message)

    def wait_db(self, query, params=(), predicate=lambda row: bool(row), message='database condition not met', timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conn = sqlite3.connect(self.db)
            try:
                row = conn.execute(query, params).fetchone()
            finally:
                conn.close()
            if predicate(row):
                return row
            time.sleep(0.05)
        raise AssertionError(message)

    def select(self, element_id, value):
        self.driver.script(
            "const e=document.getElementById(arguments[0]); e.value=arguments[1]; "
            "e.dispatchEvent(new Event('change',{bubbles:true})); return e.value;",
            element_id, value,
        )

    def configure_practice(self, lang='personal'):
        self.select('practice-user', 'alice')
        self.wait_until("document.getElementById('practice-lang').options.length > 1", 'language cascade did not load')
        self.select('practice-lang', 'german_vocabulary')
        self.wait_until("document.getElementById('practice-level').options.length > 1", 'level cascade did not load')
        self.select('practice-level', 'a1')
        self.wait_until("document.getElementById('practice-pos').options.length > 1", 'POS cascade did not load')
        self.select('practice-pos', 'noun')
        self.wait_until("[...document.getElementById('practice-file').options].some(o=>o.value==='" + lang + "')", 'file cascade did not load')
        self.select('practice-file', lang)

    def set_stage(self, day, *, score=None, box=None, lang='personal'):
        stage = 0 if day == 0 else 1 if day <= 2 else 2 if day <= 4 else 3 if day <= 6 else 4 if day <= 8 else 5
        if score is None:
            score = 2.0 if day == 0 else 9.0
        if box is None:
            box = None if score < 9 else 10
        last_practiced = None if day == 0 else (date.today() - timedelta(days=1)).isoformat()
        conn = sqlite3.connect(self.db)
        conn.execute(
            f'UPDATE "words_alice_{lang}" SET score=?, leitner_box=?, last_practiced=?, '
            'times_practiced=0, times_correct=0, times_incorrect=0, times_drilled=0, times_mastered=0, last_known_review_at=NULL',
            (score, box, last_practiced),
        )
        conn.execute(
            'INSERT OR REPLACE INTO dataset_progress '
            '(user,lang,current_stage,current_day,sessions_done_today,last_practice_date) VALUES (?,?,?,?,0,?)',
            ('alice', lang, stage, day, date.today().isoformat()),
        )
        conn.commit(); conn.close()

    def start_stage(self, day, *, score=None, box=None, lang='personal'):
        self.configure_practice(lang)
        self.set_stage(day, score=score, box=box, lang=lang)
        self.driver.script("document.getElementById('start-session').click(); return true;")
        self.wait_until("document.getElementById('practice-session').style.display !== 'none'", 'practice session did not start')
        self.wait_until("document.getElementById('answer-input').disabled === false", 'answer input did not become usable')

    def submit(self, text):
        self.wait_until("document.getElementById('answer-input').disabled === false", 'answer input unavailable')
        self.driver.script(
            "const e=document.getElementById('answer-input'); e.value=arguments[0]; "
            "e.dispatchEvent(new Event('input',{bubbles:true})); document.getElementById('submit-answer').click(); return true;",
            text,
        )

    def restart_setup(self):
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'summary not visible')
        self.driver.script("document.getElementById('summary-restart').click(); return true;")
        self.wait_until("document.getElementById('practice-setup').style.display !== 'none'", 'setup did not return')

    def word_row(self, lang='personal', content_id='one'):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            f'SELECT score,times_practiced,times_correct,times_incorrect,times_drilled,times_mastered,leitner_box,last_practiced,last_known_review_at '
            f'FROM "words_alice_{lang}" WHERE content_id=?', (content_id,),
        ).fetchone()
        conn.close()
        return row

    def tts_count(self):
        return self.driver.script('return window.__tartarusTest.ttsCalls.length;')

    def active_timers(self):
        return self.driver.script('return window.__activeStageTimers();')

    # TASK-28
    def test_tts_pending_does_not_block_typing_or_submission(self):
        self.driver.script('window.__tartarusTest.delayTts=true; return true;')
        self.start_stage(0, score=2.0)
        self.wait_until('window.__tartarusTest.ttsCalls.length === 1', 'prompt TTS was not requested')
        self.assertFalse(self.driver.script("return document.getElementById('answer-input').disabled;"))
        self.driver.script("const e=document.getElementById('answer-input'); e.value='eins'; e.dispatchEvent(new Event('input',{bubbles:true})); return true;")
        self.assertEqual(self.driver.script("return document.getElementById('answer-input').value;"), 'eins')
        self.driver.script("document.getElementById('submit-answer').click(); return true;")
        self.wait_db(
            'SELECT times_practiced,times_correct FROM "words_alice_personal" WHERE content_id="one"',
            predicate=lambda row: row == (1, 1), message='answer did not persist while TTS was pending',
        )
        self.assertEqual(self.driver.script("return document.getElementById('answer-input').value;"), 'eins')
        self.assertGreaterEqual(self.driver.script('return window.__tartarusTest.pendingTts.length;'), 1)
        self.driver.script('return window.__releaseAllTts();')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'correct result did not advance')
        self.assertEqual(self.word_row()[:3], (2.5, 1, 1))

    # TASK-29
    def test_forging_progressive_mask_auto_tts_and_no_stage_timer(self):
        self.start_stage(0, score=2.0)
        self.wait_until('window.__tartarusTest.ttsCalls.length >= 1', 'Forging did not request prompt TTS')
        shown = self.driver.script("return document.getElementById('word-display').textContent;")
        self.assertTrue(shown)
        self.assertIn('_', shown)
        self.assertEqual(self.active_timers(), [])

    # TASK-30
    def test_crucible_heavy_mask_definition_single_prompt_tts_and_no_timer(self):
        self.start_stage(1)
        self.wait_until("document.getElementById('session-type').textContent === 'Fading Structure'", 'Crucible label is wrong')
        self.wait_until('window.__tartarusTest.ttsCalls.length === 1', 'Crucible prompt TTS count is wrong')
        self.assertIn('_', self.driver.script("return document.getElementById('word-display').textContent;"))
        self.assertIn('one', self.driver.script("return document.getElementById('definition-lines').textContent;"))
        self.assertEqual(self.active_timers(), [])

    # TASK-31
    def test_shadows_hidden_two_correct_streak_contract(self):
        self.start_stage(3)
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'Shadows drill did not start')
        self.assertTrue(self.driver.script("return document.getElementById('word-display').classList.contains('hidden-word');"))
        self.assertIn('one', self.driver.script("return document.getElementById('definition-lines').textContent;"))
        self.assertGreaterEqual(self.tts_count(), 1)
        self.assertEqual(self.driver.script("return document.getElementById('drill-dots').textContent.length;"), 2)
        self.submit('eins')
        self.wait_until("document.getElementById('drill-streak').textContent === '1'", 'first Shadows correct did not show 1/2')
        self.assertTrue(self.driver.script("return document.getElementById('word-display').classList.contains('hidden-word');"))
        self.submit('wrong')
        self.wait_until("document.getElementById('drill-streak').textContent === '0'", 'Shadows wrong answer did not reset streak')
        self.submit('eins')
        self.wait_until("document.getElementById('drill-streak').textContent === '1'", 'Shadows streak did not restart')
        self.submit('eins')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'two correct Shadows answers did not complete item')
        self.assertEqual(self.word_row()[4], 1)

    # TASK-32
    def test_depths_manual_replay_and_captured_timeout(self):
        self.start_stage(5)
        self.wait_until("document.getElementById('session-type').textContent === 'Audio on Demand'", 'Depths label is wrong')
        self.assertEqual(self.tts_count(), 0)
        self.assertFalse(self.driver.script("return document.getElementById('btn-replay').disabled;"))
        self.assertEqual(self.active_timers(), [10000])
        self.driver.script("document.getElementById('btn-replay').click(); return true;")
        self.wait_until('window.__tartarusTest.ttsCalls.length === 1', 'Depths replay did not produce exactly one TTS request')
        self.assertTrue(self.driver.script('return window.__fireStageTimer(10000);'))
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'Depths timeout did not start corrective drill')
        self.assertEqual(self.driver.script("return document.getElementById('drill-dots').textContent.length;"), 9)
        self.assertEqual(self.word_row()[3], 1)

    # TASK-33
    def test_void_is_silent_replay_disabled_and_timer_is_7000(self):
        self.start_stage(7)
        self.wait_until("document.getElementById('session-type').textContent === 'Reverse Translation'", 'Void label is wrong')
        self.assertEqual(self.tts_count(), 0)
        self.assertTrue(self.driver.script("return document.getElementById('btn-replay').disabled;"))
        self.assertEqual(self.active_timers(), [7000])
        self.submit('+')
        self.wait_until("document.getElementById('answer-input').disabled === false", 'Void local replay command did not return control')
        self.assertEqual(self.tts_count(), 0)
        self.submit('eins')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'Void correct answer did not finish')
        self.assertEqual(self.tts_count(), 0)

    # TASK-34
    def test_ascension_is_silent_replay_disabled_and_timer_is_5000(self):
        self.start_stage(9)
        self.wait_until("document.getElementById('session-type').textContent === 'Speed Production'", 'Ascension label is wrong')
        self.assertEqual(self.tts_count(), 0)
        self.assertTrue(self.driver.script("return document.getElementById('btn-replay').disabled;"))
        self.assertEqual(self.active_timers(), [5000])
        self.submit('+')
        self.wait_until("document.getElementById('answer-input').disabled === false", 'Ascension local replay command did not return control')
        self.assertEqual(self.tts_count(), 0)
        self.submit('eins')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'Ascension correct answer did not finish')
        self.assertEqual(self.tts_count(), 0)

    # TASK-35
    def test_browser_corrective_drill_exact_persistence_and_cancellation(self):
        self.start_stage(0, score=4.0)
        self.submit('wrong')
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'wrong answer did not start drill')
        self.assertEqual(self.driver.script("return document.getElementById('drill-dots').textContent.length;"), 9)
        self.submit('wrong')
        self.wait_until("document.getElementById('drill-streak').textContent === '0'", 'drill wrong answer did not reset streak')
        for target in range(1, 10):
            self.submit('eins')
            if target < 9:
                self.wait_until(
                    f"document.getElementById('drill-streak').textContent === '{target}'",
                    f'drill streak did not reach {target}',
                )
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'completed drill did not end one-item session')
        self.assertEqual(self.word_row()[:5], (4.5, 2, 0, 1, 1))

        self.restart_setup()
        self.start_stage(0, score=4.5)
        self.submit('wrong')
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'second drill did not start')
        self.driver.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); return true;")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'Escape did not cancel incomplete drill')
        self.restart_setup()
        self.start_stage(0, score=4.5)
        self.assertEqual(self.driver.script("return document.getElementById('drill-block').style.display;"), 'none')
        self.assertNotEqual(self.driver.script("return document.getElementById('session-type').textContent;"), 'Drill')

    # TASK-36
    def test_manual_practice_commands_follow_current_contract(self):
        self.start_stage(0, score=4.0)
        self.assertFalse(self.driver.script("return document.getElementById('btn-reveal').disabled;"))
        self.driver.script("document.getElementById('btn-drill').click(); return true;")
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'manual $ button did not start drill')
        self.assertEqual(self.driver.script("return document.getElementById('drill-dots').textContent.length;"), 9)
        self.driver.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); return true;")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'manual drill did not cancel')

        # Button flag path preserves score/box.
        self.restart_setup()
        self.start_stage(1, score=9.0, box=3)
        self.driver.script("document.getElementById('btn-flag').click(); return true;")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'flag button did not finish')
        flagged_button = self.word_row()
        self.assertEqual((flagged_button[0], flagged_button[6]), (9.0, 3))

        # Typed flag path reaches the same score/box state.
        self.restart_setup()
        self.start_stage(1, score=9.0, box=3)
        self.submit('!')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'typed flag did not finish')
        flagged_typed = self.word_row()
        self.assertEqual((flagged_typed[0], flagged_typed[6]), (9.0, 3))

        # Master command keeps its existing semantics.
        self.restart_setup()
        self.start_stage(0, score=4.0)
        self.submit('@')
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'typed master did not finish')
        mastered = self.word_row()
        self.assertEqual((mastered[0], mastered[6]), (9.0, 1))

    # TASK-37
    def test_browser_due_review_filters_navigation_and_remains_read_only(self):
        self.api('/api/wordlist', {
            'user': 'alice', 'lang': 'personal',
            'items': [
                {'id': 'due', 'word': 'due', 'definition': ['due'], 'word_frequency': 0},
                {'id': 'later', 'word': 'later', 'definition': ['later'], 'word_frequency': 0},
                {'id': 'today', 'word': 'today', 'definition': ['today'], 'word_frequency': 0},
            ],
        })
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_personal" SET score=9, leitner_box=1, times_practiced=4, times_correct=3, times_incorrect=1, last_practiced=? WHERE content_id=?', ((date.today()-timedelta(days=30)).isoformat(), 'due'))
        conn.execute('UPDATE "words_alice_personal" SET score=9, leitner_box=10, last_practiced=? WHERE content_id=?', ((date.today()-timedelta(days=1)).isoformat(), 'later'))
        conn.execute('UPDATE "words_alice_personal" SET score=9, leitner_box=1, last_practiced=? WHERE content_id=?', (date.today().isoformat(), 'today'))
        conn.commit(); conn.close()
        before = self.word_row(content_id='due')
        self.configure_practice()
        self.driver.script("document.getElementById('start-review').click(); return true;")
        self.wait_until("document.getElementById('practice-session').style.display !== 'none'", 'due review did not start')
        self.assertEqual(self.driver.script("return document.getElementById('word-display').textContent;"), 'due')
        self.driver.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true})); return true;")
        self.wait_until("document.getElementById('word-display').textContent === 'due'", 'left boundary changed item')
        self.wait_until(
            "(document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})), "
            " document.getElementById('practice-summary').style.display !== 'none')",
            'right boundary did not finish review',
        )
        after = self.word_row(content_id='due')
        self.assertEqual(after, before)

    # TASK-38
    def test_browser_editor_creates_lossless_stable_personal_override(self):
        shared = self.word_lists / 'german' / 'vocabulary' / 'shared_noid.json'
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(json.dumps({
            'metadata': {
                'name': 'Shared no id', 'language': 'german', 'kind': 'vocabulary',
                'level': 'a1', 'pos': 'noun', 'source': 'browser-test',
            },
            'items': [
                {'word': 'das Haus', 'definition': ['house', 'line two', 'line three'], 'word_frequency': 8, 'custom': {'keep': True}},
                {'word': 'die Stadt', 'definition': ['city'], 'word_frequency': 2, 'extra': 'keep'},
            ],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        original_bytes = shared.read_bytes()
        self.driver.script("document.querySelector('[data-view=lists]').click(); return true;")
        self.wait_until("document.getElementById('editor-user').options.length > 1", 'editor users did not load')
        self.select('editor-user', 'alice')
        self.wait_until("document.getElementById('editor-category').options.length > 1", 'editor category did not load')
        self.select('editor-category', 'german_vocabulary')
        self.wait_until("document.getElementById('editor-level').options.length > 1", 'editor level did not load')
        self.select('editor-level', 'a1')
        self.wait_until("document.getElementById('editor-pos').options.length > 1", 'editor pos did not load')
        self.select('editor-pos', 'noun')
        self.wait_until("[...document.getElementById('editor-lang').options].some(o=>o.value==='shared_noid')", 'shared file missing from editor')
        self.select('editor-lang', 'shared_noid')
        self.driver.script("document.getElementById('editor-load').click(); return true;")
        self.wait_until("document.querySelectorAll('#editor-body tr').length === 2", 'editor rows did not load')
        first_id = self.driver.script("return document.querySelector('#editor-body tr').dataset.id;")
        self.assertTrue(first_id)
        self.driver.script("document.querySelector('.editor-def1').value='home'; document.getElementById('editor-save').click(); return true;")
        self.wait_until("document.getElementById('editor-message').textContent.includes('Saved 2 word')", 'first editor save failed')
        self.assertEqual(shared.read_bytes(), original_bytes)
        personal = self.word_lists / 'alice_shared_noid.json'
        saved = json.loads(personal.read_text(encoding='utf-8'))
        self.assertEqual([item['word'] for item in saved['items']], ['das Haus', 'die Stadt'])
        self.assertEqual(saved['items'][0]['id'], first_id)
        self.assertEqual(saved['items'][0]['definition'], ['home', 'line two', 'line three'])
        self.assertEqual(saved['items'][0]['word_frequency'], 8)
        self.assertEqual(saved['items'][0]['custom'], {'keep': True})
        self.assertEqual(saved['items'][1]['extra'], 'keep')
        self.assertEqual(saved['metadata']['kind'], 'vocabulary')
        self.assertEqual(saved['metadata']['level'], 'a1')
        self.driver.script("document.querySelector('.editor-def1').value='dwelling'; document.getElementById('editor-save').click(); return true;")
        self.wait_until("document.getElementById('editor-message').textContent.includes('Saved 2 word')", 'second editor save failed')
        self.assertEqual(json.loads(personal.read_text(encoding='utf-8'))['items'][0]['id'], first_id)

    # TASK-39
    def test_browser_report_uses_current_schema_without_debt_actions(self):
        self.configure_practice()
        # one wrong answer creates useful Nemesis data; cancel the drill afterwards
        self.driver.script("document.getElementById('start-session').click(); return true;")
        self.wait_until("document.getElementById('practice-session').style.display !== 'none'", 'practice did not start')
        self.submit('wrong')
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'drill did not start')
        self.driver.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); return true;")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", 'session did not end')
        self.driver.script("document.querySelector('[data-view=report]').click(); return true;")
        self.wait_until("document.getElementById('report-user').options.length > 1", 'report did not load controls')
        self.select('report-user', 'alice')
        self.driver.script("document.getElementById('load-report').click(); return true;")
        self.wait_until("document.getElementById('report-results').children.length > 0", 'report results did not render')
        html = self.driver.script("return document.getElementById('report-results').innerHTML;")
        self.assertNotIn('Flagged', html)
        self.assertNotIn('times_flagged', html)
        self.assertNotIn('undefined', html)
        self.assertFalse(self.driver.script("return [...document.querySelectorAll('#report-results button')].some(b=>/drill|debt/i.test(b.textContent));"))

    # TASK-40
    def test_browser_backup_export_import_restores_persisted_state(self):
        self.configure_practice()
        backup = self.api('/api/export?user=alice', method='GET')
        self.driver.script("document.querySelector('[data-view=report]').click(); return true;")
        self.wait_until("document.getElementById('report-user').options.length > 1", 'report controls missing')
        self.select('report-user', 'alice')
        self.driver.script("window.__downloadName=''; HTMLAnchorElement.prototype.click=function(){window.__downloadName=this.download}; document.getElementById('export-progress').click(); return true;")
        self.wait_until("window.__downloadName === 'tartarus_export_alice.json'", 'export UI did not produce backup download')

        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE "words_alice_personal" SET score=7.5, times_practiced=9 WHERE content_id="one"')
        conn.commit(); conn.close()
        self.assertEqual(self.word_row()[0], 7.5)
        backup_text = json.dumps(backup, ensure_ascii=False)
        self.driver.script(
            "const input=document.getElementById('import-file'); const transfer=new DataTransfer(); "
            "transfer.items.add(new File([arguments[0]],'backup.json',{type:'application/json'})); "
            "input.files=transfer.files; input.dispatchEvent(new Event('change',{bubbles:true})); return true;",
            backup_text,
        )
        self.wait_until("document.getElementById('report-error').textContent.includes('Import successful.')", 'import UI did not finish')
        self.assertEqual(self.api('/api/export?user=alice', method='GET'), backup)

    # TASK-41
    def test_desktop_and_mobile_layout_have_no_horizontal_overflow(self):
        for width, height in ((1280, 900), (390, 800)):
            with self.subTest(viewport=(width, height)):
                self.driver.viewport(width, height)
                self.wait_until('window.innerWidth > 0', 'viewport did not apply')
                overflow = self.driver.script('return document.documentElement.scrollWidth > window.innerWidth;')
                self.assertFalse(overflow)
                self.driver.script("document.getElementById('start-session').scrollIntoView({block:'center'}); return true;")
                rect = self.driver.script("const r=document.getElementById('start-session').getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};")
                self.assertGreater(rect['width'], 0)
                self.assertGreater(rect['height'], 0)
                self.assertGreaterEqual(rect['left'], 0)
                self.assertLessEqual(rect['right'], width + 1)
                self.assertGreaterEqual(rect['top'], 0)
                self.assertLessEqual(rect['bottom'], height + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
