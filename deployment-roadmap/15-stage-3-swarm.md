# 15 — Stage 3: Docker Swarm

## Goal

The same Compose-defined services, now running across a real multi-node
cluster, using Swarm's built-in orchestration primitives: services,
replicas, overlay networking across hosts, secrets, configs, and rolling
updates. This is the first stage where "a node can die and the app keeps
running" is something students actually cause and observe, not just read
about.

## Cluster shape for the classroom

Minimum viable cluster to make every lesson in this stage real: **3 nodes**
(can be VMs from Stage 1's provisioning scripts, repurposed) — 1 manager +
2 workers, or 3 managers for a proper quorum demo (Raft needs an odd
number ≥3 to survive one failure). Recommend **3 managers** specifically so
the "kill a manager, watch the cluster keep making decisions" lab is
possible — a 1-manager cluster has nothing interesting to say about
consensus.

```bash
# on the first node
docker swarm init --advertise-addr <manager-ip>
# on each additional manager
docker swarm join-token manager   # run the printed command on the joining node
# on each worker
docker swarm join-token worker
```

## `infra/swarm/stack.yml`

Swarm stacks are deployed from a Compose-shaped file with a `deploy:` key
per service — deliberately close to what [14](14-stage-2-docker.md)'s `docker-compose.yml`
already looks like, so the delta between "Compose on one box" and "the
same file, orchestrated across a cluster" is small and legible:

```text
infra/swarm/
├── stack.yml
├── configs/
│   └── varnish.vcl              # -> docker config, mounted read-only into the varnish service
└── secrets/
    └── README.md                # HOW secrets are created (docker secret create), never actual secret values committed here
```

Key `deploy:`-level concepts to actually exercise, not just mention:

- **`replicas:`** on the Django and Next.js services (stateless, safely
  horizontal) — start at 1, scale to 3 with
  `docker service scale tartarus_backend=3` live in class, watch the
  routing mesh spread traffic without touching Nginx/Varnish config.
- **`placement.constraints`** pinning each stateful service (Postgres,
  Mongo, ClickHouse) to a specific node label
  (`node.labels.role == db`) with a bind-mounted host path or a
  Swarm-aware volume driver. **Be explicit about the limitation here**:
  Swarm has no native equivalent of Kubernetes' `StatefulSet` — there's no
  built-in "one persistent volume per replica, stable network identity per
  replica" primitive. The honest, correct pattern at this stage is
  **single-replica, node-pinned stateful services**, same as Stage 1/2, just
  now scheduled by Swarm instead of manually placed. Real multi-replica
  stateful orchestration is deferred to Kubernetes ([16](16-stage-4-kubernetes.md)) on purpose —
  presenting Swarm as capable of something it isn't would be teaching a
  false lesson.
- **`docker config`** for non-secret configuration that needs to be
  cluster-distributed (Varnish's `default.vcl`, from [08](08-caching.md) — the exact same file used in every prior stage) — versus **`docker secret`** for
  credentials (DB passwords, API keys), which Swarm encrypts at rest and
  in transit and only exposes to the containers that need them, mounted as
  in-memory files under `/run/secrets/`, never as environment variables.
  The secrets-vs-configs distinction, and *why* credentials specifically
  shouldn't be plain environment variables (visible in `docker inspect`,
  process listings, and often accidentally logged), is a direct, concrete
  security lesson.
- **Rolling updates** — `update_config:` (`parallelism`, `delay`,
  `failure_action: rollback`) on the backend service, then a live demo:
  ship a new image tag, watch Swarm replace replicas a few at a time,
  intentionally ship a broken image tag once to watch the automatic
  rollback trigger.
- **Overlay networking** — the app's environment variables still say
  `postgres`, `redis`, `mongo` as hostnames, exactly as in [14](14-stage-2-docker.md)'s Compose file, and it still resolves — now across physical/VM hosts instead of one
  Docker daemon's bridge network. Worth pausing on explicitly: *nothing
  about the app's own configuration changed between Stage 2B and Stage 3*.
  Only the orchestrator did.

## What this stage teaches

- Cluster formation and Raft-based manager quorum (kill one of three
  managers, cluster survives; kill two, it doesn't — do this live).
- Declarative scaling of stateless services.
- The real, load-bearing distinction between "orchestrates stateless
  replicas well" (Swarm, adequately) and "orchestrates stateful,
  per-replica-identity workloads well" (Swarm, not really — motivating
  Kubernetes honestly rather than by fiat).
- Secrets as a first-class, encrypted-at-rest primitive, distinct from
  configuration.
- Rolling updates and automatic rollback on failure — the first stage
  where a bad deploy is something the orchestrator itself notices and
  corrects, rather than something a human has to catch.


## Completion checklist

- [ ] The stack runs; `ci/smoke-test.sh` passes ([09](09-testing-strategy.md)).
- [ ] Images are pulled from the GitLab registry by immutable SHA tag, not
      rebuilt ad hoc and not referenced as `latest` ([10](10-cicd-gitlab.md)).
- [ ] The ETL runs on this stage's own scheduling mechanism (a long-running scheduler service — Swarm has no native cron).
- [ ] Secrets use this stage's mechanism ([11](11-security-and-secrets.md) §11.2).
- [ ] Metrics and logs reach Prometheus/Loki ([12](12-observability-and-slos.md)).
- [ ] A backup/restore drill has been performed ([18](18-operations-and-runbooks.md)).
- [ ] **Every new file meets the commenting standard**
      ([02](02-authoring-standards.md)): header block, a stated reason for
      every non-obvious value, and at least one documented failure mode.
- [ ] The stage README can be followed start-to-finish by someone who has not
      read the other stages.

Next: [16 — Stage 4: Kubernetes](16-stage-4-kubernetes.md).
