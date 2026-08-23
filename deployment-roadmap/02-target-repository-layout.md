# 02 — Target Repository Layout

This is the shape of the monorepo once every stage in this plan has been
built out. It does not exist yet — this file is the blueprint the actual
directories get built against, one path at a time, as each stage in
`07`–`10` is implemented.

## Top-level layout

```text
tartarus/
├── deployment-roadmap/          # this plan (already exists)
├── legacy/                      # the current stdlib app, frozen as a reference
│   ├── utils/                   #   (moved here verbatim once Django parity is reached —
│   ├── web/                     #    see 03 §"Cutover", never deleted, always runnable)
│   └── README.md
│
├── backend/                     # Django project — see 03
│   ├── config/                  # Django settings module (base/dev/prod/test split)
│   ├── apps/
│   │   ├── accounts/
│   │   ├── content/
│   │   ├── progress/
│   │   ├── practice/
│   │   ├── sessions/
│   │   ├── mastery/
│   │   └── analytics/
│   ├── manage.py
│   ├── pyproject.toml
│   └── tests/
│
├── frontend/                    # Next.js app — see 04
│   ├── app/                     # App Router
│   ├── components/
│   ├── lib/                     # API client, hooks
│   ├── styles/                  # design tokens, theme
│   ├── public/
│   ├── package.json
│   └── tests/
│
├── data-platform/                # See 05
│   ├── etl/                      # Mongo -> ClickHouse job(s)
│   ├── clickhouse/               # schema migrations (table DDL)
│   ├── mongo/                    # index definitions, event schema docs
│   └── grafana/
│       ├── dashboards/           # provisioned dashboard JSON, per folder
│       │   ├── product-analytics/
│       │   └── sre-infra/
│       └── provisioning/         # datasources.yaml, dashboards.yaml
│
├── caching/                       # See 06
│   └── varnish/
│       └── default.vcl
│
├── simulator/                     # See 11 — always-on synthetic learners
│   ├── seed/
│   ├── agents/
│   ├── runner/
│   ├── scenarios/
│   ├── metrics/
│   └── config/
│
├── infra/                         # Everything deployment-mechanism-specific
│   ├── vm/                        # Stage 1 — 07
│   │   ├── provision/             # shell scripts, one per service
│   │   ├── systemd/               # unit files
│   │   ├── nginx/
│   │   └── varnish/
│   ├── docker/                    # Stage 2 — 08
│   │   ├── images/                # one Dockerfile per service (backend, frontend, etl)
│   │   ├── raw/                   # 2A: docker run / network / volume scripts, no compose
│   │   └── compose/               # 2B: docker-compose.yml + overrides
│   ├── swarm/                      # Stage 3 — 09
│   │   ├── stack.yml
│   │   └── secrets/                # (paths/instructions, never real secret values)
│   └── k8s/                        # Stage 4 — 10
│       ├── base/                   # kustomize base or plain manifests
│       ├── overlays/
│       │   ├── dev/
│       │   └── prod/
│       └── helm/
│           └── tartarus/           # umbrella chart, or values.yaml for 3rd-party charts
│
├── docs/                            # Generated/reference docs (schema guide, API reference)
├── Makefile                         # top-level convenience targets, one per stage
└── README.md
```

## Design notes

- **`legacy/` is not a throwaway.** Moving the current `utils/`/`web/` there
  (rather than deleting) keeps the original, working, single-file
  application runnable throughout the whole migration — it's the reference
  implementation the Django port is checked against (see
  `03` §"Parity testing"). It is also, on its own, still a perfectly valid
  "Stage 0" a class could look at before Stage 1 even starts: *this is what
  'no deployment story at all' looks like.*

- **One `infra/` tree, one subdirectory per stage.** Deliberately not four
  separate repos or branches. Students `cd infra/<stage>` and everything
  for that stage is there; `git log -- infra/docker/compose/` gives a clean
  history of just that stage's evolution. `git diff` between stage
  directories is itself a teaching artifact ("here's exactly what changed
  going from Compose to Swarm").

- **`infra/docker/images/` is shared by all three container-based stages.**
  The same Dockerfiles get referenced by raw `docker run` (2A), Compose
  (2B), Swarm (`09`), and — after being pushed to a registry — Kubernetes
  (`10`). Building the image is decoupled from however it gets *run*, which
  is exactly the point being taught.

- **`caching/varnish/default.vcl` is one file used by every stage** from
  Stage 1 onward — copied onto the VM, baked into a container image,
  mounted as a Swarm config, mounted as a Kubernetes ConfigMap. Same file,
  four delivery mechanisms — a small, concrete example of the plan's core
  thesis in [00](00-executive-summary.md).

- **`simulator/` is deployment-target-agnostic.** It talks to the app over
  HTTP/its public API only, so the same simulator points at
  `http://vm-host:8000`, `http://localhost:8000` (Compose), the Swarm
  ingress, or the Kubernetes Ingress hostname, via one config value. See
  `11`.

- **`data-platform/` is intentionally its own top-level directory**, not
  nested under `backend/`, because the ETL job is a separate deployable
  unit with its own lifecycle (it can be down while the app stays up), and
  because in later stages it gets its own container image, its own
  Kubernetes CronJob/Deployment, independent of the Django app's.

## How this layout maps onto the stage files

| Stage file | Reads/writes under |
|---|---|
| `03` (Django) | `backend/` |
| `04` (Next.js) | `frontend/` |
| `05` (data platform) | `data-platform/` |
| `06` (caching) | `caching/`, plus Redis config inside `backend/config/` |
| `07` (VM) | `infra/vm/` |
| `08` (Docker) | `infra/docker/` |
| `09` (Swarm) | `infra/swarm/` |
| `10` (Kubernetes) | `infra/k8s/` |
| `11` (simulator) | `simulator/` |

Next: [03 — Backend Migration to Django](03-backend-migration-django.md).
