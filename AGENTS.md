# Tartarus Agent Instructions

## Project Overview
Local-first language practice system (English/German). CLI and web UI share one SQLite database and scoring engine. Standard-library Python only — no package manager, venv, or requirements file.

## Conjugation Determinism Contract
`utils/conjugation.py` implements a fixed 20-stage German curriculum. This is the most error-prone surface for agents. Full spec: `temp_conjuction_structure.md`. Core rules:
- 20 stages in fixed order; progress cannot skip or reorder them
- Six-person forms unlock in strict order: `ich → du → er/sie/es → wir → ihr → sie/Sie` (array indices 0→5)
- Imperative order: `du → ihrr → Sie → wir`
- Later pronouns/verbs never introduced early, even with high scores
- Curriculum order is explicit (stage → verb → pronoun → exercise), not derived from JSON iteration, DB row order, alphabet, frequency, or randomness
- Review order uses deterministic SQL `ORDER BY`, not random tie-breaking
- When existing Tartarus behavior conflicts, the conjugation contract wins for the conjugation track

## Commands
```bash
make help                         # Show all commands
make web                          # Start web UI at http://127.0.0.1:9999 (long-running, Ctrl+C to stop)
make practice user=<name> list=<lang> opts="--flags"   # CLI practice
make report user=<name> [list=<lang>]  # Progress report
make init user=<name> list=<name>      # Reset/create user word list
make video opts="--user x --lang y --number N"  # Video generator
```
- `user` and `list` are required for `practice` and `init`
- `opts` passes through to `utils/tartarus.py` subcommand
- Web server (`utils/tartarus_web.py`) has no `--help`; it starts immediately on import

## Verification (no test suite exists)
```bash
python3 -m compileall -q utils              # Syntax check all modules
python3 utils/tartarus.py --help             # CLI help
python3 utils/tartarus.py practice --help
python3 utils/tartarus.py report --help
python3 utils/tartarus.py init --help
python3 utils/make_tartarus_video.py --help
```

## Architecture
- `utils/tartarus.py` — shared engine: scoring, Leitner, decay, DB sync, CLI entry
- `utils/tartarus_web.py` — localhost JSON API + `web/` frontend; imports `tartarus` as `ll`; binds `127.0.0.1:9999`
- `utils/conjugation.py` — deterministic curriculum, separate 20-box Leitner
- `utils/make_tartarus_video.py` — optional video tool (needs `ffmpeg` + macOS `say`)
- `data/tartarus.db` — single SQLite DB (gitignored, mutable state)
- `data/word_lists/` — selectable practice files by language/type/CEFR level
- `data/sources/` — raw JSON + Goethe PDFs (not for direct practice)

## Operational Notes
- `data/tartarus.db` is recreated/migrated lazily on first use; `ALTER TABLE` adds columns as needed
- Do not hand-edit the database; use `make init` to reset
- User/lang names must match `^[a-z0-9_]+$` (validated by `sanitize_name`)
- Conjugation practice (`german_conjugations`) rejects `--fast`, `--drill*`, `--known-drill-mode`, `review_mode`, `level_mode`
- Sentence lists (`*_sentences*`) disable drill modes; scoring is integer 0→9 (9 correct = mastery)
- Word frequency controls introduction order (high first); missing frequency → length-based tiebreaker
- Audio on macOS via `say`; `--no-audio` disables; `--audio-lang german` forces German voice for sub-list names
- Keep sessions <20 words for response speed; avoid `--drill` with conjugation tracks

## Emergency Reset
```bash
make init user=<user> list=german_conjugations
python3 utils/tartarus.py --rebuild-index-general
python3 utils/conjugation.py --rebuild-all
```