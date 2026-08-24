# 04 — Repository Layout

## 4.1 The invariant this layout exists to enforce

> **I1 — One codebase, four deployments.**
>
> `backend/` and `frontend/` are built **once**. Each `infra/<stage>/`
> directory contains deployment descriptors **only** — never application
> source, never a forked copy, never a stage-specific patch to app code.
>
> If a stage needs different behaviour, that is **configuration, injected at
> runtime**.

This is the single most important structural rule in the project. The entire
curriculum rests on students being able to diff two stages and see that
*only the deployment mechanism changed*. If application code differs between
stages, the thesis in [00](00-executive-summary.md) is false and the course
teaches four unrelated toolchains instead of one idea.

### How it is enforced

Not by trust. `ci/check-no-app-code-in-infra.sh`, run on every pipeline
([10](10-cicd-gitlab.md)), fails the build if application source appears
under `infra/`:

```bash
#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Enforces invariant I1: no application source code under infra/.
#
# Session:      taught in 10 (CI/CD); the rule itself is from 04
# Runs in:      the 'lint' stage of .gitlab-ci.yml, on every commit
# Fails when:   someone copies app code into a stage directory instead of
#               parameterising the existing app
# ---------------------------------------------------------------------------
set -euo pipefail

# Deployment descriptors legitimately contain YAML, VCL, Dockerfiles, and
# small shell wrappers. They must NOT contain application logic. We detect
# that by file extension: a .py/.ts/.tsx/.jsx under infra/ is, without
# exception so far, a copied-and-diverged application file.
#
# NOTE the exclusions: Ansible ships its own Python modules/plugins, and those
# are legitimate infrastructure code, not application code.
if found=$(find infra -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' \) \
             -not -path 'infra/*/library/*' \
             -not -path 'infra/*/filter_plugins/*' \
             | grep . ); then
  echo "ERROR: application source found under infra/ — this breaks invariant I1."
  echo "$found"
  echo
  echo "Fix: delete the copy and make the ONE copy in backend/ or frontend/"
  echo "configurable instead (env var, mounted config, CLI flag)."
  exit 1
fi

echo "OK: infra/ contains deployment descriptors only."
```

### The corollary students must internalise

When a stage needs to behave differently, the answer is **always** one of:

- an environment variable,
- a mounted configuration file,
- a command-line flag,
- a replica count or resource limit.

It is **never** a second copy of the code.

---

## 4.2 Top-level layout

```text
tartarus/
├── deployment-roadmap/          # this plan
│
├── legacy/                      # the current stdlib app, frozen as reference
│   ├── utils/                   #   moved here once Django reaches parity
│   ├── web/                     #   never deleted, always runnable
│   └── README.md                #   the "Stage 0" exhibit: no deployment story
│
├── backend/                     # THE Django project — built once      [05]
│   ├── config/                  # settings: base / dev / prod / test
│   ├── apps/
│   │   ├── accounts/            # users, auth
│   │   ├── content/             # word lists and items
│   │   ├── progress/            # per-user learning state
│   │   ├── practice/            # the scheduling engine (framework-agnostic)
│   │   ├── sessions/            # completed session history
│   │   ├── mastery/             # milestones, pending drills
│   │   └── analytics/           # event emission to MongoDB
│   ├── manage.py
│   ├── pyproject.toml
│   └── tests/                   # unit + integration + parity          [09]
│
├── frontend/                    # THE Next.js app — built once         [06]
│   ├── app/                     # App Router
│   ├── components/
│   ├── lib/                     # API client, hooks
│   ├── styles/                  # design tokens, light/dark themes
│   ├── package.json
│   └── tests/
│
├── data-platform/               # analytics pipeline                    [07]
│   ├── etl/                     # Mongo -> ClickHouse job (own deployable)
│   ├── clickhouse/              # schema DDL / migrations
│   ├── mongo/                   # index definitions, event schema docs
│   └── grafana/
│       ├── dashboards/
│       │   ├── product-analytics/    # ClickHouse-backed
│       │   └── sre-infra/            # Prometheus/Loki-backed
│       └── provisioning/             # datasources + dashboard providers
│
├── caching/
│   └── varnish/
│       └── default.vcl          # ONE file, delivered four ways         [08]
│
├── simulator/                   # ~1000 synthetic learners              [17]
│   ├── agents/                  # persona state machines + API client
│   ├── seed/                    # compressed-time historical backfill
│   ├── runner/                  # always-on orchestrator
│   ├── scenarios/               # steady_state / ramp / outage / cache_storm
│   ├── metrics/                 # the simulator's own /metrics endpoint
│   └── config/
│
├── ansible/                     # Stage 1 configuration management      [13]
│   ├── inventories/
│   │   ├── production/{hosts.yml,group_vars/,host_vars/}
│   │   └── staging/
│   ├── roles/                   # common, postgresql, redis, mongodb,
│   │                            # clickhouse, observability, backend,
│   │                            # frontend, etl, nginx, varnish
│   ├── playbooks/               # site.yml, bootstrap.yml, deploy-app.yml
│   ├── molecule/                # per-role tests                        [09]
│   └── ansible.cfg
│
├── infra/                       # deployment descriptors ONLY (see 4.1)
│   ├── docker/                  # Stage 2                               [14]
│   │   ├── images/              # Dockerfiles — shared by stages 2,3,4
│   │   ├── raw/                 #   2A: docker run scripts, no compose
│   │   └── compose/             #   2B: docker-compose.yml + overrides
│   ├── swarm/                   # Stage 3                               [15]
│   │   ├── stack.yml
│   │   └── configs/
│   └── k8s/                     # Stage 4                               [16]
│       ├── base/                # kustomize base
│       ├── overlays/{dev,prod}/
│       └── helm/                # values for third-party charts
│
├── ci/                          # pipeline helper scripts               [10]
│   ├── check-no-app-code-in-infra.sh
│   ├── check-comment-standard.sh                                      # [02]
│   └── smoke-test.sh            # one script, four targets
│
├── tests/
│   └── e2e/                     # Playwright, runs against any stage     [09]
│
├── docs/
│   └── adr/                     # ADRs, seeded from 03
│
├── .gitlab-ci.yml                                                      # [10]
├── Makefile                     # convenience targets, one per stage
└── README.md
```

---

## 4.3 Design notes

**`legacy/` is not a throwaway.** Keeping the original app runnable makes it
the reference implementation the Django port is checked against
([09](09-testing-strategy.md), parity tests). It also serves as the "Stage 0"
exhibit: *this is what no deployment story looks like.*

**One `infra/` tree, one subdirectory per stage.** Deliberately not four
repos or four branches. `git diff infra/docker/compose infra/swarm` is
itself a teaching artifact.

**`infra/docker/images/` is shared by all three container stages.** The same
Dockerfiles feed raw `docker run` (2A), Compose (2B), Swarm (3), and — via
the GitLab registry — Kubernetes (4). Building an image is decoupled from
how it is run. That is precisely the point being taught.

**`caching/varnish/default.vcl` is one file, delivered four ways** — copied
by Ansible, baked into an image, mounted as a Swarm config, mounted as a
Kubernetes ConfigMap. The clearest small illustration of I1 in the whole
repo.

**`ansible/` sits at top level, not under `infra/`.** It is Stage 1's
deployment mechanism, but it is also a genuine, standalone configuration-
management codebase with its own tests (Molecule). Your existing separate
Ansible codebase handles node preparation for Stages 2–4
([01](01-prerequisites-and-scope.md)); this tree covers Stage 1 only.

**`data-platform/` is its own top-level directory, not nested in `backend/`,**
because the ETL is a separate deployable with an independent lifecycle — it
can be down while the app stays up (ADR-8), and it gets its own image and
its own schedule in every stage.

**`simulator/` is deployment-target-agnostic.** It speaks only the public
HTTP API, so one `--target` flag points it at any stage.

---

## 4.4 Document-to-directory map

| Document | Owns |
|---|---|
| [05](05-backend-django.md) | `backend/` |
| [06](06-frontend-nextjs.md) | `frontend/` |
| [07](07-data-platform.md) | `data-platform/` |
| [08](08-caching.md) | `caching/`, Redis config in `backend/config/` |
| [09](09-testing-strategy.md) | `backend/tests/`, `tests/e2e/`, `ansible/molecule/` |
| [10](10-cicd-gitlab.md) | `.gitlab-ci.yml`, `ci/` |
| [13](13-stage-1-vm-ansible.md) | `ansible/` |
| [14](14-stage-2-docker.md) | `infra/docker/` |
| [15](15-stage-3-swarm.md) | `infra/swarm/` |
| [16](16-stage-4-kubernetes.md) | `infra/k8s/` |
| [17](17-load-simulator.md) | `simulator/` |

Next: [05 — Backend: Django](05-backend-django.md).
