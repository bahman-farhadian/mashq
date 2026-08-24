# 19 — Classroom Delivery Guide

This document is about *teaching* the plan in `00`–`18`, not building it. It
is what turns a working demo environment into a course.

---

## 19.1 State the thesis on day one

Before any tooling, say the thesis from [00](00-executive-summary.md) out
loud:

> The application does not change. Only the mechanism that runs it does.

Then open every subsequent stage by re-showing the *same* login screen, the
*same* practice session, and the *same* Grafana dashboard — proving nothing
about the product changed — before showing what is different underneath.

Students retain the differential far better than four independent
toolchains taught in isolation. Reinforce it with a literal command:

```bash
# The application source is byte-identical between two deployment stages.
# Only the descriptors that run it differ.
git diff --stat infra/docker/compose infra/swarm
```

---

## 19.2 DevOps lifecycle coverage

Where each phase is taught, so the syllabus can be mapped to a standard
lifecycle diagram:

| Phase | Document(s) | Session(s) |
|---|---|---|
| **Plan** | [01](01-prerequisites-and-scope.md), [03](03-architecture-decisions.md) | 1–2 |
| **Code** | [04](04-repository-layout.md), [05](05-backend-django.md), [06](06-frontend-nextjs.md), [07](07-data-platform.md), [08](08-caching.md) | 3–8 |
| **Build** | [10](10-cicd-gitlab.md), [14](14-stage-2-docker.md) | 11, 14 |
| **Test** | [09](09-testing-strategy.md) | 9–10 |
| **Release** | [10](10-cicd-gitlab.md), [11](11-security-and-secrets.md) | 11–12 |
| **Deploy** | [13](13-stage-1-vm-ansible.md)–[16](16-stage-4-kubernetes.md) | 14–20 |
| **Operate** | [18](18-operations-and-runbooks.md) | 21–22 |
| **Monitor** | [12](12-observability-and-slos.md), [17](17-load-simulator.md) | 13, throughout |

---

## 19.3 Suggested sequencing

Relative proportions matter more than absolute session counts.

| Module | Content | Sessions |
|---|---|---|
| 0 | Read and run the legacy app; understand the domain invariants it must preserve | 1 |
| 1 | Architecture decisions; prerequisites; authoring standards ([01](01-prerequisites-and-scope.md)–[03](03-architecture-decisions.md)) | 1–2 |
| 2 | Django backend port ([05](05-backend-django.md)) | 3–4 |
| 3 | Next.js frontend ([06](06-frontend-nextjs.md)) | 2 |
| 4 | Data platform + caching ([07](07-data-platform.md), [08](08-caching.md)) | 2 |
| 5 | Testing strategy ([09](09-testing-strategy.md)) | 2 |
| 6 | CI/CD with GitLab ([10](10-cicd-gitlab.md)) | 2 |
| 7 | Security and secrets ([11](11-security-and-secrets.md)) | 1–2 |
| 8 | Observability and SLOs ([12](12-observability-and-slos.md)) | 2 |
| 9 | **Stage 1** — VM + Ansible ([13](13-stage-1-vm-ansible.md)) | 2–3 |
| 10 | **Stage 2** — Docker, both tracks ([14](14-stage-2-docker.md)) | 2–3 |
| 11 | **Stage 3** — Swarm ([15](15-stage-3-swarm.md)) | 2 |
| 12 | **Stage 4** — Kubernetes ([16](16-stage-4-kubernetes.md)) | 3–4 |
| 13 | Simulator ([17](17-load-simulator.md)) | integrated, not standalone |
| 14 | Operations, DR, on-call ([18](18-operations-and-runbooks.md)) | 2 |
| 15 | Capstone: live demo, chaos exercise, presentations | 1–2 |

### Sequencing notes

**Do not compress Modules 2–5 to make room for the deployment stages.**
Every later module depends on the application being trustworthy. Rushing the
parity gate ([09](09-testing-strategy.md)) is the single most likely cause
of a broken, hard-to-debug capstone.

**CI/CD (Module 6) comes before the deployment stages, deliberately.** Once
images are built and published once, every stage becomes "pull this exact
image, run it this way" — which shortens Stages 1–4 *and* makes invariant I1
operationally obvious rather than merely asserted.

**Start the simulator early and leave it running.** From Module 8 onward it
should be generating traffic during every session, so dashboards are always
alive. A dead dashboard teaches nothing.

---

## 19.4 Learning objectives per stage

Phrased as assessable statements.

**Stage 1 — VM + Ansible.** Explain what `systemd` provides over a bare
process. Write an idempotent Ansible role and *prove* its idempotency.
Explain why config management beats shell scripts, with reference to a
specific failure a script would have caused. Configure a reverse-proxy and
cache chain. Perform a restore from backup.

**Stage 2 — Docker.** Explain image layering and why multi-stage builds
matter. Articulate — from direct experience with track 2A — exactly what
Compose's dependency, health-check, and volume model solves. Explain why
`depends_on` alone is insufficient.

**Stage 3 — Swarm.** Explain manager quorum and why an odd number ≥3
matters. Distinguish secrets from configs and justify why credentials should
not be environment variables. **Correctly identify what Swarm cannot safely
do for stateful workloads, and why.**

**Stage 4 — Kubernetes.** Explain `Deployment` vs `StatefulSet` and when
each is correct. Read and modify an HPA. Write a `NetworkPolicy` enforcing a
stated least-privilege requirement. Diagnose a failure using
`kubectl describe`/`logs` plus Loki, rather than guessing.

**Cross-cutting — the best exam question this project sets up:**

> Given the *same* application and the *same* traffic, predict how each of
> the four mechanisms behaves when a node fails — and justify each answer.

It can only be answered by having understood the differences, not by having
memorised four command references.

---

## 19.5 Labs and assessment

### Per-stage lab checklist (pass/fail)

- App reachable; all services healthy; `ci/smoke-test.sh` green.
- Dashboards populated by the simulator.
- One deliberate failure induced, correctly diagnosed, and recovered from.
- Completion checklist met, **including the commenting standard**
  ([02](02-authoring-standards.md)).

Grading the **diagnosis**, not merely "is it up," is what keeps this an SRE
exercise rather than a deployment checklist.

### Written reflection per stage

> *"What would break first under load here, and why?"*

Answerable in a paragraph if the stage's stated limitations were genuinely
understood; not answerable if the steps were merely followed.

### Capstone rubric

| Criterion | Weight |
|---|---|
| All four stages reproducible from the repo with no undocumented manual steps | 25% |
| One codebase — no per-stage application forks (I1), demonstrated by `git diff` | 15% |
| Pipeline runs lint → test → build → scan → publish → deploy | 15% |
| Simulator running live during the presentation, dashboards populated | 10% |
| A live-induced failure with correct dashboard reaction and recovery | 15% |
| Verbal defence of the Postgres-vs-Galera and no-microservices decisions | 10% |
| Code and configuration meet the commenting standard | 10% |

The verbal-defence line is a proxy for whether the *why* landed, not just
the *how* — and it is deliberately hard to fake.

---

## 19.6 Common student difficulties, and where they surface

Anticipating these saves considerable class time:

| Difficulty | Surfaces in | Pre-empt by |
|---|---|---|
| "Why can't I run three copies?" | Stage 2 | Making it a *question* first — [05](05-backend-django.md) §5.3 |
| YAML indentation despair | Stages 2–4 | Teaching `yamllint` in Module 6, before it hurts |
| `depends_on` ≠ ready | Stage 2 | The documented failure mode in [14](14-stage-2-docker.md) |
| Secrets committed by accident | Module 7 | `gitleaks` in CI from Module 6 onward |
| "The dashboard is empty" | Module 8 | Simulator running from Module 8, always |
| Cardinality explosion | Module 8 | The explicit warning in [12](12-observability-and-slos.md) |
| Rollback that cannot roll back | Module 14 | Expand/contract migrations, [18](18-operations-and-runbooks.md) §18.3 |

---

## 19.7 Why this project is worth the investment

Most classroom deployment exercises do one of two things. They deploy a toy
"hello world," which teaches the orchestrator but nothing about what real
applications need from one. Or they deploy a real app once, on one platform,
which teaches a toolchain but never lets students *feel* the differences
between orchestration models.

This does neither. One real application, with real invariants worth
protecting, deployed four ways, with a simulator that keeps every stage's
dashboards and failure modes observably alive during class rather than
frozen in screenshots — plus the phases most courses omit entirely: testing,
CI/CD, security, observability with SLOs, and operations.

That combination — a real app, one consistent narrative, live synthetic
traffic, and full lifecycle coverage — is what should make this land
differently from a typical semester of infrastructure exercises.
