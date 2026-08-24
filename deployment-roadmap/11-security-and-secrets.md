# 11 — Security and Secrets

## 11.1 The blocking prerequisite

The current application's security model is explicit and deliberate:
**local-first, trusted-client, no authentication layer.** Its own README says
so, and for a localhost tool that is the right call.

It becomes the wrong call the moment the app is reachable from a classroom
network, a shared VM, or a Kubernetes Ingress — which is Stage 1, day one.

> **Blocking rule.** No stage exposes Tartarus on a network interface other
> than loopback until the auth work in [05](05-backend-django.md) §5.5 is
> complete and its authorisation tests ([09](09-testing-strategy.md)) pass.

This is worth enforcing visibly in class, because "we'll add auth later" is
one of the most common and most expensive real-world mistakes, and here
students can watch what it would have cost.

## 11.2 Secrets: the same problem, four mechanisms

Secrets are the single clearest illustration of this curriculum's thesis —
identical requirement, four different tools:

| Stage | Mechanism | Stored where | Delivered as |
|---|---|---|---|
| 1 — VM | `ansible-vault` | Encrypted file in git | A `0600` env file, root-owned, read by systemd |
| 2 — Docker | `.env` file (git-ignored) | Outside git, on the host | Container environment |
| 3 — Swarm | `docker secret` | Encrypted in the Raft log | In-memory file at `/run/secrets/<name>` |
| 4 — Kubernetes | `Secret` + SOPS or sealed-secrets | Encrypted in git; plaintext in etcd | Mounted file or env var |
| CI | GitLab masked + protected variables | GitLab's DB | Job environment |

### The lesson underneath the table

Each mechanism trades convenience against exposure, and each one leaks
differently:

- **Environment variables are visible** in `docker inspect`, `/proc/<pid>/environ`,
  crash dumps, and are frequently logged by frameworks on startup. Prefer
  **file-mounted** secrets where the platform supports it (Swarm always,
  Kubernetes usually).
- **Kubernetes `Secret`s are base64, not encryption.** Anyone with `get
  secret` RBAC reads them in plaintext. They are stored unencrypted in etcd
  unless encryption-at-rest is explicitly configured. This surprises almost
  everyone the first time, and is worth demonstrating with a live
  `kubectl get secret -o yaml | base64 -d`.
- **`ansible-vault` encrypts at rest in git**, which is genuinely good — but
  the vault password itself has to live somewhere, and "where does the key to
  the keys live?" is the honest, unavoidable end of every secrets discussion.

### Rules that apply in every stage

1. No plaintext secret is ever committed. Enforced by `gitleaks` in the
   `lint` stage ([10](10-cicd-gitlab.md)).
2. A leaked secret is **rotated, not un-committed.** `git push --force` does
   not unpublish anything; assume any secret that ever reached a remote is
   compromised.
3. Every stage's secret material is separate. A student who compromises the
   Compose stage learns nothing about the Kubernetes stage.
4. `.env.example` is committed with **placeholder** values, documenting every
   required variable. `.env` itself is git-ignored.

## 11.3 Supply-chain security in the pipeline

Jobs in the `scan` stage of [10](10-cicd-gitlab.md):

| Tool | Target | Failure policy |
|---|---|---|
| **Trivy** | Built container images | Fail on HIGH/CRITICAL, `--ignore-unfixed` |
| **pip-audit** | Python dependencies | Fail on known CVEs |
| **npm audit** | Node dependencies | Fail on HIGH/CRITICAL |
| **gitleaks** | Repository history | Fail on any hit |
| **syft** | Built images | Generate SBOM (report only, no gate) |
| **hadolint** | Dockerfiles | Fail on errors |

### Base-image discipline

- Pin base images by **major.minor** (`python:3.12-slim`), not `latest` — an
  unpinned base means an unrelated upstream release can change your runtime
  overnight with no commit in your repo.
- Prefer `-slim` over full images (smaller attack surface, faster pulls) and
  over `-alpine` for Python (musl forces slow source builds and has produced
  subtle DNS differences).
- Rebuild on a schedule, not only on code change: an image with no commits
  still accumulates CVEs. A weekly scheduled pipeline that rebuilds and
  rescans is a small, realistic, teachable practice.

### The honest conversation about scanners

A vulnerability job that is permanently red gets muted within a week, and a
muted job is worse than no job — it produces false confidence. Teach:
triage, `--ignore-unfixed`, documented exceptions with expiry dates, and the
organisational failure mode of alert fatigue. This is an SRE lesson wearing
a security costume.

## 11.4 Application hardening checklist (Django)

Verified by `manage.py check --deploy`, which should run in CI:

- [ ] `DEBUG = False` in every non-development settings module.
- [ ] `SECRET_KEY` from the environment, never a default in source.
- [ ] `ALLOWED_HOSTS` explicitly set (never `['*']`).
- [ ] `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` on.
- [ ] `SECURE_HSTS_SECONDS` set, with the subdomain/preload implications
      understood before enabling.
- [ ] `SESSION_COOKIE_HTTPONLY` and `SameSite` configured.
- [ ] CSRF enforced on all state-changing endpoints.
- [ ] DRF throttling on auth and answer-submission endpoints (Redis-backed,
      [08](08-caching.md)).
- [ ] No secret, answer text, or credential in any log line — the current app
      already holds this discipline ("answer text and correct targets are
      deliberately excluded from every log line"); it must survive the port,
      and matters *more* in a centralised log store ([12](12-observability-and-slos.md)).

## 11.5 Network and platform hardening per stage

**Stage 1 (VM)** — handled by the `common` Ansible role
([13](13-stage-1-vm-ansible.md)): `ufw` default-deny with only 22/80/443
open; SSH key-only, no root login, no password auth; `fail2ban`;
`unattended-upgrades` for security patches; a non-root service user; database
bound to `127.0.0.1` only.

**Stage 2 (Docker)** — containers run as a non-root `USER`; no `--privileged`;
read-only root filesystem where possible; only the reverse proxy publishes
ports (databases stay on the internal network, never `-p 5432:5432`).

**Stage 3 (Swarm)** — `docker secret` rather than environment variables;
encrypted overlay networks (`--opt encrypted`); manager nodes not running
application workloads.

**Stage 4 (Kubernetes)** — the richest set, and the best lab material:
`NetworkPolicy` default-deny with explicit allows (frontend may not reach
Postgres directly); `securityContext` with `runAsNonRoot`,
`readOnlyRootFilesystem`, dropped capabilities; RBAC with per-workload
ServiceAccounts; resource limits on every container (an unlimited pod is a
node-wide denial of service waiting to happen); Pod Security Admission.

### A lab worth running

Deploy a deliberately over-permissive `NetworkPolicy`, have students prove
from inside a frontend pod that they can reach Postgres directly, then write
the policy that stops it and prove the app still works. Concrete, verifiable,
and it teaches least privilege as something you *demonstrate* rather than
assert.

## 11.6 Completion checklist

- [ ] Auth implemented, authorisation tests passing (§11.1).
- [ ] `manage.py check --deploy` clean in CI.
- [ ] No plaintext secrets in the repo; `gitleaks` green.
- [ ] Each stage uses its stage-appropriate secret mechanism (§11.2).
- [ ] Scan jobs run and can be demonstrated failing on a known-bad image.
- [ ] Every security-relevant config file explains *why* each setting is set
      ([02](02-authoring-standards.md)).

Next: [12 — Observability and SLOs](12-observability-and-slos.md).
