# Dataset Review Prompt

Attach the matching standard ZIP archive to the model.

The archive contains this prompt and paired `*_vocabulary_*_review.json` and `*_sentences_*_review.json` files for one part of speech across languages and CEFR levels.

Ask the model to extract the ZIP and review one named language/CEFR pair at a time, for example `german / b2`. It must read the matching vocabulary and sentence JSON files for that pair together. Do not ask it to process the entire archive in one response.

```text
You are a lexicographer creating accurate CEFR learner material.

Two JSON files are attached:
1. a vocabulary review file
2. its matching sentence review file

Return exactly two complete corrected JSON objects, in this order:
1. vocabulary JSON
2. sentence JSON

Put each object in its own fenced json code block. Return no prose outside the two code blocks.

The review format is:
{
  "metadata": { ... },
  "parts": [
    {
      "metadata": { ... },
      "items": [ ... ]
    }
  ]
}

Structural restrictions:
- Preserve every top-level metadata object exactly.
- Preserve every part object, including its metadata header, part count, and part order.
- Preserve all part metadata values exactly, including the part name.
- Preserve the order of every retained item within its part.
- Do not merge, split, move, reorder, or renumber parts.
- Do not add fields or change the JSON schema.
- Do not change CEFR level, language, kind, or part of speech.
- Do not change a word into a synonym, related word, different lemma, nationality word, or different meaning.
- Do not edit unrelated definitions, examples, or translations.

Vocabulary and sentence synchronization:
- Every retained vocabulary item must have exactly one matching sentence item in the same sequence.
- The final source-language example sentence in each vocabulary definition must exactly equal the matching sentence item's `word` value.
- The sentence item must retain the same lemma and English gloss as its matching vocabulary item.
- For German sentence files, retain a correct English translation after the lemma and gloss in the sentence definition.
- For English sentence files, retain the matching English sentence and its lemma/gloss definition.
- If a vocabulary correction requires correcting its example sentence, apply the same corrected source-language sentence to the matching sentence item's `word` value and correct its translation only when needed.
- Do not invent new examples. Correct an existing example only to repair spelling, grammar, agreement, word form, translation, or vocabulary/sentence synchronization.
- If an obsolete standalone alphabet-letter entry exists, such as `das A`, `das F`, or `das X`, remove it from both tracks. This is the only permitted item removal.
- Do not remove abbreviations, months, numbers, units, real words, or uncertain entries.
- Where a former alphabet-letter entry has shifted a sentence track, remove only that obsolete sentence entry and preserve the order of every remaining entry.

Language rules:
- Use Standard German used in Germany, including standard capitalization and `ß` where required.
- Preserve consistent English spelling.
- Normalize accidental double spaces and use exactly one space after commas.
- If a correction is uncertain, retain the existing entry unchanged.

Adverb-specific rules:
- Every `word` value must be the standard adverb lemma or a fixed adverbial expression.
- Do not use inflected adjectives, finite verbs, nouns, or conjunctions as the adverb headword.
- Correct spelling, word separation, capitalization, and sentence placement only where required.
- Preserve lexicalized adverbs, particles, and discourse adverbs when they are valid entries.
- Do not reclassify borderline items unless the existing entry is clearly malformed.
- Preserve the intended meaning and CEFR difficulty.

Return the two complete corrected JSON objects and nothing else.
```
