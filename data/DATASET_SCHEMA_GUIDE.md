# Tartarus Dataset Schema Guide

This document defines the learning-material contract implemented by the current Tartarus codebase.

Tartarus keeps learning material in JSON and learner progress in SQLite. A dataset file therefore describes **what should be learned**; it does not contain score, Leitner, session, or Gauntlet state.

---

## 1. Current bundled corpus layout

Shared datasets are stored under:

```text
data/word_lists/<language>/<kind>/<level>/<part-of-speech>/<file>.json
```

Current dimensions are:

| Dimension | Current values |
| --- | --- |
| Language | `english`, `german` |
| Kind | `vocabulary`, `sentences` |
| CEFR level | `a1`, `a2`, `b1`, `b2`, `c1`, `c2` |
| Part of speech | `noun`, `verb`, `adjective`, `adverb` |

Current repository snapshot:

```text
1,308 JSON files
81,404 learning items
654 vocabulary files
654 sentence files
```

Every bundled file currently uses exactly these top-level metadata keys:

```text
name
language
kind
level
```

Every bundled item currently uses exactly:

```text
word
definition
```

Bundled files intentionally do **not** require explicit item IDs or numeric frequency fields.

---

## 2. Shared filename convention

The current corpus uses two filename forms.

### Vocabulary

```text
<language>_<part-of-speech>_<level>_partNN.json
```

Examples:

```text
english_noun_a1_part01.json
english_verb_b2_part19.json
german_adjective_c1_part03.json
german_noun_a1_part01.json
```

### Sentences

```text
<language>_sentences_<part-of-speech>_<level>_partNN.json
```

Examples:

```text
english_sentences_noun_a1_part01.json
english_sentences_adjective_b2_part01.json
german_sentences_verb_a2_part01.json
german_sentences_noun_b1_part01.json
```

The filename stem is the **list id** used by Tartarus.

Example:

```text
file:    german_noun_a1_part01.json
list id: german_noun_a1_part01
```

The Web UI derives Part of Speech from the filename when `metadata.pos` is absent.

---

## 3. Canonical JSON shape

A canonical dataset is one JSON object containing `metadata` and `items`.

```json
{
  "metadata": {
    "name": "German Noun A1 Part 01",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "das Mal, die Male",
      "definition": "time (as in 'one time';'many times')\nIch war schon viele Male dort."
    },
    {
      "word": "die Zeit, die Zeiten",
      "definition": "time\nIch habe keine Zeit."
    }
  ]
}
```

At runtime the JSON shape is validated without expanding it into type-specific records.

---

## 4. Metadata contract

### `name`

**Type:** string

**Canonical:** required for bundled/new canonical material.

Human-readable list name shown in UI/descriptors.

```json
"name": "German Noun A1 Part 01"
```

### `language`

**Type:** string

**Canonical values:** `english`, `german` for the bundled corpus.

```json
"language": "german"
```

Custom personal material may technically use another lower-case value, but the current Web category selector is designed around English and German.

### `kind`

**Type:** string

**Canonical values:**

```text
vocabulary
sentences
```

```json
"kind": "vocabulary"
```

The writer normalizes legacy `type` metadata into `kind`. New canonical files should never introduce `type`.

### `level`

**Type:** string

**Canonical bundled values:**

```text
a1 a2 b1 b2 c1 c2
```

Personal material may use `all` when it is not CEFR-specific.

The writer normalizes legacy `cefr_level` metadata into `level`. New canonical files should never introduce `cefr_level`.

### `pos` — optional

**Type:** string

Optional explicit part-of-speech metadata. Bundled files currently omit it because POS is encoded in the path/filename and inferred by the Web descriptor.

Known inferred POS values include:

```text
noun
verb
adjective
adverb
pronoun
preposition
conjunction
interjection
```

If no known POS can be inferred, the Web descriptor uses `all`.

### `ordered` — optional

**Type:** boolean

**Default:** `false`

```json
"ordered": true
```

This marks a list whose source order must be treated explicitly as sequential in modes that consult the ordered flag.

No bundled file in the current repository sets `ordered: true`.

### Unknown metadata

The canonical writer preserves unknown metadata fields instead of deleting them. This lets personal/extensions metadata survive editor round-trips.

---

## 5. Item contract

Each element of `items` is an object.

Canonical minimal item:

```json
{
  "word": "die Zeit, die Zeiten",
  "definition": "time\nIch habe keine Zeit."
}
```

### `word`

**Type:** string

**Required:** yes.

This is the exact target the learner must reproduce.

The validator also reads legacy `text` as a compatibility fallback, but canonical material should write `word`.

### `definition`

**Canonical type:** string; arrays are also accepted by editor/runtime compatibility paths.

The current bundled corpus uses newline-delimited strings.

Runtime normalization converts definition arrays into newline-joined text for practice.

If a legacy record lacks `definition`, the loader can fall back to `translation`, then to the target word; canonical datasets should not rely on those fallbacks.

### `id` — optional in shared material

Bundled datasets currently contain no explicit IDs.

When `id` is absent, Tartarus generates a stable content identity from:

```text
source file path + 1-based item position + word
```

The generated form is:

```text
legacy-<24 hex characters>
```

The generated ID deliberately does **not** include the definition, example, or frequency. Therefore editing only those fields does not create a new learner-progress identity.

However, changing any of these identity coordinates changes the generated ID:

- source file path;
- item position;
- `word` text.

For this reason, moving/reordering/renaming shared items after learners already have progress should be treated as a migration-sensitive operation.

When the Web editor creates a user's first personal copy, the resolved IDs are written explicitly into the personal file and preserved on future saves.

### `word_frequency` — optional

**Type:** non-negative integer

**Default:** `0`

The reader also accepts legacy `frequency` and normalizes it to `word_frequency` when appropriate.

The current bundled corpus does not use numeric frequency fields. Instead, the curated **JSON item order itself is the priority/frequency order** used to introduce equal-score material in normal Tartarus practice.

### Unknown item fields

Unknown item fields are allowed. The Web editor's save path preserves them when the learner changes another field.

This is intentional: the editor should not destroy future/extended schema data it does not understand.

---

## 6. JSON order is pedagogical data

Item order is not cosmetic.

Normal Tartarus selection works as follows for unfinished items:

```text
1. sort by score descending
2. use JSON position as the tie-break for focus-pool membership
3. take at most 16 items
4. shuffle only equal-score groups for presentation
```

For a new dataset every score is `0.0`, so the first 16 JSON items become the initial focus pool.

Therefore dataset authors should order items from the material they want introduced first to the material they want introduced later. In the bundled corpus this is the frequency/priority order.

Do **not** alphabetically re-sort a dataset merely for aesthetics unless that new order is intentionally the learning priority.

---

## 7. Vocabulary multi-form syntax

For vocabulary only, comma-separated forms inside `word` are one target with multiple required forms.

Canonical German noun example:

```json
{
  "word": "das Buch, die Bücher",
  "definition": "book\nDas Buch liegt auf dem Tisch."
}
```

The learner must enter all forms.

Accepted:

```text
das Buch, die Bücher
die Bücher, das Buch
```

Rejected:

```text
das Buch
die Bücher
Buch
```

Matching rules:

- outer whitespace is trimmed;
- whitespace around comma separators is ignored;
- form order is ignored;
- spelling is exact;
- case is exact.

This is the complete noun contract. German nouns are ordinary vocabulary items; Tartarus does not store or generate nominative/accusative/dative/genitive records.

### Do not use comma alternatives in sentence targets

Sentence mode treats the complete `word` as literal text, so commas are sentence punctuation rather than alternative-form separators.

---

## 8. Current bundled definition conventions

The runtime allows multiline definitions and treats them as authored prompt material. The current bundled corpus is highly regular.

### English vocabulary

Exactly two lines:

```text
English definition
English example sentence
```

Example:

```json
{
  "word": "people",
  "definition": "human beings in general\nMany people attended the event."
}
```

### German vocabulary

Exactly two lines:

```text
English meaning
German example sentence
```

Example:

```json
{
  "word": "das Jahr, die Jahre",
  "definition": "year\nEin Jahr hat zwölf Monate."
}
```

Multiple English senses can be separated inside the first line with semicolons when needed.

### English sentences

Exactly two lines:

```text
focus word/phrase
English definition
```

Example:

```json
{
  "word": "Many people attended the event.",
  "definition": "people\nhuman beings in general"
}
```

### German sentences

Exactly three lines:

```text
focus German word/lemma
English meaning
English translation of the sentence
```

Example:

```json
{
  "word": "Ein Jahr hat zwölf Monate.",
  "definition": "das Jahr\nyear\nA year has twelve months."
}
```

These conventions describe the current bundled dataset. They are content-authoring conventions, not separate runtime object types.

---

## 9. Shared material vs personal overrides

Shared corpus files live in the nested hierarchy.

Personal files live directly under `data/word_lists/`:

```text
data/word_lists/<user>_<list-id>.json
```

Example:

```text
data/word_lists/bahman_german_noun_a1_part01.json
```

For user `bahman`, the personal file above overrides the shared list whose id is:

```text
german_noun_a1_part01
```

The shared source remains unchanged and is still visible to other users.

Ownership is determined from the longest valid user-name prefix, so user names that are prefixes of other user names do not leak each other's personal files.

---

## 10. Personal-list creation

`make init user=<name> list=<list-id>` can create an empty personal vocabulary list.

The canonical metadata created for an empty personal list is:

```json
{
  "name": "...",
  "language": "unknown",
  "kind": "vocabulary",
  "level": "all"
}
```

The CLI initializer also writes `"pos": "all"` for a newly created personal list. The Web descriptor treats a missing/unrecognized POS as `all`, so `pos` is optional rather than part of the four-key canonical core.

The Web API's empty-list initializer currently accepts only `vocabulary` as a newly created material type.

Editing an existing shared sentence list can still create a personal sentence override because the original metadata is preserved.

---

## 11. Web editor save contract

The Word Lists editor is designed to be lossless.

When saving:

- shared source bytes are not modified;
- a root-level user override is written;
- existing explicit IDs are preserved;
- generated IDs become explicit on the first personal save;
- item order is preserved;
- multiline definitions are preserved;
- unknown item fields are preserved;
- unknown metadata fields are preserved;
- legacy metadata aliases are normalized to canonical `kind` / `level`;
- writes are atomic through a same-directory temporary file + replacement.

This means an editor change to one definition should not silently erase unrelated schema fields or reset learner identity.

---

## 12. Custom import contract

`save_custom_list()` accepts either:

1. a complete object containing `metadata` and `items`; or
2. a raw items array, which is wrapped in canonical metadata.

For custom imports, every item must already have an explicit stable `id`.

This stricter rule prevents an imported file from receiving identities that depend on an accidental temporary import path/order.

---

## 13. Identifier rules

User names and list ids are sanitized to lowercase and may contain only:

```text
a-z
0-9
_
-
.
!
```

Equivalent regular expression:

```regex
^[a-z0-9_\-\.!]+$
```

Rejected examples include whitespace, path separators, quotes, and traversal syntax.

These identifiers are used in root personal filenames and SQLite table names, so the restriction is part of the persistence contract.

---

## 14. List discovery rules

For a requested `(user, list-id)`, Tartarus resolves material in this order:

1. `data/word_lists/<user>_<list-id>.json` personal override;
2. one unambiguous shared file named `<list-id>.json` anywhere below `data/word_lists/`.

If no shared file exists, lookup fails.

If more than one shared file has the same filename stem, lookup fails as ambiguous rather than choosing one arbitrarily.

This makes globally unique shared filename stems an important dataset requirement.

---

## 15. Source changes and learner progress

`sync_word_list()` maps JSON `content_id` values to SQLite progress rows.

- New content IDs receive new progress rows.
- Existing IDs keep their progress.
- Material no longer present in JSON is marked `active = 0` rather than deleted.
- Reintroduced material with the same ID can reuse its progress row.

For shared no-ID material, remember that changing path, item position, or `word` changes the generated identity. Definition-only edits do not.

### Safe edits after release

Generally safe:

- correcting a definition;
- correcting an example sentence;
- adding an unknown metadata/item field;
- changing optional frequency metadata;
- formatting/indentation changes that do not alter JSON values.

Potentially identity-changing:

- changing `word`;
- reordering existing no-ID items;
- moving/renaming the source file;
- splitting one item into multiple items;
- merging multiple items.

If identity stability matters for existing learners, persist explicit IDs before making structural edits or plan a progress migration.

---

## 16. Formatting recommendations

Tartarus parses normal JSON and does not depend on one-line item formatting. For human review and clean diffs, use:

- UTF-8 without BOM;
- 2-space indentation;
- valid JSON, not JSON5;
- no comments or trailing commas;
- canonical metadata names;
- one logical item per object;
- `\n` inside a JSON string for multiline definitions;
- stable item order.

Recommended style:

```json
{
  "metadata": {
    "name": "German Noun A1 Part 01",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "die Zeit, die Zeiten",
      "definition": "time\nIch habe keine Zeit."
    },
    {
      "word": "das Jahr, die Jahre",
      "definition": "year\nEin Jahr hat zwölf Monate."
    }
  ]
}
```

---

## 17. Validation checklist for a new shared dataset

Before adding a file, verify all of the following:

- [ ] File is under `data/word_lists/<language>/<kind>/<level>/<pos>/`.
- [ ] Filename follows the vocabulary or sentences convention.
- [ ] Filename stem is globally unique under `data/word_lists/`.
- [ ] `metadata` is an object.
- [ ] `metadata.name` is meaningful.
- [ ] `metadata.language` matches the directory language.
- [ ] `metadata.kind` is `vocabulary` or `sentences` and matches the directory.
- [ ] `metadata.level` matches the directory level.
- [ ] `items` is an array.
- [ ] Every item has a non-empty `word`.
- [ ] Every canonical item has a meaningful `definition`.
- [ ] Item order represents intended learning/frequency priority.
- [ ] Multi-form vocabulary targets include every form the learner must type.
- [ ] Sentence commas are ordinary punctuation.
- [ ] No duplicate explicit IDs exist if IDs are supplied.
- [ ] Existing no-ID items are not casually reordered after learner progress exists.
- [ ] File is UTF-8 and valid JSON.

Then run the unified suite:

```bash
PYTHONPYCACHEPREFIX=/tmp/tartarus-pycache \
python3 utils/test_tartarus.py -v
```

---

## 18. Schema examples by dataset family

### English vocabulary

```json
{
  "metadata": {
    "name": "English Noun A1 Part 01",
    "language": "english",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "people",
      "definition": "human beings in general\nMany people attended the event."
    }
  ]
}
```

### German vocabulary noun

```json
{
  "metadata": {
    "name": "German Noun A1 Part 01",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "das Buch, die Bücher",
      "definition": "book\nDas Buch liegt auf dem Tisch."
    }
  ]
}
```

### English sentence

```json
{
  "metadata": {
    "name": "English Sentences Noun A1 Part 01",
    "language": "english",
    "kind": "sentences",
    "level": "a1"
  },
  "items": [
    {
      "word": "Many people attended the event.",
      "definition": "people\nhuman beings in general"
    }
  ]
}
```

### German sentence

```json
{
  "metadata": {
    "name": "German Sentences Noun A1 Part 01",
    "language": "german",
    "kind": "sentences",
    "level": "a1"
  },
  "items": [
    {
      "word": "Ein Jahr hat zwölf Monate.",
      "definition": "das Jahr\nyear\nA year has twelve months."
    }
  ]
}
```

---

## 19. What does not belong in dataset JSON

Do not store learner progress inside material files.

These belong to SQLite, not JSON:

```text
score
last_practiced
active
times_practiced
times_correct
times_incorrect
times_drilled
times_mastered
leitner_box
last_known_review_at
Gauntlet day/stage/session state
```

Do not add the removed review-era concepts as material requirements:

```text
noun_forms
noun_case
drill_pending
times_flagged
stage_reached
```

The material model is intentionally simple: a stable target, its prompt/definition, canonical metadata, and source order.
