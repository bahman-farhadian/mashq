# 03 — Architecture Decisions

Each section is an ADR: **context → options → decision → consequences**.
Written so a student can read it and understand *why*, not just *what* — and
so you can defend each choice in front of a class that asks "but why not X?"

---

## ADR-1 — Backend framework: Django + DRF

**Context.** The current backend is a single stdlib `http.server` module pair
with hand-rolled routing. It needs a real framework to gain auth, an ORM, an
admin, and a maintainable API surface.

**Decision.** Django with Django REST Framework.

**Reasoning.**

- Django's ORM makes the SQLite→PostgreSQL migration tractable. The current
  per-user dynamic tables (`words_<user>_<list>`) are a SQLite-era workaround
  that Django replaces with one normalised `Progress` table.
- DRF gives the Next.js frontend a versioned, typed JSON API, replacing the
  ad-hoc `POST /api/*` handlers.
- Django's auth system closes the security gap in
  [00](00-executive-summary.md) — session auth for the browser, token auth
  for the simulator.
- `django-admin` becomes the word-list content editor almost for free,
  replacing the bespoke editor view.

**Consequences.** Django's "batteries included" surface is large; students
will meet settings modules, migrations, and the request/response cycle.
That is acceptable — it is one framework, taught once, used in all four
stages.

---

## ADR-2 — Frontend framework: Next.js

**Context.** Requested. The current frontend is one IIFE in `web/app.js`
with no build step, no component model, and no theming.

**Decision.** Next.js (App Router), with dark/light theming via CSS custom
properties and `next-themes`.

**Reasoning.** Rendering mode is deliberately mixed, not uniform:

- Practice-session screens are stateful, server-authoritative, and
  latency-sensitive → **Client Components** talking to the DRF API.
- Content-heavy views (About, list browsing) → **Server Components** /
  static generation, which also makes them cacheable by Varnish (ADR-9).

**Consequences.** A Node build step and a second runtime in every stage.
Detail in [06](06-frontend-nextjs.md).

---

## ADR-3 — Primary database: PostgreSQL, not MariaDB Galera

**Context.** You asked directly whether a MariaDB Galera master-master
cluster is a good idea for the application database.

**Decision.** **PostgreSQL is the primary datastore.** Galera is kept as a
separate elective lab (§ADR-4), not as the store the app writes to.

**Why not Galera as the primary store.** Galera's "multi-master" label is
true at the replication-protocol level and misleading at the
application-safety level:

- **Write conflicts are real.** Two nodes committing conflicting writes to
  the same row cause a *certification failure* on one, surfaced to the app
  as a deadlock-class error **after** the transaction appeared to succeed
  locally. Django's ORM does not retry this for you. Every write path needs
  explicit retry logic — or you pin all writes to one node via ProxySQL, at
  which point you have paid Galera's complexity for a single-writer system.
- **This app's write pattern is the adversarial case.** Many small
  transactions repeatedly hitting the *same* row (one learner's `Progress`
  row, once per answer, in bursts) is precisely the pattern most likely to
  certification-fail under concurrent multi-node writes.
- **Weaker JSON support** than Postgres's `JSONB`, which matters for
  word-list content whose shape genuinely varies (vocabulary vs. sentence
  entries, multiple accepted answer forms).
- **Django's MySQL/MariaDB backend is a second-class citizen** — fewer field
  types, no native array fields, historically behind on new releases.

**Why PostgreSQL.**

- The reference database for Django; best-supported path by a wide margin.
- `JSONB` + GIN indexes for semi-structured content without EAV tables.
- Strong transactional integrity for the score/Leitner/consolidation state
  machine — the one part of this app that must never silently corrupt.
- For HA, the open-source-native pattern is **Patroni + etcd**: one leader,
  N standbys, automatic failover. Not multi-master — but well understood,
  and it *fails safe* (brief write unavailability during failover, never a
  silent conflict).

**Consequences.** No multi-master write availability. Accepted: this app has
one writer per learner and no geographic distribution requirement.

---

## ADR-4 — Galera survives as an isolated elective lab

**Context.** Multi-master replication, quorum, and split-brain are excellent
SRE topics. The objection in ADR-3 is to putting *this application's* data
on it, not to teaching it.

**Decision.** **Module 4B — "Galera: Multi-Master Replication and Its
Gotchas"**, run alongside Stage 4 in its own namespace, wired to nothing.

**Shape of the lab.** Students stand up a 3-node MariaDB Galera cluster with
ProxySQL, run a conflict-inducing write workload, then *deliberately remove
the write-pinning* and watch certification failures happen live.

**Reasoning.** This teaches the multi-master lesson honestly — including the
sharp edges — instead of hiding them behind a configuration that only works
because it never actually writes from two nodes at once.

---

## ADR-5 — No microservices: modular monolith + horizontal replicas

**Context.** You asked whether to slice the monolith into microservices
"without introducing overengineering."

**Decision.** **No microservice decomposition.** Django stays one deployable
with clean internal app boundaries. Scale comes from **replica count**, not
service count.

**Reasoning.**

The application is a *single bounded context*. The practice engine reads
`progress` + `content` + `mastery` inside one transaction to decide what
question comes next. Splitting those into networked services would convert a
local transaction into a distributed one, forcing sagas or eventual
consistency into a course that is **not about development**. Students would
spend class time debugging distributed state instead of learning deployment.

The orchestration lesson does not require many services — it requires
**many replicas of a service**, which is where load balancing, rolling
updates, health checks, and autoscaling actually live.

**Deployable units** (each its own image/unit, its own lifecycle):

```
frontend    Next.js                     <- replicated
backend     Django + gunicorn           <- replicated  (modular monolith)
varnish     HTTP edge cache             <- replicated
etl         Mongo -> ClickHouse job     <- scheduled, independent lifecycle
simulator   synthetic load generator    <- out-of-band
postgres · redis · mongodb · clickhouse · prometheus · grafana · loki
```

~12 services — ample for Swarm and Kubernetes to be interesting — with zero
artificial network boundaries.

**Internal boundaries stay clean anyway.** The Django apps
(`accounts`, `content`, `progress`, `practice`, `sessions`, `mastery`,
`analytics` — see [05](05-backend-django.md)) are separated as if they
*might* one day split. That is the modular monolith's actual benefit: the
option is preserved without paying for it now.

**Consequences.** No independent per-domain scaling or deploy. Neither is
needed at this scale, and both are demonstrated adequately by the
independently-deployed `etl` and `frontend`.

---

## ADR-6 — The backend must become stateless (the enabling change)

**Context.** `utils/tartarus_web.py:49` holds live practice sessions in a
module-level Python dict:

```python
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()
```

**Decision.** Move live session state to **Redis**, keyed `session:<uuid>`
with a TTL, before any multi-replica deployment.

**Reasoning — and why this is a headline lesson, not a footnote.**

This single dict is why the legacy app *cannot* run more than one copy. Two
processes would each hold half the sessions; a learner's next request would
land on the wrong one and their session would vanish. Every scaling lesson
in Stages 2–4 depends on fixing it.

It also deletes code: Redis's native key expiry replaces the hand-written
`cleanup_sessions()` TTL sweep entirely.

**Teaching thread.** Pose it as a question before revealing the answer:
*"why can't I just run three copies of the legacy app?"* → in-process state
→ externalise it → **now** replicas mean something. Students who work
through that understand statelessness far better than students told about
it.

**Consequences.** Redis becomes a hard dependency of the request path. If
Redis is down, in-flight sessions are lost — an acceptable trade, and itself
a good failure-domain discussion.

---

## ADR-7 — No Celery, no message broker

**Context.** You asked directly: "do we truly need Celery here?"

**Decision.** **No.** Drop Celery, its worker, its beat scheduler, and its
broker.

**Reasoning.**

1. **There is no background work today.** Verified in the current code: the
   only `threading` use in `utils/tartarus_web.py` is the session-dict lock.
   Nothing is queued, deferred, or scheduled.
2. **The only recurring job is the Mongo→ClickHouse ETL** — and it is
   *better* as a standalone scheduled deployable, because "how do I run a
   scheduled job" has a genuinely different answer in each stage:

   | Stage | Mechanism |
   |---|---|
   | 1 — VM | `systemd` timer |
   | 2 — Docker | host cron invoking `docker run`, or a small scheduler container |
   | 3 — Swarm | long-running scheduler service (Swarm has no native cron) |
   | 4 — Kubernetes | `CronJob` |

   That progression *is* the curriculum's thesis in miniature. Celery would
   hide it behind one uniform abstraction and teach nothing.
3. **Analytics writes don't need a queue.** Events go to MongoDB
   fire-and-forget with a short timeout and graceful degradation: if Mongo is
   unreachable, the write is dropped and logged, and practice continues.
   Dashboards go stale; learners are unaffected. At this scale (~1000
   simulated learners ≈ a few hundred writes/sec) Mongo absorbs this trivially.
4. **Cost avoided:** two services, a broker dependency, and an entire class
   of "why is my task stuck in PENDING" debugging that is not this course's
   lesson.

**Consequences.** Redis remains — for session state (ADR-6), Django's cache,
and rate limiting — but is **no longer a broker**. If genuine async work
appears later, Celery can be added then; nothing here forecloses it.

---

## ADR-8 — Analytics: MongoDB → ClickHouse → Grafana

**Context.** You specified Mongo for interaction data and ClickHouse +
Grafana for analytics.

**Decision.** Adopt as specified, with a batch ETL between them.

**Reasoning.**

- **Mongo is the raw event sink**, not the query engine: schema-flexible
  (every event type has a different payload), high write throughput, no
  upfront schema negotiation.
- **Mongo is a poor dashboard backend** at volume. Aggregating millions of
  documents for "accuracy trend by day" is exactly ClickHouse's design
  centre (columnar, vectorised) and exactly where Mongo's aggregation
  pipeline struggles.
- **ETL, not application dual-writes.** The app writes once, to Mongo. A
  separate job moves data to ClickHouse. If ClickHouse is down, learners are
  unaffected — only dashboards go stale. Two independent failure domains, on
  purpose, and a demo worth doing live.

Detail in [07](07-data-platform.md).

---

## ADR-9 — Two caches: Redis (application) and Varnish OSS (HTTP edge)

**Context.** You asked whether to use the free version of Varnish.

**Decision.** **Varnish Cache (open-source, BSD-2-Clause).** Not Enterprise.

**Reasoning.** Varnish Enterprise adds a GUI, extra VMODs, and vendor
support. For teaching HTTP caching fundamentals — VCL hooks, hit/miss/pass,
TTLs, purge vs. ban, cache stampedes — **the OSS edition is the same caching
engine**, and is what most real deployments run. There is no reason to bring
commercial licensing into a classroom.

**The two caches are not redundant.** They sit at different layers and
cannot substitute for each other:

- **Redis** — inside the app: session state, Django cache, rate limiting.
  Personalised, per-user, requires atomic operations.
- **Varnish** — in front of the app: whole HTTP responses, keyed by URL, for
  content that is identical for everyone.

Authenticated practice endpoints are **never** cached by Varnish; serving
one learner's next question to another is the failure mode that section of
[08](08-caching.md) is built around.

---

## ADR-10 — Observability is core, not optional

**Context.** Not in your original list. Flagged as a recommendation rather
than added silently.

**Decision.** Include **Prometheus + Alertmanager + Grafana + Loki**, with
defined SLIs and SLOs.

**Reasoning.** For a *DevOps/SRE* course, shipping without observability
would be a curriculum gap, not a simplification. Half of SRE is knowing
whether the thing you deployed is healthy — and SLOs/error budgets are the
discipline that separates SRE from sysadmin work.

Grafana serves both worlds from one instance: a **Product Analytics** folder
(ClickHouse) and an **SRE** folder (Prometheus + Loki). That split — business
metrics vs. system metrics, same tool — is itself a lesson.

Detail in [12](12-observability-and-slos.md).

---

## Summary

| Concern | Choice | ADR |
|---|---|---|
| Backend | Django + DRF | 1 |
| Frontend | Next.js, dark/light | 2 |
| Primary DB | PostgreSQL (Patroni for HA) | 3 |
| Multi-master lab | MariaDB Galera + ProxySQL, isolated | 4 |
| Service decomposition | Modular monolith + replicas — **no microservices** | 5 |
| Session state | Redis — **the enabling change** | 6 |
| Background jobs | **No Celery** — per-stage scheduled ETL | 7 |
| Events / analytics | MongoDB → ClickHouse → Grafana | 8 |
| Caching | Redis + Varnish **OSS** | 9 |
| Observability | Prometheus + Alertmanager + Grafana + Loki, with SLOs | 10 |

Next: [04 — Repository Layout](04-repository-layout.md).
