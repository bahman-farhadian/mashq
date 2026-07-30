# Tartarus Dataset Schema Guide

Tartarus is a data-driven learning application. Content is fed to the engine via simple, standard JSON dataset files placed in `data/word_lists/`.

---

## Dataset Format Specification

Every dataset JSON file contains a top-level `"metadata"` object and an `"items"` array.

```json
{
  "metadata": {
    "name": "German Personal Pronouns",
    "language": "german",
    "kind": "pronouns",
    "ordered": true
  },
  "items": [
    { "word": "ich", "definition": "I", "index": 1 },
    { "word": "du", "definition": "you (informal singular)", "index": 2 },
    { "word": "er", "definition": "he", "index": 3 }
  ]
}
```

---

## Field Reference

### Top-level Metadata (`metadata`)
- `name` *(string, required)*: Display name for the dataset.
- `language` *(string, required)*: Language identifier (e.g. `"german"`, `"english"`).
- `kind` *(string, optional)*: Content type (e.g., `"pronouns"`, `"numbers"`, `"verbs"`, `"nouns"`, `"vocabulary"`).
- `ordered` *(boolean, optional, default: false)*: Set to `true` for sequential lists (numbers, days of the week, pronouns) where items must be practiced in strict file order.

### Items Array (`items`)
Each item in `"items"` contains:
- `word` *(string, required)*: The target German text the learner must type (e.g., `"das Buch"`, `"ich habe gemacht"`, `"Montag"`).
- `definition` *(string, required)*: The prompt or translation displayed to the learner (e.g., `"the book"`, `"I have made / done"`, `"Monday"`).
- `index` *(integer, optional)*: Sequence position for ordered lists.

---

## Sample Datasets Provided

- **Pronouns**: `data/word_lists/german/german_pronouns.json`
- **Numbers**: `data/word_lists/german/german_numbers_a1.json`
- **Days & Months**: `data/word_lists/german/german_days_months.json`
- **Verbs (Präsens)**: `data/word_lists/german/german_verbs_praesens_stage1.json`
- **Verbs (Perfekt)**: `data/word_lists/german/german_verbs_perfekt_stage2.json`
- **Nouns**: `data/word_lists/german/german_nouns_a1.json`
