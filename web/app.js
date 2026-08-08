(() => {
  'use strict';

  // --- Navigation ---
  const navButtons = document.querySelectorAll('nav button[data-view]');
  const views = document.querySelectorAll('.view');

  function switchView(view) {
    navButtons.forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    views.forEach((v) => v.classList.remove('active'));
    document.getElementById(`view-${view}`).classList.add('active');
    if (view === 'lists') {
      loadWordLists();
    }
  }

  navButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      if (speechPending > 0) return;
      switchView(btn.dataset.view);
    });
  });

  // In-page links (e.g. on the About page) that jump to another view.
  document.querySelectorAll('[data-view-link]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (speechPending > 0) return;
      switchView(btn.dataset.viewLink);
    });
  });

  // --- API helper ---
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body != null && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const res = await fetch(path, { ...options, cache: 'no-store', headers });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  function showError(el, message) {
    if (!message) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="error">${escapeHtml(message).replace(/\n/g, '<br>')}</div>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // --- Speech (backend TTS via macOS say) ---
  let speechTail = Promise.resolve();
  let speechPending = 0;

  function speak(text) {
    // TTS is queued so prompts/feedback never overlap. Stage policy decides
    // whether speech is automatic, manual-only, or disabled.
    const wpmInput = document.getElementById('practice-wpm');
    let wpm = 128;
    if (wpmInput) {
      const parsed = parseInt(wpmInput.value, 10);
      if (!Number.isNaN(parsed) && parsed >= 30 && parsed <= 400) wpm = parsed;
    }
    const request = () => fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang: sessionLang, wpm }),
    }).then(() => {}).catch(() => {});
    speechPending += 1;
    // During speech only prompt typing may remain available. All buttons and
    // submit/navigation actions are locked until the queued speech finishes.
    submitAnswerButton.disabled = true;
    setActionButtons(false);
    const queued = speechTail.then(request, request);
    speechTail = queued.finally(() => {
      speechPending = Math.max(0, speechPending - 1);
      if (speechPending === 0) restoreInteractionAfterSpeech();
    });
    return speechTail;
  }


  function focusCurrentAnswer() {
    if (!currentQuestion) return;
    answerInput.focus();
  }

  function restoreInteractionAfterSpeech() {
    if (speechPending > 0 || !sessionId || !currentQuestion || answering) return;
    setAnswerInputEnabled(true);
    setActionButtons(true);
    focusCurrentAnswer();
  }

  const QUESTION_AUDIO_POLICY = {
    crucible: 'auto',
    shadows: 'auto',
    depths: 'manual',
    void: 'off',
    ascension: 'off',
  };

  function automaticAudioAllowed(type) {
    const policy = QUESTION_AUDIO_POLICY[type];
    return policy === undefined || policy === 'auto';
  }

  function replayAudioAllowed(type) {
    return QUESTION_AUDIO_POLICY[type] !== 'off';
  }

  function presentQuestionAudio(question, onReady) {
    // Prompt speech is the only speech interval where typing is permitted.
    // Submit/Enter, session controls, navigation and card changes stay locked.
    setAnswerInputEnabled(true, false);
    setActionButtons(false);
    focusCurrentAnswer();
    return speak(questionAudioText(question)).then(() => {
      if (currentQuestion === question && !answering) {
        onReady?.();
        restoreInteractionAfterSpeech();
      }
    });
  }

  function questionAudioText(question) {
    return question.audio_text || question.word_unmasked || question.word;
  }

  // --- Practice state ---
  let sessionId = null;
  let sessionLang = '';
  let currentQuestion = null;
  let drillActive = false;
  let answering = false;

  const setupCard = document.getElementById('practice-setup');
  const practiceOverview = document.getElementById('practice-overview');
  const sessionCard = document.getElementById('practice-session');
  const summaryCard = document.getElementById('practice-summary');
  const practiceError = document.getElementById('practice-error');

  const sessionProgress = document.getElementById('session-progress');
  const sessionGauge = document.getElementById('session-gauge');
  const sessionType = document.getElementById('session-type');
  const wordDisplay = document.getElementById('word-display');
  const definitionLines = document.getElementById('definition-lines');
  const answerBlock = document.getElementById('answer-block');
  const answerInput = document.getElementById('answer-input');
  const submitAnswerButton = document.getElementById('submit-answer');
  const drillBlock = document.getElementById('drill-block');
  const drillRep = document.getElementById('drill-rep');
  const drillStreak = document.getElementById('drill-streak');
  const drillDots = document.getElementById('drill-dots');
  const feedback = document.getElementById('feedback');
  const btnReplay = document.getElementById('btn-replay');
  const btnEnd = document.getElementById('btn-end');

  const TYPE_LABELS = {
    learning: 'Learning',
    production: 'Reverse Translation',
    crucible: 'Fading Structure',
    shadows: 'Heavy Masking',
    depths: 'Audio on Demand',
    void: 'Reverse Translation',
    ascension: 'Speed Production',
    maintenance: 'Leitner Maintenance',
  };

  // --- Gauntlet status panel helpers ---
  const gauntletStageLabel = document.getElementById('gauntlet-stage-label');
  const gauntletDayLabel = document.getElementById('gauntlet-day-label');
  const gauntletSessionsLabel = document.getElementById('gauntlet-sessions-label');
  const gauntletModeLabel = document.getElementById('gauntlet-mode-label');

  const GAUNTLET_MODE_DESC = {
    forging: 'Standard learning — score each word from 0 to 9',
    crucible: 'Fading Structure — heavily masked word + audio + definition',
    shadows: 'Dictation & Recall — word hidden + audio + definition',
    depths: 'Audio on Demand — word hidden + definition (audio manual)',
    void: 'Pure Production — word hidden + definition (NO audio)',
    ascension: 'Speed Production — word hidden + definition (NO audio, 5s timer)',
    maintenance: 'Leitner maintenance — scheduled practice is ready',
  };

  async function fetchGauntletStatus(user, lang) {
    if (!user || !lang) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      return;
    }
    try {
      const data = await api(`/api/gauntlet/progress?user=${encodeURIComponent(user)}&lang=${encodeURIComponent(lang)}`);
      const p = data.progress;
      if (!p) return;
      if (practiceOverview) practiceOverview.style.display = '';
      if (gauntletStageLabel) gauntletStageLabel.textContent = p.stage_name || '—';
      if (gauntletDayLabel) gauntletDayLabel.textContent = p.complete ? 'Gauntlet complete' : `Day ${p.current_day} / ${p.max_day}`;
      if (gauntletSessionsLabel) gauntletSessionsLabel.textContent = p.complete ? 'Tartarus track complete' : `Daily Task Remaining: ${p.remaining_tasks} words`;
      if (gauntletModeLabel) gauntletModeLabel.textContent = p.complete ? 'Leitner maintenance continues on its own schedule' : (GAUNTLET_MODE_DESC[p.session_mode] || '');

      const roadmapContainer = document.getElementById('practice-roadmap-container');
      if (roadmapContainer) {
        roadmapContainer.innerHTML = '';
        if (data.roadmap) {
          roadmapContainer.appendChild(renderRoadmapCard(data.roadmap));
        }
      }
    } catch (err) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      showError(practiceError, `Could not load Gauntlet status: ${err.message}`);
      const roadmapContainer = document.getElementById('practice-roadmap-container');
      if (roadmapContainer) roadmapContainer.innerHTML = '';
    }
  }

  document.getElementById('start-session').addEventListener('click', () => startSession());
  // Only text inputs get Enter-to-submit; selects use their native behaviour.
  ['practice-wpm'].forEach(id => {
    document.getElementById(id)?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); startSession(); }
    });
  });
  async function restorePracticeSetup() {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';

    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();

    // Rebuild the complete setup view after a session. The status/roadmap and
    // focused progress card are the same components shown before practice;
    // returning from a summary must not degrade the setup into a partial view.
    await Promise.all([
      fetchGauntletStatus(user, lang),
      loadSelectedProgress(),
    ]);

    document.getElementById('start-session').focus();
  }

  document.getElementById('summary-restart').addEventListener('click', () => {
    restorePracticeSetup().catch((err) => showError(practiceError, err.message));
  });
  submitAnswerButton.addEventListener('click', submitTextAnswer);

  // Shift+Enter is a transport shortcut for Replay. It is handled as a
  // keyboard action, never as answer text, so the answer field remains a
  // strict dataset-string channel.
  document.addEventListener('keydown', (e) => {
    if (!sessionId || e.key !== 'Enter' || !e.shiftKey) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    replayAudio();
  }, true);

  answerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitTextAnswer(); }
    // Prevent Tab from escaping the input to action buttons; Backspace is
    // handled in the input so no need to guard it here.
    if (e.key === 'Tab') { e.preventDefault(); }
  });
  answerInput.addEventListener('paste', (e) => e.preventDefault());

  function answerInteractionLocked() {
    return answering || speechPending > 0;
  }

  function isAnswerControl(target) {
    return target === answerInput;
  }

  for (const eventName of ['keydown', 'beforeinput', 'input']) {
    document.addEventListener(eventName, (event) => {
      if (!answering || !isAnswerControl(event.target)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.target instanceof HTMLInputElement) event.target.value = '';
    }, true);
  }

  document.addEventListener('keydown', (event) => {
    if (!sessionId || event.key !== 'Escape') return;
    event.preventDefault();
    if (answerInteractionLocked() || drillActive) return;
    cancelSession();
  });

  function setAnswerInputEnabled(enabled, allowSubmit = enabled) {
    const allowTyping = enabled && !answering;
    answerInput.disabled = !allowTyping;
    answerInput.readOnly = !allowTyping;
    submitAnswerButton.disabled = !(allowSubmit && !answering && speechPending === 0);
  }

  btnReplay.addEventListener('click', replayAudio);
  btnEnd.addEventListener('click', cancelSession);

  function replayAudio() {
    if (!currentQuestion || speechPending > 0 || answering) return Promise.resolve();
    if (!replayAudioAllowed(currentQuestion.type)) {
      feedback.textContent = 'Audio is unavailable in this stage.';
      feedback.className = 'feedback info';
      return Promise.resolve();
    }
    return speak(questionAudioText(currentQuestion));
  }

  async function cancelSession() {
    if (!sessionId || answering || speechPending > 0 || drillActive) return;
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/cancel', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      });
      showSummary(data.session || {
        practiced: 0, correct: 0, incorrect: [], drilled: 0,
        elapsed_seconds: 0, ended_early: true,
      });
    } catch (err) {
      answering = false;
      restoreInteractionAfterSpeech();
      showError(practiceError, err.message);
    }
  }

  // After a session ends, Enter goes back to setup.
  // On the setup card, Enter starts a session (unless focus is on a select).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (!document.getElementById('view-practice').classList.contains('active')) return;
    if (speechPending > 0 || answering) { e.preventDefault(); return; }

    if (summaryCard.style.display !== 'none') {
      e.preventDefault();
      document.getElementById('summary-restart').click();
      return;
    }
    // If setup card is showing and active element is not a select/textarea, start session.
    if (setupCard.style.display !== 'none' && !sessionId) {
      const tag = document.activeElement?.tagName;
      if (tag !== 'SELECT' && tag !== 'TEXTAREA') {
        e.preventDefault();
        startSession();
      }
    }
  });

  // During an active session, prevent Backspace from triggering browser
  // back-navigation when no input element is focused (macOS produces a
  // system alert sound when the browser tries to go back with no history).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Backspace') return;
    if (!sessionId) return;
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    e.preventDefault();
  });

  async function startSession() {
    showError(practiceError, '');
    const userInput = document.getElementById('practice-user');
    const fileInput = document.getElementById('practice-file');
    const user = userInput.value.trim();
    const lang = fileInput.value.trim();
    const wpmInput = document.getElementById('practice-wpm');
    let wpm = 128;
    if (wpmInput) {
      const parsed = parseInt(wpmInput.value, 10);
      if (!Number.isNaN(parsed) && parsed >= 30 && parsed <= 400) wpm = parsed;
    }

    if (!user || !lang) {
      showError(practiceError, 'Select a user and a word list file before entering the Gauntlet.');
      if (!user) userInput.focus();
      else if (!lang) fileInput.focus();
      return;
    }

    try {
      // Gauntlet: backend determines mode. Only send essential fields.
      const body = { user, lang, wpm };

      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.audio_lang || data.lang || '';
      setupCard.style.display = 'none';
      if (practiceOverview) practiceOverview.style.display = 'none';
      summaryCard.style.display = 'none';
      sessionCard.style.display = 'block';
      const pProg = document.getElementById('practice-progress');
      if (pProg) pProg.style.display = 'none';
      renderQuestion(data.question, data.progress);
    } catch (err) {
      showError(practiceError, err.message);
    }
  }



  function renderQuestion(question, progress) {
    if (window.gauntletTimer) {
      clearTimeout(window.gauntletTimer);
      window.gauntletTimer = null;
    }
    currentQuestion = question;
    drillActive = false;
    answering = false;
    submitAnswerButton.textContent = 'Submit';
    setAnswerInputEnabled(false);
    setActionButtons(false);
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.style.display = 'none';
    wordDisplay.style.display = '';
    answerBlock.style.display = 'flex';
    answerInput.style.display = '';
    answerInput.value = '';

    const q = progress.questions ?? 0;
    const maxQ = progress.max_questions ?? progress.total ?? '?';
    const gMeta = question.gauntlet || {};
    const dayLabel = Number(gMeta.day) >= 11 ? 'Complete' : `Day ${gMeta.day ?? 0}/10`;
    sessionProgress.textContent = `${gMeta.stage_name || 'Practice'} · ${dayLabel} · Q${Math.min(q + 1, maxQ)}/${maxQ}`;
    sessionGauge.textContent = `${question.gauge || '●●●'} (score: ${formatScore(question)})`;
    sessionGauge.className = 'gauge band-gauntlet';
    sessionType.textContent = TYPE_LABELS[gMeta.mode] || TYPE_LABELS[question.type] || question.type;

    wordDisplay.textContent = question.word || '';
    wordDisplay.className = `word-display ${question.gender || ''}`;
    definitionLines.innerHTML = '';
    (question.definition || []).forEach((line) => {
      const div = document.createElement('div');
      div.textContent = line;
      definitionLines.appendChild(div);
    });

    if (question.drill_start) {
      sessionType.textContent = gMeta.mode === 'shadows' ? TYPE_LABELS.shadows : 'Mandatory Drill';
      showDrill(question.drill_start);
      return;
    }

    if (['production', 'shadows', 'depths', 'void', 'ascension', 'maintenance'].includes(question.type)) {
      wordDisplay.classList.add('hidden-word');
      wordDisplay.textContent = '';
    } else {
      wordDisplay.classList.remove('hidden-word');
    }

    const timerMs = { depths: 10000, void: 7000, ascension: 5000 }[question.type];
    const ready = () => {
      answerInput.placeholder = timerMs ? `Type the answer (${timerMs / 1000}s timer!)...` : 'Type your answer...';
      if (timerMs) {
        window.gauntletTimer = setTimeout(() => {
          if (currentQuestion === question && !answerInteractionLocked()) sendTimeout();
        }, timerMs);
      }
      restoreInteractionAfterSpeech();
    };
    if (automaticAudioAllowed(question.type)) {
      presentQuestionAudio(question, ready);
    } else {
      ready();
    }
  }

  function setActionButtons(enabled) {
    const interactive = enabled && speechPending === 0 && !answering;
    btnReplay.disabled = !interactive || !replayAudioAllowed(currentQuestion?.type);
    btnEnd.disabled = !interactive || drillActive;
  }

  function formatScore(question) {
    return Number(question.score).toFixed(1);
  }

  function submitTextAnswer() {
    sendAnswer(answerInput.value);
  }


  function newAttemptId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }

  async function sendTimeout() {
    if (!sessionId || answering || speechPending > 0) return;
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/timeout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          question_id: currentQuestion?.question_id,
          sequence: currentQuestion?.sequence,
          attempt_id: newAttemptId(),
        }),
      });
      handleAnswerResult(data);
    } catch (err) {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      showError(practiceError, err.message);
    }
  }

  async function sendAnswer(answer) {
    if (!sessionId || answering || speechPending > 0) return;
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId, answer,
          question_id: currentQuestion?.question_id, sequence: currentQuestion?.sequence,
          attempt_id: newAttemptId(),
        }),
      });
      handleAnswerResult(data);
    } catch (err) {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      showError(practiceError, err.message);
    }
  }



  function handleAnswerResult(data) {
    if (data.result === 'drill_start' || data.result === 'drill_progress') {
      answering = false;
      showDrill(data.drill);
      return;
    }

    if (data.result === 'drilled' && data.drill) {
      showDrill(data.drill, false);
      answering = true;
      setAnswerInputEnabled(false);
      const afterDrillFeedback = () => setTimeout(() => {
        if (data.done) { showSummary(data.session); return; }
        answering = false;
        setActionButtons(true);
        renderQuestion(data.question, data.progress);
      }, 700);
      if (automaticAudioAllowed(currentQuestion?.type)) {
        speak(questionAudioText(currentQuestion)).then(afterDrillFeedback);
      } else {
        afterDrillFeedback();
      }
      return;
    }





    if (data.result === 'correct') {
      feedback.textContent = `Correct! '${data.word}'`;
      feedback.className = 'feedback correct';
    } else if (data.result === 'incorrect') {
      feedback.textContent = data.message;
      feedback.className = 'feedback incorrect';
    } else if (data.result === 'drilled') {
      feedback.textContent = data.message || 'Drill complete.';
      feedback.className = 'feedback info';
    } else if (data.result === 'end') {
      feedback.textContent = 'Session ended.';
      feedback.className = 'feedback info';
    }

    // Feedback is already shown above. Void/Ascension intentionally stay
    // silent; other modes can speak feedback before advancing.
    const audioOn = automaticAudioAllowed(currentQuestion?.type);
    const advance = () => {
      if (data.done) { showSummary(data.session); return; }
      setActionButtons(true);
      renderQuestion(data.question, data.progress);
    };

    if ((data.result === 'correct' || data.result === 'incorrect') && audioOn) {
      speak(questionAudioText(currentQuestion)).then(advance);
    } else {
      setTimeout(advance, 700);
    }
  }

  function showDrill(drill, playAudio = true) {
    drillActive = true;
    setAnswerInputEnabled(true);
    drillBlock.style.display = 'block';
    answerBlock.style.display = 'flex';
    setActionButtons(false);

    wordDisplay.textContent = drill.word;
    wordDisplay.classList.toggle('hidden-word', drill.show_word === false);
    definitionLines.innerHTML = '';
    if (drill.definition && drill.definition.length) {
      drill.definition.forEach((line) => {
        const div = document.createElement('div');
        div.textContent = line;
        definitionLines.appendChild(div);
      });
    }

    drillRep.textContent = drill.repetition;
    drillStreak.textContent = drill.correct_in_a_row;
    drillDots.textContent = '●'.repeat(drill.correct_in_a_row) + '○'.repeat(drill.target - drill.correct_in_a_row);

    if (drill.correct === true) {
      feedback.textContent = 'Correct!';
      feedback.className = 'feedback correct';
    } else if (drill.correct === false) {
      feedback.textContent = 'Incorrect. Streak reset.';
      feedback.className = 'feedback incorrect';
    } else {
      feedback.textContent = '';
      feedback.className = 'feedback';
    }

    answerInput.value = '';
    if (playAudio && automaticAudioAllowed(currentQuestion?.type)) {
      presentQuestionAudio(currentQuestion);
    } else {
      restoreInteractionAfterSpeech();
      focusCurrentAnswer();
    }
  }

  function showSummary(session) {
    answering = false;
    drillActive = false;
    if (window.gauntletTimer) {
      clearTimeout(window.gauntletTimer);
      window.gauntletTimer = null;
    }
    setAnswerInputEnabled(false);
    sessionCard.style.display = 'none';
    summaryCard.style.display = 'block';
    sessionId = null;
    currentQuestion = null;

    const minutes = Math.floor((session.elapsed_seconds || 0) / 60);
    const seconds = (session.elapsed_seconds || 0) % 60;
    let html = '<ul class="summary-list">';
    html += `<li>Words practiced: <strong>${session.practiced || 0}</strong></li>`;
    html += `<li>Correct answers: <strong>${session.correct || 0}</strong></li>`;
    html += `<li>Incorrect answers: <strong>${(session.incorrect || []).length}</strong></li>`;
    html += `<li>Words drilled: <strong>${session.drilled || 0}</strong></li>`;
    html += `<li>Session time: <strong>${minutes}m ${seconds}s</strong></li>`;
    html += '</ul>';
    if ((session.incorrect || []).length) {
      html += '<h3>Incorrect answers</h3><ul class="summary-list">';
      session.incorrect.forEach((item) => {
        html += `<li>You wrote '<strong>${escapeHtml(item.attempt)}</strong>'; target: '<strong>${escapeHtml(item.word)}</strong>'</li>`;
      });
      html += '</ul>';
    }
    document.getElementById('summary-body').innerHTML = html;
    loadSelectedProgress();
  }

  // --- Report ---
  ['report-user', 'report-lang', 'report-level', 'report-pos', 'report-file'].forEach(id => {
    document.getElementById(id).addEventListener('change', loadReport);
  });
  document.getElementById('load-report').addEventListener('click', loadReport);

  async function loadReport() {
    const reportError = document.getElementById('report-error');
    const resultsEl = document.getElementById('report-results');
    showError(reportError, '');
    resultsEl.innerHTML = '';
    const userInput = document.getElementById('report-user');
    const categoryInput = document.getElementById('report-lang');
    const levelInput = document.getElementById('report-level');
    const posInput = document.getElementById('report-pos');
    const langInput = document.getElementById('report-file');
    const user = userInput.value.trim();
    const category = categoryInput.value.trim();
    const level = levelInput.value.trim();
    const pos = posInput.value.trim();
    const lang = langInput.value.trim();
    if (!user) {
      showError(reportError, 'Select a user.');
      userInput.focus();
      return;
    }
    if (!lang && (category || level || pos)) {
      showError(reportError, 'Select a word list file, or clear the filters for the full report.');
      (pos ? langInput : level ? posInput : levelInput).focus();
      return;
    }
    try {
      const params = new URLSearchParams({ user });
      if (lang) params.set('lang', lang);

      if (!lang) {
        const summaryData = await api(`/api/report/summary?user=${encodeURIComponent(user)}`);
        if (summaryData.summary) resultsEl.appendChild(renderUserSummaryCard(summaryData.summary));
      }

      const data = await api(`/api/report?${params.toString()}`);

      if (data.roadmap) {
        resultsEl.appendChild(renderRoadmapCard(data.roadmap));
      }

      if (!data.reports.length && !resultsEl.hasChildNodes()) {
        resultsEl.innerHTML = '<div class="card muted">No practice sessions found.</div>';
      } else {
        data.reports.forEach((report) => {
          resultsEl.appendChild(renderReportTable(report));
        });
      }


      if (lang) {
        // Dashboard analytics cards (before the word-by-word stats table)
        try {
        const dParams = new URLSearchParams({ user, lang });
        const dash = await api(`/api/dashboard?${dParams}`);
        const secHeader = document.createElement('div');
        secHeader.className = 'dash-section-header';
        secHeader.innerHTML = '<h2>Analytics</h2>';
        resultsEl.appendChild(secHeader);
        resultsEl.appendChild(renderDashCard1(dash.overview));
        const g1 = document.createElement('div');
        g1.className = 'dashboard-grid';
        if (dash.tracks) g1.appendChild(renderTrackProgressCard(dash.tracks));
        g1.appendChild(renderPracticePaceCard(dash.velocity));
        resultsEl.appendChild(g1);
        if (dash.nemesis !== null) resultsEl.appendChild(renderMistakeHistoryCard(dash.nemesis));
        } catch (error) {
          appendReportWarning(resultsEl, `Analytics unavailable: ${error.message}`);
        }
        await loadWordListStats(user, lang, resultsEl);
      }
    } catch (err) {
      showError(reportError, err.message);
    }
  }

  function renderDailyChart(days) {
    if (!days || days.length === 0) return '';
    // Oldest-to-newest for left→right bars, cap at 60 days
    const chartDays = [...days].reverse().slice(-60);
    const maxVal = Math.max(...chartDays.map((d) => d.practiced), 1);
    const bars = chartDays.map((day) => {
      const pct = day.practiced > 0 ? Math.max(4, Math.round(100 * day.practiced / maxVal)) : 0;
      return `<div class="day-bar${pct === 0 ? ' day-bar-empty' : ''}" style="height:${pct}%" title="${day.date}: ${day.practiced} words"></div>`;
    }).join('');
    return `<div class="daily-chart-wrap">
      <div class="daily-chart-label muted">Words practiced per day (last ${chartDays.length} day${chartDays.length !== 1 ? 's' : ''})</div>
      <div class="daily-chart">${bars}</div>
    </div>`;
  }

  function renderUserSummaryCard(summary) {
    const card = document.createElement('div');
    card.className = 'card';
    const streak = summary.streak;
    let html = `<h3>User Overview: ${escapeHtml(summary.user)}</h3>`;
    html += `<p class="muted">Streak &rsaquo; Current: <strong>${streak.current}</strong> day${streak.current !== 1 ? 's' : ''} &nbsp;&middot;&nbsp; Best: <strong>${streak.best}</strong> day${streak.best !== 1 ? 's' : ''}</p>`;
    html += renderDailyChart(summary.days);
    html += '<table><caption>Daily Summary (All Languages)</caption>';
    html += '<thead><tr><th>Date</th><th>Sessions</th><th>Languages</th><th>Time</th>'
      + '<th>Words</th><th>Correct</th><th>Wrong</th><th>Accuracy</th><th>Avg/Word</th></tr></thead><tbody>';
    summary.days.forEach((day) => {
      const m = Math.floor(day.seconds / 60), s = day.seconds % 60;
      html += `<tr><td>${day.date}</td><td>${day.sessions}</td><td>${day.languages}</td>`
        + `<td>${m}m ${s}s</td><td>${day.practiced}</td><td>${day.correct}</td><td>${day.incorrect}</td>`
        + `<td>${day.accuracy != null ? day.accuracy + '%' : 'N/A'}</td>`
        + `<td>${day.avg_time != null ? day.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    });
    const t = summary.total;
    const th = Math.floor(t.seconds / 3600), tm = Math.floor((t.seconds % 3600) / 60);
    html += `<tr class="total-row"><td><strong>Total</strong></td><td>${t.sessions}</td><td>${t.languages}</td>`
      + `<td>${th}h ${tm}m</td><td>${t.practiced}</td><td>${t.correct}</td><td>${t.incorrect}</td>`
      + `<td>${t.accuracy != null ? t.accuracy + '%' : 'N/A'}</td>`
      + `<td>${t.avg_time != null ? t.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  function appendReportWarning(container, message) {
    const warning = document.createElement('p');
    warning.className = 'muted report-warning';
    warning.textContent = message;
    container.appendChild(warning);
  }

  async function loadWordListStats(user, lang, container) {
    const params = new URLSearchParams({ user, lang });
    try {
      const leitnerData = await api(`/api/wordlist/leitner?${params.toString()}`);
      if (leitnerData.leitner) container.appendChild(renderLeitnerCard(lang, leitnerData.leitner));
    } catch (error) {
      appendReportWarning(container, `Leitner details unavailable: ${error.message}`);
    }
    try {
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) container.appendChild(renderWordStatsTable(lang, data.words, 'Full Word List'));
    } catch (error) {
      appendReportWarning(container, `Word-list details unavailable: ${error.message}`);
      return;
    }
  }

  function renderWordStatsTable(lang, words, caption) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(caption || `Word list: ${lang}`)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Box</th><th>Maintenance</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Last activity</th></tr></thead><tbody>';
    words.forEach((w) => {
      const maintenance = w.leitner_box == null ? '—' : (w.maintenance_ready ? 'Ready' : (w.next_maintenance || '—'));
      html += `<tr${w.active ? '' : ' class="muted"'}><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.gauge_band}">${w.gauge}</td>`
        + `<td>${w.leitner_box ?? '—'}</td><td>${escapeHtml(maintenance)}</td>`
        + `<td>${w.times_practiced}</td><td>${w.times_correct}</td><td>${w.times_incorrect}</td>`
        + `<td>${w.times_drilled}</td><td>${formatDateTime(w.last_practiced)}</td></tr>`;
    });
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  function formatDateTime(value) {
    if (!value) return 'never';
    return String(value).replace('T', ' ').split('.')[0];
  }

  function renderReportTable(report) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(report.language)}</caption>`;
    html += '<thead><tr><th>Date</th><th>Sessions</th><th>Time</th><th>Practiced</th>'
      + '<th>Correct</th><th>Wrong</th><th>Drilled</th><th>Avg/Word</th></tr></thead><tbody>';
    report.days.forEach((day) => {
      const minutes = Math.floor(day.seconds / 60);
      const seconds = day.seconds % 60;
      html += `<tr><td>${day.date}</td><td>${day.sessions}</td><td>${minutes}m ${seconds}s</td>`
        + `<td>${day.practiced}</td><td>${day.correct}</td><td>${day.incorrect}</td>`
        + `<td>${day.drilled}</td><td>${day.avg_time != null ? day.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    });
    const t = report.total;
    const tHours = Math.floor(t.seconds / 3600);
    const tMinutes = Math.floor((t.seconds % 3600) / 60);
    html += `<tr class="total-row"><td>Total</td><td>${t.sessions}</td><td>${tHours}h ${tMinutes}m</td>`
      + `<td>${t.practiced}</td><td>${t.correct}</td><td>${t.incorrect}</td>`
      + `<td>${t.drilled}</td><td>${t.avg_time != null ? t.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  function renderRoadmapCard(roadmap) {
    const card = document.createElement('div');
    card.className = 'card roadmap-card';

    const stages = [
      { id: 0, name: 'The Forging', days: 'Day 0' },
      { id: 1, name: 'The Crucible', days: 'Days 1-2' },
      { id: 2, name: 'The Shadows', days: 'Days 3-4' },
      { id: 3, name: 'The Depths', days: 'Days 5-6' },
      { id: 4, name: 'The Void', days: 'Days 7-8' },
      { id: 5, name: 'Ascension', days: 'Days 9-10' }
    ];

    let gauntletHtml = `<div class="roadmap-section">
      <h3>The 10-Day Gauntlet</h3>
      <p class="muted">Your progress through the intense cognitive trials for this specific list.</p>
      <div class="roadmap-timeline">`;

    const currentStage = roadmap.gauntlet.current_stage;
    const gauntletComplete = !!roadmap.gauntlet.complete;
    stages.forEach(st => {
      let statusClass = '';
      if (gauntletComplete || st.id < currentStage) statusClass = 'completed';
      else if (st.id === currentStage) statusClass = 'active';
      else statusClass = 'locked';

      let dayText = gauntletComplete && st.id === 5 ? 'Complete' : (st.id === currentStage ? `Day ${roadmap.gauntlet.current_day}` : st.days);

      gauntletHtml += `
        <div class="timeline-node ${statusClass}">
          <div class="node-circle">${st.id}</div>
          <div class="node-info">
            <div class="node-name">${escapeHtml(st.name)}</div>
            <div class="node-days">${escapeHtml(dayText)}</div>
          </div>
        </div>
      `;
    });
    gauntletHtml += `</div>`;

    if (roadmap.gauntlet && roadmap.gauntlet.total_tasks && currentStage <= 5) {
      let total_tasks = roadmap.gauntlet.total_tasks;
      let remaining_tasks = roadmap.gauntlet.remaining_tasks;
      let current_day = roadmap.gauntlet.current_day;

      let stageTotalTasks = total_tasks * (currentStage === 0 ? 1 : 2);
      let isDay2 = currentStage > 0 && current_day > (currentStage * 2 - 1);
      let tasksCompleted = gauntletComplete ? stageTotalTasks : ((isDay2 ? total_tasks : 0) + Math.max(0, total_tasks - remaining_tasks));
      let pct = gauntletComplete ? 100 : Math.max(0, Math.min(100, Math.round((tasksCompleted / stageTotalTasks) * 100)));

      let displayStage = currentStage;
      if (!gauntletComplete && pct === 100 && displayStage < 5) {
        displayStage++;
        stageTotalTasks = total_tasks * 2;
        tasksCompleted = 0;
        pct = 0;
      }

      gauntletHtml += `
        <div class="roadmap-stage-progress-wrap">
          <div class="stage-progress-header">
            <span class="stage-progress-title">${stages[displayStage].name} Progress</span>
            <span class="stage-progress-stats">${pct}% (${tasksCompleted}/${stageTotalTasks} Tasks)</span>
          </div>
          <div class="stage-progress-bar-container">
            <div class="stage-progress-bar" style="width: ${pct}%"></div>
          </div>
        </div>
      `;
    }

    gauntletHtml += `</div>`;

    const leitnerBoxes = [];
    for (let i = 1; i <= 10; i++) {
      leitnerBoxes.push({
        box: i,
        count: roadmap.leitner_distribution[i] || 0,
      });
    }

    const leitnerHtml = `<div class="roadmap-section leitner-section">
      <h3>Lifetime Leitner Maintenance</h3>
      <p class="muted">The maintenance distribution of score-9 items (Box 1 = 1 day, Box 10 = 10 days). ${roadmap.maintenance_ready || 0} ready now.</p>
      ${renderLeitnerRoadmap(leitnerBoxes)}
    </div>`;

    card.innerHTML = gauntletHtml + leitnerHtml;
    return card;
  }

  function renderLeitnerRoadmap(boxes, { showIntervals = false } = {}) {
    let html = '<div class="leitner-roadmap-scroll"><div class="leitner-roadmap-track">';
    for (const box of boxes) {
      const b = Number(box.box || 0);
      const total = Number(box.count ?? box.total ?? 0);
      const stateClass = total > 0 ? 'has-words' : 'empty';
      html += `
        <div class="leitner-roadmap-node ${stateClass}">
          <div class="leitner-roadmap-square" aria-label="Leitner Box ${b}"><span class="leitner-roadmap-number">${b}</span></div>
          <div class="leitner-roadmap-info">
            <div class="leitner-roadmap-name">Box ${b}</div>
            <div class="leitner-roadmap-count">${total} word${total === 1 ? '' : 's'}</div>
            ${showIntervals ? `<div class="leitner-roadmap-interval">${b} day${b === 1 ? '' : 's'}</div>` : ''}
          </div>
        </div>`;
    }
    html += '</div></div>';
    return html;
  }

  // --- Word lists + cascading dropdowns ---

  var allWordLists = [];

  // Report cascade: user -> category -> level -> part of speech -> file
  function setupReportCascade() {
    createCascade(
      ['report-user', 'report-lang', 'report-level', 'report-pos', 'report-file'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select language…', disabled: true}];
        if (category === undefined) {
          return [{value: '', label: 'All languages'}].concat(
            PRACTICE_CATEGORIES.map(([value, label]) => ({value, label}))
          );
        }
        if (level === undefined) {
          const levels = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category)
            .map(w => w.cefr_level))].sort((a, b) => a === 'all' ? -1 : b === 'all' ? 1 : a.localeCompare(b));
          return levels.map(value => ({value, label: value ? value.toUpperCase() : 'ALL'}));
        }
        if (pos === undefined) {
          const poses = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category && w.cefr_level === level)
            .map(w => w.pos))].sort();
          return poses.map(value => ({value, label: value ? value.toUpperCase() : 'ALL'}));
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a, b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${w.word_count}) ${w.lang}`}));
      }
    );
  }

  // Editor cascade: user -> category -> level -> pos -> file
  function setupEditorCascade() {
    createCascade(
      ['editor-user', 'editor-category', 'editor-level', 'editor-pos', 'editor-lang'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select word list…', disabled: true}];
        if (category === undefined) {
          return PRACTICE_CATEGORIES.map(([value, label]) => ({
            value,
            label: allWordLists.some(w => w.user === user && w.category === value) ? label : `${label} (no files)`,
            disabled: false,
          }));
        }
        if (level === undefined) {
          const levels = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category)
            .map(w => w.cefr_level))].sort();
          return levels.map(val => ({
            value: val,
            label: val ? val.toUpperCase() : 'ALL',
            disabled: false
          }));
        }
        if (pos === undefined) {
          const poses = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category && w.cefr_level === level)
            .map(w => w.pos))].sort();
          return poses.map(val => ({
            value: val,
            label: val ? val.toUpperCase() : 'ALL',
            disabled: false
          }));
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${w.word_count}) ${w.lang}`}));
      }
    );
  }

  // --- Progress widget ---
  const progressEl = document.getElementById('practice-progress');

  function loadSelectedProgress() {
    return loadUserProgress(
      document.getElementById('practice-user').value,
      document.getElementById('practice-lang').value,
      document.getElementById('practice-level').value,
      document.getElementById('practice-file').value,
    );
  }

  async function loadUserProgress(user, category, level, lang = '') {
    if (!user || !category || !level) { progressEl.style.display = 'none'; return; }
    try {
      const params = new URLSearchParams({ user, category, level });
      if (lang) params.set('lang', lang);
      const data = await api(`/api/user/progress?${params.toString()}`);
      if (!data.lists || !data.lists.length) { progressEl.style.display = 'none'; return; }
      progressEl.innerHTML = renderProgressWidget(data.lists, category, level);
      progressEl.style.display = 'block';
    } catch (err) {
      progressEl.innerHTML = `<div class="card"><div class="error">${escapeHtml(`Could not load progress: ${err.message}`)}</div></div>`;
      progressEl.style.display = 'block';
    }
  }

  function renderProgressWidget(lists, category, level) {
    const labels = {
      english_vocabulary: 'English vocabulary', english_sentences: 'English sentences',
      german_vocabulary: 'German vocabulary', german_sentences: 'German sentences',
    };
    const title = category && level ? `Progress · ${labels[category] || category} · ${level.toUpperCase()}` : 'Progress';
    let html = `<div class="card"><h2>${escapeHtml(title)}</h2><div class="progress-list">`;
    lists.forEach((item) => {
      const total = item.total || 0;
      const boxPct = total ? Math.round(1000 * item.leitner_box10 / total) / 10 : 0;
      html += `<div class="progress-row"><div class="progress-header"><span class="progress-lang">${escapeHtml(item.lang)}</span>`
        + `<span class="progress-pct">${item.learning_complete ? 'Complete' : `Box 10 ${boxPct.toFixed(1)}%`}</span></div>`
        + `<div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${Math.min(boxPct,100)}%"></div></div>`
        + `<div class="progress-meta"><span>Tartarus score 9: ${item.tartarus_score9} / ${total}</span>`
        + `<span>Leitner Box 10: ${item.leitner_box10} / ${total}</span>`
        + `<span>Gauntlet: ${item.tartarus_track_complete ? 'complete' : 'in progress'}</span></div></div>`;
    });
    html += '</div></div>';
    return html;
  }


  function renderLeitnerCard(lang, stats) {
    const card = document.createElement('div');
    card.className = 'card';
    const boxes = Object.entries(stats.distribution || {}).map(([box, count]) => ({ box: Number(box), count, interval_days: Number(box) }));
    const total = boxes.reduce((sum, item) => sum + Number(item.count || 0), 0);
    card.innerHTML = `<h3>Lifetime Leitner Maintenance &mdash; ${escapeHtml(lang)}</h3>`
      + `<p class="muted">${total} items in maintenance · ${stats.box10 || 0} at Box 10 · ${stats.ready || 0} ready now</p>`
      + renderLeitnerRoadmap(boxes, { showIntervals: true });
    return card;
  }

  const PRACTICE_CATEGORIES = [
    ['english_vocabulary', 'English vocabulary'],
    ['english_sentences', 'English sentences'],
    ['german_vocabulary', 'German vocabulary'],
    ['german_sentences', 'German sentences'],
  ];

  // Generic cascade: given a chain of select IDs and a filter function,
  // populate each select based on the previous one's value.
  function createCascade(selectIds, getOptions) {
    const selects = selectIds.map(id => document.getElementById(id));
    selects.forEach((sel, i) => {
      if (i === selects.length - 1) return;
      sel.addEventListener('change', () => {
        for (let child = i + 1; child < selects.length; child += 1) {
          const vals = selects.slice(0, child).map(s => s.value);
          populateSelect(selects[child], getOptions(...vals), '');
        }
      });
    });
  }

  function populateSelect(select, options, selectedValue = '') {
    select.innerHTML = '<option value="">' + (select.dataset.placeholder || 'Select…') + '</option>';
    options.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      o.disabled = opt.disabled;
      if (opt.value === selectedValue) o.selected = true;
      select.appendChild(o);
    });
  }

  // Practice cascade: user -> category -> level -> pos -> file
  function setupPracticeCascade() {
    createCascade(
      ['practice-user', 'practice-lang', 'practice-level', 'practice-pos', 'practice-file'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select language…', disabled: true}];
        if (category === undefined) {
          return PRACTICE_CATEGORIES.map(([value, label]) => ({
            value,
            label: allWordLists.some(w => w.user === user && w.category === value) ? label : `${label} (no files)`,
            disabled: false,
          }));
        }
        if (level === undefined) {
          const levels = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category)
            .map(w => w.cefr_level))].sort();
          return levels.map(val => ({
            value: val,
            label: val ? val.toUpperCase() : 'ALL',
            disabled: false
          }));
        }
        if (pos === undefined) {
          const poses = [...new Set(allWordLists
            .filter(w => w.user === user && w.category === category && w.cefr_level === level)
            .map(w => w.pos))].sort();
          return poses.map(val => ({
            value: val,
            label: val ? val.toUpperCase() : 'ALL',
            disabled: false
          }));
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${w.word_count}) ${w.lang}`}));
      }
    );
  }

  setupReportCascade();
  setupEditorCascade();
  setupPracticeCascade();
  document.getElementById('practice-file').addEventListener('change', () => {
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    fetchGauntletStatus(user, lang);
  });
  document.getElementById('practice-user').addEventListener('change', () => {
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    fetchGauntletStatus(user, lang);
  });


  async function loadWordLists() {
    let apiUsers = [];
    try {
      const data = await api('/api/wordlists');
      allWordLists = data.wordlists || [];
      apiUsers = data.users || [];
    } catch (err) {
      console.error('Failed to load word lists:', err);
      allWordLists = [];
    }

    // Always refresh dropdowns, even if API failed (will use cached/empty data).
    // Populate user dropdowns
    const users = apiUsers.length ? apiUsers : [...new Set(allWordLists.map(w => w.user))].sort();
    ['practice-user', 'report-user', 'editor-user'].forEach(id => {
      const sel = document.getElementById(id);
      if (sel) {
        let prev = sel.value;
        if (!prev && users.length === 1) prev = users[0];
        sel.innerHTML = '<option value="">Select user…</option>' + users.map(u => `<option value="${u}"${u === prev ? ' selected' : ''}>${u}</option>`).join('');
        if (prev) sel.value = prev;
      }
    });
    // Populate all dependent selects after the word-list data is available.
    document.getElementById('practice-user')?.dispatchEvent(new Event('change'));
    document.getElementById('report-user')?.dispatchEvent(new Event('change'));
    document.getElementById('editor-user')?.dispatchEvent(new Event('change'));
  }

  // Load word lists immediately so dropdowns are populated on first page load.
  // After the dropdowns settle, load progress for whichever user is pre-selected.
  loadWordLists().then(() => {
    const user = document.getElementById('practice-user').value;
    if (user) loadUserProgress(user);
  });

  // Fallback: ensure dropdowns are populated even if initial load failed
  async function ensureDropdownsPopulated(retries = 3) {
    const userSel = document.getElementById('practice-user');
    if (!userSel || userSel.options.length > 1) return;
    for (let i = 0; i < retries; i++) {
      try {
        await loadWordLists();
        if (userSel.options.length > 1) break;
      } catch (_) {}
      await new Promise(r => setTimeout(r, 200 * (i + 1)));
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureDropdownsPopulated);
  } else {
    ensureDropdownsPopulated();
  }

  // --- Dashboard card renderers (used inside loadReport) ---

  // Generic card factory: creates a card with optional header and body
  function createCard(className, title, bodyHtml, extraClass = '') {
    const card = document.createElement('div');
    card.className = `card ${className} ${extraClass}`.trim();
    if (title) {
      const h3 = document.createElement('h3');
      h3.textContent = title;
      card.appendChild(h3);
    }
    if (typeof bodyHtml === 'string') {
      card.insertAdjacentHTML('beforeend', bodyHtml);
    } else if (bodyHtml instanceof Node) {
      card.appendChild(bodyHtml);
    }
    return card;
  }

  // Stat tile helper
  function statTile(num, label, extraClass = '', unit = '') {
    return `<div class="stat-tile ${extraClass}"><span class="stat-num">${num}${unit ? `<span class="stat-unit">${unit}</span>` : ''}</span><span class="stat-label">${label}</span></div>`;
  }

  // Factual dashboard cards only — no debt badges, predictions, or psychological labels.
  function renderDashCard1(overview) {
    const h = Math.floor((overview.total_seconds || 0) / 3600);
    const m = Math.floor(((overview.total_seconds || 0) % 3600) / 60);
    const accuracy = overview.overall_accuracy;
    const r = 38, circ = +(2 * Math.PI * r).toFixed(1);
    const filled = accuracy != null ? +(circ * accuracy / 100).toFixed(1) : 0;
    const arcColor = accuracy == null ? 'var(--surface1)' : accuracy >= 85 ? 'var(--green)' : accuracy >= 70 ? 'var(--yellow)' : 'var(--red)';
    const ringLabel = accuracy != null ? `${accuracy}%` : 'N/A';
    return createCard('dash-card-full dash-card-overview', 'Current Status', `
      <div class="stat-tiles">
        ${statTile(`${overview.streak.current}<span class="stat-unit">day${overview.streak.current !== 1 ? 's' : ''}</span>`, 'Current Streak')}
        ${statTile(`${h}h ${m}m`, 'Total Practice Time')}
        <div class="stat-tile stat-ring-tile"><svg width="90" height="90" viewBox="0 0 90 90" class="accuracy-ring">
          <circle cx="45" cy="45" r="${r}" fill="none" stroke="var(--surface1)" stroke-width="9"/>
          <circle cx="45" cy="45" r="${r}" fill="none" stroke="${arcColor}" stroke-width="9" stroke-dasharray="${filled} ${circ-filled}" stroke-linecap="round" transform="rotate(-90 45 45)"/>
          <text x="45" y="45" text-anchor="middle" dominant-baseline="middle" fill="${arcColor}" font-size="14" font-weight="700">${ringLabel}</text></svg>
          <span class="stat-label">Overall Accuracy</span></div>
      </div>`);
  }

  function renderTrackProgressCard(tracks) {
    const total = tracks.total || 0;
    const tPct = total ? Math.round(1000 * tracks.tartarus_score9 / total) / 10 : 0;
    const lPct = total ? Math.round(1000 * tracks.leitner_box10 / total) / 10 : 0;
    return createCard('dash-card-tracks', 'Learning Tracks', `
      <div class="track-metric"><strong>Tartarus score 9</strong><span>${tracks.tartarus_score9} / ${total} (${tPct.toFixed(1)}%)</span></div>
      <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${Math.min(tPct,100)}%"></div></div>
      <div class="track-metric"><strong>Leitner Box 10</strong><span>${tracks.leitner_box10} / ${total} (${lPct.toFixed(1)}%)</span></div>
      <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${Math.min(lPct,100)}%"></div></div>
      <p class="muted">Gauntlet: <strong>${tracks.tartarus_track_complete ? 'complete' : 'in progress'}</strong> · Learning path: <strong>${tracks.learning_complete ? 'complete' : 'in progress'}</strong></p>`);
  }

  function renderMistakeHistoryCard(words) {
    if (!words.length) return createCard('dash-card-nemesis', 'Most Mistaken Words', '<p class="muted">No incorrect-answer history for this list.</p>');
    const rows = words.map((w) => `<tr><td>${escapeHtml(w.word)}</td><td>${w.times_incorrect}</td><td>${w.times_correct}</td><td>${w.score.toFixed(1)}</td></tr>`).join('');
    return createCard('dash-card-nemesis', 'Most Mistaken Words', `
      <p class="muted">Historical incorrect-answer counts only. These rows do not create pending drills or session priority.</p>
      <table class="nemesis-table"><thead><tr><th>Word</th><th>Wrong</th><th>Right</th><th>Score</th></tr></thead><tbody>${rows}</tbody></table>`);
  }

  function renderPracticePaceCard(velocity) {
    const spw = velocity.avg_seconds_per_word != null ? `${velocity.avg_seconds_per_word}s` : 'N/A';
    return createCard('dash-card-velocity', 'Practice Pace', `
      <div class="velocity-tiles">
        <div class="vel-tile"><span class="vel-num">${spw}</span><span class="vel-label muted">average per practiced item</span></div>
        <div class="vel-tile"><span class="vel-num">${velocity.sessions || 0}</span><span class="vel-label muted">recorded sessions</span></div>
      </div>`);
  }

  // --- Word list editor ---
  const editorUser = document.getElementById('editor-user');
  const editorLang = document.getElementById('editor-lang');
  const editorTableWrap = document.getElementById('editor-table-wrap');
  const editorBody = document.getElementById('editor-body');
  const editorMessage = document.getElementById('editor-message');

  async function loadEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) {
      showError(editorMessage, 'Select a user and word list before loading.');
      return;
    }
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist?${params.toString()}`);
      editorBody.innerHTML = '';
      (data.items || []).forEach(addEditorRow);
      editorTableWrap.style.display = 'block';
    } catch (err) {
      editorTableWrap.style.display = 'none';
      showError(editorMessage, err.message);
    }
  }

  function addEditorRow(item) {
    const tr = document.createElement('tr');
    tr.dataset.id = item.id || '';
    tr._record = item.record || {};
    const definition = Array.isArray(item.definition) ? item.definition : [];
    const values = {
      word: item.word || '',
      def1: definition[0] || '',
      def2: definition[1] || '',
    };
    ['word', 'def1', 'def2'].forEach((field) => {
      const td = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.className = `editor-${field}`;
      input.value = values[field];
      input.autocomplete = 'off';
      input.autocorrect = 'off';
      input.autocapitalize = 'off';
      input.spellcheck = false;
      td.appendChild(input);
      tr.appendChild(td);
    });
    const td = document.createElement('td');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'secondary';
    removeBtn.textContent = '×';
    removeBtn.title = 'Remove';
    removeBtn.addEventListener('click', () => tr.remove());
    td.appendChild(removeBtn);
    tr.appendChild(td);
    editorBody.appendChild(tr);
  }

  async function saveEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    const items = [...editorBody.querySelectorAll('tr')].map((tr) => {
      const record = structuredClone(tr._record || {});
      const original = Array.isArray(record.definition) ? [...record.definition]
        : (record.definition ? String(record.definition).split('\n') : []);
      original[0] = tr.querySelector('.editor-def1').value;
      original[1] = tr.querySelector('.editor-def2').value;
      return {
        id: tr.dataset.id,
        word: tr.querySelector('.editor-word').value,
        definition: original,
        record,
      };
    }).filter((item) => item.word.trim());
    try {
      const data = await api('/api/wordlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang, items }),
      });
      editorMessage.innerHTML = `<div class="success">Saved ${data.count} word(s) to ${escapeHtml(data.path)}</div>`;
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  document.getElementById('editor-load').addEventListener('click', loadEditor);
  document.getElementById('editor-add-row').addEventListener('click', () => {
    addEditorRow({word: '', definition: ['', ''], record: {}});
  });
  document.getElementById('editor-save').addEventListener('click', saveEditor);

  async function createWordList() {
    const initMessage = document.getElementById('init-message');
    showError(initMessage, '');
    const userInput = document.getElementById('init-user');
    const langInput = document.getElementById('init-lang');
    const typeInput = document.getElementById('init-type');
    const user = userInput.value.trim();
    const lang = langInput.value.trim();
    if (!user || !lang) {
      showError(initMessage, 'User and language are required.');
      (user ? langInput : userInput).focus();
      return;
    }
    try {
      const data = await api('/api/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang, type: typeInput.value }),
      });
      initMessage.innerHTML = `<div class="success">${data.created ? 'Created' : 'Already existed'}: ${escapeHtml(data.path)}</div>`;
      loadWordLists();
    } catch (err) {
      showError(initMessage, err.message);
    }
  }

  document.getElementById('init-create').addEventListener('click', createWordList);
  ['init-user', 'init-lang'].forEach((id) => {
    document.getElementById(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); createWordList(); }
    });
  });


  // --- Import / Export / Custom Lists ---
  const btnCreateUser = document.getElementById('create-user');
  const createUserContainer = document.getElementById('create-user-container');
  const newUsernameInput = document.getElementById('new-username');
  const btnSubmitCreateUser = document.getElementById('submit-create-user');
  const btnCancelCreateUser = document.getElementById('cancel-create-user');

  if (btnCreateUser && createUserContainer) {
    btnCreateUser.addEventListener('click', () => {
      createUserContainer.style.display = 'flex';
      newUsernameInput.focus();
    });

    btnCancelCreateUser.addEventListener('click', () => {
      createUserContainer.style.display = 'none';
      newUsernameInput.value = '';
    });

    const submitUser = async () => {
      const newUser = newUsernameInput.value.trim();
      if (!newUser) {
        alert("Username cannot be empty");
        return;
      }
      try {
        await api('/api/user/create', {
          method: 'POST',
          body: JSON.stringify({ user: newUser })
        });
        alert(`User '${newUser}' created successfully!`);
        await loadWordLists(); // refresh user lists
        document.getElementById('report-user').value = newUser;
        createUserContainer.style.display = 'none';
        newUsernameInput.value = '';
      } catch (err) {
        alert('Failed to create user: ' + err.message);
      }
    };

    btnSubmitCreateUser.addEventListener('click', submitUser);
    newUsernameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitUser();
      if (e.key === 'Escape') btnCancelCreateUser.click();
    });
  }
  const btnExportProgress = document.getElementById('export-progress');
  if (btnExportProgress) {
    btnExportProgress.addEventListener('click', async () => {
      const user = document.getElementById('report-user').value;
      if (!user) { alert('Please select a user first'); return; }
      try {
        const data = await api(`/api/export?user=${encodeURIComponent(user)}`);
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `tartarus_export_${user}.json`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert('Export failed: ' + err.message);
      }
    });
  }

  const btnImportProgress = document.getElementById('import-progress');
  const fileImportProgress = document.getElementById('import-file');
  if (btnImportProgress && fileImportProgress) {
    btnImportProgress.addEventListener('click', () => fileImportProgress.click());
    fileImportProgress.addEventListener('change', async (e) => {
      const reportError = document.getElementById('report-error');
      const user = document.getElementById('report-user').value;
      showError(reportError, '');
      if (!user) { showError(reportError, 'Select a user before importing.'); return; }
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async (ev) => {
        try {
          const payload = { user, data: JSON.parse(ev.target.result) };
          await api('/api/import', { method: 'POST', body: JSON.stringify(payload) });
          await loadReport();
          reportError.innerHTML = '<div class="success">Import successful.</div>';
        } catch (err) {
          showError(reportError, `Import failed: ${err.message}`);
        } finally {
          fileImportProgress.value = '';
        }
      };
      reader.onerror = () => {
        showError(reportError, 'Import failed: could not read the selected file.');
        fileImportProgress.value = '';
      };
      reader.readAsText(file);
    });
  }

  const btnImportCustom = document.getElementById('import-custom-list');
  const fileImportCustom = document.getElementById('import-custom-file');
  if (btnImportCustom && fileImportCustom) {
    btnImportCustom.addEventListener('click', () => fileImportCustom.click());
    fileImportCustom.addEventListener('change', async (e) => {
      const user = document.getElementById('init-user').value;
      if (!user) { alert('Please enter a username in the field above'); return; }
      const file = e.target.files[0];
      if (!file) return;
      const listName = file.name.replace(/\.json$/, '');
      const reader = new FileReader();
      reader.onload = async (ev) => {
        try {
          const items = JSON.parse(ev.target.result);
          const payload = { user, list_name: listName, items };
          await api('/api/wordlist/custom', { method: 'POST', body: JSON.stringify(payload) });
          alert('Custom list imported successfully!');
          loadWordLists();
        } catch (err) {
          alert('Import failed: ' + err.message);
        }
      };
      reader.readAsText(file);
    });
  }

})();
