# 01 — Prerequisites and Scope

This document exists to prevent two failure modes: students arriving at
Module 1 without the infrastructure they need, and the project quietly
growing into a course about cloud provisioning.

---

## 1.1 Assumed to already exist

The project **starts from a running VM**. How that VM came to exist is
outside this curriculum.

### Virtual machines

Assumed provisioned by your existing KVM/Ansible tooling (or any equivalent
mechanism — a cloud instance would work identically; the project simply does
not teach or depend on either).

What the project requires of them:

| Property | Requirement |
|---|---|
| OS | Ubuntu Server LTS (22.04 or 24.04), minimal install |
| Access | SSH reachable, a sudo-capable user, your SSH public key installed |
| Python | Python 3 present (Ansible's control requirement on the target) |
| Network | Static or reserved IPs, nodes able to reach each other |
| Internet | Package-repository access, or a configured local mirror |

### Host counts and sizes per stage

Sizing is a *starting point* — [18](18-operations-and-runbooks.md) includes
a capacity exercise where students derive real numbers from simulator data
rather than trusting this table.

| Stage | Hosts | Per-host baseline | Notes |
|---|---|---|---|
| **1 — VM** | 1 | 4 vCPU, 8 GB RAM, 60 GB disk | Everything co-located; the point is that this is the simple case |
| **2 — Docker** | 1 | 4 vCPU, 8 GB RAM, 80 GB disk | Same box, containerised; extra disk for images and volumes |
| **3 — Swarm** | 3 | 2–4 vCPU, 4–8 GB RAM, 40 GB disk | 3 managers, so Raft quorum survives one failure |
| **4 — Kubernetes** | 3+ | 1 control-plane 4 vCPU/8 GB, 2+ workers 4 vCPU/8 GB | k3s or kubeadm; more workers make scheduling lessons better |

Stages can reuse the same hosts if they are torn down between modules —
worth doing deliberately at least once, since rebuilding a node from scratch
is itself the "cattle, not pets" lesson.

### GitLab CE

Assumed installed, running, and reachable, with:

- at least one registered **runner** capable of building container images
  (Docker executor with privileged mode, or a Kaniko-based setup);
- the **Container Registry** feature enabled — this is where every stage
  from 2 onward pulls its images;
- a project created for this repository.

**Provisioning GitLab is explicitly out of scope.** This project writes
`.gitlab-ci.yml` and its jobs ([10](10-cicd-gitlab.md)); it does not teach
installing or operating GitLab itself.

### Name resolution

Either working DNS records or coordinated `/etc/hosts` entries for the
service hostnames each stage exposes. TLS via a local CA or Let's Encrypt is
covered in [11](11-security-and-secrets.md), but certificate *issuance
infrastructure* is assumed available.

### Student prerequisites

The curriculum assumes a student can:

- use a Linux shell (navigate, edit files, read logs, manage a package);
- read basic Python and JavaScript;
- use `git` at the level of clone/branch/commit/push.

It assumes **no prior knowledge** of Django, Next.js, Ansible, Docker,
Swarm, Kubernetes, Prometheus, Grafana, ClickHouse, or Varnish. That
assumption is what drives the commenting standard in
[02](02-authoring-standards.md).

---

## 1.2 Explicit non-goals

Every item here was considered and deliberately excluded. Listing them
prevents the plan from being "improved" into something unteachable.

| Excluded | Why |
|---|---|
| **Cloud providers** (AWS, GCP, DigitalOcean…) | Out of scope by instruction. Every stage runs on self-managed VMs. Nothing in the design *prevents* running on cloud instances; the course simply never depends on a provider's API, console, or managed service. |
| **VM provisioning** (Terraform, Vagrant, Packer) | Hosts are assumed to exist (§1.1). Adding an IaC tool would front-load a large topic before a single service is deployed. |
| **Installing/operating GitLab** | Assumed to exist. The lesson is *pipelines*, not GitLab administration. |
| **Managed Kubernetes** | Contradicts the no-cloud constraint; k3s/kubeadm on your own hosts is the target. |
| **Microservice decomposition** | Rejected as overengineering — full reasoning in [03](03-architecture-decisions.md) ADR-5. |
| **Service mesh** (Istio, Linkerd) | Enormous surface area, near-zero payoff for ~12 services with no inter-service call graph worth managing. |
| **Multi-region / geo-replication** | Nothing in the curriculum's failure model needs it. |
| **Celery / message broker** | Rejected — the only recurring job is better taught as a per-stage scheduled deployable. [03](03-architecture-decisions.md) ADR-7. |
| **Mobile/native clients** | The web frontend is the whole product surface. |

---

## 1.3 Scope boundary for the application itself

Two pieces of the current app cannot survive the migration unchanged. Both
are handled in [05](05-backend-django.md), flagged here so the boundary is
visible early:

- **Pre-generated audio** (per-list SQLite files) — the concept survives;
  the storage moves to a real media store (local filesystem at Stage 1,
  self-hosted MinIO from Stage 2 onward) because a multi-replica deployment
  cannot depend on files sitting on one node's local disk.
- **Live TTS via the macOS `say` command** — does not survive. It is
  macOS-only and therefore dead on every Linux VM, container, and pod this
  plan targets. Replaced by a self-hosted engine (Piper) or dropped for
  personal lists, matching how the app already degrades gracefully on
  non-macOS hosts.

---

## 1.4 Definition of done for the whole project

The project is complete when **all** of the following hold:

1. `backend/` and `frontend/` exist once, with no per-stage forks (I1).
2. The ported test suite passes, including parity against `legacy/`
   ([09](09-testing-strategy.md)).
3. All four stages deploy the same images/artifacts and serve the same app.
4. The GitLab pipeline runs lint → test → build → scan → publish → deploy.
5. The simulator ([17](17-load-simulator.md)) can drive any stage and
   populate dashboards.
6. Every shipped file meets the commenting standard
   ([02](02-authoring-standards.md)) — enforced in CI where mechanisable.
7. Each stage has a working backup/restore drill
   ([18](18-operations-and-runbooks.md)).

Next: [02 — Authoring Standards](02-authoring-standards.md).
