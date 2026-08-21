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

  // --- Client-side event/error reporting -> server log ---
  // Best-effort and fire-and-forget: logging must never itself break the page.
  function reportClientEvent(level, message, extra = {}) {
    try {
      const user = document.getElementById('practice-user')?.value
        || document.getElementById('report-user')?.value
        || document.getElementById('editor-user')?.value || '';
      fetch('/api/client-log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level, message: String(message).slice(0, 2000), url: location.pathname, user, ...extra }),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  window.addEventListener('error', (e) => {
    reportClientEvent('error', e.message || 'Uncaught error',
      { stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 2000) : '' });
  });
  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    reportClientEvent('error', (reason && reason.message) ? reason.message : String(reason),
      { stack: (reason && reason.stack) ? String(reason.stack).slice(0, 2000) : '' });
  });

  function showError(el, message) {
    if (!message) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="error">${escapeHtml(message).replace(/\n/g, '<br>')}</div>`;
    reportClientEvent('warn', message, { context: el.id || '' });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Some German B1/B2 noun lists alone run 4,000-5,000+ words, and a
  // language's total across all levels/parts-of-speech can pass 20,000 --
  // a raw count that size overflows a dropdown's closed-state text (browsers
  // clip native <select> labels with no ellipsis). Keep counts compact and
  // consistent everywhere a list-size shows up.
  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    return String(n);
  }

  // --- Speech (backend TTS via macOS say) ---
  let speechTail = Promise.resolve();
  let speechPending = 0;

  // Bundled content has pre-generated audio in a per-list database; personal/
  // custom lists don't, and fall back to live server-side say() as before.
  // Returns true if pre-generated audio was found and played.
  async function playPreGeneratedAudio(user, lang, text) {
    if (!user || !lang) return false;
    const params = new URLSearchParams({ user, lang, text });
    let response;
    try {
      response = await fetch(`/api/audio?${params.toString()}`);
    } catch (err) {
      return false;
    }
    if (!response.ok) return false;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    try {
      await new Promise((resolve) => {
        const audio = new Audio(url);
        audio.addEventListener('ended', resolve, { once: true });
        audio.addEventListener('error', resolve, { once: true });
        audio.play().catch(resolve);
      });
    } finally {
      URL.revokeObjectURL(url);
    }
    return true;
  }

  function speak(text) {
    // TTS is queued so prompts/feedback never overlap. Stage policy decides
    // whether speech is automatic, manual-only, or disabled.
    const request = async () => {
      const played = await playPreGeneratedAudio(sessionUser, sessionListId, text);
      if (played) return;
      try {
        await fetch('/api/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, lang: sessionLang }),
        });
      } catch (err) { /* best-effort, matches the previous swallow-errors behavior */ }
    };
    speechPending += 1;
    // During speech only prompt typing may remain available. All buttons and
    // submit/navigation actions are locked until the queued speech finishes.
    answerSubmitReady = false;
    wordDisplay.classList.remove('can-submit');
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

  // Audio must never be muted during practice, in any stage -- every
  // question plays its prompt automatically, and Replay always works.
  function automaticAudioAllowed(_type) {
    return true;
  }

  function replayAudioAllowed(_type) {
    return true;
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
  let sessionUser = '';
  let sessionListId = '';
  let currentQuestion = null;
  let drillActive = false;
  let answering = false;
  let answerSubmitReady = false;
  let answerTarget = '';
  let answerPrompt = '';

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
  const answerInput = document.getElementById('answer-input');
  const drillBlock = document.getElementById('drill-block');
  const drillRep = document.getElementById('drill-rep');
  const drillStreak = document.getElementById('drill-streak');
  const drillTargetLabel = document.getElementById('drill-target');
  const drillDots = document.getElementById('drill-dots');
  const answerTimerWrap = document.getElementById('answer-timer-wrap');
  const answerTimerBar = document.getElementById('answer-timer-bar');
  const answerTimerLabel = document.getElementById('answer-timer-label');
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

  function isMaskableCharacter(ch) {
    return /[\p{L}\p{N}]/u.test(String(ch || ''));
  }

  function fullyMaskedTarget(target) {
    return Array.from(String(target || ''))
      .map((ch) => isMaskableCharacter(ch) ? '_' : ch)
      .join('');
  }

  function promptForQuestion(question) {
    const target = String(question?.word_unmasked || '');
    const supplied = String(question?.word || '');
    const hiddenMode = ['production', 'shadows', 'depths', 'void', 'ascension', 'maintenance'].includes(question?.type);
    if (hiddenMode || !supplied) return fullyMaskedTarget(target);
    return Array.from(supplied).length === Array.from(target).length ? supplied : fullyMaskedTarget(target);
  }

  function setAnswerSurface(target, prompt) {
    answerTarget = String(target || '');
    answerPrompt = String(prompt || '');
    if (Array.from(answerPrompt).length !== Array.from(answerTarget).length) {
      answerPrompt = fullyMaskedTarget(answerTarget);
    }
    // Typing past the target is never valid input, so don't let it happen:
    // cap the field at exactly the target's length (paste is already
    // blocked separately below).
    if (answerInput) answerInput.maxLength = Array.from(answerTarget).length;
    renderAnswerSurface();
  }

  function renderDefinitionPanel(lines) {
    if (!definitionLines) return;
    const values = Array.isArray(lines)
      ? lines.map((line) => String(line ?? '')).filter((line) => line.length > 0)
      : [];
    definitionLines.replaceChildren();
    definitionLines.classList.toggle('has-content', values.length > 0);
    values.forEach((line, index) => {
      const div = document.createElement('div');
      div.className = index === 0 ? 'definition-primary' : 'definition-context';
      div.textContent = line;
      definitionLines.appendChild(div);
    });
  }

  function renderAnswerSurface() {
    if (!wordDisplay) return;
    const target = Array.from(answerTarget);
    const prompt = Array.from(answerPrompt);
    const typed = Array.from(answerInput?.value || '');
    const sequence = document.createElement('span');
    sequence.className = 'answer-sequence';

    // The learning surface deliberately uses one immutable character cell per
    // target character.  The cell geometry never depends on the glyph, mask,
    // glow, or typed value, so the learner's focal point cannot shift while
    // spelling.  A monospace stack makes the visual advance deterministic too.
    target.forEach((targetChar, index) => {
      const span = document.createElement('span');
      span.className = 'answer-char';

      const isSpace = /\s/u.test(targetChar);
      const maskable = isMaskableCharacter(targetChar);
      if (isSpace) span.classList.add('space');
      else if (!maskable) span.classList.add('punctuation');

      if (index < typed.length) {
        // Render exactly what the learner typed. Spaces remain real spaces and
        // punctuation remains punctuation; neither is converted to a fake gap.
        span.textContent = typed[index];
        span.classList.add('typed');
      } else if (!maskable) {
        // Preserve the target's text structure at every masking level. A comma,
        // period, apostrophe, hyphen, or whitespace character is always drawn
        // literally (dim until reached, bright once typed). Only letters/digits
        // may become underscore fill slots.
        span.textContent = targetChar;
        span.classList.add('prompt');
      } else {
        const ch = prompt[index] ?? '_';
        span.textContent = ch;
        span.classList.add(ch === '_' ? 'masked' : 'prompt');
      }

      if (!answering && typed.length === index) span.classList.add('caret-before');
      if (!answering && typed.length === target.length && index === target.length - 1) {
        span.classList.add('caret-after');
      }
      sequence.appendChild(span);
    });

    // Keep over-typing visible without letting it change or escape the target
    // grid.  The correction tail is a separate bounded row below the target,
    // so the target stays centered and immutable while long accidental input
    // remains visible and backspaceable before submission.
    const overflow = typed.slice(target.length);
    const children = [sequence];
    if (overflow.length) {
      const tail = document.createElement('span');
      tail.className = 'answer-extra-tail';
      overflow.forEach((character, index) => {
        const span = document.createElement('span');
        span.className = 'answer-char typed extra';
        span.textContent = character;
        if (!answering && index === overflow.length - 1) span.classList.add('caret-after');
        tail.appendChild(span);
      });
      children.push(tail);
    }

    wordDisplay.replaceChildren(...children);
    const maskableIndexes = target.map((ch, index) => isMaskableCharacter(ch) ? index : -1).filter((index) => index >= 0);
    wordDisplay.classList.toggle(
      'fully-masked',
      maskableIndexes.length > 0 && maskableIndexes.every((index) => prompt[index] === '_'),
    );
    wordDisplay.classList.toggle('long-target', target.length >= 48);
    wordDisplay.classList.toggle('very-long-target', target.length >= 90);
    wordDisplay.dataset.typedLength = String(typed.length);
  }

  // --- Gauntlet status panel helpers ---
  const gauntletStageLabel = document.getElementById('gauntlet-stage-label');
  const gauntletDayLabel = document.getElementById('gauntlet-day-label');
  const gauntletSessionsLabel = document.getElementById('gauntlet-sessions-label');
  const gauntletModeLabel = document.getElementById('gauntlet-mode-label');

  async function fetchTrend(user, lang, metric) {
    if (!user || !lang) return [];
    const params = new URLSearchParams({ user, lang, metric });
    const data = await api(`/api/report/trend?${params.toString()}`);
    return Array.isArray(data.series) ? data.series : [];
  }

  async function fetchGauntletStatus(user, lang, { includeRoadmap = true } = {}) {
    if (!user || !lang) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = false;
      return;
    }
    try {
      const [data, masterySeries] = await Promise.all([
        api(`/api/gauntlet/progress?user=${encodeURIComponent(user)}&lang=${encodeURIComponent(lang)}`),
        includeRoadmap ? fetchTrend(user, lang, 'mastered') : Promise.resolve([]),
      ]);
      const p = data.progress;
      if (!p) return;
      if (practiceOverview) practiceOverview.style.display = '';
      if (gauntletStageLabel) gauntletStageLabel.textContent = 'Per-word Gauntlet';
      if (gauntletDayLabel) {
        gauntletDayLabel.textContent = `${p.forging || 0} in the Forging · ${p.reinforcement_total || 0} in the 10-day track · ${p.long_term_review || 0} in long-term review`;
      }

      const maintenanceReady = data.roadmap ? Number(data.roadmap.maintenance_ready || 0) : 0;
      const nothingAvailable = Number(p.available_tasks || 0) === 0 && maintenanceReady === 0;
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = nothingAvailable;

      if (gauntletSessionsLabel) {
        if (nothingAvailable) {
          gauntletSessionsLabel.textContent = p.complete
            ? 'The Gauntlet is complete for this material; no Leitner review is due today.'
            : 'Nothing left to practice here today — pick different material.';
        } else if (p.due_reinforcement) {
          gauntletSessionsLabel.textContent = `${p.due_reinforcement} reinforcement item${p.due_reinforcement === 1 ? '' : 's'} ready first`;
        } else if (maintenanceReady) {
          gauntletSessionsLabel.textContent = `${maintenanceReady} Leitner review item${maintenanceReady === 1 ? '' : 's'} ready now`;
        } else {
          gauntletSessionsLabel.textContent = `${p.forging || 0} item${p.forging === 1 ? '' : 's'} left to master`;
        }
      }
      if (gauntletModeLabel) {
        gauntletModeLabel.textContent = 'Each mastered word follows its own 10-day clock; due review always comes first.';
      }

      const roadmapContainer = document.getElementById('practice-roadmap-container');
      if (roadmapContainer) {
        roadmapContainer.innerHTML = '';
        if (includeRoadmap && data.roadmap) {
          data.roadmap.mastery_series = masterySeries;
          roadmapContainer.appendChild(renderRoadmapCard(data.roadmap));
        }
      }
    } catch (err) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      showError(practiceError, `Could not load Gauntlet status: ${err.message}`);
      const roadmapContainer = document.getElementById('practice-roadmap-container');
      if (roadmapContainer) roadmapContainer.innerHTML = '';
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = false;
    }
  }


  document.getElementById('start-session').addEventListener('click', () => startSession());
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
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitTextAnswer(); }
    // Keep the learner's visual focus on the inline answer surface.
    if (e.key === 'Tab') { e.preventDefault(); }
  });
  answerInput.addEventListener('input', renderAnswerSurface);
  window.addEventListener('resize', () => { if (answerTarget) requestAnimationFrame(renderAnswerSurface); });
  answerInput.addEventListener('focus', () => wordDisplay.classList.add('is-focused'));
  answerInput.addEventListener('blur', () => wordDisplay.classList.remove('is-focused'));
  answerInput.addEventListener('paste', (e) => e.preventDefault());
  wordDisplay.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    if (!answerInput.disabled) answerInput.focus();
  });

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
      if (event.target instanceof HTMLInputElement) { event.target.value = ''; renderAnswerSurface(); }
    }, true);
  }

  document.addEventListener('keydown', (event) => {
    if (!sessionId || event.key !== 'Escape') return;
    event.preventDefault();
    if (answerInteractionLocked()) return;
    cancelSession();
  });

  function setAnswerInputEnabled(enabled, allowSubmit = enabled) {
    const allowTyping = enabled && !answering;
    answerInput.disabled = !allowTyping;
    answerInput.readOnly = !allowTyping;
    answerSubmitReady = Boolean(allowSubmit && allowTyping && speechPending === 0);
    wordDisplay.classList.toggle('is-disabled', !allowTyping);
    wordDisplay.classList.toggle('can-submit', answerSubmitReady);
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
    if (!sessionId || answering || speechPending > 0) return;
    if (drillActive) {
      feedback.textContent = 'Complete the mandatory drill before ending the session.';
      feedback.className = 'feedback info';
      focusCurrentAnswer();
      return;
    }
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
    const posInput = document.getElementById('practice-pos');
    const fileInput = document.getElementById('practice-file');
    const user = userInput.value.trim();
    const lang = fileInput.value.trim();

    if (!user || !lang) {
      showError(practiceError, 'Select a user and a part of speech before entering the Gauntlet.');
      if (!user) userInput.focus();
      else if (!lang) posInput.focus();
      return;
    }

    try {
      // Gauntlet: backend determines mode. Only send essential fields.
      const body = { user, lang };

      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.audio_lang || data.lang || '';
      sessionUser = user;
      sessionListId = data.lang || '';
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
    resetAnswerCountdown();
    currentQuestion = question;
    drillActive = false;
    answering = false;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.classList.remove('is-active');
    wordDisplay.style.display = '';
    answerInput.style.display = '';
    answerInput.value = '';

    const q = progress.questions ?? 0;
    const maxQ = progress.max_questions ?? progress.total ?? '?';
    const gMeta = question.gauntlet || {};
    // Leitner maintenance is its own track, not a day of the 10-day
    // Gauntlet (day 0 already means The Forging elsewhere in this app) --
    // don't show a day fraction that doesn't apply to it.
    const dayLabel = gMeta.mode === 'maintenance'
      ? null
      : (Number(gMeta.day) >= 11 ? 'Complete' : `Day ${gMeta.day ?? 0}/10`);
    const progressParts = [gMeta.stage_name || 'Practice'];
    if (dayLabel) progressParts.push(dayLabel);
    progressParts.push(`Q${Math.min(q + 1, maxQ)}/${maxQ}`);
    sessionProgress.textContent = progressParts.join(' · ');
    sessionGauge.textContent = `${question.gauge || '●●●'} (score: ${formatScore(question)})`;
    sessionGauge.className = 'gauge band-gauntlet';
    sessionType.textContent = TYPE_LABELS[gMeta.mode] || TYPE_LABELS[question.type] || question.type;

    wordDisplay.className = `word-display answer-entry ${question.gender || ''}`;
    setAnswerSurface(question.word_unmasked || '', promptForQuestion(question));
    renderDefinitionPanel(question.definition || []);

    if (question.drill_start) {
      showDrill(question.drill_start);
      return;
    }

    // Response time scales with how much there is to type: 0.75s/character
    // normally, half that for the harder silent-recall stages (Void,
    // Ascension) that already ask for more from memory.
    const msPerChar = { depths: 750, void: 500, ascension: 500 }[question.type];
    const timerMs = msPerChar
      ? Math.round(Array.from(question.word_unmasked || '').length * msPerChar)
      : undefined;
    const timerSeconds = timerMs / 1000;
    const timerLabel = Number.isInteger(timerSeconds) ? String(timerSeconds) : timerSeconds.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
    answerInput.setAttribute('aria-label', timerMs ? `Type the full answer; ${timerLabel} second timer; press Enter to submit` : 'Type the full answer and press Enter to submit');
    if (timerMs) {
      // Starts the moment the question is shown, not after the prompt
      // audio finishes -- the response clock runs independently of
      // speech, not after it.
      window.gauntletTimer = setTimeout(() => {
        if (currentQuestion === question && !answerInteractionLocked()) sendTimeout();
      }, timerMs);
      startAnswerCountdown(timerMs);
    }
    const ready = () => {
      restoreInteractionAfterSpeech();
    };
    if (automaticAudioAllowed(question.type)) {
      presentQuestionAudio(question, ready);
    } else {
      ready();
    }
  }

  const TIMER_DIM_OPACITY = 0.32;

  function timerPercentFor(ms) {
    const remainingMs = Math.max(0, (window.gauntletTimerDeadline || 0) - Date.now());
    return ms ? Math.round((remainingMs / ms) * 100) : 0;
  }

  // A hard response timer (Depths/Void/Ascension) previously had zero visible
  // feedback -- only a screen-reader aria-label update. This bar makes the
  // countdown itself visible; it purely mirrors window.gauntletTimer's own
  // lifecycle and never drives the actual timeout logic.
  function startAnswerCountdown(ms) {
    if (!answerTimerWrap || !answerTimerBar) return;
    answerTimerWrap.classList.add('is-active');
    answerTimerBar.style.transition = 'none';
    answerTimerBar.style.width = '100%';
    answerTimerBar.style.opacity = '1';
    void answerTimerBar.offsetWidth; // force reflow so the animation below actually starts from 100%
    // Width shrinks to show elapsed-time-as-percentage; opacity fades to a
    // dim value on the same clock, matching this app's existing dim/bright
    // language (e.g. .masked/.prompt) instead of an alarm-style color swap.
    answerTimerBar.style.transition = `width ${ms}ms linear, opacity ${ms}ms linear`;
    answerTimerBar.style.width = '0%';
    answerTimerBar.style.opacity = String(TIMER_DIM_OPACITY);
    window.gauntletTimerDeadline = Date.now() + ms;
    window.gauntletTimerDuration = ms;
    if (answerTimerLabel) {
      // Percentage of time remaining, not a literal second count -- the
      // absolute duration isn't the point, how much of it is left is.
      const tick = () => { answerTimerLabel.textContent = `${timerPercentFor(ms)}%`; };
      tick();
      if (window.gauntletTimerTick) clearInterval(window.gauntletTimerTick);
      window.gauntletTimerTick = setInterval(tick, 100);
    }
  }

  // Submitting an answer stops the countdown from continuing to run, but
  // the bar stays exactly where it was and stays visible -- it does not
  // vanish. Only a genuinely new question (renderQuestion) clears and
  // re-arms it; nothing should appear to disappear mid-question.
  function freezeAnswerCountdown() {
    if (window.gauntletTimerTick) {
      clearInterval(window.gauntletTimerTick);
      window.gauntletTimerTick = null;
    }
    if (!answerTimerWrap || !answerTimerBar) return;
    if (!answerTimerWrap.classList.contains('is-active')) return;
    const ms = window.gauntletTimerDuration;
    const percent = timerPercentFor(ms);
    answerTimerBar.style.transition = 'none';
    answerTimerBar.style.width = `${percent}%`;
    answerTimerBar.style.opacity = String(TIMER_DIM_OPACITY + (1 - TIMER_DIM_OPACITY) * (percent / 100));
    if (answerTimerLabel) answerTimerLabel.textContent = `${percent}%`;
  }

  // A genuinely new question (or a drill, or the session ending) clears the
  // timer back to hidden/idle, ready for the next startAnswerCountdown().
  function resetAnswerCountdown() {
    if (window.gauntletTimerTick) {
      clearInterval(window.gauntletTimerTick);
      window.gauntletTimerTick = null;
    }
    if (!answerTimerWrap) return;
    answerTimerWrap.classList.remove('is-active');
    if (answerTimerLabel) answerTimerLabel.textContent = '';
  }

  function setActionButtons(enabled) {
    const interactive = enabled && speechPending === 0 && !answering;
    btnReplay.disabled = !interactive || !replayAudioAllowed(currentQuestion?.type);
    btnEnd.disabled = !interactive;
  }

  function formatScore(question) {
    return Number(question.score).toFixed(1);
  }

  function submitTextAnswer() {
    if (!answerSubmitReady || answerInteractionLocked()) return;
    // An answer shorter than the target can't be right and isn't a
    // meaningful attempt -- don't let Enter send it. maxLength on the input
    // already keeps it from ever running long, so this only ever blocks the
    // "too short" side.
    if (Array.from(answerInput.value).length !== Array.from(answerTarget).length) return;
    sendAnswer(answerInput.value);
  }


  function newAttemptId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }

  async function sendTimeout() {
    if (!sessionId || answering || speechPending > 0) return;
    answering = true;
    if (window.gauntletTimer) { clearTimeout(window.gauntletTimer); window.gauntletTimer = null; }
    freezeAnswerCountdown();
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
    // Stop counting down the moment an answer goes in, correct or not --
    // but stay visible, frozen where it was, rather than vanishing. Only
    // a genuinely new question clears and re-arms it (renderQuestion).
    if (window.gauntletTimer) { clearTimeout(window.gauntletTimer); window.gauntletTimer = null; }
    freezeAnswerCountdown();
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
    if (window.gauntletTimer) {
      clearTimeout(window.gauntletTimer);
      window.gauntletTimer = null;
    }
    resetAnswerCountdown();
    drillActive = true;
    drillBlock.classList.add('is-active');
    setActionButtons(false);
    // Shadows' own 2-production check-in preloads this same drill UI before
    // any mistake happens, so it keeps its stage label; every other path
    // into showDrill() is a real corrective drill triggered by a wrong
    // answer, in this question or a resumed one -- labeled consistently
    // regardless of which of those triggered it.
    const mode = currentQuestion?.gauntlet?.mode;
    sessionType.textContent = mode === 'shadows' ? TYPE_LABELS.shadows : 'Mandatory Drill';

    const drillTarget = String(drill.word || currentQuestion?.word_unmasked || '');
    const drillDefinition = Array.isArray(drill.definition)
      ? drill.definition
      : (currentQuestion?.definition || []);
    const drillComplete = drill.correct === true && Number(drill.correct_in_a_row) >= Number(drill.target);
    answerInput.value = drillComplete ? drillTarget : '';
    setAnswerSurface(drillTarget, drill.show_word === false ? fullyMaskedTarget(drillTarget) : drillTarget);
    renderDefinitionPanel(drillDefinition);

    drillRep.textContent = drill.repetition;
    drillStreak.textContent = drill.correct_in_a_row;
    if (drillTargetLabel) drillTargetLabel.textContent = drill.target;
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

    setAnswerInputEnabled(!drillComplete);
    renderAnswerSurface();
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
    resetAnswerCountdown();
    setAnswerInputEnabled(false);
    answerTarget = ''; answerPrompt = ''; answerInput.value = ''; renderAnswerSurface();
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
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    // Keep this screen to the two things worth seeing right after a session:
    // the summary above, and the compact day/stage status below. The full
    // roadmap and progress-percentage cards are for "Back to setup", not a
    // second helping right after finishing.
    fetchGauntletStatus(user, lang, { includeRoadmap: false });
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
      showError(reportError, 'Choose a part of speech to load a focused report, or clear the filters for the full report.');
      (level ? posInput : levelInput).focus();
      return;
    }
    try {
      const params = new URLSearchParams({ user });
      if (lang) params.set('lang', lang);

      if (!lang) {
        const summaryData = await api(`/api/report/summary?user=${encodeURIComponent(user)}`);
        if (summaryData.summary) resultsEl.appendChild(renderUserSummaryCard(summaryData.summary));
      }

      const [data, masterySeries, box10Series] = await Promise.all([
        api(`/api/report?${params.toString()}`),
        lang ? fetchTrend(user, lang, 'mastered') : Promise.resolve([]),
        lang ? fetchTrend(user, lang, 'box10') : Promise.resolve([]),
      ]);

      if (data.roadmap) {
        data.roadmap.mastery_series = masterySeries;
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
        if (dash.tracks) g1.appendChild(renderTrackProgressCard(dash.tracks, masterySeries, box10Series));
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
    // Oldest-to-newest for left→right, cap at 60 days
    const chartDays = [...days].reverse().slice(-60);
    const series = chartDays.map((day) => ({ date: day.date, cumulative: day.practiced }));
    return `<div class="daily-chart-wrap">
      <div class="daily-chart-label muted">Words practiced per day (last ${chartDays.length} day${chartDays.length !== 1 ? 's' : ''})</div>
      ${renderTrendChart(series, { label: 'Words practiced per day' })}
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

  const GAUNTLET_STAGE_NAMES = {
    forging: 'Forging', crucible: 'Crucible', shadows: 'Shadows',
    depths: 'Depths', void: 'Void', ascension: 'Ascension',
  };

  function formatWordStage(state, day) {
    if (state === 'long_term_review') return 'Long-term review';
    const name = GAUNTLET_STAGE_NAMES[state] || state;
    return day ? `${name} · Day ${day}` : name;
  }

  function renderWordStatsTable(lang, words, caption) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(caption || `Word list: ${lang}`)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Stage</th><th>Box</th><th>Maintenance</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Last activity</th></tr></thead><tbody>';
    words.forEach((w) => {
      const maintenance = w.leitner_box == null ? '—' : (w.maintenance_ready ? 'Ready' : (w.next_maintenance || '—'));
      html += `<tr><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.gauge_band}">${w.gauge}</td>`
        + `<td>${escapeHtml(formatWordStage(w.gauntlet_state, w.gauntlet_day))}</td>`
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

  function renderTrendChart(series, { compact = false, label = 'Cumulative progress over time' } = {}) {
    const points = (Array.isArray(series) ? series : [])
      .map((item) => ({ date: String(item.date || ''), value: Number(item.cumulative || 0) }))
      .filter((item) => item.date && Number.isFinite(item.value) && item.value >= 0);
    const width = compact ? 320 : 640;
    const height = compact ? 72 : 160;
    const left = compact ? 6 : 34;
    const right = compact ? 6 : 12;
    const top = compact ? 8 : 18;
    const bottom = compact ? 8 : 28;
    const base = height - bottom;
    const plotWidth = width - left - right;
    const plotHeight = base - top;
    const max = Math.max(1, ...points.map((item) => item.value));
    const coordinates = points.map((item, index) => {
      const x = points.length === 1 ? left + plotWidth / 2 : left + (plotWidth * index / (points.length - 1));
      const y = base - (plotHeight * item.value / max);
      return { ...item, x, y };
    });
    const line = coordinates.map((item, index) => `${index ? 'L' : 'M'}${item.x.toFixed(1)},${item.y.toFixed(1)}`).join(' ');
    const area = coordinates.length
      ? `M${coordinates[0].x.toFixed(1)},${base} ${coordinates.map((item) => `L${item.x.toFixed(1)},${item.y.toFixed(1)}`).join(' ')} L${coordinates[coordinates.length - 1].x.toFixed(1)},${base} Z`
      : '';
    const empty = coordinates.length === 0;
    const axisLabels = compact || empty ? '' : `
      <text class="trend-axis-label" x="${left}" y="${height - 7}">${escapeHtml(coordinates[0].date)}</text>
      <text class="trend-axis-label" x="${width - right}" y="${height - 7}" text-anchor="end">${escapeHtml(coordinates[coordinates.length - 1].date)}</text>`;
    const marks = coordinates.map((item) => `<circle class="trend-point" cx="${item.x.toFixed(1)}" cy="${item.y.toFixed(1)}" r="${compact ? 2 : 3}"><title>${escapeHtml(`${item.date}: ${item.value}`)}</title></circle>`).join('');
    return `<div class="trend-chart-wrap${compact ? ' trend-chart-compact' : ''}">
      <svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}">
        <title>${escapeHtml(label)}</title>
        <line class="trend-axis" x1="${left}" y1="${base}" x2="${width - right}" y2="${base}"/>
        ${area ? `<path class="trend-area" d="${area}"/><path class="trend-line" d="${line}"/>${marks}` : ''}
        ${axisLabels}
      </svg>
      ${empty ? '<span class="trend-empty">Milestone history starts with newly recorded progress.</span>' : ''}
    </div>`;
  }

  function renderRoadmapCard(roadmap) {
    const card = document.createElement('div');
    card.className = 'card roadmap-card';
    const gauntlet = roadmap.gauntlet || {};
    const stageCounts = new Map(
      (gauntlet.reinforcement_stages || []).map((item) => [Number(item.stage), Number(item.count || 0)])
    );
    const stages = [
      { id: 0, name: 'The Forging', days: 'Day 0', count: Number(gauntlet.forging || 0) },
      { id: 1, name: 'The Crucible', days: 'Days 1-2', count: stageCounts.get(1) || 0 },
      { id: 2, name: 'The Shadows', days: 'Days 3-4', count: stageCounts.get(2) || 0 },
      { id: 3, name: 'The Depths', days: 'Days 5-6', count: stageCounts.get(3) || 0 },
      { id: 4, name: 'The Void', days: 'Days 7-8', count: stageCounts.get(4) || 0 },
      { id: 5, name: 'Ascension', days: 'Days 9-10', count: stageCounts.get(5) || 0 },
    ];

    let gauntletHtml = `<div class="roadmap-section">
      <h3>The per-word 10-Day Gauntlet</h3>
      <p class="muted">Each mastered word advances by its own mastery date; cohorts can occupy several stages at once.</p>
      <div class="roadmap-timeline">`;

    stages.forEach((stage) => {
      const statusClass = stage.count > 0
        ? 'active'
        : (stage.id === 0 && gauntlet.mastered_total ? 'completed' : 'locked');
      const countLabel = `${stage.count} word${stage.count === 1 ? '' : 's'} · ${stage.days}`;
      gauntletHtml += `
        <div class="timeline-node ${statusClass}">
          <div class="node-circle">${stage.id}</div>
          <div class="node-info">
            <div class="node-name">${escapeHtml(stage.name)}</div>
            <div class="node-days">${escapeHtml(countLabel)}</div>
          </div>
        </div>
      `;
    });
    gauntletHtml += `</div>`;

    if (gauntlet.total_tasks) {
      const stats = `<span class="stage-progress-stats">${gauntlet.forging || 0} Forging · ${gauntlet.reinforcement_total || 0} ten-day track · ${gauntlet.long_term_review || 0} long-term</span>`;
      gauntletHtml += `
        <div class="roadmap-stage-progress-wrap">
          <div class="stage-progress-header">
            <span class="stage-progress-title">Mastery over time</span>
            ${stats}
          </div>
          ${renderTrendChart(roadmap.mastery_series, { label: 'Cumulative words mastered by day' })}
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
          const allCount = allWordLists.filter(w => w.user === user).reduce((sum, w) => sum + (w.word_count || 0), 0);
          return [{value: '', label: `All languages (${formatCount(allCount)})`}].concat(
            PRACTICE_CATEGORIES.map(([value, label]) => {
              const count = allWordLists.filter(w => w.user === user && w.category === value).reduce((sum, w) => sum + (w.word_count || 0), 0);
              return {value, label: `${label} (${formatCount(count)})`};
            })
          );
        }
        if (level === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category);
          const levels = [...new Set(matches.map(w => w.cefr_level))].sort((a, b) => a === 'all' ? -1 : b === 'all' ? 1 : a.localeCompare(b));
          return levels.map(val => {
            const count = matches.filter(w => w.cefr_level === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`};
          });
        }
        if (pos === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category && w.cefr_level === level);
          const poses = [...new Set(matches.map(w => w.pos))].sort();
          return poses.map(val => {
            const count = matches.filter(w => w.pos === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`};
          });
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a, b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${formatCount(w.word_count)}) ${w.lang}`}));
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
          return PRACTICE_CATEGORIES.map(([value, label]) => {
            const count = allWordLists.filter(w => w.user === user && w.category === value).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value, label: count ? `${label} (${formatCount(count)})` : `${label} (no files)`, disabled: false};
          });
        }
        if (level === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category);
          const levels = [...new Set(matches.map(w => w.cefr_level))].sort();
          return levels.map(val => {
            const count = matches.filter(w => w.cefr_level === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        if (pos === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category && w.cefr_level === level);
          const poses = [...new Set(matches.map(w => w.pos))].sort();
          return poses.map(val => {
            const count = matches.filter(w => w.pos === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${formatCount(w.word_count)}) ${w.lang}`}));
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
      const series = await Promise.all(data.lists.map((item) => fetchTrend(user, item.lang, 'mastered')));
      data.lists.forEach((item, index) => { item.mastery_series = series[index]; });
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
      const masteredPct = total ? Math.round(1000 * item.tartarus_score9 / total) / 10 : 0;
      const posLabel = item.pos ? item.pos.charAt(0).toUpperCase() + item.pos.slice(1) : 'Word list';
      html += `<div class="progress-row"><div class="progress-header"><span class="progress-lang">${escapeHtml(posLabel)}</span>`
        + `<span class="progress-pct">${item.learning_complete ? 'Complete' : `Box 10 ${boxPct.toFixed(1)}%`}</span></div>`
        + renderTrendChart(item.mastery_series, { compact: true, label: `${posLabel} mastery over time` })
        + `<div class="progress-meta"><span>Mastered: ${masteredPct.toFixed(1)}%</span>`
        + `<span>Long-term review: ${boxPct.toFixed(1)}%</span>`
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
    const previousValue = select.value;
    select.innerHTML = '<option value="">' + (select.dataset.placeholder || 'Select…') + '</option>';
    options.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      o.disabled = opt.disabled;
      if (opt.value === selectedValue) o.selected = true;
      select.appendChild(o);
    });
    // When a step resolves to exactly one real choice, there is nothing left
    // for the user to decide -- select it automatically instead of forcing a
    // click through a dropdown with one option. Only dispatch 'change' when
    // this actually moves the value, so cascades settle without duplicate
    // downstream fetches.
    if (!selectedValue) {
      const enabled = options.filter(opt => !opt.disabled && opt.value !== '');
      if (enabled.length === 1) {
        select.value = enabled[0].value;
        if (select.value !== previousValue) select.dispatchEvent(new Event('change'));
      }
    }
  }

  // Practice cascade: user -> category -> level -> pos -> file
  function setupPracticeCascade() {
    createCascade(
      ['practice-user', 'practice-lang', 'practice-level', 'practice-pos', 'practice-file'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select language…', disabled: true}];
        if (category === undefined) {
          return PRACTICE_CATEGORIES.map(([value, label]) => {
            const count = allWordLists.filter(w => w.user === user && w.category === value).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value, label: count ? `${label} (${formatCount(count)})` : `${label} (no files)`, disabled: false};
          });
        }
        if (level === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category);
          const levels = [...new Set(matches.map(w => w.cefr_level))].sort();
          return levels.map(val => {
            const count = matches.filter(w => w.cefr_level === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        if (pos === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category && w.cefr_level === level);
          const poses = [...new Set(matches.map(w => w.pos))].sort();
          return poses.map(val => {
            const count = matches.filter(w => w.pos === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${formatCount(w.word_count)}) ${w.lang}`}));
      }
    );
  }

  setupReportCascade();
  setupEditorCascade();
  setupPracticeCascade();
  document.getElementById('practice-file').addEventListener('change', () => {
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    Promise.all([fetchGauntletStatus(user, lang), loadSelectedProgress()]);
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

  function renderTrackProgressCard(tracks, masterySeries = [], box10Series = []) {
    const total = tracks.total || 0;
    const tPct = total ? Math.round(1000 * tracks.tartarus_score9 / total) / 10 : 0;
    const lPct = total ? Math.round(1000 * tracks.leitner_box10 / total) / 10 : 0;
    return createCard('dash-card-tracks', 'Learning Tracks', `
      <div class="track-metric"><strong>Mastered (score 9)</strong><span>${tPct.toFixed(1)}%</span></div>
      ${renderTrendChart(masterySeries, { label: 'Cumulative Tartarus mastery by day' })}
      <div class="track-metric"><strong>Leitner Box 10</strong><span>${lPct.toFixed(1)}%</span></div>
      ${renderTrendChart(box10Series, { label: 'Cumulative Leitner Box 10 milestones by day' })}
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
  const editorRestart = document.getElementById('editor-restart');

  async function loadEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) {
      showError(editorMessage, 'Select a user, language, level, and part of speech before loading.');
      return;
    }
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist?${params.toString()}`);
      editorBody.innerHTML = '';
      (data.items || []).forEach(addEditorRow);
      editorTableWrap.style.display = 'block';
      if (editorRestart) editorRestart.style.display = '';
    } catch (err) {
      editorTableWrap.style.display = 'none';
      if (editorRestart) editorRestart.style.display = 'none';
      showError(editorMessage, err.message);
    }
  }

  async function restartEditorProgress() {
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) return;
    if (!confirm('Reset all progress for this word list? This cannot be undone.')) return;
    showError(editorMessage, '');
    try {
      await api('/api/wordlist/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang }),
      });
      editorMessage.innerHTML = '<div class="success">Progress reset. The next session starts from the beginning.</div>';
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  async function playEditorWord(text) {
    text = (text || '').trim();
    if (!text) return;
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    const played = await playPreGeneratedAudio(user, lang, text);
    if (played) return;
    fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang }),
    }).catch(() => {});
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
    const playTd = document.createElement('td');
    const playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'secondary editor-play';
    playBtn.textContent = '▶';
    playBtn.title = 'Play pronunciation';
    playBtn.addEventListener('click', () => playEditorWord(tr.querySelector('.editor-word').value));
    playTd.appendChild(playBtn);
    tr.appendChild(playTd);
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
  if (editorRestart) editorRestart.addEventListener('click', restartEditorProgress);
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

  const btnShiftDates = document.getElementById('shift-dates');
  if (btnShiftDates) {
    btnShiftDates.addEventListener('click', async () => {
      const reportError = document.getElementById('report-error');
      const user = document.getElementById('report-user').value;
      showError(reportError, '');
      if (!user) { showError(reportError, 'Select a user before shifting dates.'); return; }
      if (!confirm(
        `Shift every practice date for '${user}' forward by one day, to cover a missed day? `
        + 'This moves last-practiced, mastery, and review dates together as a block. It cannot be undone from here.'
      )) return;
      try {
        await api('/api/user/shift-dates', { method: 'POST', body: JSON.stringify({ user }) });
        await loadReport();
        reportError.innerHTML = '<div class="success">Practice dates shifted forward one day.</div>';
      } catch (err) {
        showError(reportError, `Shift failed: ${err.message}`);
      }
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
