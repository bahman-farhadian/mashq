# 14 — Stage 2: Docker (Two Tracks)

## Goal

Same services as Stage 1, now packaged as container images and run by the
Docker Engine instead of `systemd`. This stage is deliberately split into
two tracks that share images but differ in *how those images are run* —
the split is the lesson.

## Shared prerequisite: the images

Built once, used by both 2A and 2B (and later by Swarm and Kubernetes —
see [04](04-repository-layout.md)):

```text
infra/docker/images/
├── backend/Dockerfile          # Django + gunicorn, multi-stage build
├── frontend/Dockerfile         # Next.js, multi-stage build (deps → build → runtime)
├── etl/Dockerfile              # data-platform ETL job
└── varnish/Dockerfile          # base Varnish image + default.vcl baked in (06)
```

Multi-stage builds are non-negotiable here as a teaching point: a
`node_modules`-and-devDependencies-laden Next.js build stage should never
end up in the runtime image layer. Same principle for the Django image —
build wheels in one stage, copy only the installed venv/site-packages into
a slim runtime stage. This is also where students first meet
`.dockerignore`, layer caching, and image size as something to actually
look at (`docker image ls`, `docker history`).

Third-party images (Postgres, Redis, MongoDB, ClickHouse, Grafana,
Varnish base if not custom-building) are pulled from their official
upstream images, not rebuilt from scratch — rebuilding a database image
from source is not a lesson worth the class time it costs.

## Track 2A: raw Docker, no Compose

**Purpose: make the pain Compose removes fully visible before removing it.**
Everything by hand: `docker network create`, `docker volume create`,
`docker run` per service with explicit `--network`, `-e`, `-v`, `--restart`,
health checks passed as `--health-cmd`, and manual ordering (start Postgres,
wait for it to be healthy, *then* start the app — no automatic dependency
graph, because raw `docker run` has none).

```text
infra/docker/raw/
├── 00-network-and-volumes.sh
├── 10-postgres.sh
├── 20-redis.sh
├── 30-mongo.sh
├── 40-clickhouse.sh
├── 50-grafana.sh
├── 60-backend.sh
├── 70-frontend.sh
├── 80-varnish.sh
└── README.md                   # explicitly narrates the pain: "notice you had
                                 # to know the start order yourself; notice one
                                 # crashed container doesn't restart its
                                 # dependents; notice there's no single command
                                 # to tear all of this down cleanly"
```

This track should take noticeably longer to get right than 2B, and that
delta is the point — it's the argument for Compose, made experientially
rather than asserted.

## Track 2B: Docker Compose

Same services, declared, with the ordering/health/networking/volume
concerns Compose handles natively:

```text
infra/docker/compose/
├── docker-compose.yml           # base: all services, prod-shaped defaults
├── docker-compose.override.yml  # dev conveniences: bind mounts, hot reload, exposed debug ports
├── .env.example
└── README.md
```

Key things to actually demonstrate, not just configure:

- `depends_on` **with `condition: service_healthy`**, not just start-order —
  a very common real-world Compose mistake is depending on start order and
  assuming that means "ready," which it doesn't (Postgres accepts TCP
  connections before it's finished initializing). Every stateful service
  gets a real healthcheck (`pg_isready`, `redis-cli ping`, a Mongo/
  ClickHouse ping query), and the app's `depends_on` blocks on those, not
  just "container started."
- Named volumes for every stateful service (`postgres-data`,
  `mongo-data`, `clickhouse-data`, `grafana-data`) — and a lab moment
  where students `docker compose down` (keeps volumes) vs.
  `docker compose down -v` (destroys them) and see the difference for
  themselves.
- One overlay network, service-name-based DNS (`postgres`, `redis`, `mongo`
  as literal hostnames the app config points at) — the first time students
  see container-to-container service discovery work automatically, which
  raw Docker (2A) made them do by hand with `--link` or manual `--network`
  wiring.
- `docker-compose.override.yml` as the dev-vs-prod pattern: base file is
  what actually ships, override adds bind-mounted source + a dev server
  command for local iteration. This is the seed of the same
  base/overlay idea Kubernetes formalizes later via Kustomize
  ([16](16-stage-4-kubernetes.md)) —
  worth pointing out explicitly when Stage 4 arrives, as a "you've already
  learned this pattern once" moment.

## What this stage teaches, stage-over-stage

| Compared to Stage 1 | 2A adds | 2B adds on top of 2A |
|---|---|---|
| Process isolation via `systemd` | Filesystem/process isolation via containers; image immutability | Declarative multi-service orchestration; dependency-aware startup; one-command up/down |
| Manual dependency install per box | Reproducible builds (`Dockerfile`, layer caching) | `.env`-driven configuration, dev/prod overlay pattern |
| Bespoke backup script | Volumes as the explicit unit of persistent state | Named volumes managed by the same tool that manages the services |


## Completion checklist

- [ ] The stack runs; `ci/smoke-test.sh` passes ([09](09-testing-strategy.md)).
- [ ] Images are pulled from the GitLab registry by immutable SHA tag, not
      rebuilt ad hoc and not referenced as `latest` ([10](10-cicd-gitlab.md)).
- [ ] The ETL runs on this stage's own scheduling mechanism (host cron invoking `docker run`, or a small scheduler container).
- [ ] Secrets use this stage's mechanism ([11](11-security-and-secrets.md) §11.2).
- [ ] Metrics and logs reach Prometheus/Loki ([12](12-observability-and-slos.md)).
- [ ] A backup/restore drill has been performed ([18](18-operations-and-runbooks.md)).
- [ ] **Every new file meets the commenting standard**
      ([02](02-authoring-standards.md)): header block, a stated reason for
      every non-obvious value, and at least one documented failure mode.
- [ ] The stage README can be followed start-to-finish by someone who has not
      read the other stages.

Next: [15 — Stage 3: Docker Swarm](15-stage-3-swarm.md).
