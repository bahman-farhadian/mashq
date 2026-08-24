# 05 — Backend: `tartarus.py`/`tartarus_web.py` → Django

This is a **port**, not a rewrite. Every scheduling invariant documented in
the current `README.md` ("What you can rely on") must hold identically
after this migration, verified by a ported test suite before any deployment
work begins.

## 5.1 Django apps and what each owns

| App | Owns | Ported from |
|---|---|---|
| `accounts` | Users, auth, sessions (real auth — see §5.5) | `users` table + the missing auth layer |
| `content` | Word lists and their items (the learning material) | `data/word_lists/*.json` |
| `progress` | Per-user, per-item learning state (score, Leitner box, consolidation step) | the `words_<user>_<list>` dynamic tables |
| `practice` | The scheduling/orchestration engine — the actual algorithm | the bulk of `tartarus.py` + `tartarus_web.py` |
| `sessions` | Completed session history (the log, not the live in-progress state) | `sessions_<user>` |
| `mastery` | Append-only mastery milestones, durable pending drills | `mastery_events`, `pending_drills` |
| `analytics` | Fire-and-forget event emission into Mongo | (new — see [07](07-data-platform.md)) |

## 5.2 Model shape (field lists, not code — the actual `models.py` is
implementation work for when this stage is built)

**`content.WordList`**
`slug` (e.g. `german_noun_a1`, matches current list-id convention) ·
`language` · `kind` (vocabulary/sentences) · `level` (CEFR) ·
`owner` (nullable FK to `accounts.User` — null = shared/bundled, set =
personal override, replacing the current `<user>_<list>.json` override
convention) · `created_at` / `updated_at`.

**`content.WordListItem`**
`word_list` (FK) · `content_id` (the stable external id from
`DATASET_SCHEMA_GUIDE.md`) · `word` · `definition` (or a `JSONField` if an
item's accepted-forms/sentence structure needs more than one string — see
`DATASET_SCHEMA_GUIDE.md` in the current repo for the exact shape) ·
`position` (ordering) · `word_frequency` · `active` (soft-delete, matches
current "removed material is marked inactive, never reassigned" rule).

**`progress.Progress`** — the normalized replacement for one
per-user-per-list SQLite table
`user` (FK) · `item` (FK to `WordListItem`) · `score` (0.0–9.0, 0.5 steps)
· `last_practiced` · `last_tartarus_completed` · `active` ·
`times_practiced` / `times_correct` / `times_incorrect` / `times_drilled` /
`times_mastered` · `leitner_box` (nullable) · `leitner_last_reviewed` ·
`consolidation_step`.
Unique constraint on `(user, item)`. This single table replaces every
`words_<user>_<list>` table the legacy app creates dynamically — the
dynamic-table-per-user pattern was a SQLite workaround; Postgres doesn't
need it and shouldn't have it (see [03](03-architecture-decisions.md) ADR-3 for why Postgres).

**`sessions.SessionLog`**
`user` · `word_list` · `session_date` · `duration_seconds` ·
`words_practiced` · `correct_count` · `incorrect_count` · `drilled_count` ·
`mode` · `stage` — a direct field-for-field port of `sessions_<user>`.

**`mastery.MasteryEvent`** (append-only, never updated or deleted)
`user` · `word_list` · `item` · `event_type` (`mastered` / `box_ten`) ·
`event_date`.

**`mastery.PendingDrill`**
`user` · `word_list` · `item` · `target` · `correct_in_a_row` · `context` ·
`mode` · `created_at` — direct port of `pending_drills`.

**Not ported as a model: `practice_bucket`.** The bag-of-tiles table
existed to let a session draw a bounded slice (≤16 items) from a larger
eligible pool across multiple sessions without repeats. As of the most
recent change to the legacy app, the three supplementary tracks (Encoding
Practice, Reading Retrieval, Listening Retrieval) are uncapped — a session
now draws and shuffles the *entire* eligible pool fresh every time, so
there is no cross-session state left to persist. This is a case where the
Django port is *simpler* than the schema it's replacing, not just a
translation — call this out explicitly when teaching this stage as an
example of migrations being a chance to remove complexity that's no longer
earning its keep, not just move it.

## 5.3 Statelessness — the single most important change in this port

**This is not a detail. It is the change that makes every scaling lesson in
Stages 2–4 possible, and it deserves to be taught as a headline.**

### The problem

`utils/tartarus_web.py:49`:

```python
SESSIONS = {}                      # every live practice session, in RAM
SESSIONS_LOCK = threading.RLock()  # ...guarded by an in-process lock
```

Live sessions live in a module-level dict inside one Python process. That
works perfectly for exactly one process, and breaks the instant there are
two: each holds half the sessions, requests land on whichever replica the
load balancer picks, and a learner's session vanishes mid-practice.

### The teaching thread

Pose it as a question **before** revealing the answer, ideally at the start
of Stage 2 when replicas first become possible:

> *"We containerised the app. Why can't I just run three copies of it?"*

Let students find `SESSIONS = {}` themselves. The realisation — that a
single dictionary is the difference between a program that scales and one
that cannot — lands far harder than being told about statelessness.

### The fix

One Redis key per active session:

- key `session:<uuid>`, value a JSON blob mirroring the legacy `session`
  dict (`queue`, `current`, `practiced`, `correct`, …);
- written with `SETEX`, TTL equal to `TARTARUS_SESSION_TTL_SECONDS`.

Redis's native key expiry **deletes** the bespoke `cleanup_sessions()` sweep
entirely — the port removes code rather than adding it, and the result is
correct under multi-process deployment in a way the dict never could be.

### What this unlocks, stage by stage

| Stage | Only possible because the backend is stateless |
|---|---|
| 1 — VM | Multiple gunicorn workers behind Nginx |
| 2 — Docker | More than one backend container |
| 3 — Swarm | `docker service scale backend=3`; rolling updates |
| 4 — Kubernetes | Deployment replicas; HPA autoscaling; pod eviction survival |

### The trade-off, stated honestly

Redis becomes a hard dependency of the request path: if Redis dies,
in-flight sessions are lost. That is an acceptable trade for a practice
app (a learner restarts a session), and it is a genuinely good failure-domain
discussion to have with students — compare it with the analytics pipeline
(ADR-8), which is deliberately *not* on the critical path.

## 5.4 The engine stays framework-agnostic

Port `select_practice_words`, `select_bucket_words`, `process_answer`,
`process_bucket_answer`, `process_drill_answer`, `advance`,
`finalize_session`, and the mastery/Leitner/consolidation-step math into a
`practice/engine.py` (or `services.py`) that takes and returns plain
Python objects / Django model instances — **no DRF request/response
objects, no HTTP concerns** — exactly mirroring how the legacy `tartarus.py`
(`ll`) is already cleanly separated from `tartarus_web.py`'s HTTP layer.
DRF views in `practice/api.py` become thin: parse the request, call the
engine, serialize the result. This split is what makes the parity testing
in §5.6 possible without spinning up a test client for every case.

## 5.5 Authentication (closing the gap from [00](00-executive-summary.md))

- `accounts.User` extends Django's built-in `auth.User` (or a custom user
  model swapped in before the first migration — standard Django advice).
- Session-based auth for the Next.js frontend (same-origin, cookie-based —
  simplest, matches "trusted browser, same site" reasonably well) *plus*
  DRF token or JWT auth for any non-browser client — chiefly the load
  simulator in [17](17-load-simulator.md), which needs to authenticate as up to ~1000 distinct
  learners without a browser in the loop.
- Every DRF view enforces `request.user` as the implicit scope for all
  `Progress`/`SessionLog`/`MasteryEvent`/`PendingDrill` queries — there is
  no `user` field accepted from the request body the way the legacy
  trusted-client API accepts one today. This single change is what makes
  the app safe to put behind a real Ingress in Stage 4.

## 5.6 Parity testing: the gate before any deployment work starts

> Summarised here because it gates this document's work. The full testing
> strategy — including e2e, load, and infrastructure testing — is
> [09](09-testing-strategy.md).

The current `utils/test_tartarus.py` is described as "the project
intentionally has one unified Python test file" — port that structure:

- **Engine-level tests** (mirrors `CoreContractTest`): call
  `practice/engine.py` functions directly against a test database, assert
  the exact same invariants — scores never regress, a session never mixes
  modes, due work outranks new work, etc. These should be near line-for-line
  translations of the existing test names and assertions.
- **API-level tests** (mirrors `HttpContractTest`): DRF `APITestCase`
  hitting the real endpoints, including the new auth boundary (assert user
  A cannot read/mutate user B's progress — a test category that has no
  legacy equivalent, because the legacy app has no such boundary to test).
- **Migration tests** (mirrors `MigrationContractTest`): the one-time
  legacy-data-import management command (§5.7) gets the same treatment the
  current `migrate_database()` already gets — verified backup before
  touching anything, atomic all-or-nothing apply, an injected-failure test
  proving a bad row rolls back the *entire* import rather than leaving a
  half-migrated user. This discipline already exists in this codebase
  (see `test_injected_failure_rolls_back_every_table`); the Django import
  tooling should hold itself to the same standard, not a lesser one just
  because it's "only" a one-time script.

**Gate:** the Django backend does not get deployed anywhere (Stage 1
onward) until its own parity suite is green and a real dataset (a copy of
`data/tartarus.db`) has been imported and spot-checked against the legacy
app's own reports for the same user.

## 5.7 Cutover plan for existing data

1. Freeze the legacy app (or just work from a copy of `data/tartarus.db` —
   it's a single file, trivially copyable).
2. `manage.py migrate_legacy_data --db-path <path> --dry-run` — reads every
   `words_<user>_<list>` table plus the corresponding
   `data/word_lists/**/*.json` files, reports what it *would* create,
   touches nothing.
3. `manage.py migrate_legacy_data --db-path <path>` — same read, now
   writes: creates `content.WordList` + `WordListItem` rows for material,
   `progress.Progress` rows per user/item, `sessions.SessionLog` rows from
   `sessions_<user>`, `mastery.MasteryEvent` from `mastery_events`,
   `mastery.PendingDrill` from `pending_drills`. Wrapped in one DB
   transaction — any failure rolls back everything, matching the legacy
   app's own migration discipline (§5.6).
4. Verification pass: row counts per table, spot-check a handful of users'
   score/box/step values against what the legacy report endpoint returns
   for the same user.
5. `legacy/` stays in the repo and stays runnable (see [04](04-repository-layout.md)) — it is the
   reference implementation this comparison is checked against, not a
   throwaway.

## 5.8 Two pieces that cannot survive unchanged: audio and live TTS

- **Pre-generated pronunciation** (`data/audio/*.db`, one SQLite file per
  bundled list, streamed by `GET /api/audio`): the *concept* survives —
  pre-generate once, serve statically, works the same regardless of host
  OS — but the storage moves from ad hoc per-list SQLite files to a real
  media store: local filesystem in Stage 1, then **self-hosted MinIO** (an
  S3-compatible object store you run yourself — no cloud account involved,
  consistent with [01](01-prerequisites-and-scope.md)) from Stage 2 onward,
  since a multi-replica deployment can't rely on a file living on one node's
  local disk. `generate_audio_database.py` becomes a Django management
  command, run manually or as a one-off Job when new content is added — no
  task queue required (ADR-7).
- **Live TTS fallback for personal lists** (currently: shell out to macOS
  `say`): this does not survive the migration as-is. It's macOS-only (dead
  on every Linux container/VM/pod this plan targets) and shelling out based
  on request-influenced text is a smell worth removing on its own merits.
  Recommendation: replace it with a self-hosted open-source TTS engine
  (Piper is a good fit — small, fast, no per-request cloud cost, easy to
  containerize) exposed as an internal service the Django app calls, or —
  simplest for a teaching deployment — drop the live-fallback feature
  entirely for personal/custom lists and rely solely on pre-generated
  audio, documenting that personal lists are silent until pre-generated
  (matches how the legacy app already degrades gracefully on non-macOS
  hosts: "the application remains usable ... the browser continues without
  audio for that content").

Next: [06 — Frontend: Next.js](06-frontend-nextjs.md).
