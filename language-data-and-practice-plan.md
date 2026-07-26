# Language Data And Practice Plan

## Decision

Tartarus should not be JSON-free.

Use a JSON-first model:

- Version-controlled JSON is the canonical source for curated English and German material.
- User-created vocabulary, sentence, and noun lists are also JSON files.
- SQLite stores only users, per-item progress, Leitner state, drill state, and session history.

This keeps language data reviewable in Git, makes dataset changes reproducible,
and keeps mutable learning state separate from curriculum content.

## Core Practice Contract

Vocabulary and sentences use exactly one learning engine.

| Rule | Contract |
|---|---|
| Score range | `0.0` through `9.0` in `0.5` increments |
| New material | score `0.0` |
| Correct answer | add `0.5`, capped at `9.0` |
| Wrong answer | preserve score and start a drill |
| Drill | require 9 consecutive correct answers; completion preserves score |
| Mastery | score `9.0`; enter Leitner box 1 |
| Leitner boxes | 10 boxes; boxes 1 through 10 are due after 1 through 10 days |
| Scores below 8 | progressively mask the answer for both words and sentences |
| Scores 8 and 8.5 | fully mask the answer; show definition and play audio |
| Score 9 review | fully mask the answer; use definition and audio when due |

No score decay. Time away from the application must not erase demonstrated
learning. Leitner scheduling controls review timing after mastery.

## JSON Content Model

Every JSON item must have a stable `id` so SQLite progress survives a wording
correction or an application restart. The database references the JSON item ID,
not its displayed text.

```json
{
  "id": "german-a1-noun-buch-nominative",
  "kind": "vocabulary",
  "language": "german",
  "level": "a1",
  "text": "das Buch, die Bücher",
  "definition": "book",
  "audio_language": "german",
  "frequency": 0
}
```

SQLite progress references `content_id`; it must not use display text as an
identity. This prevents duplicate-form bugs and allows wording corrections
without losing progress.

User-created lists use the same schemas and live in user JSON files. The web
editor writes validated JSON; it does not make the SQLite database the source
of content.

## Seed Material

Keep a compact, reviewed seed dataset for development and manual tests:

- English: 16 vocabulary items and 16 sentences.
- German: 16 vocabulary items and 16 sentences.
- Every seed item has a stable ID, definition/translation, audio language,
  and a frequency value of `0` when frequency is unknown.

The seed data is test material, not a claim of a complete curriculum.

## German Noun Curriculum

Each noun produces four ordered practice items with independent score, drill,
and Leitner state. The learner answers singular and plural together for one
case at a time, so commas are never part of an answer. The fixed order is
nominative, accusative, dative, then genitive.

| Practice order | Case | JSON fields |
|---:|---|---|
| 1 | Nominative | `nominative_singular`, `nominative_plural` |
| 2 | Accusative | `accusative_singular`, `accusative_plural` |
| 3 | Dative | `dative_singular`, `dative_plural` |
| 4 | Genitive | `genitive_singular`, `genitive_plural` |

Every form also has a German example sentence and English translation for
future sentence practice. A correct case answer requires its singular and
plural cells. Noun practice shows only the English definition and German audio;
at score 8 and above, form hints are hidden as well.

```json
{
  "id": "german-a1-noun-buch",
  "kind": "noun",
  "noun": "Buch",
  "definition": "book",
  "word_frequency": 0,
  "nominative_singular": "das Buch",
  "nominative_plural": "die Bücher",
  "accusative_singular": "das Buch",
  "accusative_plural": "die Bücher",
  "dative_singular": "dem Buch",
  "dative_plural": "den Büchern",
  "genitive_singular": "des Buches",
  "genitive_plural": "der Bücher"
}
```

## Noun Authoring UI

The German noun editor uses a 4-by-2 table, not a flat word-list row:

| Case | Singular | Plural |
|---|---|---|
| Nominative | text field | text field |
| Accusative | text field | text field |
| Dative | text field | text field |
| Genitive | text field | text field |

Each of the eight cells has a paired German example sentence and English
translation input. The editor validates all eight forms before it writes the
JSON file. English nouns remain ordinary vocabulary JSON for now; predictable
plural endings are represented directly in the data when needed.

## Database Boundaries

SQLite owns mutable state only:

- users;
- per-user progress (`score`, Leitner box, due date, drill state, counters);
- session history;
- optional source revision metadata linked to JSON item IDs.

The application reads JSON to synchronize lists idempotently. A rerun refreshes
definitions and examples but preserves progress for unchanged item IDs.

## Import And Migration Plan

1. Restore JSON as the canonical source directory and remove DB-backed content
   tables from the runtime path.
2. Define JSON Schema documents for vocabulary, sentences, nouns, and noun
   forms.
3. Add one idempotent JSON synchronizer that materializes only progress rows in
   SQLite.
4. Store source ID and revision in SQLite; reject duplicate IDs within a file.
5. Materialize progress rows lazily when a user first practices an item.
6. Export the current DB seed content to JSON before removing the DB-only
   content tables.
7. Delete obsolete tables only after JSON validation and progress migration
   verification.

## Engine Refactor Plan

1. Replace separate word and sentence scoring functions with one `apply_answer`
   operation over a generic practice item.
2. Represent a pending drill explicitly and block queue advancement until nine
   correct answers are recorded.
3. Implement one deterministic masking function used by every item type.
4. Use a single ten-box due-date function everywhere: CLI, web UI, reports,
   and queries.
5. Make fast, review, and drill modes wrappers around the same item state;
   they must not introduce alternate scoring rules.
6. Add noun sentence unlock checks based on the explicit order field.

## Web UI Plan

1. Restore JSON file discovery for curated and user-owned lists.
2. Keep the current user -> language -> level -> list selection flow.
3. Add a German noun editor with a 4-by-2 case-number table and paired
   sentence/translation inputs for all eight forms.
4. Show validation errors before saving: missing article, missing plural when
   required, duplicate content ID, missing case sentence, or invalid order.
5. Keep English noun tooling out of scope until the German noun flow is stable.

## Quality Gates

Before shipping the refactor:

1. Validate all canonical JSON against schemas.
2. Verify every seed list has exactly 16 vocabulary items or 16 sentences.
3. Verify each noun produces four ordered case-pair practice items and has
   exactly eight case-number example sentences.
4. Test score progression from 0.0 to 9.0 in 18 correct answers.
5. Test wrong answers at scores 0.0, 7.5, 8.0, 8.5, and 9.0; each must retain
   score and require a nine-correct drill.
6. Test all ten Leitner due intervals.
7. Test the same scenarios through both CLI and web UI using a temporary DB.
8. Test idempotent import twice and confirm no duplicate content or progress.

## Deliberately Out Of Scope

- Bulk third-party datasets.
- Automatic linguistic generation or translation APIs.
- English noun case tables.
- Conjugation curriculum changes.

Those features can be added after this content and practice contract is stable.
