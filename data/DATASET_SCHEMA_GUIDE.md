# Tartarus Dataset Schema & Style Guide

Tartarus is a data-driven language learning application. Content is provided via clean, standardized JSON dataset files placed under `data/word_lists/`.

---

## Dataset Format Specification

Every dataset JSON file contains a top-level `"metadata"` object and an `"items"` array.

```json
{
  "metadata": {
    "name": "German Days of the Week",
    "language": "german",
    "kind": "days",
    "level": "a1",
    "ordered": true
  },
  "items": [
    { "word": "Montag", "definition": "Monday", "index": 1 },
    { "word": "Dienstag", "definition": "Tuesday", "index": 2 },
    { "word": "Mittwoch", "definition": "Wednesday", "index": 3 },
    { "word": "Donnerstag", "definition": "Thursday", "index": 4 },
    { "word": "Freitag", "definition": "Friday", "index": 5 },
    { "word": "Samstag", "definition": "Saturday", "index": 6 },
    { "word": "Sonntag", "definition": "Sunday", "index": 7 }
  ]
}
```

---

## Formatting Style Standard

To maintain codebase readability and consistency:

1. **Pretty-Printed Metadata**: The top-level `"metadata"` block is formatted with 2-space indentation.
2. **Compact Single-Line Items**: Each item object inside the `"items"` array is written on a single line.
3. **Encoding**: UTF-8 encoding without BOM (`ensure_ascii=False`).

---

## Field Reference

### Top-level Metadata (`metadata`)
- `name` *(string, required)*: Human-readable display name for the dataset.
- `language` *(string, required)*: Target language identifier (e.g., `"german"`, `"english"`).
- `kind` *(string, optional)*: Content classification (e.g., `"days"`, `"months"`, `"numbers"`, `"pronouns"`, `"verbs"`, `"nouns"`, `"vocabulary"`, `"sentences"`).
- `level` *(string, optional)*: CEFR level (e.g., `"a1"`, `"a2"`, `"b1"`, `"b2"`, `"c1"`, `"c2"`).
- `ordered` *(boolean, optional, default: false)*: Set to `true` for sequential lists (numbers, days of the week, months, pronouns) where items must be practiced in strict position order.

### Items Array (`items`)
- `word` *(string, required)*: The target text the learner must type (e.g., `"das Buch"`, `"ich habe gemacht"`, `"Montag"`).
- `definition` *(string, required)*: The prompt or translation shown to the learner (e.g., `"the book"`, `"I have made / done"`, `"Monday"`).
- `index` *(integer, optional)*: Explicit sequence position for ordered datasets.

---

## Bundled Datasets Provided

- **Days of the Week**: `data/word_lists/german/vocabulary/a1/german_days_of_week.json`
- **Months of the Year**: `data/word_lists/german/vocabulary/a1/german_months.json`
- **Numbers (1–10)**: `data/word_lists/german/vocabulary/a1/german_numbers_a1.json`
- **Pronouns**: `data/word_lists/german/vocabulary/a1/german_pronouns.json`
- **Nouns**: `data/word_lists/german/vocabulary/a1/german_nouns_a1.json`
- **Verbs (Präsens)**: `data/word_lists/german/vocabulary/a1/german_verbs_praesens_stage1.json`
- **Verbs (Perfekt)**: `data/word_lists/german/vocabulary/a2/german_verbs_perfekt_stage2.json`
- **Conjugations Showcase**: `data/word_lists/german/vocabulary/a1/tartarus_sample_german_conjugations.json`
- **Sentences**: `data/word_lists/german/sentences/a1/tartarus_sample_german_sentences_a1.json`
- **English Vocabulary**: `data/word_lists/english/vocabulary/a1/tartarus_sample_english_a1.json`
- **English Sentences**: `data/word_lists/english/sentences/a1/tartarus_sample_english_sentences_a1.json`
