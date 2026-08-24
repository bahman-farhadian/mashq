# 12 — Observability and SLOs

## 12.1 Why this is core, not optional

Half of SRE is knowing whether the thing you deployed is healthy. A course
that deploys four ways but never answers *"is it working, and how would I
know before a user tells me?"* teaches deployment, not operations.

This document also carries the piece that most distinguishes **SRE** from
**sysadmin** work: **SLIs, SLOs, and error budgets** — turning "it feels
slow" into a number you can make decisions with.

## 12.2 The stack

| Component | Job |
|---|---|
| **Prometheus** | Scrapes and stores time-series metrics |
| **Alertmanager** | Routes, groups, and silences alerts |
| **Grafana** | Dashboards for both metrics and logs |
| **Loki** | Log aggregation, queried like metrics |
| **Promtail / Vector** | Ships logs into Loki |
| **Exporters** | `node_exporter`, `postgres_exporter`, `redis_exporter`, `mongodb_exporter`, cAdvisor, kube-state-metrics |

Application metrics come from `django-prometheus` plus custom metrics the
app defines itself (§12.4).

### How it is deployed per stage

The same thesis as everything else — one stack, four mechanisms:

| Stage | Deployment |
|---|---|
| 1 — VM | `observability` Ansible role: packages + systemd units |
| 2 — Docker | Containers in the Compose file |
| 3 — Swarm | Services in `stack.yml`, pinned to a node with a volume |
| 4 — Kubernetes | `kube-prometheus-stack` + `loki-stack` Helm charts |

Grafana dashboards are **provisioned as code** in every stage
(`data-platform/grafana/provisioning/`), never clicked together in the UI —
so a freshly built environment comes up with identical dashboards, and a
student who breaks one can rebuild it from git.

## 12.3 Two dashboard folders, one Grafana

| Folder | Datasource | Audience | Content |
|---|---|---|---|
| **Product Analytics** | ClickHouse ([07](07-data-platform.md)) | "Is the product being used?" | Daily active learners, accuracy by track, mastery funnel, session-length distribution |
| **SRE / Infra** | Prometheus + Loki | "Is the system healthy?" | RED metrics, saturation, error budget burn, logs |

Keeping both in one Grafana, clearly separated, is itself the lesson:
business metrics and system metrics are different questions, answered with
the same tool, and confusing them is how you end up alerting on the wrong
thing.

## 12.4 What to measure

### RED, per service

- **R**ate — requests per second
- **E**rrors — failed requests per second
- **D**uration — latency distribution (histogram, so you get real quantiles)

### USE, per resource

- **U**tilisation, **S**aturation, **E**rrors — for CPU, memory, disk, and
  connection pools.

### Custom application metrics worth adding

These are what make the dashboards *about Tartarus* rather than about a
generic web app:

```python
# ---------------------------------------------------------------------------
# Application-specific Prometheus metrics.
#
# Session:      12 (Observability)
# Depends on:   django-prometheus installed and its middleware enabled
# Consumed by:  Prometheus scrape -> Grafana 'SRE / Infra' folder; the
#               answer-latency histogram also backs the SLO in section 12.5
# ---------------------------------------------------------------------------
from prometheus_client import Counter, Histogram

# A Histogram, not a Summary: histograms are aggregatable across replicas,
# which is required the moment there is more than one backend pod. Summaries
# compute quantiles per-process and CANNOT be meaningfully averaged -- a
# classic mistake that produces confidently wrong p95 numbers.
practice_answer_latency = Histogram(
    "tartarus_answer_processing_seconds",
    "Time to process a submitted answer and return the next question",
    # Explicit buckets, because the defaults are tuned for slow web pages and
    # would put nearly every observation of this fast path into one bucket,
    # making the p95 useless. These straddle our 300ms SLO threshold so the
    # burn rate can actually be computed.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5),
)

# Labelled by track so a regression in ONE practice mode is visible, rather
# than being averaged away across all of them.
# WARNING: only ever label with bounded, low-cardinality values. Labelling by
# user_id here would create ~1000 time series per metric and eventually take
# Prometheus down -- this is the single most common self-inflicted outage in
# metric systems.
sessions_started = Counter(
    "tartarus_sessions_started_total",
    "Practice sessions started",
    ["track"],
)
```

The cardinality warning is worth teaching explicitly, because it is the most
common way people break their own monitoring, and the failure arrives weeks
after the mistake.

### Structured logging

The current app already has good logging discipline — every request logged,
every error logged, and **answer text and targets deliberately excluded**.
Preserve that and make it JSON, so Loki can query on fields rather than
regex. The privacy rule matters *more* in a centralised store than it did in
a rotating local file ([11](11-security-and-secrets.md)).

Include a request/correlation ID in every log line and return it in an error
response header — so a student who sees a failure in the browser can find
the exact server-side log lines for it. That single practice removes most of
the guesswork from debugging distributed systems.

## 12.5 SLIs, SLOs, and error budgets

The genuinely SRE-specific section.

### Definitions, taught concretely

- **SLI** — a measurement. *"The proportion of answer submissions served in
  under 300 ms."*
- **SLO** — a target for that measurement. *"99% of answer submissions served
  in under 300 ms, over a rolling 30 days."*
- **Error budget** — what the SLO permits you to fail: `100% − 99% = 1%`. Over
  30 days that is ~7 hours 12 minutes of "too slow."

### Proposed SLOs for Tartarus

| SLI | SLO | Why this number |
|---|---|---|
| Answer-submission latency < 300 ms | 99% over 30d | The learner is mid-flow; above ~300 ms the app feels laggy and breaks concentration |
| Session-start success rate | 99.9% over 30d | A failure here blocks the user entirely, not just slows them |
| Availability (`/healthz` 200) | 99.5% over 30d | A study tool, not a payment system — deliberately not chasing more nines than the product needs |
| Dashboard data freshness < 15 min | 95% over 30d | Analytics are explicitly *not* on the critical path (ADR-8), so this is intentionally loose |

**Teach the choosing, not just the numbers.** The most valuable part of this
exercise is students defending *why* availability is 99.5% and not 99.99% —
each extra nine costs real engineering effort, and choosing more than the
product needs is a real and common failure of judgement.

### Error budget as a decision tool

- **Budget remaining** → ship features, take deployment risk, run chaos labs.
- **Budget exhausted** → freeze features, fix reliability.

This converts "should we deploy on Friday?" from an argument into a query —
which is precisely the point of SRE, and lands well in a classroom because
students can *see* the budget move.

### Multi-window burn-rate alerting

Alert on **budget burn rate**, not raw thresholds. A raw "error rate > 1%"
alert fires on brief harmless blips and misses slow steady burns.

```yaml
# ---------------------------------------------------------------------------
# Fast-burn alert: paging severity.
#
# Session:      12 (Observability)
# Fires when:   the 30-day error budget is being consumed 14.4x faster than
#               sustainable -- i.e. the entire month's budget would be gone in
#               ~2 days if this continued. That factor is the standard fast-burn
#               constant from Google's SRE workbook, not an arbitrary number.
# Two windows:  the 1h window detects the problem; the 5m window ensures it is
#               STILL happening. Without the short window, this keeps firing for
#               an hour after an incident is resolved.
# ---------------------------------------------------------------------------
- alert: TartarusErrorBudgetFastBurn
  expr: |
    (
      slo:answer_latency_error_ratio:rate1h > (14.4 * 0.01)
      and
      slo:answer_latency_error_ratio:rate5m > (14.4 * 0.01)
    )
  for: 2m
  labels:
    severity: page
  annotations:
    summary: "Answer-latency error budget burning 14.4x too fast"
    # ALWAYS link the runbook from the alert. An alert without a runbook is a
    # 3am puzzle; with one it is a checklist. Runbooks live in doc 18.
    runbook_url: "https://<your-gitlab>/tartarus/-/blob/main/docs/runbooks/answer-latency.md"
```

## 12.6 Alerting philosophy

- **Page only on symptoms users feel.** "p95 latency breached" pages;
  "CPU is at 80%" does not — high CPU on a healthy system is a *feature*.
- **Every alert has a runbook** ([18](18-operations-and-runbooks.md)).
- **Every alert is actionable.** If the response is "watch it," it should be
  a dashboard, not a page.
- **Alert fatigue is an outage waiting to happen.** A noisy alert gets muted,
  and a muted alert is worse than no alert — it produces false confidence.

## 12.7 Labs

1. **Instrument and observe** — add a custom metric, scrape it, graph it.
2. **Define an SLO** — pick an SLI, choose and *defend* a target, write the
   recording rule.
3. **Burn the budget on purpose** — use the simulator's `spike` scenario
   ([17](17-load-simulator.md)) to breach the latency SLO and watch the
   burn-rate alert fire.
4. **Debug from logs alone** — instructor breaks something; students diagnose
   using Loki and the correlation ID, with no shell access to the host.
5. **Cardinality bomb** (optional, memorable) — deliberately label a metric
   by user id, watch Prometheus memory climb, then fix it.

## 12.8 Completion checklist

- [ ] Prometheus scrapes app, databases, and host/container metrics.
- [ ] Both Grafana folders provisioned from code and populated.
- [ ] At least three SLOs defined with recording rules.
- [ ] Burn-rate alerts fire correctly under the simulator's spike scenario.
- [ ] Every alert links a runbook.
- [ ] Logs are structured, correlation-ID'd, and contain no answer text or secrets.

Next: [13 — Stage 1: VM Deployment with Ansible](13-stage-1-vm-ansible.md).
