# 18 — Operations, Backup/DR, and Runbooks

## 18.1 The phase most courses skip

Deployment is the beginning of a system's life, not the end of it. This
document covers the **Operate** phase: what happens on day 2, day 90, and at
3am when something breaks.

It is also where a DevOps course becomes an *SRE* course. Anyone can follow
a deployment tutorial; operating a system under failure is the harder and
more valuable skill.

---

## 18.2 Backup and restore

### The rule this whole section exists to teach

> **A backup that has never been restored is not a backup. It is a hope.**

Every stage's completion checklist includes a *restore drill*, not a backup
job. Students who have only ever written `pg_dump` to a cron file have not
learned backups; students who have restored into a fresh database and
verified row counts have.

### What must be backed up

| Data | Tool | Frequency | RPO target |
|---|---|---|---|
| PostgreSQL (learner progress) | `pg_dump` + WAL archiving | Nightly full, continuous WAL | < 5 min |
| MongoDB (raw events) | `mongodump` | Nightly | < 24 h |
| ClickHouse (analytics) | `BACKUP` statement / `clickhouse-backup` | Weekly | < 7 d |
| Grafana dashboards | Provisioned as code in git | Every commit | 0 |
| Application config | Ansible + git | Every commit | 0 |
| Redis (session state) | **Not backed up** | — | — |

**The Redis line is a deliberate teaching moment.** Session state is
ephemeral by design ([03](03-architecture-decisions.md) ADR-6): losing it
costs a learner one interrupted session, and nothing more. Backing it up
would be effort spent protecting data that has no value at rest. Deciding
*what not to back up* is a real engineering judgement, and asking students
to justify each line of this table teaches more than the table itself.

Note the asymmetric RPOs. Learner progress is irreplaceable — a learner
cannot re-earn three months of mastery. Raw analytics events are merely
useful. Grafana dashboards are already in git, so their RPO is zero for
free. Recovery objectives follow from *what the data is worth*, not from a
uniform policy.

### Point-in-time recovery, and why `pg_dump` alone is not enough

A nightly `pg_dump` means up to 24 hours of lost progress. WAL archiving
plus a base backup allows recovery to any moment — including *one minute
before* someone ran a destructive migration.

This is best taught by doing it: have students run a destructive statement
against a scratch database, then recover to the moment before it. That
exercise converts PITR from a bullet point into a skill.

### Per-stage restore drill

| Stage | Drill |
|---|---|
| 1 — VM | `pg_dump`, drop a table, restore, verify row counts |
| 2 — Docker | `docker compose down -v` (destroys volumes), restore from backup, verify |
| 3 — Swarm | Simulate node loss, recover the volume on another node |
| 4 — Kubernetes | Delete the Postgres PVC, restore via the operator's backup CR |

The Stage 2 drill is especially effective because `down -v` genuinely
destroys the data in front of them. Nothing teaches the value of a tested
backup quite like watching the database disappear and getting it back.

---

## 18.3 Runbooks

### Template

Every runbook, and every alert's `runbook_url`
([12](12-observability-and-slos.md)), follows one shape:

```markdown
# Runbook: <alert or symptom name>

## Severity
page | ticket | info

## What the user experiences
The observable symptom. Written from the learner's point of view, not the
system's — this is what tells the responder whether it is really urgent.

## How to confirm
Exact commands/queries that prove or disprove this diagnosis.

## Immediate mitigation
How to stop the bleeding. Explicitly NOT a root-cause fix.

## Root cause investigation
Where to look next, in order.

## Escalation
Who to wake, and after how long.

## Prevention
Links to follow-up issues. A runbook used twice for the same cause is a bug
report, not an operations document.
```

### Worked runbook 1 — Postgres unreachable

**User experience.** Practice sessions fail to start; the app shows a
generic error. Existing sessions may continue briefly (Redis-backed) until
they need to persist.

**Confirm.**
```bash
# Is the process alive at all?           (Stage 1)
systemctl status postgresql
# Does it accept connections?            (any stage)
pg_isready -h <host> -p 5432
# What is it saying?
journalctl -u postgresql -n 100 --no-pager
```

**Immediate mitigation.** Restart the service. If it will not start, check
disk (§Worked runbook 2 — a full disk is the most common cause of a Postgres
that refuses to start), then restore from backup (§18.2).

**Root cause.** In rough order of likelihood: disk full; OOM killer (check
`dmesg -T | grep -i oom`); connection-pool exhaustion (`max_connections`
reached — look for a leak in the app, not just a low limit); corrupted data
directory after an unclean shutdown.

**Prevention.** Alert on disk usage > 80% *before* it becomes an outage;
set `max_connections` deliberately and use a pooler; monitor OOM events.

### Worked runbook 2 — disk full

**User experience.** Writes fail; Postgres may refuse to start; logs stop.

**Confirm.**
```bash
df -h
# Largest directories, quickly:
du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20
```

**Immediate mitigation.** In this system the usual culprits, in order:
1. **Log files** — `journalctl --vacuum-size=500M`, check rotation.
2. **Docker** (Stages 2–4) — `docker system prune` removes dangling images
   and stopped containers. **Never** `prune --volumes` without checking what
   is in them; that is how backups get deleted during an incident.
3. **WAL accumulation** — if WAL archiving is failing, Postgres retains WAL
   forever and *will* fill the disk. Fix the archive command; the WAL then
   drains on its own.

**Prevention.** Disk alerts with enough headroom to act. Log rotation
configured from day one. Monitoring on the WAL archive command's success,
not just on disk space — the archiving failure precedes the disk problem by
hours or days, and alerting on the *cause* rather than the *symptom* is a
central SRE idea.

### Worked runbook 3 — bad deploy

**User experience.** Errors spike immediately after a deploy; the SRE
dashboard's error-rate panel steps up.

**Immediate mitigation — roll back first, diagnose after.** This ordering is
itself the lesson: restore service, *then* investigate.

| Stage | Rollback |
|---|---|
| 1 — VM | `ansible-playbook deploy-app.yml -e app_version=<previous-sha>` |
| 2 — Docker | Re-pull the previous SHA tag, `docker compose up -d` |
| 3 — Swarm | `docker service rollback tartarus_backend` |
| 4 — Kubernetes | `kubectl rollout undo deployment/backend` |

**Root cause.** Compare the two image SHAs; read the pipeline for the bad
commit; check whether a migration ran that the previous version cannot
tolerate.

**Prevention.** This is where **backward-compatible migrations** must be
taught, because rollback is a lie if the database moved forward
incompatibly. The expand/contract pattern:

1. **Expand** — add the new column/table; deploy code that writes both old
   and new.
2. **Migrate** — backfill.
3. **Contract** — only once the new version is known good, remove the old
   column in a *later* release.

A migration that drops a column in the same release that stops using it
makes rollback impossible. Students should meet this the hard way, in a lab,
rather than in production.

---

## 18.4 Capacity planning

Use the simulator ([17](17-load-simulator.md)) to derive real numbers
instead of guessing:

1. Ramp load until a *saturation* signal appears (latency knee, queue depth,
   CPU pegged).
2. Record requests/sec per backend replica at that point.
3. Compute headroom: if peak real traffic is N req/s and one replica serves
   M req/s, you need `N/M` replicas plus buffer for failure and growth.
4. Repeat for the database — usually the first thing to saturate, and the
   hardest to scale horizontally.

This is also where the sizing table in
[01](01-prerequisites-and-scope.md) gets replaced by measured values, which
is a satisfying, concrete payoff for the whole simulator investment.

---

## 18.5 Incident response

### Roles (even in a class of three)

- **Incident Commander** — coordinates, decides, does *not* debug.
- **Operations** — the person actually running commands.
- **Communications** — keeps a timeline, informs stakeholders.

Separating command from debugging is the single most valuable structural
lesson here, because the natural failure mode is everyone debugging at once
and nobody deciding.

### Severity levels

| Sev | Meaning | Response |
|---|---|---|
| SEV1 | Complete outage; learners cannot practice | Page immediately, all hands |
| SEV2 | Major degradation; SLO burning fast | Page during working hours |
| SEV3 | Minor; workaround exists | Ticket |

### Blameless postmortem template

```markdown
# Postmortem: <title>   (date, duration, severity)

## Impact          Who was affected, how many, for how long, measurably.
## Timeline        Detection -> mitigation -> resolution, with timestamps.
## Root cause      The technical cause, followed by the systemic one.
## What went well  Genuinely — this is not filler; it identifies what to keep.
## What went badly
## Action items    Owner + due date each. No item, no follow-through.
```

**Blameless is a technique, not a courtesy.** If people are punished for
causing incidents, they stop reporting them, and you lose the data you need
to prevent the next one. The question is never "who ran the command" but
"why was it possible for that command to cause this, and why did nothing
catch it first."

### The on-call simulation lab

The most valuable single exercise in the course:

1. Simulator running steady-state; students on "call."
2. Instructor breaks something (kill a pod, fill a disk, deploy a bad image,
   partition the network) **without telling them what**.
3. Students detect via alerts, diagnose via dashboards and logs, mitigate
   via runbooks, and write a postmortem.
4. Repeat with a different failure — ideally one with no existing runbook,
   so they have to write it afterwards.

This exercises everything the course has built: observability
([12](12-observability-and-slos.md)), the runbooks above, the deployment
mechanism, and the rollback path — under mild time pressure, which is the
condition the skills are actually needed in.

---

## 18.6 Routine operational work

Worth naming explicitly, because "day 2 operations" is otherwise invisible:

| Cadence | Task |
|---|---|
| Daily | Check dashboards, review overnight alerts, verify backups ran |
| Weekly | Rebuild+rescan images for new CVEs ([11](11-security-and-secrets.md)); review error-budget burn |
| Monthly | **Restore drill**; review SLOs against reality; dependency updates |
| Quarterly | Capacity review; disaster-recovery exercise; runbook accuracy audit |

The monthly restore drill is the non-negotiable one (§18.2).

---

## 18.7 Completion checklist

- [ ] Automated backups configured for Postgres, Mongo, and ClickHouse.
- [ ] A restore has been *performed and verified*, not just configured.
- [ ] PITR demonstrated at least once.
- [ ] Every alert has a runbook; every runbook has been walked through.
- [ ] One postmortem written from a real (or instructor-induced) incident.
- [ ] Capacity numbers derived from simulator data, replacing the estimates
      in [01](01-prerequisites-and-scope.md).
- [ ] Runbooks meet the commenting/clarity standard
      ([02](02-authoring-standards.md)) — a stranger can follow them at 3am.

Next: [19 — Classroom Delivery Guide](19-classroom-delivery.md).
