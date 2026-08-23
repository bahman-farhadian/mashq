# Tartarus Deployment Roadmap

A plan to re-platform Tartarus (currently: stdlib Python backend, vanilla-JS
frontend, one SQLite file, JSON content) into a production-shaped stack —
Django, Next.js, PostgreSQL, MongoDB, ClickHouse, Grafana, Redis, Varnish —
and deploy it four progressively more sophisticated ways for a DevOps/SRE
classroom: bare VM → Docker (raw, then Compose) → Docker Swarm →
Kubernetes.

**This directory is documentation only.** Nothing here has been
implemented; nothing in the application, its data, or its deployment has
been changed. Each file is the spec to build against when that piece of
work is actually picked up.

## Read in this order

| # | File | What it covers |
|---|---|---|
| 00 | [Executive Summary](00-executive-summary.md) | The whole plan in one page: scope, the core teaching thesis, what must survive the migration unchanged, and the one gap (auth) that must be closed before any network exposure |
| 01 | [Architecture & Technology Decisions](01-architecture-and-technology-decisions.md) | Every "which technology" question answered with a recommendation and reasoning — Django, Next.js, **PostgreSQL vs. MariaDB Galera**, the Mongo→ClickHouse→Grafana pipeline, Redis, **Varnish (open-source) vs. Enterprise**, and a recommended addition: Prometheus/Loki |
| 02 | [Target Repository Layout](02-target-repository-layout.md) | The full monorepo directory tree this plan builds toward |
| 03 | [Backend Migration: Django](03-backend-migration-django.md) | App-by-app, model-by-model port of `tartarus.py`/`tartarus_web.py`; auth; cutover plan for existing data |
| 04 | [Frontend Migration: Next.js](04-frontend-migration-nextjs.md) | View-by-view port of `web/app.js`; dark/light theming; which interaction rules must survive verbatim |
| 05 | [Data Platform: Mongo → ClickHouse → Grafana](05-data-platform-mongo-clickhouse-grafana.md) | Event schema, ETL design, ClickHouse schema, dashboard plan |
| 06 | [Caching: Redis and Varnish](06-caching-redis-varnish.md) | What each cache layer is for, what's never cached, and why |
| 07 | [Stage 1 — VM Deployment](07-stage-1-vm-deployment.md) | Every service as a `systemd`-managed OS process |
| 08 | [Stage 2 — Docker](08-stage-2-docker.md) | Track A: raw `docker run`. Track B: Docker Compose |
| 09 | [Stage 3 — Docker Swarm](09-stage-3-docker-swarm.md) | Multi-node cluster, `docker stack`, secrets/configs, rolling updates |
| 10 | [Stage 4 — Kubernetes](10-stage-4-kubernetes.md) | StatefulSets, operators, Ingress, HPA, NetworkPolicy, chaos exercises |
| 11 | [Load Simulator & Data Seeder](11-load-simulator.md) | The always-on ~1000-learner simulator, plus a compressed-time historical backfill |
| 12 | [Classroom Delivery Guide](12-classroom-delivery-guide.md) | Sequencing, per-stage learning objectives, lab structure, assessment ideas |

## The one-sentence thesis, if you only remember one thing

> The application and its supporting services stay constant across all four
> stages. Only the mechanism that packages, schedules, and connects them
> changes — and that diff, made visible, *is* the curriculum.

## Direct answers to the questions you asked

- **MariaDB Galera or PostgreSQL?** → PostgreSQL runs the app; Galera is a
  deliberately separate, isolated elective lab about multi-master
  replication. Full reasoning: [01 §3](01-architecture-and-technology-decisions.md#3-primary-application-database-postgresql-not-mariadb-galera).
- **Varnish free or not?** → Free/open-source (Varnish Cache). It's the
  same caching engine Enterprise ships; Enterprise adds support/tooling
  this deployment doesn't need. Full reasoning: [01 §5](01-architecture-and-technology-decisions.md#5-cache-layer-redis--varnish-open-source).
- **"Three stages" of deployment** → Written up as four numbered stages
  (VM, Docker, Swarm, Kubernetes) because Docker itself splits into the two
  tracks you described (raw vs. Compose) — see the note at the top of
  [08](08-stage-2-docker.md) and the sequencing table in
  [12](12-classroom-delivery-guide.md).

## Next step

Nothing needs to be built yet. When you're ready to start on a specific
piece, say which file/stage, and that becomes an implementation task —
this plan stays as the reference the implementation is checked against.
