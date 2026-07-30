# AGENT.md — Tartarus Project Guide for AI Agents

## Project Overview
Tartarus is a local-first language practice engine for English and German vocabulary, sentences, German noun declensions, and German verb conjugations. 

- **Version-Controlled Source Material**: JSON files located in `data/word_lists/` store all vocabulary, sentence, and conjugation data.
- **Progress & History Database**: SQLite database at `data/tartarus.db` (gitignored, configurable via `TARTARUS_DB`) stores users, word progress, Leitner box states, active drills, and session logs.
- **User Interfaces**:
  - **CLI**: `utils/tartarus.py` (command-line practice engine, reports, and data management).
  - **Web UI**: `utils/tartarus_web.py` (lightweight `ThreadingHTTPServer` backend serving single-page application from `web/`).
- **Audio Engine**: Uses macOS native `say` command for text-to-speech. CLI accepts `--no-audio` to disable.

---

## Architecture & Repository Map

```
tartarus/
├── Makefile                     # Shortcut commands for running web, practice, report, init
├── README.md                    # Project overview & architecture diagrams
├── AGENTS.md                    # Project documentation for AI agents
├── AGENT.md                     # Project documentation for AI agents (alias)
├── data/
│   ├── tartarus.db              # SQLite DB (gitignored; user progress, sessions, drills)
│   ├── schemas/                 # JSON Schemas for validation
│   │   └── german_conjugations.schema.json
│   └── word_lists/              # Practice material JSON files
│       ├── english/             # Bundled English sample lists (A1, A2, etc.)
│       └── german/              # Bundled German sample lists & full verb conjugations
├── utils/
│   ├── tartarus.py              # Core engine: DB sync, scoring, Leitner, drills, CLI
│   ├── tartarus_web.py          # Python stdlib HTTP server & REST API
│   └── conjugation.py           # Deterministic German verb conjugation curriculum
└── web/                         # Web frontend (Vanilla HTML / CSS / JS)
    ├── index.html               # Main Web UI single page app
    ├── style.css                # Custom CSS styling (dark mode, layout, components)
    └── app.js                   # Frontend SPA logic & TTS queue
```

---

## Technical Stack & Constraints

- **Python Standard Library Only**: No external PyPI or pip dependencies (`sqlite3`, `json`, `argparse`, `http.server`, `threading`, `datetime`, `pathlib`, `subprocess`).
- **Frontend**: Vanilla HTML5, CSS3, JavaScript (ES6+). No npm/node dependencies, bundlers, or external CSS frameworks.
- **Server**: `http.server.ThreadingHTTPServer` running by default on `http://127.0.0.1:9999/`.
- **Database**: SQLite3 (`data/tartarus.db`). Environment variable `TARTARUS_DB` can override the path (useful for testing).
- **Text-to-Speech**: macOS native `say` command. On non-macOS systems or when `--no-audio` is set, TTS operations are skipped gracefully.

---

## Core Key Commands

### Makefile Targets
```bash
make help                          # Display available Makefile commands
make web                           # Launch Web UI on http://127.0.0.1:9999/
make init user=<name> [list=<name>] # Create a user; optionally add a personal list
make practice user=<name> list=<name> [opts="--no-audio"] # Run CLI practice session
make report user=<name> [list=<name>] # Display user progress report
```

### Direct CLI Commands
```bash
# Start CLI Practice
python3 utils/tartarus.py practice --user <user> --lang <list> [OPTIONS]

# CLI Flags:
#   --no-audio          Disable audio playback
#   --fast              Review mastered words (oldest review first), scores unchanged
#   --drill             Run 9-rep drill for all words in session
#   --drill-mode        Review high-mistake words without changing score
#   --instant-drill     Trigger 9-rep drill immediately upon any wrong answer
#   --known-drill-mode  Review mastered words with drill mode
#   --wpm <N>           Set speech rate (default: 128 WPM)

# Generate Progress Report
python3 utils/tartarus.py report --user <user> [--lang <list>]

# Initialize User / Create Personal List
python3 utils/tartarus.py init --user <user> [--lang <list>]
```

---

## Learning Mechanics & Algorithms

### Scoring & Leitner System
- **Score Range**: `0.0` to `9.0` in `0.5` steps.
- **Wrong Answers**: NEVER decrease a word's score. Instead, a wrong answer places the word into a 9-correct drill (requiring 9 consecutive correct repetitions to clear).
- **Progressive Masking**:
  - `Score < 8.0`: Progressive character masking based on current score level.
  - `8.0 <= Score < 9.0`: Complete mask; user relies on definition prompt and audio.
  - `Score >= 9.0`: Word achieved **Mastery** and enters **Leitner Box 1** (intervals 1 to 10 days).
- **Leitner Review**: When a mastered word is due based on its Leitner box interval, it is scheduled for review.
- **Score Decay**: Inactive words undergo automated score decay during list sync.

### German Noun Declension
German nouns in JSON lists specify all 4 case forms (Nominative, Accusative, Dative, Genitive) in both Singular and Plural:
- Learners practice one case at a time and may provide either singular or plural form.
- Rendered with gender-based color coding in UI (`der` = masculine, `die` = feminine/plural, `das` = neuter).

### German Conjugation Curriculum (`utils/conjugation.py`)
- Structured into 20 progressive curriculum stages.
- Covers full verb paradigm: Indikativ (Präsens, Präteritum, Perfekt, Plusquamperfekt, Futur I, Futur II), Konjunktiv I & II, Imperativ, Infinitiv, Partizip I & II.
- Dataset: `tartarus_german_verb_conjugations.json` (808 verbs) and `tartarus_sample_german_conjugations.json` (20 core sample verbs).
- **Single-Stage Practice Sessions**: Supported via API parameter `stage` in `POST /api/practice/start`.

---

## Web API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | `GET` | Serves `web/index.html` static single-page application |
| `/api/wordlists` | `GET` | Returns list of available vocabulary, sentence, and conjugation lists |
| `/api/init` | `POST` | Initialize user and optional personal word list |
| `/api/practice/start` | `POST` | Start practice session (JSON payload: `user`, `lang`, optional `stage`, `drill`, `fast`, etc.) |
| `/api/practice/answer` | `POST` | Submit an answer for the active question |
| `/api/report` | `GET` | Retrieve user progress statistics (`?user=<name>&lang=<list>`) |
| `/api/dashboard` | `GET` | Retrieve aggregated dashboard analytics |
| `/api/wordlist/leitner` | `GET` | Retrieve Leitner box distribution statistics |
| `/api/wordlist` | `POST` | Create or update a custom JSON word list |
| `/api/noun` | `POST` | Save/edit German noun declension entries |
| `/api/tts` | `POST` | Execute macOS `say` TTS command |

---

## Web UI Design & Audio Mechanics

- **Concurrent Typing & Audio**: The input field remains enabled and unfocused/focused while TTS audio plays. Learners can type while audio is active.
- **Serialized Audio Queue**: Web UI uses a JavaScript promise chain to queue TTS requests sequentially, preventing overlapping `say` processes.
- **Immediate Question Progression**: Submitting an answer instantly evaluates results and loads the next question without blocking on background audio playback.

---

## Special In-Practice Commands (CLI)

| Key Command | Action |
|-------------|--------|
| `!!` or `Ctrl+C` | Quit session immediately and save current progress |
| `?` | Reveal answer (for unmastered words) |
| `+` | Replay TTS audio |
| `!word` | Flag word (resets score to 1.0) |
| `@word` | Mark word as known (sets score to 9.0) |
| `$word` | Manually initiate 9-rep drill for current word |

---

## Verification & Testing

Verify Python syntax and standard library compliance without external packages:
```bash
make help
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile utils/tartarus.py utils/tartarus_web.py utils/conjugation.py
```

Manual integration test:
```bash
# Test CLI practice with sample list
make practice user=demo list=tartarus_sample_english_a1 opts="--no-audio"

# Check report generation
make report user=demo

# Verify Web server startup
make web
```

---

## Key Development Conventions

1. **Strict Python Stdlib Constraint**: Do not introduce `pip` dependencies (e.g. `requests`, `flask`, `pydantic`). Always use standard library equivalents.
2. **Name Sanitization**: All user names and list names are sanitized to lowercase alphanumeric + underscores (`sanitize_name()` in `tartarus.py`).
3. **Sample List Isolation**: Sample lists (`tartarus_sample_*`) are read-only templates. Creating a personal user list hides sample lists and retires sample user progress for that user.
4. **Database Safety**: Never commit `data/tartarus.db`. Use `TARTARUS_DB` env var when running isolated tests.
