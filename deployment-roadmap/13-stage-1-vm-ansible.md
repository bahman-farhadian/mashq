# 13 — Stage 1: VM Deployment with Ansible

## 13.1 Goal

Every service running as an OS process under `systemd` on a prepared Linux
VM, with **Ansible as the only mechanism that touches the host**. No
containers anywhere.

This is deliberately the most laborious stage. That is the point: students
should *feel* how much manual bookkeeping later stages remove, so the removal
reads as relief rather than fashion.

### Why Ansible and not shell scripts

A first draft of this curriculum used numbered `bash` scripts. Ansible is
strictly better here, and the reasons are themselves the lesson:

| Shell scripts | Ansible |
|---|---|
| Re-running may break things (`useradd` fails if the user exists) | **Idempotent** — describes desired state, safe to re-run |
| No dry run | `--check --diff` shows what *would* change |
| Order and state tracked in your head | Declarative; the playbook *is* the documentation |
| Copy-paste for a second host | Inventory — 1 host or 30, same command |
| Untestable in practice | **Molecule** tests each role ([09](09-testing-strategy.md)) |
| Secrets end up in the script | `ansible-vault` encrypts them at rest in git |

Ansible is agentless (plain SSH), which also means nothing needs installing
on the target beyond Python — a real advantage worth naming when students
ask why not Puppet or Chef.

### Scope boundary

This tree covers **Stage 1 only**: node preparation, hardening, and service
deployment. Node preparation for Stages 2–4 is handled by your existing
separate Ansible codebase ([01](01-prerequisites-and-scope.md)). VMs are
assumed already provisioned.

## 13.2 What runs on the host

| Service | How it runs |
|---|---|
| PostgreSQL | PGDG apt repo, version-pinned |
| Redis | Distro package |
| MongoDB | Official MongoDB apt repo |
| ClickHouse | Official ClickHouse apt repo |
| Prometheus / Grafana / Loki / exporters | Packages + systemd units ([12](12-observability-and-slos.md)) |
| Django backend | Python venv + `gunicorn`, systemd unit |
| Next.js frontend | `next build`, `next start`, systemd unit |
| ETL job | Same venv, **systemd timer** (no Celery — ADR-7) |
| Varnish | Distro package, config from `caching/varnish/default.vcl` |
| Nginx | Distro package — TLS termination |

### Request path

```text
Internet
  │
  ▼
Nginx        :443   TLS termination, static files
  │
  ▼
Varnish      :80    HTTP cache (08)
  │
  ├──────────────► Next.js  :3000   (pages, assets)
  └──────────────► gunicorn :8000   (/api/*)
```

**Why both Nginx and Varnish?** The question every class asks. Answer: they
do different jobs. Open-source Varnish does not terminate TLS, so Nginx sits
in front for HTTPS; Varnish caches. Naming this explicitly prevents the
reasonable assumption that one is redundant.

## 13.3 Ansible layout

```text
ansible/
├── ansible.cfg
├── inventories/
│   ├── production/
│   │   ├── hosts.yml            # which hosts, in which groups
│   │   ├── group_vars/
│   │   │   ├── all.yml          # variables for every host
│   │   │   ├── all/vault.yml    # ansible-vault encrypted secrets
│   │   │   └── db.yml           # variables for the 'db' group only
│   │   └── host_vars/
│   └── staging/
├── roles/
│   ├── common/                  # hardening — runs on every host, first
│   ├── postgresql/
│   ├── redis/
│   ├── mongodb/
│   ├── clickhouse/
│   ├── observability/
│   ├── backend/
│   ├── frontend/
│   ├── etl/
│   ├── varnish/
│   └── nginx/
├── playbooks/
│   ├── site.yml                 # everything, in order
│   ├── bootstrap.yml            # hardening only, first contact
│   └── deploy-app.yml           # app only — the fast path CI calls
└── molecule/                    # per-role tests (09)
```

Each role uses the standard structure (`tasks/`, `handlers/`, `templates/`,
`defaults/`, `vars/`, `meta/`). Teaching the convention matters more than
inventing a bespoke one — students will meet this exact layout everywhere.

### Why `deploy-app.yml` is separate from `site.yml`

`site.yml` converges the whole host and takes minutes. `deploy-app.yml`
updates only the application and takes seconds — and it is what the GitLab
deploy job calls ([10](10-cicd-gitlab.md)). Separating "build the machine"
from "ship the code" is a real operational distinction worth teaching, and it
is the seed of the immutable-infrastructure idea that Stage 2 makes concrete.

## 13.4 Annotated examples

Written to the [02](02-authoring-standards.md) standard — this is the bar
every file in `ansible/` must meet.

### The systemd unit template

```jinja
{#
  ---------------------------------------------------------------------------
  systemd unit for the Django backend, served by gunicorn.

  Session:      13 (Stage 1 — VM deployment)
  Rendered by:  roles/backend/tasks/main.yml
  Depends on:   postgresql.service, redis-server.service (see After= below)
  Concept doc:  13-stage-1-vm-ansible.md
  ---------------------------------------------------------------------------
#}
[Unit]
Description=Tartarus Django backend (gunicorn)
# After= controls START ORDER only -- it does NOT wait for the database to be
# READY to accept queries. Postgres opens its socket seconds before it will
# answer. The app therefore MUST tolerate a database that is not yet ready;
# Restart=on-failure below is what actually makes this converge.
# This distinction reappears in Stage 2 as Compose's depends_on vs.
# condition: service_healthy, and in Stage 4 as readiness probes.
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=notify
# Never run application code as root. This user is created by roles/common
# with no login shell and no home directory: if the app is compromised, the
# attacker lands as an unprivileged user that cannot log in.
User={{ tartarus_service_user }}
Group={{ tartarus_service_group }}
WorkingDirectory={{ tartarus_app_dir }}/backend

# Secrets are NOT inlined here: this file is world-readable under /etc/systemd,
# and 'systemctl show' prints Environment= values to any user. The env file is
# root-owned 0600 and written from ansible-vault (11 — Security and Secrets).
EnvironmentFile={{ tartarus_config_dir }}/backend.env

ExecStart={{ tartarus_venv }}/bin/gunicorn \
    --workers {{ gunicorn_workers }} \
    --bind 127.0.0.1:8000 \
    {# Bound to loopback ONLY. Nothing reaches gunicorn except via Nginx ->
       Varnish. Binding 0.0.0.0 here would expose the app directly, bypassing
       TLS termination and the cache. #}
    --access-logfile - \
    {# '-' sends logs to stdout, which systemd captures into the journal, which
       Promtail ships to Loki (12). Writing our own log file would create a
       second rotation problem we do not need. #}
    config.wsgi:application

# Restart on crash, but give up if it crashes 5 times in 60s -- that is a
# genuine fault (bad config, missing migration) and an endless restart loop
# would mask it while burning CPU.
Restart=on-failure
RestartSec=5s
StartLimitBurst=5
StartLimitIntervalSec=60s

[Install]
WantedBy=multi-user.target
```

### The ETL systemd timer (replacing Celery beat — ADR-7)

```ini
# ---------------------------------------------------------------------------
# Schedules the Mongo -> ClickHouse ETL (07).
#
# Session:      13. Compare with the SAME job scheduled four different ways:
#               stage 2 host cron, stage 3 scheduler service, stage 4 CronJob.
#               That progression is the whole point of this curriculum.
# ---------------------------------------------------------------------------
[Unit]
Description=Run the Tartarus analytics ETL

[Timer]
# Every 5 minutes, measured from when the previous run FINISHED
# (OnUnitActiveSec), not from when it started. If a run ever takes longer than
# the interval, this prevents runs from stacking up on top of each other.
OnBootSec=5min
OnUnitActiveSec=5min

# Without this, every host with this timer fires at exactly the same instant.
# With one host it is harmless; the habit matters when there are fifty.
RandomizedDelaySec=30s

# Run a missed occurrence immediately after boot -- otherwise a host that was
# down over a schedule silently skips that window.
Persistent=true

[Install]
WantedBy=timers.target
```

## 13.5 Teaching points

**Idempotency — demonstrate, don't assert.** Run `site.yml` twice. The second
run reports `changed=0`. Then break something by hand (`systemctl stop
postgresql`, edit a config) and re-run: Ansible repairs exactly the drift and
nothing else. This is the moment configuration management makes sense to
people, and it cannot be replicated with shell scripts.

**Dry runs.** `--check --diff` shows what would change before it changes.
Teach it as the habit it should be, especially against production inventories.

**Handlers.** A config change notifies a handler; the service restarts *once*
at the end of the play, not once per changed file.

**Variable precedence.** `defaults/` → `group_vars/all` → `group_vars/<group>`
→ `host_vars/` → `--extra-vars`. Confusing, universally encountered, and best
learned by deliberately overriding the same variable at several levels and
predicting the winner before running.

**Vault.** Encrypt `group_vars/all/vault.yml`. Commit it. Show `git show`
revealing ciphertext. Discuss where the vault password itself lives — the
honest end of every secrets conversation ([11](11-security-and-secrets.md)).

**Tags.** `--tags backend` re-deploys only the app. This is what makes
`deploy-app.yml` fast enough for CI to call on every merge.

## 13.6 What this stage deliberately does not solve

Naming the limitations is the setup for the next stage:

- **No horizontal scaling beyond one host.** Multiple gunicorn workers, yes —
  but one machine, one failure domain.
- **No automated Postgres failover.** Single instance, `pg_dump` backups
  ([18](18-operations-and-runbooks.md)). Patroni is deferred to Stage 4 where
  StatefulSets make it tractable.
- **No artifact immutability.** Deploy = `git pull` + restart. The host
  accumulates state; two hosts provisioned months apart *will* drift, however
  careful the playbooks are. Ansible narrows this problem; containers
  eliminate it — which is exactly why Stage 2 exists.
- **Dependency conflicts are the host's problem.** One Python version, one
  Node version, shared by everything.

Ask students to predict how each of these changes in Stage 2 *before*
starting it. The prediction is worth more than the answer.

## 13.7 Completion checklist

- [ ] `site.yml` converges a clean host with zero manual steps.
- [ ] Running it a second time reports `changed=0`.
- [ ] `--check --diff` runs clean against a converged host.
- [ ] All services survive a reboot (`systemctl is-enabled` for each).
- [ ] `ci/smoke-test.sh` passes against the host ([09](09-testing-strategy.md)).
- [ ] Secrets are vault-encrypted; no plaintext credential in git.
- [ ] Every Molecule scenario passes, including idempotency.
- [ ] Every role, template, and unit file meets the commenting standard
      ([02](02-authoring-standards.md)) — header block, reasons for every
      non-obvious value, at least one documented failure mode.

Next: [14 — Stage 2: Docker](14-stage-2-docker.md).
