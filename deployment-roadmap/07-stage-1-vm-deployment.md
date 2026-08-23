# 07 — Stage 1: Bare Linux VM Deployment

## Goal

Every service running as a real OS process on a single Ubuntu LTS VM,
managed by `systemd`, with no container runtime involved anywhere. This is
deliberately the most tedious stage — that's the point. Students should
feel exactly how much manual, error-prone bookkeeping containers later
remove, so that removal reads as a *relief*, not an arbitrary tool
preference.

## Target VM shape

One VM is enough to teach the stage (a multi-VM variant — app on one box,
Postgres on another — is a reasonable extension exercise once the
single-box version works, not the starting point).

| Service | How it runs |
|---|---|
| PostgreSQL | Distro package (`apt install postgresql`), or built from the official APT repo for a specific version pin |
| Redis | Distro package |
| MongoDB | Official MongoDB APT repo package |
| ClickHouse | Official ClickHouse APT repo package |
| Grafana | Official Grafana APT repo package |
| Django app | Python venv, `gunicorn` as the WSGI server, behind Nginx |
| Next.js app | `node`, built with `next build`, run with `next start` (or exported and served statically if the route mix allows it — decided in `04`) |
| Celery worker + beat | Same venv as Django, two separate `systemd` units |
| Nginx | Distro package — TLS termination, routes to Varnish |
| Varnish | Distro package — sits between Nginx and the app processes |
| Data-platform ETL | Same Python venv pattern, `systemd` timer instead of Celery beat (a good place to teach `systemd` timers as a cron alternative) |

## Request path

```text
Internet → Nginx (TLS termination, :443)
         → Varnish (:80, HTTP cache — see 06)
         → Next.js (:3000)  or  Django/gunicorn (:8000, /api/*)
```

Nginx terminates TLS (via Let's Encrypt/`certbot`) because Varnish's
open-source edition doesn't do TLS itself — a genuinely useful fact to
teach explicitly (why is there an Nginx *and* a Varnish here, don't they do
the same thing? no — different jobs, see `06`).

## Deliverables under `infra/vm/`

```text
infra/vm/
├── provision/
│   ├── 00-base.sh              # OS packages, users, firewall (ufw) baseline
│   ├── 10-postgres.sh
│   ├── 20-redis.sh
│   ├── 30-mongo.sh
│   ├── 40-clickhouse.sh
│   ├── 50-grafana.sh
│   ├── 60-app.sh               # venv, migrations, static collection
│   └── 70-frontend.sh          # node install, next build
├── systemd/
│   ├── tartarus-gunicorn.service
│   ├── tartarus-nextjs.service
│   ├── tartarus-celery-worker.service
│   ├── tartarus-celery-beat.service
│   └── tartarus-etl.timer / .service
├── nginx/
│   └── tartarus.conf
├── varnish/
│   └── default.vcl             # symlink/copy of caching/varnish/default.vcl — one source of truth
└── README.md                   # step-by-step, copy-pasteable, this stage's actual lab sheet
```

Numbered provisioning scripts (`00-`, `10-`, `20-`...) are a deliberate
choice over one monolithic script: each one is independently re-runnable
and independently gradeable as a lab step, and the numbering leaves room to
insert steps later without renumbering everything.

## What this stage teaches

- Linux service management: `systemd` unit anatomy (`ExecStart`,
  `Restart=`, `User=`, `EnvironmentFile=`), enabling/starting services,
  reading `journalctl` output when something fails to start.
- Manual dependency and version management — and the specific pain of
  "it works on my machine because I have Python 3.11 and the VM has 3.10,"
  which motivates Stage 2 without anyone needing to be told to feel it.
- Reverse proxy chains (Nginx → Varnish → app) and TLS termination.
- Basic host hardening: `ufw` rules (only 22/80/443 open), a non-root
  service user, `fail2ban` as an optional add-on.
- A first, minimal backup story: a cron/`systemd` timer job doing
  `pg_dump` on a schedule, pushed off-box — the un-glamorous but essential
  precursor to the "real HA/DR story" that comes later once Patroni is
  introduced as a stretch topic in Stage 4.

## What this stage deliberately does *not* solve

- No horizontal scaling of the app tier (one Nginx, one gunicorn process
  group, one box).
- No automated failover for Postgres — single instance, manual backup/
  restore only. (Patroni-based HA is out of scope until Stage 4, `10`,
  where StatefulSets make multi-replica stateful services tractable to
  actually run in class.)
- No image immutability — a `git pull` + service restart is the deploy
  mechanism, with all the "what state is actually on this box right now"
  drift risk that implies. Naming this risk explicitly is the setup for
  Stage 2.

Next: [08 — Stage 2: Docker (raw, then Compose)](08-stage-2-docker.md).
