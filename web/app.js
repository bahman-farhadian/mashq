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
    btn.addEventListener('click', async () => {
      await waitForSpeech();
      switchView(btn.dataset.view);
    });
  });

  // In-page links (e.g. on the About page) that jump to another view.
  document.querySelectorAll('[data-view-link]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await waitForSpeech();
      switchView(btn.dataset.viewLink);
    });
  });

  // --- API helper ---
  async function api(path, options = {}) {
    options.cache = 'no-store';
    const res = await fetch(path, options);
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
    // Audio is ALWAYS on in Tartarus — neuroplasticity requires listening.
    // There is no audio toggle. If you cannot listen, you cannot practice.
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
    setAnswerInputEnabled(false);
    setActionButtons(false);
    const queued = speechTail.then(request, request);
    speechTail = queued.finally(() => {
      speechPending -= 1;
      if (speechPending === 0) restoreInteractionAfterSpeech();
    });
    return speechTail;
  }

  function waitForSpeech() {
    return speechTail.catch(() => {});
  }

  function focusCurrentAnswer() {
    if (!currentQuestion || sessionReviewMode) return;
    (currentQuestion.noun_forms ? nounSingularAnswer : answerInput).focus();
  }

  function restoreInteractionAfterSpeech() {
    if (!sessionId || !currentQuestion || sessionReviewMode || answering || speechPending) return;
    setAnswerInputEnabled(true);
    setActionButtons(!drillActive);
    focusCurrentAnswer();
  }

  const QUESTION_AUDIO_POLICY = {
    crucible: 'auto',
    shadows: 'auto',
    depths: 'manual',
    void: 'off',
    ascension: 'off',
  };

  function presentQuestionAudio(question, onReady) {
    return speak(questionAudioText(question)).then(() => {
      if (currentQuestion === question && !answering) onReady?.();
    });
  }

  function questionAudioText(question) {
    return question.audio_text || question.word_unmasked || question.word;
  }

  // --- Practice state ---
  let sessionId = null;
  let sessionLang = '';
  let sessionWpm = 128;
  let sessionFastMode = false;
  let sessionReviewMode = false;
  let reviewAudioPromise = Promise.resolve();
  let currentQuestion = null;
  let drillActive = false;
  let answering = false;

  const setupCard = document.getElementById('practice-setup');
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
  const nounAnswerInputs = document.getElementById('noun-answer-inputs');
  const nounSingularAnswer = document.getElementById('noun-singular-answer');
  const nounPluralAnswer = document.getElementById('noun-plural-answer');
  const submitAnswerButton = document.getElementById('submit-answer');
  const drillBlock = document.getElementById('drill-block');
  const drillRep = document.getElementById('drill-rep');
  const drillStreak = document.getElementById('drill-streak');
  const drillDots = document.getElementById('drill-dots');
  const feedback = document.getElementById('feedback');
  const reviewKeyHint = document.getElementById('review-key-hint');

  const btnReplay = document.getElementById('btn-replay');
  const btnReveal = document.getElementById('btn-reveal');
  const btnFlag = document.getElementById('btn-flag');
  const btnMaster = document.getElementById('btn-master');
  const btnDrill = document.getElementById('btn-drill');
  const btnEnd = document.getElementById('btn-end');

  const TYPE_LABELS = {
    learning: 'Learning',
    audio: 'Audio',
    crucible: 'Fading Structure',
    depths: 'Audio on Demand',
    ascension: 'Speed Production',
    spelling: 'Learning',
    production: 'Reverse Translation',
    known_review: 'Known Review',
    fast: 'Audio-Only',
    shadows: 'Heavy Masking',
    void: 'Reverse Translation',
    maintenance: 'Leitner Review',
  };

  // --- Gauntlet status panel helpers ---
  const gauntletStatus = document.getElementById('gauntlet-status');
  const gauntletStageLabel = document.getElementById('gauntlet-stage-label');
  const gauntletDayLabel = document.getElementById('gauntlet-day-label');
  const gauntletSessionsLabel = document.getElementById('gauntlet-sessions-label');
  const gauntletLockLabel = document.getElementById('gauntlet-lock-label');
  const gauntletModeLabel = document.getElementById('gauntlet-mode-label');

  const GAUNTLET_MODE_DESC = {
    forging: 'Standard learning — score each word from 0 to 9',
    crucible: 'Fading Structure — heavily masked word + audio + definition',
    shadows: 'Dictation & Recall — word hidden + audio + definition',
    depths: 'Audio on Demand — word hidden + definition (audio manual)',
    void: 'Pure Production — word hidden + definition (NO audio)',
    ascension: 'Speed Production — word hidden + definition (NO audio, 7s timer)',
    maintenance: 'Leitner maintenance — decayed words due for review',
  };

  async function fetchGauntletStatus(user, lang) {
    if (!user || !lang) {
      if (gauntletStatus) gauntletStatus.style.display = 'none';
      return;
    }
    try {
      const data = await api(`/api/gauntlet/progress?user=${encodeURIComponent(user)}&lang=${encodeURIComponent(lang)}`);
      const p = data.progress;
      if (!p) return;
      if (gauntletStatus) gauntletStatus.style.display = '';
      if (gauntletStageLabel) gauntletStageLabel.textContent = p.stage_name || '—';
      if (gauntletDayLabel) gauntletDayLabel.textContent = `Day ${p.current_day} / ${p.max_day}`;
      if (gauntletSessionsLabel) gauntletSessionsLabel.textContent = `Daily Task Remaining: ${p.remaining_tasks} words`;
      if (gauntletLockLabel) gauntletLockLabel.style.display = p.locked_today ? '' : 'none';
      if (gauntletModeLabel) gauntletModeLabel.textContent = GAUNTLET_MODE_DESC[p.session_mode] || '';
      
      const roadmapContainer = document.getElementById('practice-roadmap-container');
      if (roadmapContainer) {
        roadmapContainer.innerHTML = '';
        if (data.roadmap) {
          roadmapContainer.appendChild(renderRoadmapCard(data.roadmap));
        }
      }
    } catch (_) {
      if (gauntletStatus) gauntletStatus.style.display = 'none';
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
  document.getElementById('summary-restart').addEventListener('click', () => {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';
    if (userSelect.value && fileSelect.value) {
      fetchGauntletStatus(userSelect.value, fileSelect.value);
    }
    document.getElementById('start-session').focus();
  });
  submitAnswerButton.addEventListener('click', submitTextAnswer);
  for (const input of [nounSingularAnswer, nounPluralAnswer]) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); submitTextAnswer(); }
      if (e.key === 'Tab') { e.preventDefault(); }
    });
    input.addEventListener('paste', (e) => e.preventDefault());
  }
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
    return target === answerInput || target === nounSingularAnswer || target === nounPluralAnswer;
  }

  for (const eventName of ['keydown', 'beforeinput', 'input']) {
    document.addEventListener(eventName, (event) => {
      if (!answerInteractionLocked() || !isAnswerControl(event.target)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.target instanceof HTMLInputElement) event.target.value = '';
    }, true);
  }

  document.addEventListener('keydown', (event) => {
    if (!sessionId) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!answering) sendAnswer('!!');
      return;
    }
    if (speechPending || !sessionReviewMode || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    sendReviewMove(event.key);
  });

  function setAnswerInputEnabled(enabled) {
    const allowInput = enabled && !answering;
    answerInput.disabled = !allowInput;
    answerInput.readOnly = !allowInput;
    nounSingularAnswer.disabled = !allowInput;
    nounSingularAnswer.readOnly = !allowInput;
    nounPluralAnswer.disabled = !allowInput;
    nounPluralAnswer.readOnly = !allowInput;
    submitAnswerButton.disabled = !allowInput;
  }

  btnReplay.addEventListener('click', replayAudio);
  btnReveal.addEventListener('click', () => {
    runLocalCommand(answerInput, revealWord, answerInput);
  });

  function replayAudio() {
    return currentQuestion ? speak(questionAudioText(currentQuestion)) : Promise.resolve();
  }

  async function revealWord() {
    const question = currentQuestion;
    if (!question) return;
    await waitForSpeech();
    if (currentQuestion !== question) return;
    if (!question.can_reveal) {
      feedback.textContent = 'Reveal is unavailable after mastery.';
      feedback.className = 'feedback info';
      return;
    }

    const wasHidden = wordDisplay.classList.contains('hidden-word');
    wordDisplay.textContent = question.word_unmasked;
    wordDisplay.classList.remove('hidden-word');

    await speak(questionAudioText(question));
    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (currentQuestion !== question) return;
    wordDisplay.textContent = question.word;
    wordDisplay.classList.toggle('hidden-word', wasHidden);
  }

  btnFlag.addEventListener('click', () => sendAnswer('!'));
  btnMaster.addEventListener('click', () => sendAnswer('@'));
  btnDrill.addEventListener('click', () => sendAnswer('$'));
  btnEnd.addEventListener('click', () => sendAnswer('!!'));

  // After a session ends, Enter goes back to setup.
  // On the setup card, Enter starts a session (unless focus is on a select).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (!document.getElementById('view-practice').classList.contains('active')) return;
    
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
    const languageInput = document.getElementById('practice-lang');
    const fileInput = document.getElementById('practice-file');
    const user = userInput.value.trim();
    const language = languageInput.value.trim();
    const level = document.getElementById('practice-level').value.trim();
    const lang = fileInput.value.trim();
    const audioLang = (document.getElementById('practice-audio-lang')?.value ?? '').trim() || undefined;
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
      if (audioLang) body.audio_lang = audioLang;

      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.conjugation_mode ? 'german' : (data.audio_lang || data.lang || '');
      sessionWpm = wpm;
      sessionFastMode = !!data.fast_mode;
      sessionReviewMode = !!data.review_mode;
      setupCard.style.display = 'none';
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
    answerInput.style.display = '';
    nounAnswerInputs.style.display = 'none';

    if (question.review_mode) {
      const q = progress.questions ?? 0;
      const maxQ = progress.max_questions ?? progress.total ?? '?';
      sessionProgress.textContent = `Review · ${Math.min(q + 1, maxQ)}/${maxQ}`;
      sessionGauge.textContent = 'Due today';
      sessionGauge.className = 'gauge';
      sessionType.textContent = 'Review';
      wordDisplay.textContent = question.word_unmasked || question.word;
      wordDisplay.className = `word-display ${question.gender}`;
      definitionLines.innerHTML = '';
      answerBlock.style.display = 'none';
      reviewKeyHint.style.display = 'block';
      setActionButtons(false);
      reviewAudioPromise = speak(questionAudioText(question));
      return;
    }

    reviewKeyHint.style.display = 'none';

    // Full drill mode: auto-enter drill UI immediately.
    if (question.drill_start) {
      const q = progress.questions ?? 0;
      const maxQ = progress.max_questions ?? '?';
      sessionProgress.textContent = `Drilled ${progress.drilled ?? 0}/${progress.total} · Q${Math.min(q + 1, maxQ)}/${maxQ}`;
      sessionGauge.textContent = `${question.gauge} (score: ${formatScore(question)})`;
      sessionGauge.className = `gauge band-${question.band}`;
      sessionType.textContent = 'Drill';
      wordDisplay.textContent = question.word;
      wordDisplay.className = `word-display ${question.gender}`;
      definitionLines.innerHTML = '';
      setActionButtons(true);
      showDrill(question.drill_start);
      return;
    }

    const q = progress.questions ?? 0;
    const maxQ = progress.max_questions ?? '?';
    // Show gauntlet metadata if available
    const gMeta = question.gauntlet;
    if (gMeta) {
      sessionProgress.textContent = `${gMeta.stage_name} · Day ${gMeta.day}/10 · Q${Math.min(q + 1, maxQ)}/${maxQ}`;
      sessionGauge.textContent = `${question.gauge || '○○○'} (score: ${formatScore(question)})`;
      sessionGauge.className = `gauge band-gauntlet`;
      sessionType.textContent = TYPE_LABELS[gMeta.mode] || TYPE_LABELS[question.type] || question.type;
    } else {
      sessionProgress.textContent = `Correct ${progress.correct ?? 0}/${progress.total} · Q${Math.min(q + 1, maxQ)}/${maxQ}`;
      sessionGauge.textContent = `${question.gauge} (score: ${formatScore(question)})`;
      sessionGauge.className = `gauge band-${question.band}`;
      sessionType.textContent = TYPE_LABELS[question.type] || question.type;
    }

    if (question.conjugation) {
      const meta = question.conjugation;
      sessionProgress.textContent = `Conjugations · Stage ${meta.stage}/20 · ${Math.min(q + 1, maxQ)}/${maxQ}`;
      sessionGauge.textContent = `${meta.stage_name} · score ${formatScore(question)}`;
      sessionGauge.className = 'gauge';
      sessionType.textContent = meta.stage_name || 'Conjugation';
    }

    if (question.fast_mode) {
      const fastQuestion = progress.questions ?? 0;
      const fastTotal = progress.max_questions ?? progress.total;
      sessionProgress.textContent = `Fast mode · ${Math.min(fastQuestion + 1, fastTotal)}/${fastTotal} · Correct ${progress.correct ?? 0}`;
      sessionGauge.textContent = 'Mastered';
      sessionGauge.className = 'gauge';
      sessionType.textContent = 'Fast mode';
      wordDisplay.textContent = question.word_unmasked || question.word;
      wordDisplay.className = `word-display ${question.gender}`;
      definitionLines.innerHTML = '';
      if (question.definition && question.definition.length) {
        question.definition.forEach((line) => {
          const div = document.createElement('div');
          div.textContent = line;
          definitionLines.appendChild(div);
        });
      }
      answerBlock.style.display = 'flex';
      answerInput.value = '';
      answerInput.placeholder = question.conjugation ? 'Type full form (e.g. ich habe gemacht)...' : 'Type your answer...';
      setActionButtons(true);
      presentQuestionAudio(question);
      return;
    }

    wordDisplay.textContent = question.word;
    wordDisplay.className = `word-display ${question.gender}`;
    const nounQuestion = Boolean(question.noun_forms);
    answerInput.style.display = nounQuestion ? 'none' : '';
    nounAnswerInputs.style.display = nounQuestion ? 'grid' : 'none';
    nounSingularAnswer.value = '';
    nounPluralAnswer.value = '';
    if (nounQuestion) {
      nounSingularAnswer.placeholder = question.noun_forms.singular || 'Singular';
      nounPluralAnswer.placeholder = question.noun_forms.plural || 'Plural';
    }

    definitionLines.innerHTML = '';
    if (question.type === 'learning' && question.definition.length) {
      question.definition.forEach((line) => {
        const div = document.createElement('div');
        div.textContent = line;
        definitionLines.appendChild(div);
      });
    }

    setActionButtons(true);



    if (question.type === 'production' || question.type === 'known_review') {
      // Band 3: show definition + play audio; user types the word.
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      if (question.definition && question.definition.length) {
        question.definition.forEach((line) => {
          const div = document.createElement('div');
          div.textContent = line;
          definitionLines.appendChild(div);
        });
      }
      answerInput.value = '';
      answerInput.placeholder = question.conjugation ? 'Type full form (e.g. ich habe gemacht)...' : 'Type your answer...';
      presentQuestionAudio(question);
    } else if (['crucible', 'shadows', 'depths', 'void', 'ascension'].includes(question.type)) {
      answerBlock.style.display = 'flex';
      
      if (question.type === 'crucible') {
        wordDisplay.classList.remove('hidden-word');
        wordDisplay.textContent = question.word; 
      } else {
        wordDisplay.classList.add('hidden-word');
        wordDisplay.textContent = '';
      }
      
      definitionLines.innerHTML = '';
      if (question.definition && question.definition.length) {
        question.definition.forEach((line) => {
          const div = document.createElement('div');
          div.textContent = line;
          definitionLines.appendChild(div);
        });
      }
      
      answerInput.value = '';
      const timerMs = { ascension: 5000 }[question.type];
      const ready = () => {
        if (timerMs) {
          answerInput.placeholder = `Type the word (${timerMs / 1000}s timer!)...`;
          clearTimeout(window.gauntletTimer);
          window.gauntletTimer = setTimeout(() => {
            if (currentQuestion === question && !answerInteractionLocked()) sendAnswer('!!TIMEOUT!!');
          }, timerMs);
        } else {
          answerInput.placeholder = 'Type the word...';
        }
        restoreInteractionAfterSpeech();
      };
      if (QUESTION_AUDIO_POLICY[question.type] === 'auto') presentQuestionAudio(question, ready);
      else ready();
    } else if (question.type === 'audio') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      answerInput.value = '';
      presentQuestionAudio(question);
    } else if (question.type === 'spelling') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      presentQuestionAudio(question, () => setTimeout(() => {
        if (currentQuestion === question) wordDisplay.classList.add('hidden-word');
      }, 700));
    } else {
      // learning / default
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      presentQuestionAudio(question);
    }
  }


  function setActionButtons(enabled) {
    btnFlag.disabled = !enabled || sessionFastMode || sessionReviewMode;
    btnMaster.disabled = !enabled || sessionFastMode || sessionReviewMode;
    btnDrill.disabled = !enabled || sessionFastMode || sessionReviewMode;
    btnReveal.disabled = !enabled || sessionFastMode || sessionReviewMode
      || !currentQuestion?.can_reveal;
  }

  function formatScore(question) {
    return Number(question.score).toFixed(1);
  }

  function submitTextAnswer() {
    if (currentQuestion?.noun_forms) {
      sendAnswer('', { singular: nounSingularAnswer.value, plural: nounPluralAnswer.value });
      return;
    }
    const value = answerInput.value;
    // '+' and '?' are always local commands — never submitted as answers.
    if (value.trim() === '+') { runLocalCommand(answerInput, replayAudio); return; }
    if (value.trim() === '?') { runLocalCommand(answerInput, revealWord); return; }
    sendAnswer(value);
  }

  async function runLocalCommand(input, command, focusTarget = input) {
    if (answering) return;
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    await waitForSpeech();
    input.value = '';
    await command();
    answering = false;
    restoreInteractionAfterSpeech();
  }



  function newAttemptId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }

  async function sendAnswer(answer, nounAnswers = null) {
    if (!sessionId || answering || speechPending) return;
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId, answer, noun_answers: nounAnswers,
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

  async function sendReviewMove(direction) {
    if (!sessionId || answering || !sessionReviewMode) return;
    answering = true;
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId, answer: direction,
          question_id: currentQuestion?.question_id, sequence: currentQuestion?.sequence,
          attempt_id: newAttemptId(),
        }),
      });
      if (data.done) {
        showSummary(data.session);
        return;
      }
      await waitForSpeech();
      if (!data.done) renderQuestion(data.question, data.progress);
    } catch (err) {
      showError(practiceError, err.message);
    } finally {
      answering = false;
    }
  }

  function handleAnswerResult(data) {
    if (data.result === 'drill_required') {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(false);
      feedback.textContent = data.message;
      feedback.className = 'feedback incorrect';
      return;
    }
    if (data.result === 'drill_start' || data.result === 'drill_progress') {
      answering = false;
      showDrill(data.drill);
      return;
    }

    if (data.result === 'drilled' && data.drill) {
      showDrill(data.drill, false);
      answering = true;
      setAnswerInputEnabled(false);
      speak(questionAudioText(currentQuestion)).then(() => setTimeout(() => {
        if (data.done) { showSummary(data.session); return; }
        answering = false;
        setActionButtons(true);
        renderQuestion(data.question, data.progress);
      }, 700));
      return;
    }

    if (data.fast_retry) {
      answering = false;
      setAnswerInputEnabled(false);
      setActionButtons(false);
      feedback.textContent = data.message || 'Incorrect. Try again.';
      feedback.className = 'feedback incorrect';
      answerInput.value = '';
      speak(questionAudioText(currentQuestion));
      return;
    }

    if (data.result === 'sentence_retry') {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      feedback.textContent = data.message || 'Incorrect. Try one more time.';
      feedback.className = 'feedback incorrect';
      answerInput.value = '';
      answerInput.focus();
      return;
    }

    if (data.result === 'correct') {
      feedback.textContent = `Correct! '${data.word}'`;
      feedback.className = 'feedback correct';
    } else if (data.result === 'incorrect') {
      feedback.textContent = data.message;
      feedback.className = 'feedback incorrect';
    } else if (data.result === 'mastered' || data.result === 'flagged' || data.result === 'drilled') {
      feedback.textContent = data.message;
      feedback.className = 'feedback info';
    } else if (data.result === 'end') {
      feedback.textContent = 'Session ended.';
      feedback.className = 'feedback info';
    }

    // Feedback is already shown above. Now advance:
    // - audio on: speak the word (server blocks until say finishes), then advance
    // Audio is always enforced. Wait for it to finish, then advance.
    const audioOn = true;
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
    setAnswerInputEnabled(!playAudio);
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
    if (playAudio) presentQuestionAudio(currentQuestion);
    else focusCurrentAnswer();
  }

  function showSummary(session) {
    setAnswerInputEnabled(false);
    sessionCard.style.display = 'none';
    summaryCard.style.display = 'block';
    sessionId = null;
    currentQuestion = null;

    if (session.review_mode) {
      sessionReviewMode = false;
      reviewKeyHint.style.display = 'none';
      const minutes = Math.floor(session.elapsed_seconds / 60);
      const seconds = session.elapsed_seconds % 60;
      document.getElementById('summary-body').innerHTML = '<ul class="summary-list">'
        + `<li>Words reviewed: <strong>${session.practiced}</strong></li>`
        + `<li>Review time: <strong>${minutes}m ${seconds}s</strong></li>`
        + '<li>Scores changed: <strong>No</strong></li></ul>';
      loadSelectedProgress();
      return;
    }

    if (session.fast_mode) {
      sessionFastMode = false;
      const accuracy = session.accuracy == null ? 'N/A' : `${session.accuracy}%`;
      const average = session.avg_seconds_per_item == null ? 'N/A' : `${session.avg_seconds_per_item}s`;
      const minutes = Math.floor(session.elapsed_seconds / 60);
      const seconds = session.elapsed_seconds % 60;
      let html = '<ul class="summary-list">';
      html += `<li>Items reviewed: <strong>${session.practiced}</strong></li>`;
      html += `<li>Correct answers: <strong>${session.correct}</strong></li>`;
      html += `<li>Incorrect answers: <strong>${session.incorrect.length}</strong></li>`;
      html += `<li>Accuracy: <strong>${accuracy}</strong></li>`;
      html += `<li>Total time: <strong>${minutes}m ${seconds}s</strong></li>`;
      html += `<li>Average time per item: <strong>${average}</strong></li>`;
      html += '</ul>';
      document.getElementById('summary-body').innerHTML = html;
      loadSelectedProgress();
      return;
    }

    const minutes = Math.floor(session.elapsed_seconds / 60);
    const seconds = session.elapsed_seconds % 60;
    let html = '<ul class="summary-list">';
    html += `<li>Words practiced: <strong>${session.practiced}</strong></li>`;
    html += `<li>Correct answers: <strong>${session.correct}</strong></li>`;
    html += `<li>Incorrect answers: <strong>${session.incorrect.length}</strong></li>`;
    html += `<li>Words drilled: <strong>${session.drilled}</strong></li>`;
    html += `<li>Session time: <strong>${minutes}m ${seconds}s</strong></li>`;
    html += '</ul>';
    if (session.incorrect.length) {
      html += '<h3>Words you got wrong</h3><ul class="summary-list">';
      session.incorrect.forEach((item) => {
        html += `<li>You wrote '<strong>${escapeHtml(item.attempt)}</strong>', correct was '<strong>${escapeHtml(item.word)}</strong>'</li>`;
      });
      html += '</ul>';
    }
    document.getElementById('summary-body').innerHTML = html;
    loadSelectedProgress();
  }

  // --- Report ---
  ['report-user', 'report-lang', 'report-level', 'report-file'].forEach(id => {
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
    const langInput = document.getElementById('report-file');
    const user = userInput.value.trim();
    const category = categoryInput.value.trim();
    const level = levelInput.value.trim();
    const lang = langInput.value.trim();
    if (!user) {
      showError(reportError, 'Select a user.');
      userInput.focus();
      return;
    }
    if (!lang && (category || level)) {
      showError(reportError, 'Select a word list file, or clear the filters for the full report.');
      (level ? langInput : levelInput).focus();
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

      if (typeof Chart !== 'undefined' && data.reports.length > 0) {
        document.getElementById('chart-card').style.display = 'block';
        const ctx = document.getElementById('activity-chart').getContext('2d');
        if (window.activityChart) window.activityChart.destroy();
        
        // Aggregate by date (last 14 days or so)
        let datesMap = {};
        data.reports.forEach(r => {
            if (r.days) {
                r.days.forEach(d => {
                    if (!datesMap[d.date]) datesMap[d.date] = { practiced: 0, correct: 0 };
                    datesMap[d.date].practiced += d.practiced;
                    datesMap[d.date].correct += d.correct;
                });
            }
        });
        
        const sortedDates = Object.keys(datesMap).sort();
        const chartData = sortedDates.slice(-14); // last 14 days
        
        window.activityChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: chartData,
                datasets: [
                    {
                        label: 'Practiced',
                        data: chartData.map(d => datesMap[d].practiced),
                        backgroundColor: '#cba6f7',
                        borderRadius: 4,
                        barPercentage: 0.6
                    },
                    {
                        label: 'Correct',
                        data: chartData.map(d => datesMap[d].correct),
                        backgroundColor: '#a6e3a1',
                        borderRadius: 4,
                        barPercentage: 0.6
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#cdd6f4', font: { family: '-apple-system, sans-serif' } } }
                },
                scales: {
                    x: {
                        ticks: { color: '#a6adc8' },
                        grid: { display: false }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: { color: '#a6adc8' },
                        grid: { color: '#313244' }
                    }
                }
            }
        });
      } else {
        document.getElementById('chart-card').style.display = 'none';
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
        g1.appendChild(renderDashCard4(dash.velocity, user, lang));
        if (dash.mastery) g1.appendChild(renderDashCard2(dash.mastery));
        resultsEl.appendChild(g1);
        if (dash.nemesis !== null && dash.prediction !== null) {
          const g2 = document.createElement('div');
          g2.className = 'dashboard-grid';
          g2.appendChild(renderDashCard3(dash.nemesis, user, lang));
          g2.appendChild(renderDashCard5(dash.prediction, lang));
          resultsEl.appendChild(g2);
        }
        } catch (_) {}
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

  async function loadWordListStats(user, lang, container) {
    const params = new URLSearchParams({ user, lang });
    // Leitner stats card first
    try {
      const leitnerData = await api(`/api/wordlist/leitner?${params.toString()}`);
      if (leitnerData.leitner) {
        container.appendChild(renderLeitnerCard(lang, leitnerData.leitner));
      }
    } catch (_) {}
    // Full word list table
    try {
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) {
        container.appendChild(renderWordStatsTable(lang, data.words, 'Full Word List'));
      }
    } catch (_) {}
    // Due today table (separate)
    try {
      params.set('due_today', 'true');
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) {
        container.appendChild(renderWordStatsTable(lang, data.words, `Due Today (${data.words.length})`));
      }
    } catch (_) {}
  }

  function renderWordStatsTable(lang, words, caption) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(caption || `Word list: ${lang}`)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Box</th><th>Next Review</th><th>Known Review</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Flagged</th><th>Mastered</th></tr></thead><tbody>';
    words.forEach((w) => {
      const nextReview = w.next_review ?? 'now';
      const knownReview = formatDateTime(w.last_known_review_at);
      html += `<tr${w.active ? '' : ' class="muted"'}><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.band}">${w.gauge}</td>`
        + `<td>${w.leitner_box ?? '—'}</td><td>${nextReview}</td><td>${knownReview}</td>`
        + `<td>${w.times_practiced}</td><td>${w.times_correct}</td><td>${w.times_incorrect}</td>`
        + `<td>${w.times_drilled}</td><td>${w.times_flagged}</td><td>${w.times_mastered}</td></tr>`;
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
    stages.forEach(st => {
      let statusClass = '';
      if (st.id < currentStage) statusClass = 'completed';
      else if (st.id === currentStage) statusClass = 'active';
      else statusClass = 'locked';
      
      let dayText = st.id === currentStage ? `Day ${roadmap.gauntlet.current_day}` : st.days;
      
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
      let tasksCompleted = (isDay2 ? total_tasks : 0) + Math.max(0, total_tasks - remaining_tasks);
      let pct = Math.max(0, Math.min(100, Math.round((tasksCompleted / stageTotalTasks) * 100)));
      
      let displayStage = currentStage;
      if (pct === 100 && displayStage < 5) {
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
    
    let leitnerHtml = `<div class="roadmap-section leitner-section">
      <h3>Lifetime Leitner Maintenance</h3>
      <p class="muted">The spaced-repetition distribution of your mastered words (Box 1 = 1 day, Box 10 = 10 days).</p>
      <div class="leitner-boxes">`;
      
    for (let i = 1; i <= 10; i++) {
      const count = roadmap.leitner_distribution[i] || 0;
      leitnerHtml += `
        <div class="leitner-box ${count > 0 ? 'has-words' : 'empty'}">
          <div class="box-label">Box ${i}</div>
          <div class="box-count">${count}</div>
        </div>
      `;
    }
    leitnerHtml += `</div></div>`;
    
    card.innerHTML = gauntletHtml + leitnerHtml;
    return card;
  }

  // --- Word lists + cascading dropdowns ---

  var allWordLists = [];

  const KNOWN_BASE_LANGS = new Set(['german', 'english']);

  // Generic cascade: populate a chain of selects based on filter functions
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

  // Report cascade: user -> category -> level -> file
  function setupReportCascade() {
    createCascade(
      ['report-user', 'report-lang', 'report-level', 'report-file'],
      (user, category, level) => {
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
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level)
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
    loadUserProgress(
      document.getElementById('practice-user').value,
      document.getElementById('practice-lang').value,
      document.getElementById('practice-level').value,
    );
  }

  async function loadUserProgress(user, category, level) {
    if (!user || !category || !level) { progressEl.style.display = 'none'; return; }
    try {
      const params = new URLSearchParams({ user, category, level });
      const data = await api(`/api/user/progress?${params.toString()}`);
      if (!data.lists || !data.lists.length) { progressEl.style.display = 'none'; return; }
      progressEl.innerHTML = renderProgressWidget(data.lists, category, level);
      progressEl.style.display = 'block';
    } catch (_) {
      progressEl.style.display = 'none';
    }
  }

  function renderProgressWidget(lists, category, level) {
    const labels = {
      english_vocabulary: 'English vocabulary',
      english_sentences: 'English sentences',
      german_vocabulary: 'German vocabulary',
      german_sentences: 'German sentences',
    };
    const title = category && level
      ? `Progress · ${labels[category] || category} · ${level.toUpperCase()}`
      : 'Progress';
    let html = `<div class="card"><h2>${escapeHtml(title)}</h2><div class="progress-list">`;
    lists.forEach((item) => {
      const pct = Math.min(item.progress, 100);
      html += `<div class="progress-row">
        <div class="progress-header">
          <span class="progress-lang">${escapeHtml(item.lang)}</span>
          <span class="progress-pct">${item.progress.toFixed(1)}%</span>
        </div>
        <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
        <div class="progress-meta">
          <span>${item.learned} / ${item.total} learned</span>`;
      if (item.due_today > 0) {
        html += `<span class="due-today-badge">${item.due_today} due today</span>`;
      }
      if (item.to_drill > 0) {
        html += `<span class="drill-badge">${item.to_drill} to drill</span>`;
      }
      html += '</div></div>';
    });
    html += '</div></div>';
    return html;
  }

  // Progress overview card used in the Report view (no specific lang selected).
  function renderProgressOverview(lists) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = '<h3>Word List Progress</h3><div class="progress-list">';
    lists.forEach((item) => {
      const pct = Math.min(item.progress, 100);
      html += `<div class="progress-row">
        <div class="progress-header">
          <span class="progress-lang">${escapeHtml(item.lang)}</span>
          <span class="progress-pct">${item.progress.toFixed(1)}%</span>
        </div>
        <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
        <div class="progress-meta">
          <span>${item.learned} / ${item.total} learned</span>`;
      if (item.due_today > 0) {
        html += `<span class="due-today-badge">${item.due_today} due today</span>`;
      }
      if (item.to_drill > 0) {
        html += `<span class="drill-badge">${item.to_drill} to drill</span>`;
      }
      html += '</div></div>';
    });
    html += '</div>';
    card.innerHTML = html;
    return card;
  }

  function renderLeitnerCard(lang, stats) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<h3>Leitner Flashcard Status &mdash; ${escapeHtml(lang)}</h3>`;

    // Top-level summary: four stat tiles
    html += '<div class="leitner-summary">';
    html += `<div class="leitner-stat-item"><span class="leitner-stat-num">${stats.total}</span><span class="muted">total</span></div>`;
    html += `<div class="leitner-stat-item lsi-learned"><span class="leitner-stat-num">${stats.learned}</span><span class="muted">learned</span></div>`;
    html += `<div class="leitner-stat-item lsi-new"><span class="leitner-stat-num">${stats.never_practiced}</span><span class="muted">new</span></div>`;
    html += `<div class="leitner-stat-item lsi-due"><span class="leitner-stat-num">${stats.due_today}</span><span class="muted">due today</span></div>`;
    html += '</div>';

    // Per-box breakdown
    html += '<div class="leitner-boxes">';
    for (const box of (stats.boxes || [])) {
      const b = box.box;
      const fillPct = stats.total > 0 ? Math.min(100, Math.round(100 * box.total / stats.total)) : 0;
      html += `<div class="leitner-box-row">
        <div class="leitner-box-meta">
          <span>Box ${b}</span>
          <span class="muted" style="font-size:0.78rem">${escapeHtml(box.interval || '')}</span>
        </div>
        <div class="leitner-bar-wrap"><div class="leitner-bar-fill" style="width:${fillPct}%"></div></div>
        <div class="leitner-box-counts">
          <span class="muted">${box.total} word${box.total !== 1 ? 's' : ''}</span>
          ${box.due > 0 ? `<span class="due-today-badge">${box.due} due</span>` : ''}
        </div>
      </div>`;
    }
    html += '</div>';
    card.innerHTML = html;
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

  // Card 1 — Current Status (scoped to selected list)
  function renderDashCard1(overview) {
    const h = Math.floor(overview.total_seconds / 3600);
    const m = Math.floor((overview.total_seconds % 3600) / 60);
    const accuracy = overview.overall_accuracy;
    const r = 38, circ = +(2 * Math.PI * r).toFixed(1);
    const filled = accuracy != null ? +(circ * accuracy / 100).toFixed(1) : 0;
    const arcColor = accuracy == null ? 'var(--surface1)'
      : accuracy >= 85 ? 'var(--green)' : accuracy >= 70 ? 'var(--yellow)' : 'var(--red)';
    const ringLabel = accuracy != null ? `${accuracy}%` : 'N/A';
    return createCard('dash-card-full dash-card-overview', 'Current Status', `
      <div class="stat-tiles">
        ${statTile(overview.due_today, 'Due Today', 'stat-due')}
        ${statTile(`${overview.streak.current}<span class="stat-unit">day${overview.streak.current !== 1 ? 's' : ''}</span>`, 'Current Streak')}
        ${statTile(`${h}h ${m}m`, 'Total Practice Time')}
        <div class="stat-tile stat-ring-tile">
          <svg width="90" height="90" viewBox="0 0 90 90" class="accuracy-ring">
            <circle cx="45" cy="45" r="${r}" fill="none" stroke="var(--surface1)" stroke-width="9"/>
            <circle cx="45" cy="45" r="${r}" fill="none" stroke="${arcColor}" stroke-width="9"
              stroke-dasharray="${filled} ${circ - filled}" stroke-linecap="round"
              transform="rotate(-90 45 45)"/>
            <text x="45" y="45" text-anchor="middle" dominant-baseline="middle"
              fill="${arcColor}" font-size="14" font-weight="700">${ringLabel}</text>
          </svg>
          <span class="stat-label">Overall Accuracy</span>
        </div>
      </div>`);
  }

  // Card 2 — Mastery Funnel (per list)
  function renderDashCard2(mastery) {
    const { learning, familiar, mastered, total } = mastery;
    const lPct = total ? Math.round(100 * learning / total) : 0;
    const fPct = total ? Math.round(100 * familiar / total) : 0;
    const mPct = 100 - lPct - fPct;
    const masteredPct = total ? Math.round(100 * mastered / total) : 0;
    return createCard('dash-card-mastery', 'Mastery Funnel', `
      <div class="stacked-bar">
        ${lPct > 0 ? `<div class="stacked-seg seg-learning" style="width:${lPct}%" title="Learning: ${learning}"></div>` : ''}
        ${fPct > 0 ? `<div class="stacked-seg seg-familiar" style="width:${fPct}%" title="Familiar: ${familiar}"></div>` : ''}
        ${mPct > 0 ? `<div class="stacked-seg seg-mastered" style="width:${mPct}%" title="Mastered: ${mastered}"></div>` : ''}
      </div>
      <div class="stacked-legend">
        <span><span class="legend-dot dot-learning"></span>Learning: <strong>${learning}</strong></span>
        <span><span class="legend-dot dot-familiar"></span>Familiar: <strong>${familiar}</strong></span>
        <span><span class="legend-dot dot-mastered"></span>Mastered: <strong>${mastered}</strong></span>
      </div>
      <p class="muted insight-text">${mastered > 0
        ? `You&rsquo;ve pushed <strong>${masteredPct}%</strong> of your vocabulary into long-term memory.`
        : 'Keep practicing — mastered words will appear here.'
      }</p>`);
  }

  // Card 3 — Nemesis Words (per list)
  function renderDashCard3(nemesis, user, lang) {
    if (!nemesis.length) {
      return createCard('dash-card-nemesis', 'Hardest Words', '<p class="muted">No words with incorrect answers yet — great work!</p>');
    }
    let rows = nemesis.map((w) =>
      `<tr><td>${escapeHtml(w.word)}</td><td>${w.times_incorrect}</td><td>${w.times_correct}</td><td>${w.score.toFixed(1)}</td></tr>`
    ).join('');
    const card = createCard('dash-card-nemesis', 'Hardest Words', `
      <table class="nemesis-table">
        <thead><tr><th>Word</th><th>Wrong</th><th>Right</th><th>Score</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <button type="button" class="secondary" id="btn-drill-nemesis" style="margin-top:0.75rem;">Drill these words</button>`);
    card.querySelector('#btn-drill-nemesis').addEventListener('click', () => {
      const practiceList = allWordLists.find((item) => item.user === user && item.lang === lang);
      if (!practiceList) return;
      const userSelect = document.getElementById('practice-user');
      const languageSelect = document.getElementById('practice-lang');
      const levelSelect = document.getElementById('practice-level');
      const posSelect = document.getElementById('practice-pos');
      const fileSelect = document.getElementById('practice-file');
      userSelect.value = user;
      userSelect.dispatchEvent(new Event('change'));
      languageSelect.value = practiceList.category;
      languageSelect.dispatchEvent(new Event('change'));
      levelSelect.value = practiceList.cefr_level;
      levelSelect.dispatchEvent(new Event('change'));
      posSelect.value = practiceList.pos;
      posSelect.dispatchEvent(new Event('change'));
      fileSelect.value = practiceList.lang;
      fileSelect.dispatchEvent(new Event('change'));
      switchView('practice');
    });
    return card;
  }

  // Card 4 — Velocity & Efficiency (scoped to selected list)
  function renderDashCard4(velocity, user, lang) {
    const card = document.createElement('div');
    card.className = 'card dash-card-velocity';
    const { avg_seconds_per_word, avg_words_per_day_7d, avg_minutes_per_day_7d, benchmark, enough_data } = velocity;
    const benchmarkColors = {
      'Hyper-Learner': 'var(--green)',
      'On Track': 'var(--green)',
      'Building Momentum': 'var(--yellow)',
      'Getting Started': 'var(--yellow)',
    };
    const badgeColor = benchmark ? (benchmarkColors[benchmark] || 'var(--subtext0)') : 'var(--subtext0)';
    const spwText = avg_seconds_per_word != null ? `${avg_seconds_per_word}s` : 'N/A';
    card.innerHTML = `
      <h3>Velocity &amp; Efficiency</h3>
      <div class="velocity-tiles">
        <div class="vel-tile">
          <span class="vel-num">${spwText}</span>
          <span class="vel-label muted">avg. per word</span>
        </div>
        <div class="vel-tile">
          <span class="vel-num">${avg_words_per_day_7d}</span>
          <span class="vel-label muted">words / day (7d avg)</span>
        </div>
        <div class="vel-tile">
          <span class="vel-num">${avg_minutes_per_day_7d}m</span>
          <span class="vel-label muted">practice / day (7d avg)</span>
        </div>
      </div>
      ${benchmark ? `<div class="benchmark-badge" style="color:${badgeColor}; border-color:${badgeColor};">${benchmark}</div>` : ''}
      ${!enough_data ? '<p class="muted" style="margin-top:0.75rem;font-size:0.85rem;">Practice a few more sessions to unlock full velocity stats.</p>' : ''}`;
    return card;
  }

  // Card 5 — Completion Forecast (per list)
  function renderDashCard5(prediction, lang) {
    const card = document.createElement('div');
    card.className = 'card dash-card-forecast';
    if (!prediction.enough_data) {
      const need = prediction.sessions_needed ?? 3;
      card.innerHTML = `
        <h3>Completion Forecast</h3>
        <p class="muted">We&rsquo;re still analyzing your learning speed. Practice for ${need} more session${need !== 1 ? 's' : ''} to unlock your forecast!</p>`;
      return card;
    }
    card.innerHTML = `
      <h3>Completion Forecast &mdash; ${escapeHtml(lang)}</h3>
      <div class="prediction-rows">
        <div class="pred-row">
          <div class="pred-label">Active practice needed</div>
          <div class="pred-value">${prediction.grind_hours}h to score all words 9.0</div>
        </div>
        <div class="pred-row">
          <div class="pred-label">Long-term memory (Box 5)</div>
          <div class="pred-value pred-date">${prediction.box5_date}</div>
        </div>
      </div>
      <p class="muted insight-text">At your current pace, every word in <strong>${escapeHtml(lang)}</strong> will be locked into long-term memory by <strong>${prediction.box5_date}</strong>. Keep it up!</p>`;
    return card;
  }

  // --- Word list editor ---
  const editorUser = document.getElementById('editor-user');
  const editorLang = document.getElementById('editor-lang');
  const editorTableWrap = document.getElementById('editor-table-wrap');
  const editorBody = document.getElementById('editor-body');
  const editorMessage = document.getElementById('editor-message');
  const nounEditor = document.getElementById('noun-editor');
  const nounEditorBody = document.getElementById('noun-editor-body');
  const nounExampleBody = document.getElementById('noun-example-body');
  const editorAddButton = document.getElementById('editor-add-row');
  const editorSaveButton = document.getElementById('editor-save');
  const nounSaveButton = document.getElementById('noun-save');

  document.getElementById('editor-load').addEventListener('click', loadEditor);
  editorAddButton.addEventListener('click', () => addEditorRow({}));
  editorSaveButton.addEventListener('click', saveEditor);

  function setEditorReadOnly(readOnly) {
    editorTableWrap.dataset.readOnly = String(readOnly);
    editorAddButton.disabled = readOnly;
    editorSaveButton.disabled = readOnly;
    nounSaveButton.disabled = readOnly;
    editorBody.querySelectorAll('input').forEach((input) => { input.readOnly = readOnly; });
    editorBody.querySelectorAll('button').forEach((button) => { button.disabled = readOnly; });
    nounEditor.querySelectorAll('input').forEach((input) => { input.readOnly = readOnly; });
  }

  async function loadEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) {
      showError(editorMessage, 'User and language are required.');
      (user ? editorLang : editorUser).focus();
      return;
    }
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist?${params.toString()}`);
      editorBody.innerHTML = '';
      (data.items || []).forEach(addEditorRow);
      const isNoun = data.metadata?.type === 'nouns';
      editorTableWrap.style.display = isNoun ? 'none' : 'block';
      nounEditor.style.display = isNoun ? 'block' : 'none';
      if (isNoun) renderNounRows();
      setEditorReadOnly(Boolean(data.read_only));
      if (data.read_only) {
        editorMessage.innerHTML = '<div class="muted">Tartarus sample material is read-only. Create a personal list to edit it.</div>';
      }
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  function renderNounRows() {
    nounEditorBody.innerHTML = '';
    nounExampleBody.innerHTML = '';
    for (const caseName of ['nominative', 'accusative', 'dative', 'genitive']) {
      const tr = document.createElement('tr');
      tr.dataset.caseName = caseName;
      tr.innerHTML = `<td>${caseName}</td>`
        + '<td><input class="noun-form" data-number="singular" type="text"></td>'
        + '<td><input class="noun-form" data-number="plural" type="text"></td>';
      nounEditorBody.appendChild(tr);
      for (const number of ['singular', 'plural']) {
        const example = document.createElement('tr');
        example.dataset.caseName = caseName;
        example.dataset.number = number;
        example.innerHTML = `<td>${caseName}</td><td>${number}</td>`
          + '<td><input class="noun-sentence" type="text"></td>'
          + '<td><input class="noun-translation" type="text"></td>';
        nounExampleBody.appendChild(example);
      }
    }
  }

  nounSaveButton.addEventListener('click', async () => {
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    const forms = {};
    nounEditorBody.querySelectorAll('tr').forEach((tr) => {
      tr.querySelectorAll('.noun-form').forEach((input) => {
        forms[`${tr.dataset.caseName}_${input.dataset.number}`] = {form: input.value.trim()};
      });
    });
    nounExampleBody.querySelectorAll('tr').forEach((tr) => {
      const form = forms[`${tr.dataset.caseName}_${tr.dataset.number}`];
      form.sentence = tr.querySelector('.noun-sentence').value.trim();
      form.translation = tr.querySelector('.noun-translation').value.trim();
    });
    try {
      const data = await api('/api/noun', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user, lang, noun: document.getElementById('noun-word').value.trim(),
          translation: document.getElementById('noun-translation').value.trim(), ...forms}),
      });
      editorMessage.innerHTML = `<div class="success">Saved noun ${data.item_id}.</div>`;
    } catch (err) { showError(editorMessage, err.message); }
  });

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
      input.readOnly = editorTableWrap.dataset.readOnly === 'true';
      td.appendChild(input);
      tr.appendChild(td);
    });
    const td = document.createElement('td');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'secondary';
    removeBtn.textContent = '×';
    removeBtn.title = 'Remove';
    removeBtn.disabled = editorTableWrap.dataset.readOnly === 'true';
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
      original[0] = tr.querySelector('.editor-def1').value.trim();
      original[1] = tr.querySelector('.editor-def2').value.trim();
      return {
        id: tr.dataset.id,
        word: tr.querySelector('.editor-word').value.trim(),
        definition: original,
        record,
      };
    }).filter((item) => item.word);
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
      const user = document.getElementById('report-user').value;
      if (!user) { alert('Please select a user first'); return; }
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async (ev) => {
        try {
          const payload = { user, data: JSON.parse(ev.target.result) };
          await api('/api/import', { method: 'POST', body: JSON.stringify(payload) });
          alert('Import successful!');
          loadReport();
        } catch (err) {
          alert('Import failed: ' + err.message);
        }
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
