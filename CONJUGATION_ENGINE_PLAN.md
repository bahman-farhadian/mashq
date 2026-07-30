# German Conjugation Engine Specification & Plan

## Overview
The Tartarus German Conjugation Engine is a deterministic, 20-stage active recall learning system for German verb conjugations. It combines active typing, native speech audio listening, and a 10-box Leitner spaced repetition system.

---

## 20 Curriculum Stages

| Stage # | Stage Name | Learning Focus & Pedagogical Goal | Header Display | Prompt Example | Target Answer |
| :---: | :--- | :--- | :--- | :--- | :--- |
| **01** | Personal Pronouns | Practice 9 German personal pronouns | `ich` | `Personal pronouns · English: I` | `ich` |
| **02** | Infinitive & Meaning | Recall German infinitives from English meaning | `to make, do` | `Infinitive · English: to make, do` | `machen` |
| **03** | Indikativ Präsens | Present tense verb conjugations | `machen` | `Indikativ Präsens · Verb: machen (meaning: to make, do) · ich` | `ich mache` |
| **04** | Irregular Present | Irregular & separable present forms | `können` | `Separable and irregular present forms · Verb: können · ich` | `ich kann` |
| **05** | Imperative | Command forms (*du*, *ihr*, *Sie*, *wir*) | `machen` | `Imperative · Verb: machen (meaning: to make, do) · du` | `Mach!` |
| **06** | Partizip II | Past participle forms | `machen` | `Partizip II · Verb: machen (meaning: to make, do)` | `gemacht` |
| **07** | Indikativ Perfekt | Conversational past tense (*haben* / *sein*) | `machen` | `Indikativ Perfekt · Verb: machen (meaning: to make, do) · ich` | `ich habe gemacht` |
| **08** | Indikativ Präteritum | Simple past / narrative past tense | `machen` | `Indikativ Präteritum · Verb: machen (meaning: to make, do) · ich` | `ich machte` |
| **09** | zu-Infinitiv | Infinitives with *zu* (*zu machen*) | `machen` | `zu-infinitive · Verb: machen (meaning: to make, do)` | `zu machen` |
| **10** | Indikativ Plusquamperfekt | Pluperfect / past perfect tense | `machen` | `Indikativ Plusquamperfekt · Verb: machen · ich` | `ich hatte gemacht` |
| **11** | Indikativ Futur I | Future tense (*ich werde machen*) | `machen` | `Indikativ Futur I · Verb: machen · ich` | `ich werde machen` |
| **12** | Konjunktiv II Präteritum | Subjunctive / hypothetical forms | `machen` | `Konjunktiv II Präteritum · Verb: machen · ich` | `ich würde machen` |
| **13** | Passive Präsens & Präteritum | Present & past passive voice | `machen` | `Passive Präsens and Präteritum · Verb: machen · ich` | `ich werde gemacht` |
| **14** | Konjunktiv II Plusquamperfekt | Past hypothetical forms | `machen` | `Konjunktiv II Plusquamperfekt · Verb: machen · ich` | `ich hätte gemacht` |
| **15** | Advanced Passive | Perfekt & Futur passive voice | `machen` | `Passive Perfekt and advanced passive tenses · Verb: machen · ich` | `ich bin gemacht worden` |
| **16** | Konjunktiv I | Indirect discourse / reported speech | `machen` | `Konjunktiv I Präsens and Perfekt · Verb: machen · ich` | `ich mache` |
| **17** | Indikativ Futur II | Future perfect tense | `machen` | `Indikativ Futur II · Verb: machen · ich` | `ich werde gemacht haben` |
| **18** | Mixed Konjunktiv Futures | Complex hypothetical future forms | `machen` | `Konjunktiv I/II future forms · Verb: machen · ich` | `ich werde machen` |
| **19** | Partizip I | Present participle forms | `machen` | `Partizip I · Verb: machen (meaning: to make, do)` | `machend` |
| **20** | Curriculum Mastery | Full curriculum review & mastery | `to make, do` | `Curriculum Mastery · English: to make, do` | `machen` |

---

## Data Schema & Dataset Standards
All verb datasets conform to `data/schemas/german_conjugations.schema.json`.
- Required top-level keys: `infinitiv`, `translation`, `verb_class`, `english`, `indikativ`, `konjunktiv1`, `konjunktiv2`, `imperativ`.
- `translation`: Primary English verb translation (e.g., `"translation": "to make, do"`).

---

## Dedicated Stage Test Users
To enable instant testing of all curriculum stages without completing hundreds of prerequisite questions:
- 20 pre-seeded users (`stage_01_user` through `stage_20_user`) are created in `data/tartarus.db`.
- **Pre-mastery Rule**: User `stage_N_user` has **all stages strictly less than N** pre-completed with `score = 9.0`, `completed = 1`, `leitner_box = 1`, and `daily_pronoun_done = 1`.
- When `stage_N_user` starts a session, the engine immediately serves Stage `N` units.

---

## Queue Ordering & Progression Rules
1. **Single-Stage Queue Isolation**: A session queue contains units belonging strictly to ONE stage. No stage mixing occurs.
2. **Verb Paradigm Grouping**: All pronoun forms of a verb are practiced together in structured `(verb_order, pronoun_order)` sequence before advancing to the next verb.
3. **Natural Verb Advancement**: Incomplete items (`score < 9.0`) are ordered by `(verb_order, pronoun_order)`. When a verb's items reach mastery (`score >= 9.0`), that verb clears from the queue and the session automatically advances to the next verb (`können` $\rightarrow$ `fahren` $\rightarrow$ `lernen` $\rightarrow$ `geben`...).
4. **Third-Person Pronoun Separation**: `er`, `sie`, and `es` are separated into 3 distinct questions in every stage with clear notes (`er (note: form works for er/sie/es)`).

---

## Automated Verification Suite
- **Unit Tests**: `tests/test_conjugation_curriculum.py` (engine logic, schema, queue sorting, verb progression).
- **API Tests**: `tests/test_conjugation_api.py` (HTTP endpoints `/api/practice/start`, `/api/practice/answer`, `/api/practice/drill/step`).
