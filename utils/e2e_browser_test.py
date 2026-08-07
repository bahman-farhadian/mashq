#!/usr/bin/env python3
"""Safari smoke coverage against an isolated Tartarus web server."""

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DRIVER_URL = os.environ.get("TARTARUS_WEBDRIVER_URL", "http://127.0.0.1:4444")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url, method="GET", data=None, headers=None, timeout=20):
    body = None if data is None else json.dumps(data).encode()
    request_headers = headers or {}
    if body is not None:
        request_headers = {"Content-Type": "application/json", **request_headers}
    with urlopen(Request(url, body, request_headers, method=method), timeout=timeout) as response:
        return json.loads(response.read())


class WebDriver:
    def __init__(self):
        self.base = DRIVER_URL.rstrip("/")
        payload = {"capabilities": {"alwaysMatch": {"browserName": "safari"}}}
        result = self.call("POST", "/session", payload)
        self.session_id = result["value"]["sessionId"]

    def close(self):
        if getattr(self, "session_id", None):
            try:
                self.call("DELETE", f"/session/{self.session_id}")
            finally:
                self.session_id = None

    def call(self, method, path, payload=None):
        parsed = DRIVER_URL.removeprefix("http://").removeprefix("https://")
        host, port = parsed.rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=30)
        body = None if payload is None else json.dumps(payload)
        connection.request(method, path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode()
        connection.close()
        if response.status >= 400:
            raise AssertionError(f"WebDriver {method} {path}: {response.status} {raw}")
        return json.loads(raw) if raw else {"value": None}

    def open(self, url):
        self.call("POST", f"/session/{self.session_id}/url", {"url": url})

    def script(self, script, *args):
        return self.call("POST", f"/session/{self.session_id}/execute/sync", {"script": script, "args": list(args)})["value"]

    def viewport(self, width, height):
        self.call("POST", f"/session/{self.session_id}/window/rect", {"width": width, "height": height})


class BrowserSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tartarus-browser-")
        root = Path(self.temp.name)
        self.db = root / "tartarus.db"
        self.word_lists = root / "word_lists"
        self.word_lists.mkdir()
        (self.word_lists / 'alice_personal.json').write_text(json.dumps({
            'metadata': {'language': 'german', 'type': 'vocabulary', 'cefr_level': 'a1', 'category': 'german_vocabulary', 'pos': 'noun'},
            'items': [
                {'id': 'one', 'word': 'eins', 'definition': ['one'], 'word_frequency': 0},
                {'id': 'two', 'word': 'zwei', 'definition': ['two'], 'word_frequency': 0},
            ],
        }), encoding='utf-8')
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "TARTARUS_DB": str(self.db),
            "TARTARUS_WORD_LISTS_DIR": str(self.word_lists),
            "TARTARUS_PORT": str(self.port),
            "TARTARUS_LOG_FILE": str(root / "tartarus.log"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.server = subprocess.Popen(
            [sys.executable, "utils/tartarus_web.py"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_server)
        self.wait_for_server()
        request(f"{self.base}/api/user/create", "POST", {"user": "alice"})
        self.driver = WebDriver()
        self.addCleanup(self.driver.close)

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(10)
        if self.server.stderr:
            self.server.stderr.close()
        self.temp.cleanup()

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise AssertionError(self.server.stderr.read())
            try:
                request(f"{self.base}/api/wordlists")
                return
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(.1)
        raise AssertionError("isolated web server did not become ready")

    def wait_until(self, predicate, message, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.driver.script("return (" + predicate + ");")
            if value:
                return value
            time.sleep(.1)
        raise AssertionError(message)

    def select(self, element_id, value):
        self.driver.script(
            "const e=document.getElementById(arguments[0]); e.value=arguments[1]; "
            "e.dispatchEvent(new Event('change',{bubbles:true}));",
            element_id, value,
        )

    def test_practice_audio_reveal_drill_navigation_report_and_mobile_layout(self):
        self.driver.viewport(1280, 900)
        self.driver.open(self.base)
        self.wait_until("document.getElementById('practice-user').options.length > 1", "setup did not load")
        self.select("practice-user", "alice")
        self.wait_until("document.getElementById('practice-lang').options.length > 1", "language cascade did not load")
        self.select("practice-lang", "german_vocabulary")
        self.wait_until("document.getElementById('practice-level').options.length > 1", "level cascade did not load")
        self.select("practice-level", "a1")
        self.wait_until("document.getElementById('practice-pos').options.length > 1", "POS cascade did not load")
        self.select("practice-pos", "noun")
        self.wait_until("document.getElementById('practice-file').options.length > 1", "file cascade did not load")
        self.select("practice-file", "personal")
        self.driver.script("document.getElementById('start-session').click()")
        self.wait_until("document.getElementById('practice-session').style.display !== 'none'", 'practice session did not start')
        self.wait_until("document.getElementById('answer-input').disabled", "audio did not lock the answer")
        self.wait_until("!document.getElementById('answer-input').disabled", "audio did not unlock the answer")
        self.driver.script("document.getElementById('btn-reveal').click()")
        self.wait_until("document.getElementById('answer-input').disabled", "reveal audio did not lock the answer")
        self.wait_until("!document.getElementById('answer-input').disabled", "reveal audio did not unlock the answer")
        self.driver.script("document.getElementById('answer-input').value='eins'; document.getElementById('submit-answer').click()")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none' || document.getElementById('answer-input').disabled === false", "correct answer did not complete")
        self.driver.script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        self.wait_until("document.getElementById('practice-summary').style.display !== 'none'", "Escape did not end a non-drill session")
        self.driver.script("document.getElementById('summary-restart').click()")
        self.wait_until("document.getElementById('practice-setup').style.display !== 'none'", 'restart did not return to setup')
        self.select("practice-user", "alice")
        self.select("practice-lang", "german_vocabulary")
        self.select("practice-level", "a1")
        self.select("practice-pos", "noun")
        self.select("practice-file", "personal")
        self.driver.script("document.getElementById('start-session').click()")
        self.wait_until("document.getElementById('practice-session').style.display !== 'none'", 'second practice session did not start')
        self.wait_until("!document.getElementById('answer-input').disabled", 'second audio did not unlock the answer')
        self.driver.script("document.getElementById('answer-input').value='wrong'; document.getElementById('submit-answer').click()")
        self.wait_until("document.getElementById('drill-block').style.display !== 'none'", 'wrong answer did not start a drill')
        self.driver.script("document.querySelector('[data-view=report]').click()")
        self.wait_until("document.getElementById('report-user').options.length > 1", "report controls did not load")
        self.select("report-user", "alice")
        self.driver.script("document.getElementById('load-report').click()")
        self.wait_until("document.getElementById('report-results').children.length > 0 && !document.getElementById('report-error').textContent", "report did not render")
        self.driver.script("window.__downloadName=''; HTMLAnchorElement.prototype.click=function(){window.__downloadName=this.download}; document.getElementById('export-progress').click()")
        self.wait_until("window.__downloadName === 'tartarus_export_alice.json'", 'export control did not produce a backup download')
        self.driver.script("fetch('/api/export?user=alice').then((r)=>r.json()).then((data)=>{const input=document.getElementById('import-file'); const transfer=new DataTransfer(); transfer.items.add(new File([JSON.stringify(data)], 'backup.json', {type:'application/json'})); input.files=transfer.files; input.dispatchEvent(new Event('change',{bubbles:true}))})")
        self.wait_until("document.getElementById('report-error').textContent.includes('Import successful.')", 'import did not report success')
        self.driver.script("document.querySelector('[data-view=lists]').click()")
        self.wait_until("document.getElementById('editor-user').options.length > 1", "word-list editor did not load")
        self.select("editor-user", "alice")
        self.wait_until("document.getElementById('editor-category').options.length > 1", "editor category cascade did not load")
        self.select("editor-category", "german_vocabulary")
        self.wait_until("document.getElementById('editor-level').options.length > 1", "editor level cascade did not load")
        self.select("editor-level", "a1")
        self.wait_until("document.getElementById('editor-pos').options.length > 1", "editor POS cascade did not load")
        self.select("editor-pos", "noun")
        self.wait_until("document.getElementById('editor-lang').options.length > 1", "editor file cascade did not load")
        self.select("editor-lang", "personal")
        self.driver.script("document.getElementById('editor-load').click()")
        self.wait_until("document.getElementById('editor-table-wrap').style.display !== 'none'", "editor did not load material")
        self.driver.script("document.querySelector('.editor-def1').value='one edited'; document.getElementById('editor-save').click()")
        self.wait_until("document.getElementById('editor-message').textContent.includes('Saved 2 word')", "editor save did not complete")
        self.driver.viewport(390, 800)
        self.wait_until("document.documentElement.scrollWidth <= window.innerWidth", "mobile view overflows horizontally")


    def configure_practice(self):
        self.select("practice-user", "alice")
        self.wait_until("document.getElementById(\"practice-lang\").options.length > 1", "language cascade did not load")
        self.select("practice-lang", "german_vocabulary")
        self.wait_until("document.getElementById(\"practice-level\").options.length > 1", "level cascade did not load")
        self.select("practice-level", "a1")
        self.wait_until("document.getElementById(\"practice-pos\").options.length > 1", "POS cascade did not load")
        self.select("practice-pos", "noun")
        self.wait_until("document.getElementById(\"practice-file\").options.length > 1", "file cascade did not load")
        self.select("practice-file", "personal")

    def set_gauntlet_day(self, day):
        stage = {1: 1, 5: 3, 7: 4, 9: 5}[day]
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE dataset_progress SET current_stage=?, current_day=?, sessions_done_today=0, last_practice_date=? WHERE user=? AND lang=?",
            (stage, day, date.today().isoformat(), "alice", "personal"),
        )
        conn.commit()
        conn.close()

    def start_staged_session(self, day):
        self.set_gauntlet_day(day)
        self.driver.script("document.getElementById(\"start-session\").click()")
        self.wait_until("document.getElementById(\"practice-session\").style.display !== \"none\"", "staged session did not start")

    def end_staged_session(self):
        self.driver.script("document.dispatchEvent(new KeyboardEvent(\"keydown\",{key:\"Escape\",bubbles:true}))")
        self.wait_until("document.getElementById(\"practice-summary\").style.display !== \"none\"", "Escape did not end staged session")
        self.driver.script("document.getElementById(\"summary-restart\").click()")
        self.wait_until("document.getElementById(\"practice-setup\").style.display !== \"none\"", "staged restart did not return to setup")

    def test_gauntlet_audio_and_timer_policies(self):
        request(f"{self.base}/api/practice/start", "POST", {"user": "alice", "lang": "personal"})
        self.driver.viewport(1280, 900)
        self.driver.open(self.base)
        self.wait_until("document.getElementById(\"practice-user\").options.length > 1", "setup did not load")
        self.configure_practice()

        self.start_staged_session(5)
        self.wait_until("document.getElementById(\"session-type\").textContent === \"Audio on Demand\"", "Depths label is wrong")
        self.wait_until("!document.getElementById(\"answer-input\").disabled", "Depths incorrectly auto-locked input")
        self.driver.script("document.getElementById(\"btn-replay\").click()")
        self.wait_until("document.getElementById(\"answer-input\").disabled", "Depths replay did not lock input")
        self.wait_until("!document.getElementById(\"answer-input\").disabled", "Depths replay did not unlock input")
        self.end_staged_session()

        self.configure_practice()
        self.start_staged_session(7)
        self.wait_until("document.getElementById(\"session-type\").textContent === \"Reverse Translation\"", "Void label is wrong")
        self.wait_until("!document.getElementById(\"answer-input\").disabled", "Void incorrectly auto-locked input")
        self.end_staged_session()

        self.configure_practice()
        self.start_staged_session(9)
        self.wait_until("document.getElementById(\"session-type\").textContent === \"Speed Production\"", "Ascension label is wrong")
        self.wait_until("document.getElementById(\"drill-block\").style.display !== \"none\"", "Ascension timer did not start a corrective drill", timeout=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
