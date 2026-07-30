# Tartarus Dataset Schema & Naming Convention Guide

Tartarus is a data-driven language learning application. Content is provided via clean, standardized JSON dataset files placed under `data/word_lists/`.

---

## File Naming Convention

All dataset files follow a strict, unified naming pattern:

$$\text{\{language\}\_\{kind\}\_\{level\}.json}$$

Examples:
- `german_vocabulary_a1.json`
- `german_ordered_a1.json`
- `german_sentences_a1.json`
- `english_vocabulary_a1.json`
- `english_sentences_a1.json`

---

## Dataset Format Specification

Every dataset JSON file contains a top-level `"metadata"` object and an `"items"` array.

```json
{
  "metadata": {
    "name": "German Vocabulary A1",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    { "word": "das Buch", "definition": "the book" },
    { "word": "der Tisch", "definition": "the table" },
    { "word": "die Frau", "definition": "the woman" }
  ]
}
```

For ordered/sequential datasets (where items must be practiced in strict position order):

```json
{
  "metadata": {
    "name": "German Numbers 1-10",
    "language": "german",
    "kind": "numbers",
    "level": "a1",
    "ordered": true
  },
  "items": [
    { "word": "eins", "definition": "one (1)", "index": 1 },
    { "word": "zwei", "definition": "two (2)", "index": 2 },
    { "word": "drei", "definition": "three (3)", "index": 3 }
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
- `name` *(string, required)*: Display name for the dataset.
- `language` *(string, required)*: Target language identifier (`"german"`, `"english"`).
- `kind` *(string, optional)*: Content classification (`"vocabulary"`, `"sentences"`, `"numbers"`, `"verbs"`).
- `level` *(string, optional)*: CEFR level (`"a1"`, `"a2"`, `"b1"`, `"b2"`).
- `ordered` *(boolean, optional, default: false)*: Set to `true` for sequential lists where items must be practiced in strict file order.

### Items Array (`items`)
- `word` *(string, required)*: Target text the learner must type.
- `definition` *(string, required)*: Translation or prompt shown to the learner.
- `index` *(integer, optional)*: Sequence position index.

---

## Bundled Core Datasets

- `data/word_lists/german/vocabulary/a1/german_vocabulary_a1.json`
- `data/word_lists/german/vocabulary/a1/german_ordered_a1.json`
- `data/word_lists/german/sentences/a1/german_sentences_a1.json`
- `data/word_lists/english/vocabulary/a1/english_vocabulary_a1.json`
- `data/word_lists/english/sentences/a1/english_sentences_a1.json`
