# Mandatory Deterministic Learning Order

## Priority of This Requirement

The German conjugation track must be completely deterministic.

Nothing in the learning sequence may be randomized.

This requirement overrides Tartarus’s normal randomized item-selection behavior and any other requirement that could be interpreted as allowing questions, verbs, pronouns, stages, modules, or review items to be shuffled.

The developer may reuse Tartarus’s scoring, mastery, and spaced-review principles, but those systems must not change the prescribed curriculum order.

---

## Fixed Stage Order

The learner must progress through the following stages in exactly this order:

```text
1. Personal pronouns
2. Infinitive
3. Indikativ Präsens
4. Separable and irregular present forms
5. Imperative
6. Partizip II
7. haben/sein auxiliary selection
8. Indikativ Perfekt
9. Indikativ Präteritum
10. zu-infinitive
11. Indikativ Plusquamperfekt
12. Indikativ Futur I
13. Konjunktiv II Präteritum
14. Passive Präsens and Präteritum
15. Konjunktiv II Plusquamperfekt
16. Passive Perfekt and advanced passive tenses
17. Konjunktiv I Präsens and Perfekt
18. Indikativ Futur II
19. Konjunktiv I/II future forms
20. Partizip I and stylistic mastery
```

A learner must not:

* begin a later stage before completing the required earlier stage;
* skip a stage;
* study stages in a different order;
* unlock a stage because of random selection;
* receive learning questions from a locked stage;
* receive new material from several stages mixed together.

Previously completed stages may appear in review, but new learning material must always come from the learner’s current unlocked stage.

---

## Fixed Pronoun Order

All six-person conjugation material must be learned in this exact order:

```text
1. ich
2. du
3. er/sie/es
4. wir
5. ihr
6. sie/Sie
```

This corresponds to the JSON array positions:

```text
0 = ich
1 = du
2 = er/sie/es
3 = wir
4 = ihr
5 = sie/Sie
```

The application must not shuffle these positions.

The application must not begin with a randomly selected pronoun.

The application must not present `wir`, `ihr`, or `sie/Sie` before the preceding pronouns have been introduced in the current learning unit.

The learner must encounter person-dependent forms in this progression:

```text
ich
→ du
→ er/sie/es
→ wir
→ ihr
→ sie/Sie
```

Where a stage contains six-person arrays, the sequence must remain the same for every tense, mood, voice, and verb.

---

## Person Unlocking

Within a person-dependent stage, pronouns must be unlocked sequentially.

The learner begins with:

```text
ich
```

After completing the required learning and production work for `ich`, the learner proceeds to:

```text
du
```

This continues until:

```text
sie/Sie
```

The application must not unlock a later pronoun merely because the learner answered a small number of earlier questions correctly.

The configured mastery requirement for the current pronoun must be satisfied before the next pronoun becomes part of new learning.

Items from already completed pronouns may continue to appear as review items.

Example:

```text
Current new-learning pronoun: er/sie/es

Allowed:
- new er/sie/es material;
- review of ich material;
- review of du material.

Not allowed:
- new wir material;
- new ihr material;
- new sie/Sie material.
```

---

## Fixed Order Inside Each Stage

Every stage must define an explicit ordered list of:

* lessons;
* grammatical concepts;
* verbs or expressions;
* pronouns where applicable;
* exercise types.

The application must follow that order exactly.

The curriculum order must be stored or represented independently from the raw `conjugations.json` object structure.

The following must not determine the curriculum order:

* random selection;
* JSON property iteration;
* alphabetical sorting;
* database row order;
* database IDs;
* file-system order;
* answer history;
* mistake count;
* frequency alone;
* score alone;
* response time;
* random tie-breaking.

Scoring and performance may determine whether an item requires additional practice, but they must not move a learner to a later prescribed item prematurely.

---

## Ordered Verb Inventory

Each stage must use an explicitly curated ordered verb list.

The source file contains the available verb inventory, but it does not define the pedagogical order of those verbs.

The conjugation feature must therefore distinguish between:

```text
source inventory
```

and:

```text
curriculum order
```

The source inventory answers:

```text
Which forms are available?
```

The curriculum answers:

```text
Which verb is learned first, second, third, and so on?
```

No stage may select verbs randomly from the 859 source records.

No stage may silently substitute alphabetical order for a pedagogical verb order.

No stage should be released without a defined, reviewable ordered verb list.

The ordered list may include:

```text
core verbs
expansion verbs
optional mastery verbs
```

These groups must also remain ordered.

---

## Deterministic Learning-Unit Sequence

Each assessable learning unit must have a stable curricular position.

Conceptually, its order is determined by:

```text
stage order
→ lesson order
→ verb order
→ pronoun order
→ exercise progression
```

For example:

```text
Stage 3: Indikativ Präsens
  Lesson 1: regular verbs
    Verb 1: machen
      1. ich mache
      2. du machst
      3. er/sie/es macht
      4. wir machen
      5. ihr macht
      6. sie/Sie machen

    Verb 2: lernen
      1. ich lerne
      2. du lernst
      3. er/sie/es lernt
      4. wir lernen
      5. ihr lernt
      6. sie/Sie lernen
```

The exact grouping of verbs may be defined in the curriculum, but once defined, the application must preserve it.

Identical answer strings must still retain their separate curricular positions.

For example:

```text
wir machen
sie/Sie machen
```

must not be merged or treated as one completed person merely because the verb form is the same.

---

## Deterministic Exercise Progression

Exercises for each learning unit must progress in a fixed order.

Recommended progression:

```text
1. Introduction
2. Recognition
3. Guided recall
4. Independent production
5. Mastery check
6. Spaced review
```

The developer may define the exact presentation of these phases, but the application must not randomly choose between them.

A learner must not receive an independent production question before the appropriate introduction or guided practice has occurred.

The adaptive score may determine how long the learner remains in a phase, but it must not randomly select the phase or skip required phases.

---

## Deterministic Handling of Errors

An incorrect answer must not cause the learner to be moved randomly to another verb, pronoun, or stage.

The learner should remain on, or return predictably to, the affected learning unit according to a documented correction sequence.

Recommended correction sequence:

```text
incorrect answer
→ show correct form and explanation
→ guided retry
→ independent retry
→ return to the ordered curriculum position
```

Repeated errors may create additional repetitions, but they must not reorder the curriculum.

---

## Deterministic Review Order

Review must also be deterministic.

Due review items must be ordered by stable rules such as:

```text
1. earliest due date;
2. lower curriculum stage;
3. lower lesson position;
4. lower verb position;
5. pronoun order;
6. exercise or form order;
7. stable source identity as the final tie-breaker.
```

There must be no random tie-breaker.

A review session may contain material from several completed stages, but the review order must be reproducible from the learner’s stored progress.

Running the same review against the same progress state must produce the same item order.

Review must never include an unintroduced or locked form.

---

## Relationship Between Mastery and Order

Mastery determines whether the learner is ready to advance.

Mastery does not determine what comes next.

What comes next is always determined by the prescribed curriculum sequence.

For example:

```text
Incorrect:
The learner mastered several present-tense verbs, so select any remaining
verb randomly.

Correct:
The learner mastered the current ordered unit, so continue to the next
defined unit in the curriculum.
```

A learner who struggles may receive more repetitions of the current and previously introduced units.

A learner who performs well may complete the requirements more quickly.

Neither case changes the order of stages, verbs, pronouns, or required exercise phases.

---

## Non-Person Forms

Some stages do not use six-person arrays, including:

```text
infinitiv
partizip1
partizip2
zu_infinitiv
hilfsverb selection
```

These stages must still use an explicit ordered verb list and fixed exercise progression.

They must not select verbs randomly.

Example:

```text
Partizip II:
1. machen → gemacht
2. lernen → gelernt
3. spielen → gespielt
4. gehen → gegangen
5. fahren → gefahren
```

The exact verb list belongs to the curriculum, but its order must be stable.

---

## Imperative Order

Imperative forms must be taught in this fixed order:

```text
1. du
2. ihr
3. Sie
4. wir
```

This order follows the imperative object rather than the normal six-person array.

Records where:

```text
.imperativ == null
```

must be skipped without altering the order of the remaining curriculum entries.

Skipping an unavailable imperative record must not cause random replacement.

The curriculum must continue with the next explicitly ordered eligible record.

---

## Passive Eligibility

Passive lessons must only use records where:

```text
.passiv != null
```

The eligible passive verbs must have their own explicit curriculum order.

The application must not randomly choose 269 eligible verbs from the source.

When an ineligible record is encountered, the application must follow the next eligible record in the defined passive curriculum list.

---

## Conflict Resolution With Existing Tartarus Behavior

The existing vocabulary and sentence systems may retain their current selection behavior.

The no-randomization rule applies specifically and mandatorily to the German conjugation learning track.

The conjugation track must not inherit or reuse behavior that causes:

* randomized database selection order;
* shuffled questions;
* random fallback ordering;
* randomized verb sampling;
* randomized person selection;
* random question-type selection;
* randomized review tie-breaking.

Existing shared scoring or Leitner functionality may be reused only where it preserves the deterministic curriculum contract.

When existing Tartarus behavior conflicts with this contract, this contract takes precedence for German conjugation.

---

## Required Learner Experience

At every point, the learner should be able to understand:

```text
Where am I in the curriculum?
What have I already completed?
What exact item comes next?
Why is that item next?
Which earlier items are being reviewed?
```

The application should display stable progress such as:

```text
Stage 3 of 20
Lesson 1 of 4
Verb 2 of 10
Pronoun 3 of 6: er/sie/es
Exercise phase: Guided recall
```

Returning to the application must resume from the same ordered curricular position, except when scheduled review must be completed first.

Scheduled review must itself follow the deterministic review order.

---

## Updated Acceptance Criteria

In addition to the existing acceptance criteria:

* No learning item is selected randomly.
* No review item is selected or tie-broken randomly.
* The 20 stages are completed in the exact prescribed order.
* A locked stage never contributes new learning material.
* Six-person forms always follow `ich`, `du`, `er/sie/es`, `wir`, `ihr`, `sie/Sie`.
* Imperative forms always follow `du`, `ihr`, `Sie`, `wir`.
* A later pronoun is not introduced before the current pronoun’s requirement is satisfied.
* Every stage has an explicit ordered lesson list.
* Every stage has an explicit ordered verb or expression list.
* Every learning unit has a stable curriculum position.
* Scoring may add repetitions but may not reorder units.
* Mistakes may add correction work but may not change the prescribed next unit.
* Mastery controls advancement but does not select the next item.
* Review order is reproducible from the same stored learner state.
* Database IDs, alphabetical sorting, source-object order, frequency, and random values do not define pedagogical order.
* The learner resumes from the same curriculum position after restarting.
* Existing randomized behavior elsewhere in Tartarus does not affect the conjugation track.
* Automated tests verify that repeated runs with identical learner state produce identical question sequences.
* Automated tests verify that no later pronoun, verb, lesson, or stage is introduced early.

---

## Determinism Test Requirement

The completed feature must include a behavioral test equivalent to the following:

```text
Given:
- the same conjugation dataset;
- the same curriculum version;
- the same learner progress;
- the same review dates;
- the same application configuration;

Then:
- the next learning item is identical;
- the complete generated session order is identical;
- the review order is identical;
- no random seed is required to reproduce the result.
```

A second test must confirm:

```text
Changing a learner’s score may add or remove required repetitions,
but it must not change the prescribed identity of the next unlocked
curriculum unit.
```

---

## Definition of Done Amendment

The conjugation feature is not complete if any part of the learner’s progression depends on random selection.

Completion requires a deterministic path from:

```text
Personal pronouns
```

through:

```text
Partizip I and stylistic mastery
```

with fixed stage order, fixed lesson order, fixed verb order, fixed pronoun order, fixed exercise progression, and deterministic review behavior.
