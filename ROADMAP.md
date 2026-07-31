# The Tartarus Pedagogical Roadmap: A 10-Day Descent

The goal of this architectural shift is to transform Tartarus from a scattered feature-set into a highly opinionated cognitive crucible. A user no longer selects how to practice; they simply start a session, and the backend determines the exact 16-question payload they need to survive.

Tartarus is designed as a strict language torture dungeon—but it must also be psychologically viable. 

## The Brutal Rules of Tartarus

1. **The Cost of Entry (The Forging):** It takes exactly 18 correct answers to drive a word to Score 9.0. For a 64-word list, this means 72 perfect 16-question sessions. This is not a bug; this is the intense, multi-hour cognitive cost required to enter the 10-day spaced repetition pipeline.
2. **Lifting More Weight (The "9 Women" Rule):** As the saying goes, *you cannot produce a baby in one month by getting nine women pregnant.* Neuroplasticity requires sleep and the actual passage of time. Therefore, you cannot compress 10 days of spaced repetition into 1 day. Once a dataset's daily quota is cleared, that specific dataset is firmly time-locked until the next calendar day. If a hyper-motivated user wishes to lift more weight, they must start or practice *other* datasets in parallel. You cannot cheat time.
3. **The Voided Session (No Rage Quits):** If a user is trapped in a 9-rep Instant Drill and closes the browser, the session is entirely dropped. No progress is saved. Like the brutality of real life, if you walk away mid-struggle, you get nothing. 
4. **Never Move Backward (Localized Torture):** To prevent infinite, demoralizing loops, a word *never* drops back to a previous stage. The punishment for a mistake is intense but localized: you are locked into an immediate, inescapable **9-repetition Instant Drill**. Once you pay the toll, you move forward.
5. **The Unforgiving Senses:** The core of Tartarus is typing, listening, and reading simultaneously. Audio is **never** muted in any stage. If a user cannot listen to the audio, they cannot practice. 

---

## The Dual-Track Architecture
Tartarus operates on two completely independent, parallel tracks. A user must follow both to achieve true neuroplasticity:
1. **The 10-Day Gauntlet (Dataset Level):** This is the intense, highly-structured 10-day roadmap defined below. Its purpose is to forcefully forge a brand new 64-word list into long-term memory.
2. **The Leitner Maintenance (Word Level):** This is the lifetime spaced-repetition track. Independent of the 10-day plan, every word ever learned is tracked in Leitner boxes (1 through 10). If a word decays after 30 days, 60 days, or a year, it comes due for a maintenance review. 

**Backend Responsibility:** The backend is solely responsible for following and managing both tracks. The user does not select what to study. When a user clicks "Start Session," the backend priority queue evaluates the entire database. It will dynamically determine whether the user must pay their dues to the lifetime Leitner Maintenance track across all lists, or if they need to push forward in the 10-Day Gauntlet for a specific list. The backend seamlessly stitches these tracks together into the 16-question payload.

---

## The Roadmap: Prerequisite + 5 Stages

A standard dataset contains 64 words. Sessions strictly contain 16 questions. 

### Prerequisite: The Forging (Day 0)
- **Goal:** Drive every word in the 64-word dataset from Score 0.0 to 9.0 (Mastery) to unlock the 10-Day Plan, and simultaneously place the word into **Leitner Box 1** for lifetime tracking.
- **Activity:** Progressive masking. Every correct answer adds +0.5 points (18 correct answers per word required). The user grinds 16-question sessions until all 64 words hit 9.0.
- **The Toll:** Any mistake triggers the 9-rep Instant Drill.

### The 10-Day Ascent (2 Days per Stage)
Once words hit 9.0, they enter the 10-day spaced repetition pipeline. 

#### Stage 1: The Crucible (Days 1-2)
- **Goal:** Prove short-term retention.
- **Activity:** **Audio-Only Fast Mode.** The training wheels come off. The user must type the word entirely from memory using *only* the audio prompt.
- **The Toll:** A mistake triggers an immediate 9-rep drill. Once paid, the word advances.

#### Stage 2: The Shadows (Days 3-4)
- **Goal:** Visual recall under heavy constraint.
- **Activity:** **Known Drill (Heavy Masking).** The word is heavily visually masked (e.g., only the first letter is visible) to force deep cognitive retrieval. Audio plays normally.
- **The Toll:** 9-rep Instant Drill on failure.

#### Stage 3: The Depths (Days 5-6)
- **Goal:** Contextual and speed synthesis.
- **Activity:** **Rapid Fire.** The user sees the translation and hears the audio, but they must type the answer without hesitating. 
- **The Toll:** 9-rep Instant Drill on failure.

#### Stage 4: The Void (Days 7-8)
- **Goal:** Complete inversion of neuro-pathways.
- **Activity:** **Reverse Translation.** Instead of hearing/seeing the target language and typing the target language, the user is presented only with the *native* definition and must produce the target language from scratch. 
- **The Toll:** 9-rep Instant Drill on failure.

#### Stage 5: Ascension (Days 9-10)
- **Goal:** The final endurance test.
- **Activity:** **The Ultimate Review.** Words surviving to this stage are deeply embedded in neuroplasticity. The user does one final audio-only review (Fast Mode) for the entire 64-word list.
- **Reward:** Surviving Stage 5 marks the dataset as "Permanently Mastered." 

---

## Architectural & UI Implementation (Development Path)

To support this highly structured 10-day roadmap, significant backend and frontend overhauls are required. The legacy piecemeal practice features will be removed.

### Component 1: Database Schema Migration
We must introduce dataset-level progress tracking.

**`utils/tartarus.py` modifications:**
- Add a new table `dataset_progress` to track a user's global state for a specific word list.
  - Columns: `user`, `lang`, `current_stage` (0-5), `current_day` (0-10), `sessions_done_today` (INTEGER), `last_practice_date` (DATE).
  - Primary Key: `(user, lang)`
- Update the `ensure_word_table()` schema:
  - Add `stage_reached` (INTEGER) to track individual word progress (to handle stragglers if needed, or simply rely on the global dataset tracker).
  - Remove legacy columns (`drill_pending`, `last_fast_review_at`, etc.) that are no longer relevant to the automated pipeline since drills are now synchronous and inescapable.
  - Implement a migration block that uses `ALTER TABLE` to preserve existing scores (`0.0` to `9.0`) but maps them into the new `dataset_progress` paradigm.

### Component 2: Backend Logic & Prioritization Queue
The backend will automatically generate the 16-question payload based on the `dataset_progress` state.

**`utils/tartarus.py` (Session Generation):**
- **Rewrite `generate_session()`**:
  - **Check `dataset_progress`**: Determine what Day the user is currently on for the requested `lang`.
  - **Day 0 (The Forging):** Query 16 words with score < 9.0. Session format is standard learning.
  - **Days 1-2 (The Crucible):** Query 16 words. Session format is automatically forced to Audio-Only Fast Mode.
  - **Days 3-4 (The Shadows):** Session format forced to heavy visual masking.
  - **Days 5-6 (The Depths):** Session format forced to rapid-fire context.
  - **Days 7-8 (The Void):** Session format forced to exhaustive mixed recall.
  - **Days 9-10 (Ascension):** Final audio-only review.
- **Update Quotas**: Once a user successfully completes four 16-question sessions (64 words) for a given Day, `current_day` increments.

**`utils/tartarus_web.py` (API Routing):**
- **Rewrite `/api/practice/start`:** Remove all URL/body parameters related to manual mode toggling (`fast_mode`, `review_mode`, `drill_all`). It only needs `user` and `lang`. The API will append metadata to the response indicating the current `stage` and `day` so the frontend knows how to render the UI.
- **Rewrite `/api/practice/answer`:** Force `drill_start` on *any* mistake across *any* stage. Do not allow the user to escape the session with pending drills.

### Component 3: Frontend Simplification & Progress UI
The frontend must be stripped of manual toggles and updated to heavily visualize the 10-day roadmap.

**`web/index.html`:**
- Delete all feature checkboxes from the Setup Card (Fast Mode, Mistake Drill, Drill All, etc.).
- Rewrite the `practice-progress` dashboard widget. It will now display a unified progress bar: "Stage X: Day Y of 10" and a fraction for the daily quota (e.g., "Session 1 of 4 completed today").

**`web/app.js`:**
- Remove all state variables related to checkboxes.
- Modify the `startSession` function to accept the `stage` and `day` metadata from the backend, and dynamically adjust the CSS/DOM (e.g., hiding translations, hiding visual clues) based on the current stage's specific cognitive constraints (Audio Only, Masked, etc.).
- Ensure that if the backend returns `drill_start`, the frontend locks the user into the 9-rep instant drill without offering an exit until completed.
