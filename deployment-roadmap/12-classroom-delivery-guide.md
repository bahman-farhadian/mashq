# 12 — Classroom Delivery Guide

This file is about *teaching* the plan in `00`–`11`, not building it. It's
the piece that turns a working demo environment into an actual course.

## 12.1 The narrative thread to state explicitly, on day one

Say the thesis in `00` out loud before touching any tooling: *the
application does not change; only the mechanism that runs it does.* Every
subsequent stage should open by re-showing the same login screen, the same
practice session, the same Grafana dashboard — proving nothing about the
product changed — before showing what's different underneath. Students
retain the differential far better than four independent toolchains taught
in isolation.

## 12.2 Suggested sequencing and pacing

A rough allocation, adjustable to term length — the relative proportions
matter more than the absolute numbers:

| Module | Content | Suggested duration |
|---|---|---|
| 0 | Read the existing app, run it (`legacy/`), understand the domain model it must preserve | 1 session |
| 1 | Django + Next.js re-platform (`03`, `04`) — build, don't just read | 3–4 sessions |
| 2 | Data platform: Postgres, Mongo→ClickHouse→Grafana, Redis/Varnish (`01` §3–6, `05`, `06`) | 2–3 sessions |
| 3 | Stage 1 — VM deployment (`07`) | 1–2 sessions |
| 4 | Stage 2 — Docker, both tracks (`08`) | 2 sessions |
| 5 | Stage 3 — Swarm (`09`) | 1–2 sessions |
| 6 | Stage 4 — Kubernetes (`10`) | 3–4 sessions |
| 7 | Load simulator build-out (`11`), run continuously from here on | integrated, not standalone |
| 8 | Capstone: live multi-stage demo, chaos exercise, class presentation | 1–2 sessions |

Modules 1–2 (get the app itself re-platformed and correct) should not be
compressed to make more room for the deployment stages — every later
module depends on the app being trustworthy first, and rushing `03`'s
parity-testing gate is the single most likely place for the whole sequence
to produce a broken, hard-to-debug capstone.

## 12.3 Learning objectives, per stage (for a syllabus)

- **Stage 1 (VM)**: explain what `systemd` gives you over a bare process;
  configure a reverse-proxy + cache chain by hand; perform and restore
  from a manual backup.
- **Stage 2 (Docker)**: explain image layering and why multi-stage builds
  matter; articulate, from direct experience, what Compose's dependency/
  health-check/volume model solves that raw `docker run` doesn't.
- **Stage 3 (Swarm)**: explain manager quorum and why an odd number ≥3
  matters; distinguish secrets from configs and justify why credentials
  are never plain environment variables; correctly identify what Swarm
  cannot safely do for stateful services (and why).
- **Stage 4 (Kubernetes)**: explain `Deployment` vs `StatefulSet` and when
  each is correct; read and modify a `HorizontalPodAutoscaler`; write a
  `NetworkPolicy` that enforces a stated least-privilege requirement;
  diagnose a failure using `kubectl describe`/`logs` and the Loki/Grafana
  stack instead of guessing.
- **Cross-cutting (all stages)**: given the *same* application and the
  *same* traffic (the simulator, `11`), correctly predict how each
  orchestration mechanism will behave under a node/pod/container failure —
  this is the single best exam-style question this whole project sets up,
  because it can only be answered by having actually understood the
  differences, not memorized four separate command references.

## 12.4 Suggested lab/assessment structure

- **Per-stage lab checklist** (pass/fail, not graded on polish): app
  reachable, all services healthy, dashboards populated by the simulator,
  one deliberate failure induced and correctly diagnosed/recovered from.
  Grading the *diagnosis*, not just "is it up," is what keeps this an SRE
  exercise rather than a deployment checklist exercise.
- **Written reflection per stage**: "what would break first under load
  here, and why" — answerable in one paragraph if the stage's actual
  limitations (named explicitly in `07`–`10`) were genuinely understood,
  not just followed as steps.
- **Capstone rubric**: all four stages independently reproducible from the
  repo (`02`) with no undocumented manual steps; simulator running
  continuously against the final (Kubernetes) stage during presentation;
  at least one live-induced failure with visible, correct dashboard
  reaction; a clear verbal explanation of the Postgres-vs-Galera decision
  (`01` §3) as a proxy for whether the "why," not just the "how," landed.

## 12.5 What makes this project worth the investment

Most classroom deployment exercises either (a) deploy a toy "hello world"
app, which teaches the orchestrator but nothing about what real
applications actually need from one, or (b) deploy a real app once, on one
platform, which teaches one toolchain but never lets students *feel* the
differences between orchestration models. This plan does neither: one real
application, with real invariants worth protecting (see `00`, "What must
not be lost"), deployed four ways, with a simulator that makes every
stage's dashboards and failure modes *observably alive* during class
instead of static screenshots. That combination — a real app, a consistent
narrative, and live, continuous synthetic traffic — is what should make
this land differently than a typical semester's infrastructure exercises.
