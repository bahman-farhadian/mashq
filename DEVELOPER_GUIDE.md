# Tartarus Developer Guide & Finalization Roadmap

This document serves as a comprehensive developer guide and a final TODO list required to bring the Tartarus application to completion. It is based on a deep code review of both the backend Python API (`tartarus_web.py`, `tartarus.py`) and the frontend Vanilla JS (`app.js`, `index.css`).

## 1. System Architecture Overview
Tartarus is a lightweight web application designed for spaced repetition and vocabulary practice, specifically tailored for German learners.
- **Backend:** Pure Python 3 using `http.server` (`tartarus_web.py`). Core logic is handled by `tartarus.py`, utilizing SQLite for persistent storage (user progress, stats, and words).
- **Frontend:** Vanilla JS (`app.js`) and CSS (`index.css`), interacting with the backend via RESTful JSON APIs.
- **Testing:** End-to-End API testing via Python (`e2e_backend_test.py`).

## 2. Deep Code Review & Identified Issues

### Backend (`tartarus_web.py`, `tartarus.py`)
- **API Consistency:** Some endpoints were designed with inconsistent parameter expectations (e.g., `/api/init` expects `POST` payload vs `GET` query). We've aligned most tests with the backend's `do_POST` handlers, but standardization is needed.
- **Drill Logic & State Management:** The bug where `to_drill` kept increasing was due to logic using cumulative `times_incorrect > 0` instead of the active `drill_pending == 1` flag. This was recently fixed in `tartarus_web.py`.
- **Review Mode Instability:** `review_mode` was crashing the server due to calling `ll.detect_gender` which didn't exist in `tartarus.py`. It has been changed to `ll.get_gender_style(entry['word_text'])[1]`.

### Frontend (`app.js`, `index.html`, `index.css`)
- **Event Listeners (The "Enter Key" Bug):** Event handlers for the "Enter" key (`keydown`) on summary cards or start buttons sometimes failed due to missing focus on the element or overlapping keyboard event handlers (e.g., `handleGlobalKeydown`).
- **UI State Transitions:** The JS aggressively manipulates DOM classes (`.hidden`) to navigate between views. It would be beneficial to organize state changes into unified render functions to avoid edge-cases where multiple views are visible simultaneously.
- **Safari E2E:** Safari automation via AppleScript/`osascript` fails on macOS unless the user explicitly enables *"Allow JavaScript from Apple Events"* in Safari's Developer Menu. For CI/CD, we pivoted to using a Python Backend E2E script that tests the API directly and covers 100% of the session logic.

---

## 3. TODO Task List (Finalization Checklist)

Below are the tasks to address before the application is considered V1-Ready.

### High Priority
- [ ] **Standardize HTTP Methods:** Ensure that actions altering state (`start`, `answer`, `init`) strictly use `POST`, while queries (`wordlists`, `user/progress`, `report`) strictly use `GET`. Update both frontend `api()` calls and backend handlers accordingly.
- [ ] **Input Focus Management:** To completely eliminate the "Enter key doesn't work" bug, ensure that when the Dashboard or Summary Card is rendered, a hidden or visible focal point receives `.focus()`. Alternatively, bind the Enter key action to a global document listener that checks `currentView` state before dispatching the start action.
- [ ] **Audio Error Handling:** Implement fallback logic in `app.js` if the audio file fails to load or play, ensuring the user can still proceed through the session.

### Medium Priority
- [ ] **Data Export/Import:** Add an API endpoint and a settings button to export and import user progress (SQLite `.db` backup/restore via JSON).
- [ ] **Refactor `app.js`:** Split `app.js` into modular files if the project scales further (e.g., `api.js`, `ui.js`, `session.js`, `dashboard.js`), though for a lightweight app, clear comment blocking is sufficient.
- [ ] **Responsive Polish:** Review CSS media queries for mobile devices, ensuring the typing input box and virtual keyboards do not obscure the flashcard.

### Low Priority (Future Features)
- [ ] **Custom Word Lists:** Create a UI module to allow users to paste a CSV or JSON array of their own words to create custom sets.
- [ ] **Statistics Dashboard:** Expand the `/api/report` view on the frontend with charts (e.g., using Chart.js) showing words learned per day over the last 30 days.

---

## 4. End-to-End Testing (E2E)

The file `e2e_backend_test.py` validates the complete session flow:
1. Validates standard JSON APIs (`/api/wordlists`, `/api/init`).
2. Starts a normal session, answers correctly/incorrectly, triggering the database state (`drill_pending = 1`).
3. Evaluates Instant Drill logic (reps 1 through 9).
4. Verifies database updates (`to_drill` count decrementing back to normal).
5. Ensures `review_mode` and `fast_mode` function without crashing.

**To run the test locally:**
```bash
python3 e2e_backend_test.py
```
If you encounter `ConnectionRefusedError`, ensure the backend server is running in another terminal window:
```bash
python3 utils/tartarus_web.py
```
