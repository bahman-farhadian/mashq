# German Dataset Quality Review

## Context

Tartarus is a German and English learning application. German vocabulary and
conjugation records are used directly for typing practice, so an incorrect
word, form, translation, or example can teach a learner something wrong and
make later correction expensive.

The relevant files are:

- `data/word_lists/german/vocabulary/`
- `data/word_lists/german/sentences/`
- `data/word_lists/german/conjugations.json`
- `utils/conjugation.py`

The application generates conjugation exercises from `conjugations.json`.
The source dataset must remain unchanged until the linguistic review is
complete.

## Goal

Have a German developer validate the vocabulary, sentence, and conjugation
data so every learner-facing item is a valid German form with an appropriate
meaning and context. The engine must expose only validated forms and must not
silently transform source data into a different grammatical form.

## Confirmed Engine Findings

### 1. Imperative answer prefix bug: fixed

The imperative source already contains complete answers:

- `Sei!`
- `Seid!`
- `Seien Sie!`
- `Seien wir!`

The engine previously generated `du Sei!` and `ihr Seid!` by adding a pronoun
to the source answer. `utils/conjugation.py` now preserves the source answer
for stage 5 while retaining the requested pronoun in the prompt.

### 2. Passive forms for `haben`: fixed by engine exclusion

The source contains these generated forms:

- `werde gehabt`
- `wurde gehabt`
- `bin gehabt worden`
- `war gehabt worden`
- `werde gehabt werden`

The engine consequently produced examples such as `ich werde gehabt` and
`ich bin gehabt worden`. These are not suitable standard learner forms for the
ordinary possession verb `haben`. The engine now excludes `haben` from passive
stages 14 and 16 without changing the source dataset. All other valid
`haben` stages remain available.

## Conjugation Review Required

The source has passive arrays for 269 verbs. Their structure is valid JSON and
usually contains six forms, but structural validity is not linguistic
validity. Review every passive paradigm, especially verbs that are:

- intransitive or normally reflexive;
- stative, modal, or semantically unsuitable as passive subjects;
- listed with an unusual or non-learner construction;
- duplicated under different source names.

For every reviewed form, record one of:

- `valid`: safe for learner practice;
- `replace`: provide the corrected German form;
- `exclude`: no suitable learner form for this stage;
- `needs-context`: valid only with a specified subject or context.

The review must cover all 20 stages, including infinitive, person forms,
imperative, participles, indicative, subjunctive, and passive forms.

## Vocabulary and Sentence Review Required

Run a source-to-output coverage check for every CEFR-level vocabulary and
sentence file. Each output record must be traceable to its source record, and
each file must be deduplicated internally.

Current records requiring linguistic review include examples such as:

- `dies-`
- `welch-`
- `Extra-`
- `das W-Wort, die W-Worter`
- noun records whose article, singular, plural, or capitalization may be
  incomplete;
- definitions or examples whose English meaning does not match the German
  headword.

These are review candidates, not automatic corrections. Some shortened forms
may be intentional source notation, but they must not be exposed as a learner
answer unless the intended practice form is clear.

The review must verify:

1. German spelling, capitalization, umlauts, and punctuation.
2. Noun article, singular form, and plural form.
3. Verb infinitive and reflexive marker placement.
4. English definition accuracy.
5. German example accuracy and English example translation.
6. CEFR-level assignment and duplicate handling.
7. Whether sentence entries are actual sentences rather than fragments.

## Acceptance Criteria

- No learner-facing record contains an unresolved fragment or malformed form.
- Every noun has a verified plural, or is explicitly marked singular-only or
  uncountable when appropriate.
- Every definition and example has been linguistically verified.
- Every generated conjugation answer is either directly present in the source
  or explicitly approved as an engine-generated combination.
- Passive forms are included only when grammatically and pedagogically valid.
- Stage order and pronoun order remain deterministic.
- The source-to-output coverage report shows no silent data loss.
- The final validation report lists all exclusions and replacements.

## Test Evidence Already Completed

Using an isolated temporary SQLite database:

- 859 source conjugation records were loaded.
- 91,489 learner units were generated after excluding `haben` passive units.
- All stages 2 through 20 were tested independently.
- Source-to-engine answer mismatches: 0.
- `haben` passive units generated: 0.
- Learning, audio, and production phases passed for every tested stage.
- Twenty Leitner boxes and deterministic queue ordering passed.

No production database is required for this data-quality review.
