# 00 — Executive Summary

## What this is

A plan to take Tartarus — currently a single-user, localhost-only Python
application (stdlib `http.server` backend, vanilla-JS frontend, one SQLite
file, JSON files as content) — and turn it into a **teaching artifact**: the
same application, re-platformed onto a production-shaped stack, then
deployed four separate ways in ascending order of operational sophistication.

Students don't learn Docker or Kubernetes in the abstract. They deploy *this
specific, already-working application* four times, watching what changes
and — more importantly — what **doesn't** change between stages.

This directory is the plan only. Nothing here is executable. Each file is a
design document for one slice of the work; when you build that slice, the
file becomes its spec.

## The teaching thesis

> **The application and its supporting services stay constant. Only the
> mechanism that packages, schedules, and network-connects them changes.**

That single idea is the spine of the whole curriculum. The same Django app,
the same Next.js frontend, the same Postgres/Redis/Mongo/ClickHouse/Grafana
services, and the same Varnish configuration are deployed:

1. by Ansible, as OS processes under `systemd`, on prepared VMs;
2. as containers — first raw `docker run`, then Compose;
3. as a `docker stack` on a Swarm cluster;
4. as workloads on Kubernetes.

Every stage answers the same question — *"how do I run a dozen cooperating
services reliably?"* — with a different tool. Students build their own
mental diff between stages instead of memorizing four unrelated toolchains.

## The three invariants this plan enforces

These are not style preferences. They are the properties that make the
curriculum work, and every later document is held to them.

### I1 — One codebase, four deployments

`backend/` and `frontend/` are built **once**. Each `infra/<stage>/`
directory holds deployment descriptors *only* — never application source,
never a forked copy, never a stage-specific patch to app code. If a stage
needs different behaviour, that is **configuration, injected at runtime**.

A CI job enforces this mechanically (see
[04](04-repository-layout.md) and [10](10-cicd-gitlab.md)). If a student
can diff two stages and find changed application code, the central lesson
has already been broken.

### I2 — No overengineering

The architecture is chosen to be *the simplest thing that still teaches the
lesson*. Concretely, that ruled out two things a naive version of this plan
would have included — microservices and Celery — each rejected with written
reasoning in [03](03-architecture-decisions.md). This is a DevOps/SRE
course, not a distributed-systems development course; class time spent
debugging self-inflicted architectural complexity is class time stolen from
deployment and operations.

### I3 — Every artifact is teaching material

This is an educational project, not a production codebase. The assumed
reader knows basic Linux and basic programming and knows *nothing* about
Django, Ansible, Docker, Swarm, Kubernetes, Prometheus, or Varnish. Every
file that ships must be commented densely enough to be read top-to-bottom
and understood without an instructor present.

**An uncommented file is an unfinished file.** The binding standard is
[02 — Authoring Standards](02-authoring-standards.md), and each stage
document ends with a completion checklist that includes it.

## Scope of the re-platform

| Layer | Today | Target |
|---|---|---|
| Backend | stdlib `http.server`, one module pair | Django + Django REST Framework, modular monolith |
| Frontend | Vanilla JS (`web/app.js`, one IIFE) | Next.js (App Router), light/dark themes |
| Primary datastore | One SQLite file | PostgreSQL — reasoning in [03](03-architecture-decisions.md) |
| Content storage | JSON files under `data/word_lists/` | PostgreSQL tables + Django admin; JSON import retained as an authoring path |
| Session state | In-process Python dict | Redis — this is what makes replicas possible ([03](03-architecture-decisions.md) ADR-6) |
| Interaction analytics | None | MongoDB (raw events) → ClickHouse (OLAP) → Grafana |
| Cache | None | Redis (app cache, sessions) + Varnish (HTTP edge) |
| Observability | One rotating text file | Prometheus + Alertmanager + Grafana + Loki, with SLOs |
| CI/CD | None | Self-hosted GitLab CE pipelines |
| Deployment | `make web`, one process | Ansible/VM → Docker → Swarm → Kubernetes |

## What must not be lost

The current `README.md` documents a set of *invariants the scoring engine
guarantees* — scores never regress, a session never mixes question modes,
due work always outranks new work, and so on. None of that is negotiable.

The Django migration ([05](05-backend-django.md)) is explicitly a **port,
not a rewrite**: the scheduling algorithm in `utils/tartarus.py` becomes a
framework-agnostic service layer inside Django, covered by a ported test
suite, *before* any deployment work begins. Get the domain logic right once,
in one place, and every later stage only has to run it.

## A gap that blocks network exposure

The current app's security model is deliberate and explicit: **local-first,
trusted-client, no auth layer**. That is correct for a localhost tool and
wrong for anything reachable from a classroom network or a Kubernetes
Ingress.

The Django migration must add real authentication and authorization —
enforced server-side, with one learner's data never addressable by another's
request — as a **blocking** part of the port, not a stretch goal. Restated
in [05](05-backend-django.md) and [11](11-security-and-secrets.md).

## DevOps lifecycle coverage

The eight canonical phases, and where each is taught:

| Phase | Document(s) |
|---|---|
| **Plan** | [01](01-prerequisites-and-scope.md), [03](03-architecture-decisions.md) |
| **Code** | [04](04-repository-layout.md), [05](05-backend-django.md), [06](06-frontend-nextjs.md), [07](07-data-platform.md), [08](08-caching.md) |
| **Build** | [10](10-cicd-gitlab.md), [14](14-stage-2-docker.md) |
| **Test** | [09](09-testing-strategy.md) |
| **Release** | [10](10-cicd-gitlab.md), [11](11-security-and-secrets.md) |
| **Deploy** | [13](13-stage-1-vm-ansible.md), [14](14-stage-2-docker.md), [15](15-stage-3-swarm.md), [16](16-stage-4-kubernetes.md) |
| **Operate** | [18](18-operations-and-runbooks.md) |
| **Monitor** | [12](12-observability-and-slos.md), [17](17-load-simulator.md) |

## Documents in this plan

See [README.md](README.md) for the full annotated index. Read
[01 — Prerequisites and Scope](01-prerequisites-and-scope.md) next: it
states what you must already have before Module 1, and what this project
deliberately does not cover.
