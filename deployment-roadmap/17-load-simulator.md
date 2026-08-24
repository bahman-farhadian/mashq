# 17 — Load Simulator and Historical Data Seeder

## Two distinct problems, two distinct tools

> "insert mock data for the presentation" and "mimic ~1000 users live using
> this application endlessly" are not the same problem, and conflating them
> produces a worse tool for both jobs.

1. **Historical backfill (seed)** — before any live demo, the dashboards
   in [07](07-data-platform.md) need months of *plausible past* data to not look empty. This
   needs to happen in **minutes**, not months of real time.
2. **Always-on live population (runner)** — during class, dashboards need
   to visibly move: new sessions completing, mastery events firing, the
   HPA in [16](16-stage-4-kubernetes.md) reacting to real load. This needs to run **continuously**,
   for hours, without drifting into unrealistic behavior or falling over.

`simulator/` is one directory with two different entry points sharing one
core: a library of *learner personas* and an *API client* that speaks the
real DRF endpoints exactly like the Next.js frontend does.

## Directory layout

```text
simulator/
├── config/
│   ├── personas.yaml         # archetype definitions, see §17.3
│   ├── targets.yaml          # named deployment endpoints: vm / compose / swarm / k8s
│   └── settings.py
├── agents/
│   ├── persona.py            # Persona dataclass + sampling from personas.yaml
│   ├── learner_agent.py      # one simulated learner's full lifecycle/state machine
│   └── api_client.py         # typed async client for the DRF API (register/login/practice/*)
├── seed/
│   ├── backfill.py           # entry point for historical data generation, see §17.2
│   └── README.md
├── runner/
│   ├── orchestrator.py       # entry point: spawn & endlessly cycle N concurrent LearnerAgents
│   └── ramp.py                # ramp population up/down over time (for the HPA demo, [16](16-stage-4-kubernetes.md))
├── scenarios/
│   ├── steady_state.py        # the default: N learners, realistic pacing, runs forever
│   ├── spike.py                # sudden 5x population increase, for autoscaling demos
│   ├── outage.py               # scripted: pause traffic to a killed dependency, resume on recovery
│   └── cache_storm.py          # burst of identical cold requests, for Varnish/Redis demos ([08](08-caching.md))
├── metrics/
│   └── self_metrics.py         # the simulator's own Prometheus /metrics endpoint, see §17.5
├── pyproject.toml
└── README.md
```

## 17.1 Tool choice: custom Python (asyncio + httpx), not k6/Locust — with a place for both

The defining requirement — "~1000 users, live, endlessly, with individual
memory of their own progress" — is a **stateful, long-horizon population
simulation**, not a load test. k6 and Locust are excellent at their actual
job: bursty, scripted, throughput/latency-focused load tests over minutes.
They are comparatively awkward at "1000 independent agents, each with a
persistent identity and its own accuracy/pacing profile, running
indefinitely and behaving like a real spaced-repetition learner would over
days." A hand-rolled `asyncio` + `httpx` runner, where each `LearnerAgent`
is a small state machine (register/login once, then loop: pick a track →
start session → answer questions at a persona-appropriate pace and
accuracy → session ends → immediately start another, forever — directly
exercising the "endless" session behavior the backend already implements),
is a better fit for the actual requirement and is a good, teachable piece
of async Python in its own right.

**Where k6/Locust still belong**: as their own, separate lab — "run a
5-minute load test against the Stage 3/4 deployment and read the p95
latency report" is a real, valuable, different exercise from "keep 1000
personas alive for the whole class period." Recommendation: keep both.
`runner/` is the always-on simulator; a short-lived `k6` script (or
Locust) lives alongside it purely for the classic load-test lab, pointed at
whichever stage is currently deployed.

## 17.2 Seeding historical data: through the real engine, at compressed time

The tempting shortcut — hand-crafting `INSERT` statements that just *look*
like plausible history — is explicitly **not** the recommendation. It
duplicates the scoring/Leitner/consolidation-step state machine outside
the one place it's allowed to live ([05](05-backend-django.md) §5.4), and it will silently drift
out of sync with the real rules the moment either one changes.

**Recommended approach: run the real `practice` engine ([05](05-backend-django.md) §5.4) through
its actual code path, with an injectable clock.** `seed/backfill.py`:

1. Creates N synthetic users (default ~1000, configurable) with personas
   assigned per `personas.yaml`.
2. For each simulated calendar day across the desired backfill window
   (e.g., 90 days), and for each user, calls the real engine functions
   directly (not over HTTP — this is a management-command-style script
   with direct access to the Django app, for speed) with `now` fixed to
   that simulated day.
3. Every score change, Leitner promotion, mastery event, and session-log
   row is therefore produced by the exact same code that would produce it
   in real, slow, calendar time — just compressed into however long the
   backfill script takes to run. This mirrors a pattern the current
   codebase already trusts for its own tests and its own "Shift Dates"
   feature (README: deterministic, code-path-verified date manipulation,
   never ad hoc date arithmetic bolted on separately).
4. Emits the corresponding analytics events into Mongo ([07](07-data-platform.md)) at each
   simulated timestamp too, so the ClickHouse/Grafana dashboards have a
   populated history immediately, not just the Postgres side.

This is slower to write than raw `INSERT`s and faster to trust — and it
doubles as a write-path load test of the migration script's correctness
before the semester's live data ever touches it.

## 17.3 Personas: `config/personas.yaml`

Each archetype is a named, sampled parameter set, not a single fixed
script — every simulated learner gets randomized-within-range values so
1000 agents don't all behave identically:

| Persona | Accuracy | Session cadence | Track mix | Notes |
|---|---|---|---|---|
| `diligent` | 92–98% | 20–40 sessions/day, evenly spaced | Follows due Consolidation Track work first, supplementary tracks when nothing's due | The "healthy" majority baseline |
| `crammer` | 80–90% | Long bursts (50+ sessions in an hour), then multi-day gaps | Heavy on Encoding Practice | Produces realistic activity spikes for dashboard variety |
| `struggler` | 55–70% | Steady but slow | Triggers drills often | Exercises the mandatory-drill code path ([05](05-backend-django.md)) under load |
| `night_owl` | 85–95% | Concentrated 22:00–02:00 (in whatever timezone the simulator clock uses) | Mixed | Produces a visible daily activity curve in the dashboards instead of flat noise |
| `explorer` | 88–94% | Moderate | Disproportionately favors the three supplementary tracks | Specifically exercises the endless/reshuffled-every-session bucket-track behavior described in this app's own recent scheduler work |

Latency-to-answer is also sampled per agent (not instant) — realistic
human think-time distributions, not a tight loop hammering the API as fast
as possible, since the *default* scenario (`steady_state.py`) is meant to
look like real usage, not a stress test (that's what `spike.py` and the
k6/Locust lab from §17.1 are for).

## 17.4 Scenarios for live classroom moments

- **`steady_state`** (default) — the population the dashboards should
  show *throughout* a lecture, running from before class starts to after
  it ends.
- **`ramp`** — population climbs from a low baseline to several thousand
  over a few minutes, timed to a live HPA demo ([16](16-stage-4-kubernetes.md)).
- **`outage`** — on a signal (or a timer), the orchestrator itself doesn't
  stop; it keeps trying, absorbing errors gracefully and logging them via
  its own metrics (§17.5) while the instructor kills a dependency (a DB
  pod, a backend replica) — the simulator's job here is to make the
  *application's* degraded/recovered behavior visible on the SRE dashboard
  ([12](12-observability-and-slos.md)), not to simulate the outage itself.
- **`cache_storm`** — a burst of agents requesting the same
  rarely-changing, cacheable resource simultaneously, right after a
  deliberate Varnish restart (cold cache) — makes the cache hit-ratio
  panel and the thundering-herd problem from [08](08-caching.md) visible and countable.

## 17.5 The simulator watches itself

`metrics/self_metrics.py` exposes its own Prometheus endpoint:
`active_agents`, `requests_total` (by outcome), `request_latency_seconds`,
`errors_total` (by type). This is scraped into the same Prometheus/Grafana
SRE stack from [12](12-observability-and-slos.md) — partly so the instructor has a simple
"is the simulator itself healthy" panel during a live demo, and partly
because it's a small, free extra example of "instrument your own tooling,
not just the system under test," which is a habit worth modeling
explicitly rather than only preaching.

## 17.6 One simulator, four targets

`config/targets.yaml` names the base URL and auth mode for each stage's
deployment (`vm`, `compose`, `swarm`, `k8s`). Switching which stage the
simulator is currently pointed at is a one-flag change
(`--target k8s`), reinforcing the plan's core thesis from [00](00-executive-summary.md): the same
client, generating the same realistic traffic, works unmodified against
every stage — because only the deployment mechanism ever changed, never
the application's actual API contract.

Next: [18 — Operations, Backup/DR, and Runbooks](18-operations-and-runbooks.md).
