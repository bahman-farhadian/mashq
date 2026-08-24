# 09 — Testing Strategy

## 9.1 Why this document exists early

Testing appears here — *before* any deployment stage — because the parity
gate in [05](05-backend-django.md) §5.6 blocks all deployment work. You
cannot meaningfully teach "deploy this application four ways" if nobody can
answer *"is the application still correct?"*

The current codebase already has an unusually good testing culture worth
preserving: one unified test module (`utils/test_tartarus.py`) with clearly
separated contract classes (`CoreContractTest`, `HttpContractTest`,
`MigrationContractTest`, `BundledCorpusContractTest`,
`StaticReleaseContractTest`, `BrowserContractTest`). The migration should
**extend** that discipline, not abandon it for a generic pyramid.

---

## 9.2 The test layers

| Layer | Scope | Tool | Runs in CI | Runtime |
|---|---|---|---|---|
| **Unit** | `practice/engine.py` functions, no HTTP, no framework | `pytest` | every commit | seconds |
| **Integration** | DRF views + real PostgreSQL + real Redis | `pytest` + `pytest-django` | every commit | ~1 min |
| **Parity** | Django output vs. `legacy/` for identical inputs | `pytest` | every commit | ~1 min |
| **Migration** | Legacy-data import correctness and rollback | `pytest` | every commit | seconds |
| **E2E** | Real browser against a running stack | Playwright | every MR | ~5 min |
| **Infrastructure** | Ansible roles converge and are idempotent | Molecule | on `ansible/` changes | ~10 min |
| **Smoke** | A deployed stage actually works | `ci/smoke-test.sh` | after each deploy | ~30 s |
| **Load** | Behaviour under sustained/burst traffic | simulator, k6 | manual / scheduled | minutes–hours |

### Unit — the engine, in isolation

Direct translations of the existing `CoreContractTest` assertions, calling
`practice/engine.py` with no HTTP layer. These are the tests that protect
the domain guarantees the app's own README makes: scores never regress, a
session never mixes question modes, due work outranks new work.

Fast, deterministic, no I/O beyond a test database. The bulk of the suite.

### Integration — the API boundary

DRF `APITestCase` against real Postgres and real Redis (not mocks —
`fakeredis` hides exactly the TTL/serialisation bugs this layer should
catch). Direct successor to `HttpContractTest`.

**One new category with no legacy equivalent: authorisation.** The legacy
app has no auth boundary, so it has no tests for one. The port must add:

- user A cannot read user B's progress, sessions, or mastery events;
- user A cannot mutate user B's state by supplying B's id in a request body;
- unauthenticated requests to practice endpoints are rejected.

These are the tests that make the app safe to expose in Stage 4
([11](11-security-and-secrets.md)).

### Parity — the gate

For a fixed corpus and a fixed sequence of answers, the Django engine and
the frozen `legacy/` engine must produce **identical** score, Leitner box,
consolidation step, and question selection.

Mechanically: seed both with the same word list and progress state, drive
both through the same scripted answer sequence, assert the resulting state
matches field-for-field.

> **This is the gate.** No deployment stage begins until parity is green.
> It is the only thing standing between "we re-platformed the app" and "we
> re-platformed the app and quietly broke the learning algorithm."

`legacy/` staying runnable ([04](04-repository-layout.md)) exists precisely
to make this possible.

### Migration — importing real data

The existing suite already holds itself to a high standard here
(`test_injected_failure_rolls_back_every_table`). The legacy-import command
([05](05-backend-django.md) §5.7) must meet the same bar:

- a verified backup before touching anything;
- atomic, all-or-nothing apply;
- an injected-failure test proving a bad row rolls back the **entire**
  import, not leaving a half-migrated user.

### E2E — the browser

Playwright, against a running stack (Compose locally, any stage in CI). Kept
deliberately small — E2E tests are slow and flaky in proportion to their
number. Cover only journeys that cannot be verified lower down:

1. Log in → start a practice session → answer correctly → advance.
2. Answer incorrectly → the correct reveal/retry behaviour occurs.
3. Theme toggle persists across reload (light/dark, [06](06-frontend-nextjs.md)).
4. The interaction-lock rules hold: typing allowed during prompt speech,
   submission blocked until it finishes.

That last one matters because those rules are *behavioural guarantees* in
the current app, not incidental UI detail — they are easy to break silently
during a framework migration.

### Infrastructure — testing Ansible with Molecule

The layer most courses skip, and the one that most distinguishes a DevOps
course from a deployment tutorial: **configuration management is code, so it
gets tests.**

Each role in `ansible/roles/` gets a Molecule scenario that:

1. spins up a clean container/VM,
2. applies the role,
3. asserts the result (service running, port listening, config present),
4. **applies the role a second time and asserts zero changes** — the
   idempotency check, and the single most valuable Ansible test there is.

```yaml
# ---------------------------------------------------------------------------
# Molecule verification for the 'postgresql' role.
#
# Session:      13 (Stage 1), tested per this document (09)
# Depends on:   molecule + the docker driver on the runner
# Consumed by:  the 'test' stage of .gitlab-ci.yml, on ansible/ changes
# ---------------------------------------------------------------------------
- name: Verify
  hosts: all
  tasks:
    # Presence is necessary but NOT sufficient: a package can be installed
    # while the service is masked or crash-looping. Assert on the RUNNING
    # service, which is what the backend role actually depends on.
    - name: Confirm the PostgreSQL service is running and enabled
      ansible.builtin.service_facts:

    - name: Assert postgresql is active
      ansible.builtin.assert:
        that:
          - ansible_facts.services['postgresql.service'].state == 'running'
        # A custom message here saves the student ten minutes of scrolling
        # Ansible's default output to work out which assertion failed.
        fail_msg: >-
          PostgreSQL is installed but not running. Check
          'journalctl -u postgresql' on the target — the usual cause is a
          data-directory permission problem from a partially-applied earlier run.
```

### Smoke — did the deploy actually work?

**One script, four targets** — the same file used after every stage's
deploy, which is itself a small demonstration of invariant I1:

```bash
# ci/smoke-test.sh <base-url>
# Asserts, against a freshly deployed stage:
#   1. GET /healthz          -> 200          (the app is up)
#   2. GET /api/health/db    -> 200          (Postgres reachable)
#   3. GET /api/health/cache -> 200          (Redis reachable)
#   4. POST login + start a session -> 200   (the real path works end to end)
# Exits non-zero on the first failure so the pipeline stops on a bad deploy.
```

Health endpoints are a deliverable of [05](05-backend-django.md): they are
also what Docker healthchecks, Swarm health, and Kubernetes readiness/
liveness probes consume in later stages. One endpoint, four consumers.

### Load — the simulator and k6

Two different jobs, covered fully in [17](17-load-simulator.md):

- **The simulator** — ~1000 stateful, long-lived synthetic learners; the
  always-on population that keeps dashboards alive during class.
- **k6** — short, sharp, throughput-and-latency load tests for a specific
  lab ("run a 5-minute test, read the p95").

---

## 9.3 What gets tested where in the pipeline

Mapped to the GitLab stages in [10](10-cicd-gitlab.md):

```
lint    ->  ruff, black --check, eslint, ansible-lint, yamllint,
            ci/check-no-app-code-in-infra.sh, ci/check-comment-standard.sh
test    ->  unit, integration, parity, migration        (Postgres+Redis services)
            molecule                                    (only on ansible/ changes)
build   ->  images
scan    ->  Trivy, pip-audit, npm audit                 (11)
publish ->  push to the GitLab registry
deploy  ->  per-stage, manual gate, then ci/smoke-test.sh
e2e     ->  Playwright against the deployed environment
```

---

## 9.4 Coverage policy — deliberately not a percentage

No global coverage target. Percentage targets reward testing trivial getters
and punish deleting dead code, and this project has one area where
correctness genuinely matters far more than elsewhere.

Instead, two rules:

1. **`practice/engine.py` requires near-total branch coverage.** It is the
   learning algorithm; a silent regression there corrupts learner data in a
   way no user will report as a bug.
2. **Everything else requires a test for each behaviour a student could
   plausibly break**, judged in review.

Worth saying out loud in class: this is a defensible engineering position,
not laziness, and being able to *justify* a testing policy is a more useful
skill than being able to hit a number.

---

## 9.5 Completion checklist

- [ ] Unit + integration + parity + migration suites pass locally.
- [ ] Auth-boundary tests exist and pass (no legacy equivalent — they are new).
- [ ] Every Ansible role has a Molecule scenario including the idempotency check.
- [ ] `ci/smoke-test.sh` passes against at least one deployed stage.
- [ ] Every test file meets the commenting standard ([02](02-authoring-standards.md)).

Next: [10 — CI/CD with GitLab](10-cicd-gitlab.md).
