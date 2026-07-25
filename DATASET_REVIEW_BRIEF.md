# Tartarus German Dataset — Full Linguistic Review Brief

You are reviewing the German learning dataset for **Tartarus**, a local-first
language-practice app. Learners type these exact forms, so a single wrong
verb ending, misspelled noun, or bad translation teaches something incorrect
that is expensive to unlearn later.

You must work in **two roles simultaneously** and report from both:

1. **German teacher** — judge grammatical correctness, register, CEFR fit,
   and pedagogical soundness of every form exposed to a learner.
2. **German student (A1→C2)** — judge whether each item is actually
   learnable: is the answer unambiguous? would a student be able to produce
   it from the prompt alone? is the feedback confusing?

Your output is a single review file (see **Output** at the end). Be ruthless
and specific. Vague approvals like "looks fine" are useless; cite the exact
verb, field, and the corrected form.

---

## 0. How to run this review

You have shell + file access in the Tartarus repo. All paths below are
relative to the repo root. The project is **standard-library Python only**
(no venv, no pip). Use these commands to inspect data:

```bash
# Conjugations: 831 verbs, the core of this review
python3 -c "import json; d=json.load(open('data/word_lists/german/conjugations.json',encoding='utf-8')); print(len(d), 'verbs'); print(json.dumps(d['sein'], ensure_ascii=False, indent=2))"

# Vocabulary per CEFR level (a1..c2)
python3 -c "import json,os; [print(l, len(json.load(open(os.path.join('data/word_lists/german/vocabulary',l,fn),encoding='utf-8')))) for l in sorted(os.listdir('data/word_lists/german/vocabulary')) for fn in os.listdir(os.path.join('data/word_lists/german/vocabulary',l)) if fn.endswith('.json')]"

# Sentences per CEFR level
python3 -c "import json,os; [print(l, len(json.load(open(os.path.join('data/word_lists/german/sentences',l,fn),encoding='utf-8')))) for l in sorted(os.listdir('data/word_lists/german/sentences')) for fn in os.listdir(os.path.join('data/word_lists/german/sentences',l)) if fn.endswith('.json')]"
```

The engine that turns the conjugation JSON into learner questions lives in
`utils/conjugation.py`. Read it — it defines how each field becomes a
question, so you must judge the field *as the learner will see it*.

---

## 1. The determinism contract (non-negotiable engine context)

The German conjugation track is **fully deterministic**. This is the most
error-prone surface, so understand it before judging data:

- **20 fixed stages in order**: (1) Personal pronouns → (2) Infinitive →
  (3) Indikativ Präsens → (4) Separable/irregular present → (5) Imperative →
  (6) Partizip II → (7) haben/sein auxiliary → (8) Perfekt →
  (9) Präteritum → (10) zu-infinitive → (11) Plusquamperfekt →
  (12) Futur I → (13) Konjunktiv II Präteritum → (14) Passive Präsens/
  Präteritum → (15) Konjunktiv II Plusquamperfekt → (16) Passive Perfekt/
  advanced → (17) Konjunktiv I Präsens/Perfekt → (18) Futur II →
  (19) Konjunktiv I/II future → (20) Partizip I.
- **Six-person order is fixed**: `ich → du → er/sie/es → wir → ihr → sie/Sie`
  (JSON array indices 0→5). Never shuffled.
- **Imperative order is fixed**: `du → ihr → Sie → wir` (NOT the six-person
  order).
- A later pronoun/verb/stage is **never** introduced early, even with a
  perfect score.
- `haben` is excluded from passive stages 14 and 16 (its passive is not a
  learner form). All other `haben` stages remain.
- The engine prefixes person answers with the pronoun (`ich mache`,
  `du machst`, `er macht`, …). This is **intentional**, not a bug — judge
  the answer string as `pronoun + space + conjugated form`.
- Imperative answers are stored verbatim from source (`Sei!`, `Mach!`,
  `Geben Sie!`) and the engine does **not** prepend a pronoun to them.

**Your job is data quality, not engine redesign.** Do not propose changes
that break determinism. If you find a form that is wrong, propose the
corrected source value.

---

## 2. Conjugation dataset — `data/word_lists/german/conjugations.json`

**831 verbs.** Each record has this exact shape:

```json
{
  "infinitiv": "machen",
  "partizip1": "machend",
  "partizip2": "gemacht",
  "zu_infinitiv": "zu machen",
  "hilfsverb": "haben",                       // "haben" or "sein"
  "indikativ": {
    "praesens":        [6 forms],             // ich, du, er/sie/es, wir, ihr, sie/Sie
    "praeteritum":     [6 forms],
    "perfekt":         [6 forms],
    "plusquamperfekt": [6 forms],
    "futur1":          [6 forms],
    "futur2":          [6 forms]
  },
  "konjunktiv1": {                            // Konjunktiv I
    "praesens":  [6 forms],
    "perfekt":   [6 forms],
    "futur1":    [6 forms],
    "futur2":    [6 forms]
  },
  "konjunktiv2": {                            // Konjunktiv II
    "praeteritum":      [6 forms],
    "plusquamperfekt":  [6 forms],
    "futur1":           [6 forms],
    "futur2":           [6 forms]
  },
  "imperativ": {                              // null for 9 impersonal verbs
    "du": "Mach!", "ihr": "Macht!", "Sie": "Machen Sie!", "wir": "Machen wir!"
  },
  "passiv": {                                 // null for 567 intransitive verbs
    "praesens": [6], "praeteritum": [6], "perfekt": [6],
    "plusquamperfekt": [6], "futur1": [6]
  },
  "english": {                                // only 3 tenses translated
    "praesens": [6], "praeteritum": [6], "perfekt": [6]
  }
}
```

### What to check — for EVERY verb, EVERY tense

For each of the 831 verbs, walk every array and verify the form at each
index matches the pronoun at that index. Use these checks:

**A. Grammatical correctness (teacher)**
1. Each six-form array has exactly 6 non-empty entries in pronoun order
   `ich, du, er/sie/es, wir, ihr, sie/Sie`.
2. The conjugated ending matches the person. Watch especially:
   - 2nd singular (`du`) must end in `-st` (beware `stst` typos, e.g. a
     past bug was `schlägstst`).
   - 3rd singular (`er/sie/es`) must **not** carry `-st` (a past bug was
     `schlägst` for `er schlägt`).
   - Vowel-change verbs (e→i, a→ä, au→äu, e→ie): the 2nd/3rd singular must
     carry the changed vowel (`geben→du gibst/er gibt`,
     `fahren→du fährst/er fährt`, `laufen→du läufst/er läuft`,
     `lesen→du liest/er liest`). The dataset had bugs here
     (`messen`→`messt`, `fressen`→`fresst`, `wachsen`→`wachst`); re-verify
     all of them.
3. Separable-prefix verbs: the prefix is detached in finite forms
   (`anfangen`→`ich fange an`, `du fängst an`) and attached at the end of
   the clause; in Perfekt/Plusquamperfekt the prefix attaches to the
   participle (`ich habe angefangen`).
4. `hilfsverb` is correct: motion/state-change verbs take `sein`
   (`gehen`, `fahren`, `kommen`, `fliegen`, `aufwachen`, `umkommen`…);
   most others take `haben`. Flag any `sein`-verb whose Perfekt is built
   with `habe/hast` or any `haben`-verb built with `bin/bist`.
5. `partizip2` is correct: ge- prefix for most, no ge- for verbs ending
   `-ieren` (`studieren`→`studiert`), `-ieren`/foreign stems; inseparable
   prefixes (`ver-`, `be-`, `er-`, `ent-`, `emp-`, `zer-`, `miss-`,
   `ge-`) take no ge- and never `-t` if strong (`verstehen`→`verstanden`).
6. `zu_infinitiv`: `zu` inserts before the prefix for separable verbs
   (`anfangen`→`anzufangen`), attaches after for inseparable
   (`zu verstehen`).
7. Imperative: `du` form drops `-st` and often the `-e`
   (`du machst`→`Mach!`, `du nimmst`→`Nimm!`); `ihr` = ihr-form without
   change (`ihr macht`→`Macht!`); `Sie`/`wir` = infinitive + pronoun
   (`Machen Sie!`, `Machen wir!`). Imperative is **null** for 9 verbs —
   confirm those are genuinely impersonal (`geschehen`, weather verbs) and
   that no learnable verb wrongly has a null imperative.
8. Konjunktiv I `praesens`: 3rd singular = stem + `-e` (`er mache`); the
   `du`/`ihr` forms use `-est`/`-et` (`du machest`, `ihr machet`).
9. Konjunktiv II `praeteritum`: `würde + infinitive` is acceptable for all
   verbs in this dataset (a pedagogical choice); strong verbs may also
   appear as the old subjunctive (`gäbe`, `käme`, `wäre`) — either is fine,
   just flag mixed paradigms within one verb.
10. Passive: only transitive (`haben`) verbs should have a `passiv` block.
    Flag any `sein`-verb with a passive. Also flag semantically odd
    passives (e.g. a reflexive-only verb that cannot naturally be passive).
    `haben` itself is correctly excluded by the engine — do not flag that.

**B. Student learnability (student)**
11. Is the answer **unambiguous** given the prompt? If two different verbs
    share the same answer at the same stage (e.g. `wir machen` and
    `sie/Sie machen` are identical strings), the engine keeps them as
    separate units by design — that is correct. But flag any case where a
    student cannot infer *which* form is expected.
12. Capitalization: noun-like forms and `Sie`/`wir` imperatives start
    uppercase; finite verb forms mid-sentence are lowercase. The answer
    string must match what a learner would correctly type.
13. Spelling: ß/ss (after diphthongs and long vowels = ß, after short
    vowels = ss), umlauts (ä/ö/ü not ae/oe/ue).

**C. English translations**
14. `english` covers only `praesens`, `praeteritum`, `perfekt`. Verify:
    - 3rd singular gets the `-s` (`he makes`), others don't.
    - Irregular English verbs are correct (`go/went/gone`, not `goed`;
      `forgive/forgave`, not `forgived`; `catch/caught`, not `catched`;
      `fight/fought`, not `fighted`; `occur/occurred`, not `occured`).
      The dataset had all of these wrong before — re-verify they stayed
      fixed and scan for any others.
    - Meaning matches the German verb (a verb renamed during cleanup must
      not keep a stale English gloss).

### Verb-name sanity (post-cleanup check)

A recent cleanup removed annotation noise from verb keys. Verify:
- No key contains `(`, `)`, or a trailing/leading space.
- Reflexive verbs use the `sich ` prefix (`sich freuen`, not `freuen (sich)`).
- Idioms keep their full form (`Bescheid geben`, `Rad fahren`,
  `spazieren gehen`, `weh tun`, `sauber machen`, `stehen bleiben`,
  `Geld abheben`, `nachhaltig konsumieren`).
- No verb appears twice under different keys (the 28 duplicate pairs were
  merged; confirm none regressed).

---

## 3. Vocabulary — `data/word_lists/german/vocabulary/<level>/german_<level>.json`

Six files: a1 (2157), a2 (2647), b1 (7081), b2 (8566), c1 (3104), c2 (365).
Each record:

```json
{"word": "das Haus, die Häuser", "definition": ["the house", "German example — English gloss"], "word_frequency": 123}
```

`definition` is a list: the first entry is the English meaning, subsequent
entries are `German example — English gloss` pairs.

### What to check

1. **Nouns** must carry article + singular + plural
   (`der Tisch, die Tische`). Flag:
   - nouns with no article, no plural, or a plural that disagrees with the
     article gender;
   - singular-only / uncountable nouns not marked as such;
   - `das W-Wort, die W-Worter` (likely should be `W-Wörter`).
2. **Fragments** — words ending in `-` (`dies-`, `welch-`, `einzig-`,
   `beid-`, `best-`, `aller-`, …). These are declension stems, not
   learner answers. For each, decide: `replace` with the full inflected
   form a student should type, or `exclude`.
3. **Adjectives** with comparison should give all three degrees
   (`dumm, dümmer, am dümmsten`) — verify the forms are correct.
4. **Spelling/capitalization**: nouns capitalized, verbs lowercased, ß/ss
   and umlauts correct.
5. **English accuracy**: the first `definition` entry must translate the
   headword; each `German — English` example must be a correct translation.
6. **CEFR fit**: is the word assigned to the right level? (a common-room
   word in C2, or a C2 academic term in A1, is a flag.)
7. **Duplicates**: within a file (already deduped — confirm) and *across*
   files (same word at two levels is acceptable but note it).

---

## 4. Sentences — `data/word_lists/german/sentences/<level>/german_sentences_<level>.json`

Six files: a1 (2638), a2 (2914), b1 (7512), b2 (8709), c1 (3177), c2 (365).
Each record:

```json
{"word": "Der Unterricht beginnt um neun Uhr.", "definition": "The class begins at nine o'clock.", "word_frequency": 703}
```

Here `word` is the full German sentence and `definition` is its English
translation (a single string, not a list).

### What to check

1. Each `word` is a **complete sentence**, not a fragment. Must start with
   a capital letter and end with `.`, `!`, or `?`.
2. German grammar/spelling correct (word order after verb/subordinate
   clause, adjective endings, case after prepositions).
3. English translation is accurate and idiomatic — flag literal-but-wrong
   or machine-translation artifacts.
4. CEFR level matches sentence complexity.
5. No duplicate sentences within a file (already deduped — confirm).

---

## 5. Output format — write your review to `DATASET_REVIEW.md`

Produce exactly one file, `DATASET_REVIEW.md`, in the repo root. Structure:

```markdown
# Tartarus German Dataset — Linguistic Review

Reviewer: <you>    Date: <iso>
Scope: 831 conjugation verbs, 6 vocab levels, 6 sentence levels.

## Summary
- verbs reviewed: 831
- forms checked: <count>
- valid: <count>
- replace: <count>
- exclude: <count>
- needs-context: <count>
- one-line verdict: <ship | ship-after-fixes | block>

## Conjugation findings

For each issue, one block:
### <verb> — <stage / field> — <verdict>
- current: `<exact wrong value>`
- correct: `<exact right value>`
- reason: <one sentence from the teacher perspective>
- student-impact: <one sentence from the student perspective>

Group by severity: **errors** (wrong form) first, then **warnings**
(odd but acceptable), then **notes** (style/CEFR).

## Passive review (264 verbs)
Table: verb | praesens OK? | praeteritum OK? | perfekt OK? |
plusquamperfekt OK? | futur1 OK? | verdict | note
List every verb whose passive is semantically questionable even if
grammatically well-formed.

## Vocabulary findings
Per-level tables: level | word | issue | verdict | corrected form

## Sentence findings
Per-level tables: level | sentence (truncated) | issue | verdict | corrected translation

## Verbs with no learner imperative (9)
List them. Confirm each is genuinely impersonal or approve an imperative.

## Cross-cutting
- any verb duplicated across keys: <list or none>
- any sein-verb with a passive block: <list or none>
- any haben-verb whose perfekt is built with sein: <list or none>
- English typo sweep results: <list or none>

## What passed (brief)
2-4 lines naming the categories that were clean, so the reader knows what
was NOT skipped.
```

### Rules for the review

- **Cite exact values.** `current: "schlägst"` not `current: wrong`.
- **Verdicts** are exactly: `valid`, `replace`, `exclude`, `needs-context`
  (from the project's review vocabulary).
- Do **not** edit the dataset. You are reviewing; the maintainer applies
  fixes. If you want to propose a corrected dataset, say so in the review
  and the maintainer will run the correction script.
- Do **not** propose engine changes that break the determinism contract
  (fixed stage/pronoun/imperative order). Flag engine concerns in a
  separate `## Engine concerns` section instead.
- If a form is unusual but correct (e.g. a literary Konjunktiv II old form
  like `gäbe`), mark it `valid` with a note, not `replace`.
- Work through **all 831 verbs**. A review that samples is not a review —
  it is a guess. If you must batch, say which ranges you covered and which
  remain, so the maintainer can resume.
- Run the existing tests before and after noting any engine-vs-data
  mismatch you discover:
  `python3 -m unittest discover -s tests && python3 tests/e2e_conjugation.py`

### Definition of done for this review

The review is complete when:
1. Every conjugation verb has been walked across all its stages.
2. Every passive paradigm has an explicit verdict.
3. Every vocab file has a per-level findings table (even if "all valid").
4. Every sentence file has a per-level findings table (even if "all valid").
5. The summary counts add up to the forms checked.
6. The one-line verdict is one of: `ship`, `ship-after-fixes`, `block`.
