# Tartarus

Tartarus is a local-first language practice engine for user-owned English and
German vocabulary, sentences, and German noun forms. JSON files are the
version-controlled source of practice material; the CLI and localhost web UI
share SQLite only for users, progress, drills, and session history.

## Learning model

Vocabulary and sentences use the same learning contract:

```mermaid
flowchart LR
    N["New\nscore 0"] -->|correct +0.5| L["Learning\nscore 0-7.5"]
    L -->|correct +0.5| P["Production\nscore 8-8.5"]
    P -->|correct +0.5| M["Mastered\nscore 9"]
    N -. wrong .-> D["9-correct drill"]
    L -. wrong .-> D
    P -. wrong .-> D
    D -->|complete| N
    M --> B["Leitner box 1-10"]
    B -->|due| P
```

- Scores range from `0.0` to `9.0` in half-point steps.
- A wrong answer never reduces the score. It starts a drill requiring nine
  consecutive correct answers.
- Scores below `8.0` use progressive masking. Scores `8.0` through `9.0`
  hide the complete answer and rely on the definition and audio.
- Mastery enters Leitner box 1. Boxes represent 1 through 10 days.
- Practice time and answer history are recorded per JSON list.

## German nouns

German nouns are JSON records with explicit singular and plural keys for the
four cases in this order:

1. Nominative
2. Accusative
3. Dative
4. Genitive

Each row in the web editor has singular and plural inputs. The learner practices
one case at a time and may answer with either form. Each JSON form can also
carry its own German example sentence and English translation.

## Quick start

```bash
make help
make web
make init user=demo list=german_vocabulary_a1
make practice user=demo list=german_vocabulary_a1 opts="--no-audio"
make report user=demo
```

The web UI runs at <http://127.0.0.1:9999/>. The database is
`data/tartarus.db`, is ignored by Git, and contains no vocabulary or sentence
content. Set `TARTARUS_DB` to use a temporary or alternate progress database.

## Repository layout

```text
utils/tartarus.py       JSON synchronization, scoring, Leitner, and CLI engine
utils/tartarus_web.py   localhost JSON API and web server
utils/conjugation.py    deterministic German conjugation curriculum
web/                    frontend assets
data/word_lists/        JSON practice material, including user-created lists
data/tartarus.db        ignored users, progress, drills, and history
```

## Verification

The project uses the Python standard library only. Basic checks are:

```bash
make help
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile utils/tartarus.py utils/tartarus_web.py utils/conjugation.py
```

No remote service or SSH connection is required. Audio uses macOS `say` when
available; `--no-audio` disables it for CLI practice.
