# 01 — Architecture and Technology Decisions

Each section below is a mini-ADR (Architecture Decision Record): the
question, the options, the recommendation, and the reasoning — written so a
student can read it and understand *why*, not just *what*.

---

## 1. Backend framework: Django

Not really in question — you specified it — but worth stating the fit
explicitly, because it constrains later choices:

- Django's ORM is what makes the SQLite→PostgreSQL migration in
  [03](03-backend-migration-django.md) tractable: the current per-user,
  per-list dynamic table creation (`words_<user>_<list>`) is a SQLite-era
  workaround that Django replaces with one normalized `Progress` table.
- Django REST Framework (DRF) gives the Next.js frontend a typed, versioned
  JSON API surface, replacing the current ad hoc `POST /api/*` handlers in
  `tartarus_web.py`.
- Django's built-in auth system is exactly what closes the security gap
  called out in [00](00-executive-summary.md) — session auth for
  same-origin browser use, and DRF's token/JWT auth for the simulator
  ([11](11-load-simulator.md)) and any future mobile client.
- `django-admin` becomes the word-list content editor almost for free,
  replacing the bespoke "Word Lists" editor view in `web/app.js`.

## 2. Frontend framework: Next.js

Also specified. Two implementation notes that matter for
[04](04-frontend-migration-nextjs.md):

- **Theming**: dark/light mode via CSS custom properties (design tokens) at
  `:root`, toggled with `next-themes` (handles the flash-of-wrong-theme
  problem and respects `prefers-color-scheme` by default — matches how the
  existing app should *not* hardcode a single look).
- **Rendering mode**: the practice session screens are inherently stateful,
  server-authoritative, and low-latency-sensitive (see the existing
  "During prompt speech" interaction rules in the project README) — those
  are Client Components talking to the DRF API. Static/content-heavy views
  (About, Word List browsing, marketing/landing) are Server Components /
  static generation. This isn't "everything SSR" or "everything CSR" —
  it's deliberately mixed, and `04` says which is which per view.

## 3. Primary application database: PostgreSQL, not MariaDB Galera

You asked directly: *"is Galera master-master a good idea, or is Postgres
better?"* Direct answer: **PostgreSQL is the primary datastore.** Run Galera
too, but as a separate, clearly-labeled elective module, not as the thing
the live application writes to.

### Why not Galera as the primary store

Galera's "multi-master" label is true at the replication-protocol level
(synchronous, certification-based replication; any node accepts writes) and
false at the application-safety level for a naive ORM:

- **Write conflicts are real, not theoretical.** Two nodes committing
  conflicting writes to the same row causes a *certification failure* on
  one of them, surfaced to the app as a deadlock-class error
  (`ER_LOCK_DEADLOCK`) *after* the transaction looked like it succeeded
  locally. Django's ORM has no idea this can happen mid-`save()` and will
  not retry for you — every write path needs explicit retry-on-deadlock
  logic, or you route all writes through one node at a time (via ProxySQL)
  and Galera's "multi-master" property becomes "multi-master, but we only
  use one master, for safety" — at which point you've paid Galera's
  operational complexity for a single-writer system.
- **Hot spots make it worse.** This app's write pattern is exactly the
  adversarial case: many small transactions hitting the *same* row
  (`Progress` row for a given `user_id, item_id`) repeatedly, in bursts
  (session answer → next question → answer → ...). That's the pattern most
  likely to certification-fail under concurrent multi-node writes.
- **No native JSON story as clean as Postgres's `JSONB`**, which matters
  for word-list content that has nested, semi-structured fields
  (definitions, multiple accepted forms — see `data/DATASET_SCHEMA_GUIDE.md`
  in the current repo).
- **Django's MariaDB/MySQL backend is a second-class citizen** compared to
  `django.db.backends.postgresql` — fewer field types, no native array
  fields, historically behind on migrations for newer Django releases.

### Why PostgreSQL

- First-class Django support (this is *the* reference database for Django).
- `JSONB` with GIN indexes for word-list content — schema-flexible where the
  content genuinely varies (vocabulary vs. sentence entries, multiple
  accepted answer forms) without needing EAV tables.
- Real transactional integrity for the score/Leitner/consolidation-step
  state machine, which is the one piece of this app that must never
  silently corrupt (that's the entire premise of the project's own
  "What you can rely on" guarantees).
- For HA, the open-source-native pattern is **Patroni + etcd/Consul**:
  one leader, N synchronous/async standbys, automatic leader election on
  failure. It is *not* multi-master — but it is a well-understood,
  battle-tested pattern with a huge amount of teaching material, and it
  fails safe (you lose write availability briefly during failover; you
  never get a silent conflict).

### Where Galera still belongs in the curriculum

Multi-master replication, quorum, and split-brain are *excellent* SRE
topics — just not ones this particular application's write pattern should
be the guinea pig for. Recommendation: add a **Module 4B — "Galera Cluster:
Multi-Master Replication & Its Gotchas"** as an elective/advanced lab
alongside the Kubernetes stage (`10`). Students stand up a 3-node MariaDB
Galera cluster, put ProxySQL in front of it, run a small conflict-inducing
write workload (the simulator from `11` can be pointed at it in isolation),
*deliberately* remove the write-pinning, and watch certification failures
happen live. That teaches the multi-master lesson honestly — including its
sharp edges — instead of quietly papering over them by never writing from
two nodes at once, which is what running Galera "safely" in production
actually requires anyway.

**Bottom line:** Postgres runs the app. Galera is a lab about Galera.

## 4. Interaction analytics: MongoDB → ClickHouse → Grafana

This is the right shape for what you described, and it's a standard,
teachable pattern (raw event lake → OLAP warehouse → dashboards). Full
design in [05](05-data-platform-mongo-clickhouse-grafana.md); the decision
points:

- **MongoDB is the raw event sink, not a queryable analytics store.**
  Every learner interaction (question shown, answer submitted, session
  started/ended, drill triggered, track selected) is written as one
  schema-flexible document. Mongo is good at this: high write throughput,
  no upfront schema negotiation, natural fit for "every event type has a
  slightly different payload shape."
- **Mongo is a poor fit for the actual dashboards.** Aggregation across
  millions of event documents (accuracy trend by day, mastery velocity,
  session-length distribution) is exactly ClickHouse's design center
  (columnar storage, vectorized execution) and exactly where Mongo's
  document-at-a-time aggregation pipeline starts to struggle at volume.
- **ETL, not application dual-writes.** The Django app writes once, to
  Mongo. A separate small service reads from Mongo and writes aggregated,
  typed rows into ClickHouse. This keeps the request path's write
  latency independent of the analytics pipeline's health — if ClickHouse is
  down, learners can still practice; only dashboards go stale. `05`
  specifies a **batch job by default** (simple, easy to teach, easy to
  demo failure/recovery of) with **Mongo Change Streams as an advanced/
  optional upgrade** to near-real-time (good follow-on lab about CDC).
- **Grafana reads ClickHouse** for product/learning analytics dashboards.

## 5. Cache layer: Redis + Varnish (open-source)

Two different caches, two different jobs — see
[06](06-caching-redis-varnish.md) for full detail:

- **Redis** — application-level: Django cache backend (word-list content,
  computed roadmap/report payloads), Celery broker (background jobs: the
  Mongo→ClickHouse ETL, audio pregeneration), and Django session store.
  This is *inside* the app's trust boundary.
- **Varnish** — HTTP edge cache, in front of the app, caching whole
  responses by URL/headers with VCL-driven rules and explicit
  purge/ban on content changes.

### Varnish: free (Community/open-source) edition — yes, use it

Varnish has two editions: the open-source **Varnish Cache** (BSD-2-Clause,
free) and commercial **Varnish Enterprise** (paid — adds a GUI, some extra
VMODs, WAF-adjacent features, vendor support SLA). For teaching HTTP
caching fundamentals — VCL request/response hooks, cache hit/miss/pass
states, TTLs, purge vs. ban, the classic "thundering herd" and
cache-stampede problems — **the open-source edition is not a limited trial;
it's the same caching engine**, and it's what the overwhelming majority of
real Varnish deployments run. There is no reason to introduce Enterprise
licensing into a classroom environment. Recommendation: **Varnish Cache
(OSS), full stop.**

## 6. Addition not in your list: an observability stack

You didn't ask for this, so flagging it explicitly as a recommendation
rather than folding it in silently: for a **DevOps/SRE class**, shipping an
app without Prometheus/Grafana/Loki would be a curriculum gap, not a
simplification. Recommendation — add:

- **Prometheus** — scrapes Django (via `django-prometheus`), Postgres
  (`postgres_exporter`), Redis (`redis_exporter`), Node/host metrics
  (`node_exporter`), and container/orchestrator metrics (`cAdvisor` in
  Docker stages, kube-state-metrics + built-in kubelet metrics in
  Kubernetes).
- **Alertmanager** — paired with Prometheus, for a "write your first alert
  rule" lab (e.g., alert on session-answer p95 latency, or on Postgres
  replication lag once Patroni is introduced).
- **Loki** (+ Promtail/Vector as the log shipper) — centralized logs,
  replacing "SSH in and `tail -f tartarus.log`" once there's more than one
  node.
- **Grafana serves both worlds** from one instance: a *Product Analytics*
  dashboard folder backed by ClickHouse (from §4), and an *SRE/Infra*
  dashboard folder backed by Prometheus + Loki. This split (business
  metrics vs. system metrics, same visualization tool) is itself a useful
  lesson.

This addition is scoped as optional-but-recommended per stage — introduce
Prometheus/Grafana starting at Stage 2 (Docker Compose) once there's more
than one process to watch, and treat it as core (not optional) by Stage 4
(Kubernetes), where `kube-prometheus-stack` is close to a default
expectation in real clusters.

## 7. Summary: full target stack

| Concern | Technology | Rationale summary |
|---|---|---|
| Backend framework | Django + DRF | Best Postgres/ORM story, admin for free, batteries-included auth |
| Frontend framework | Next.js | Requested; App Router, dark/light via `next-themes` |
| Primary database | PostgreSQL | Best Django fit, JSONB, safe single-writer HA (Patroni) |
| Multi-master lab (elective) | MariaDB Galera + ProxySQL | Teaches multi-master replication/quorum honestly, kept out of the app's critical path |
| Event/interaction store | MongoDB | Flexible schema, high write throughput, not queried directly by dashboards |
| Analytics warehouse | ClickHouse | Columnar OLAP, fed by a Mongo→ClickHouse ETL job |
| Dashboards | Grafana | One tool, two datasources (ClickHouse for product, Prometheus for infra) |
| App/object cache | Redis | Django cache backend, Celery broker, sessions |
| HTTP edge cache | Varnish Cache (OSS) | Free, full-featured, industry-standard VCL caching |
| Metrics | Prometheus + Alertmanager | Standard SRE toolchain, added recommendation |
| Logs | Loki + Promtail/Vector | Centralized logging once beyond one node |
| Background jobs | Celery (Redis broker) | ETL scheduling, audio pregeneration, async work |

Next: [02 — Target Repository Layout](02-target-repository-layout.md) turns
this into an actual directory tree.
