# 00 — Executive Summary

## What this is

A plan to take Tartarus — currently a single-user, localhost-only Python
application (stdlib `http.server` backend, vanilla-JS frontend, one SQLite
file, JSON files as content) — and turn it into a **teaching artifact**: the
same application, re-platformed onto a production-shaped stack, then
deployed four separate ways in ascending order of operational sophistication.
Students don't learn Docker or Kubernetes in the abstract; they deploy *this
specific, already-working, pedagogically self-consistent app* four times,
watching what changes and — more importantly — what **doesn't** change
between stages.

This directory is the plan only. Nothing here is executable. Each file below
is a design document for one slice of the work; when you're ready to build a
given stage, that file becomes the spec for it.

## The teaching thesis

> **The application and its supporting services stay constant. Only the
> mechanism that packages, schedules, and network-connects them changes.**

That single idea is the spine of the whole curriculum. The same Django app,
the same Next.js frontend, the same Postgres/Redis/Mongo/ClickHouse/Grafana
services, and the same Varnish cache config are deployed:

1. by hand, as OS processes under `systemd`, on a bare VM;
2. as containers, first with raw `docker run`, then with Compose;
3. as a `docker stack` on a Swarm cluster;
4. as workloads on Kubernetes.

Every stage answers the same question — "how do I run five or six
cooperating services reliably?" — with a different tool. Students build
their own mental diff between stages instead of memorizing four unrelated
toolchains.

## Scope of the re-platform

| Layer | Today | Target |
|---|---|---|
| Backend | stdlib `http.server`, one `tartarus.py`/`tartarus_web.py` module pair | Django + Django REST Framework, multi-app project |
| Frontend | Vanilla JS (`web/app.js`, one IIFE), server-rendered HTML shell | Next.js (App Router), light/dark theme |
| Primary datastore | One SQLite file (`data/tartarus.db`) | PostgreSQL (rationale: [01](01-architecture-and-technology-decisions.md)) |
| Content storage | JSON files under `data/word_lists/` | PostgreSQL tables + Django admin, JSON import preserved as an authoring path |
| Interaction analytics | None (only structural facts in a text log) | MongoDB (raw event stream) → ClickHouse (aggregated/OLAP) → Grafana |
| Cache | None | Redis (app/object cache, Celery broker, sessions) + Varnish (HTTP edge cache) |
| Observability | One rotating text log | Prometheus + Grafana (metrics), Loki (logs) — see [01](01-architecture-and-technology-decisions.md) §6 |
| Deployment | `make web`, one process | VM → Docker → Swarm → Kubernetes, see files `07`–`10` |

## What must not be lost in the re-platform

The current README documents a set of *invariants* the scoring engine
guarantees — scores never regress, a session never mixes question modes, due
work always outranks new work, and so on (see the project's own
`README.md`, "What you can rely on"). None of that is negotiable. The
Django migration ([03](03-backend-migration-django.md)) is explicitly a
**port**, not a rewrite: the scheduling algorithm in `utils/tartarus.py`
becomes a framework-agnostic service layer inside Django, covered by the
same style of test suite, before any of the deployment work begins. Get the
domain logic right once, in one place, and every later stage just needs to
run it.

## A gap that must be closed before any network exposure

The current app's security model is explicit and deliberate: **local-first,
trusted-client, no auth layer** (README, "Local-first security model"). That
is the correct choice for a localhost tool and the wrong choice for anything
reachable from a classroom network, a VM with a public IP, or a Kubernetes
Ingress. The Django migration must add real authentication/authorization
(session or JWT-based, enforced server-side, one learner's data never
addressable by another's request) as a first-class part of the port — not a
stretch goal. This is called out again in
[03](03-backend-migration-django.md) and treated as a blocking item, not an
optional hardening pass.

## Documents in this plan

| # | File | Answers |
|---|---|---|
| 00 | `00-executive-summary.md` | this file |
| 01 | `01-architecture-and-technology-decisions.md` | Every "which technology" question you asked, each with a recommendation and the reasoning behind it (Postgres vs. Galera, Mongo→ClickHouse→Grafana pipeline, Redis, Varnish, the observability stack) |
| 02 | `02-target-repository-layout.md` | What the monorepo looks like once this is fully built |
| 03 | `03-backend-migration-django.md` | How `tartarus.py`/`tartarus_web.py` becomes a Django project, app by app, model by model |
| 04 | `04-frontend-migration-nextjs.md` | How `web/app.js` becomes a Next.js app, view by view |
| 05 | `05-data-platform-mongo-clickhouse-grafana.md` | Event schema, ETL design, ClickHouse schema, dashboard plan |
| 06 | `06-caching-redis-varnish.md` | What each cache layer is actually for, and how they invalidate |
| 07 | `07-stage-1-vm-deployment.md` | Stage 1 — bare Linux VM |
| 08 | `08-stage-2-docker.md` | Stage 2 — raw Docker, then Docker Compose |
| 09 | `09-stage-3-docker-swarm.md` | Stage 3 — Docker Swarm / `docker stack` |
| 10 | `10-stage-4-kubernetes.md` | Stage 4 — Kubernetes |
| 11 | `11-load-simulator.md` | The always-on ~1000-learner simulator and historical data seeder |
| 12 | `12-classroom-delivery-guide.md` | How to actually teach this: sequencing, labs, objectives, timing |

Read `01` next — it resolves every open technology question before the
stage-by-stage plans build on top of those decisions.
