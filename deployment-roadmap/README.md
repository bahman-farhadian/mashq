# Tartarus Deployment Roadmap

A plan to re-platform Tartarus (currently: stdlib Python backend, vanilla-JS
frontend, one SQLite file, JSON content) onto a production-shaped stack —
Django, Next.js, PostgreSQL, MongoDB, ClickHouse, Grafana, Redis, Varnish —
and deploy it four progressively more sophisticated ways for a DevOps/SRE
classroom:

**Ansible/VM → Docker (raw, then Compose) → Docker Swarm → Kubernetes**

> **This directory is documentation only.** Nothing here has been
> implemented; no application code, infrastructure, or data has been changed.
> Each file is the spec to build against when that piece of work is picked
> up.

---

## The one-sentence thesis

> The application and its supporting services stay constant across all four
> stages. Only the mechanism that packages, schedules, and connects them
> changes — and that diff, made visible, **is** the curriculum.

## The three invariants

| | Invariant | Enforced by |
|---|---|---|
| **I1** | **One codebase, four deployments.** `backend/` and `frontend/` are built once; `infra/<stage>/` holds deployment descriptors only. Stage differences are *configuration*, never forked code. | A CI job ([04](04-repository-layout.md), [10](10-cicd-gitlab.md)) |
| **I2** | **No overengineering.** The simplest architecture that still teaches the lesson. Microservices and Celery were both considered and rejected, in writing. | [03](03-architecture-decisions.md) ADR-5, ADR-7 |
| **I3** | **Every artifact is teaching material.** Assume a reader who knows basic Linux and nothing about these tools. An uncommented file is an unfinished file. | [02](02-authoring-standards.md), CI checks |

---

## Read in this order

### Part 0 — Foundation

| # | File | Covers |
|---|---|---|
| 00 | [Executive Summary](00-executive-summary.md) | Thesis, scope, the three invariants, lifecycle map, the auth gap that blocks network exposure |
| 01 | [Prerequisites and Scope](01-prerequisites-and-scope.md) | What must already exist (VMs, GitLab); explicit non-goals; definition of done |
| 02 | [Authoring Standards](02-authoring-standards.md) | **The commenting criterion** — the standard every shipped file is held to, with worked before/after examples |
| 03 | [Architecture Decisions](03-architecture-decisions.md) | Ten ADRs: Postgres vs. Galera, no microservices, no Celery, statelessness, Varnish edition, observability |
| 04 | [Repository Layout](04-repository-layout.md) | The monorepo tree, and how invariant I1 is mechanically enforced |

### Part 1 — The application (built once, deployed four ways)

| # | File | Covers |
|---|---|---|
| 05 | [Backend: Django](05-backend-django.md) | App-by-app port of `tartarus.py`/`tartarus_web.py`; **statelessness**; auth; legacy-data cutover |
| 06 | [Frontend: Next.js](06-frontend-nextjs.md) | View-by-view port of `web/app.js`; dark/light theming; interaction rules that must survive |
| 07 | [Data Platform](07-data-platform.md) | Mongo event schema → ETL → ClickHouse → Grafana dashboards |
| 08 | [Caching](08-caching.md) | Redis (inside the app) vs. Varnish (in front of it); what is never cached |
| 09 | [Testing Strategy](09-testing-strategy.md) | Unit, integration, **parity gate**, e2e, Molecule for Ansible, smoke, load |

### Part 2 — Cross-cutting pipeline

| # | File | Covers |
|---|---|---|
| 10 | [CI/CD with GitLab](10-cicd-gitlab.md) | `.gitlab-ci.yml`, image build/scan/publish, registry, immutable tags, deploy gates |
| 11 | [Security and Secrets](11-security-and-secrets.md) | Secrets in four mechanisms, Trivy/SBOM/gitleaks, Django hardening, per-stage network policy |
| 12 | [Observability and SLOs](12-observability-and-slos.md) | Prometheus/Grafana/Loki, RED, custom metrics, **SLOs and error budgets**, burn-rate alerting |

### Part 3 — The four deployments

| # | File | Covers |
|---|---|---|
| 13 | [Stage 1 — VM with Ansible](13-stage-1-vm-ansible.md) | Roles, inventories, vault, systemd units and timers, Molecule |
| 14 | [Stage 2 — Docker](14-stage-2-docker.md) | Track A: raw `docker run`. Track B: Compose |
| 15 | [Stage 3 — Swarm](15-stage-3-swarm.md) | Multi-node cluster, `docker stack`, secrets/configs, rolling updates |
| 16 | [Stage 4 — Kubernetes](16-stage-4-kubernetes.md) | StatefulSets, operators, Ingress, HPA, NetworkPolicy, chaos |

### Part 4 — Demo and delivery

| # | File | Covers |
|---|---|---|
| 17 | [Load Simulator](17-load-simulator.md) | ~1000 always-on synthetic learners + compressed-time historical backfill |
| 18 | [Operations and Runbooks](18-operations-and-runbooks.md) | Backup/restore drills, PITR, worked runbooks, postmortems, on-call simulation |
| 19 | [Classroom Delivery](19-classroom-delivery.md) | Sequencing, objectives, labs, capstone rubric, common student difficulties |

---

## Direct answers to the questions you asked

**Should the database be a MariaDB Galera master-master cluster, or PostgreSQL?**
→ **PostgreSQL runs the application.** Galera's multi-master property is real
at the protocol level and hazardous at the application level for this app's
write pattern (many small transactions repeatedly hitting the same row).
Galera survives as a deliberately isolated elective lab so the topic is
taught honestly, sharp edges included.
[03 ADR-3 and ADR-4](03-architecture-decisions.md)

**Varnish free version or not?**
→ **Open-source Varnish Cache.** It is the same caching engine as Enterprise;
Enterprise adds support and tooling this project does not need, and there is
no reason to bring commercial licensing into a classroom.
[03 ADR-9](03-architecture-decisions.md)

**Can the monolith be sliced into microservices without overengineering?**
→ **No — and it should not be.** The practice engine reads progress, content,
and mastery inside one transaction; splitting that forces distributed
transactions into a course that is not about development. Scaling is taught
through *replica count* instead, which still yields ~12 services to
orchestrate. [03 ADR-5](03-architecture-decisions.md)

**Do we truly need Celery?**
→ **No.** The application has no background work today, and the only
recurring job (the ETL) is *better* taught as a standalone scheduled
deployable — because "how do I schedule a job" has a different answer in each
of the four stages, which is the curriculum's thesis in miniature.
[03 ADR-7](03-architecture-decisions.md)

**Ansible?**
→ Owns Stage 1 entirely: node prep, hardening, and service deployment, with
Molecule tests. Stages 2–4 assume nodes prepared by your existing separate
Ansible codebase. [13](13-stage-1-vm-ansible.md)

**CI/CD?**
→ Self-hosted GitLab CE. The pipeline is in scope; provisioning GitLab is
not. [10](10-cicd-gitlab.md)

**Cloud?**
→ Out of scope throughout. VMs are assumed provisioned by your KVM tooling;
Kubernetes is k3s/kubeadm on your own hosts.
[01](01-prerequisites-and-scope.md)

**"Three stages" of deployment?**
→ Written as four (VM, Docker, Swarm, Kubernetes), because Docker itself
splits into the two tracks you described — raw and Compose.

---

## Next step

Nothing needs to be built yet. When you are ready to start on a specific
piece, say which file or stage, and that becomes an implementation task —
this roadmap stays as the reference the implementation is checked against.
