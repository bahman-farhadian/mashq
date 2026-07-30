# AGENTS.md — Tartarus

## Project Overview
Tartarus is a local-first language practice engine for English/German vocabulary, sentences, and German conjugations. JSON files are the version-controlled source of practice material; SQLite stores users, progress, drills, and session history. The CLI (`tartarus.py`) and web UI (`tartarus_web.py`) share the same SQLite database (`data/tartarus.db`, gitignored). Audio requires macOS `say` command; `--no-audio` disables it.

## Key Commands (via Makefile)
```bash
make help                          # Show all commands
make web                           # Start web UI on http://127.0.0.1:9999/
make init user=<name> [list=<name>] # Create user, optionally create personal list
make practice user=<name> list=<name> [opts="--no-audio"]  # CLI practice
make report user=<name> [list=<name>]  # Progress report
```

## Direct CLI Usage
```bash
python3 utils/tartarus.py practice --user <user> --lang <list> [--no-audio] [--fast] [--drill] [--drill-mode] [--instant-drill] [--known-drill-mode] [--wpm N]
python3 utils/tartarus.py report --user <user> [--lang <list>]
python3 utils/tartarus.py init --user <user> [--lang <list>]
```

## Verification Commands
```bash
make help
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile utils/tartarus.py utils/tartarus_web.py utils/conjugation.py
```

## Architecture & Key Files
| Path | Purpose |
|------|---------|
| `utils/tartarus.py` | Core engine: JSON sync, scoring, Leitner, CLI |
| `utils/tartarus_web.py` | Web server (ThreadingHTTPServer), JSON API, session state |
| `utils/conjugation.py` | Deterministic German conjugation curriculum |
| `data/word_lists/` | JSON source material (version-controlled) |
| `data/tartarus.db` | SQLite DB (gitignored): users, progress, drills, history |
| `web/` | Frontend (HTML/JS/CSS) |

## Key Conventions
- **Python stdlib only** — no external dependencies
- **Python 3** only (stdlib: `sqlite3`, `json`, `argparse`, `http.server`, `threading`, `datetime`, `pathlib`, `json`)
- **Database**: `data/tartarus.db` (gitignored); override with `TARTARUS_DB` env var
- **Word list JSON files**: `data/word_lists/{english,german}/{vocabulary,sentences}/{level}/` or personal `data/word_lists/{user}_{list}.json`
- **German nouns**: JSON records with singular/plural for 4 cases (nominative, accusative, dative, genitive)
- **Scoring**: 0.0–9.0 in 0.5 steps; wrong answer never reduces score; starts 9-correct drill; <8.0 progressive masking; ≥8.0 full mask with definition+audio; ≥9.0 enters Leitner box 1–10 (1–10 day intervals)
- **User/language names**: sanitized to lowercase alphanumeric + underscore (`sanitize_name()` in tartarus.py)
- **Audio**: macOS `say` only; `--no-audio` or non-macOS disables

## Web API Endpoints
```
GET  /                           # Serves web/index.html
GET  /api/wordlists              # List available word lists
POST /api/init                   # Create user + optional personal list
POST /api/practice/start         # Start practice session
POST /api/practice/answer        # Submit answer
GET  /api/report?user=&lang=     # Progress report
GET  /api/dashboard?user=&lang=  # Dashboard analytics
GET  /api/wordlist/leitner       # Leitner box stats
POST /api/wordlist               # Save word list
POST /api/noun                   # Save German noun
POST /api/tts                    # Text-to-speech (macOS say)
```

## Practice Modes (CLI & Web)
| Flag | Description |
|------|-------------|
| `--fast` | Review mastered words (oldest first), scores unchanged |
| `--drill` | Every word gets 9-rep drill (no score change) |
| `--drill-mode` | Review high-mistake words, scores unchanged |
| `--instant-drill` | Immediate 9-rep drill after any wrong answer |
| `--known-drill-mode` | Review mastered words (oldest review first) |
| `--no-audio` | Disable macOS `say` |
| `--wpm N` | Speech rate (default 128) |

## Data Schemas
- **Vocabulary/Sentences**: JSON array of objects with `content_id`, `word`, `definition`, `word_frequency`, `position` (optional `noun_forms` for German nouns)
- **German Conjugations**: `data/word_lists/german/tartarus_german_verb_conjugations.json` (808 verbs, full paradigm; validated against `data/schemas/german_conjugations.schema.json`)
- **Sample conjugations (read-only)**: `data/word_lists/german/tartarus_sample_german_conjugations.json` (20 core verbs, indikativ.praesens only)
- **User progress**: SQLite tables `words_{user}_{lang}`, `sessions_{user}`, `conjugations_{user}`

## Testing & Verification
No test framework. Verification is manual via:
```bash
make practice user=demo list=tartarus_sample_english_a1 opts="--no-audio"
make report user=demo
make web
```
Then visit `http://127.0.0.1:9999/`

## Special Commands During Practice
| Key | Action |
|-----|--------|
| `!!` / `Ctrl+C` | End session early, save progress |
| `?` | Reveal answer (before mastery) |
| `+` | Replay audio |
| `!word` | Flag word (score → 1.0) |
| `@word` | Mark known (score → 9.0) |
| `$word` | Start 9-rep drill for current word |

## Common Pitfalls
- **Audio only works on macOS** — use `--no-audio` elsewhere
- **Database path**: set `TARTARUS_DB` for temp/test DBs
- **User/lang names**: must be lowercase alphanumeric + underscore
- **Sample lists are read-only** — creating a personal list hides samples for that user
- **Web server port 9999** — check `lsof -i :9999` if port in use