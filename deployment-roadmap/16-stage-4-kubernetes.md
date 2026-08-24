# 16 — Stage 4: Kubernetes

## Goal

The capstone. Everything from Stage 3 recreated on Kubernetes, but this
time state-aware primitives (`StatefulSet`, `PersistentVolumeClaim`) mean
the honest limitation named in [15](15-stage-3-swarm.md) — Swarm can't really do multi-replica
stateful workloads — actually gets resolved, not just talked about.

## Cluster choice for the classroom

Two tracks, similar to Stage 2's split, for a different reason (cost/time,
not a pedagogical either/or):

- **Local, per-student**: `kind` or `k3d` (Kubernetes-in-Docker) — free,
  fast to spin up and tear down, so every student gets their own cluster on
  their own laptop for the manifest-writing and debugging labs.
- **Shared, for the live capstone demo**: a real multi-node cluster —
  **`k3s` or `kubeadm` on your own KVM VMs**, the same hosts used in
  Stages 1 and 3. This is where the ~1000-learner simulator
  ([17](17-load-simulator.md)) and the autoscaling/chaos labs below actually
  mean something, because a single-laptop `kind` cluster cannot demonstrate
  real node-level failover.

**k3s is the recommended default** for the shared cluster: a single binary,
far lighter than `kubeadm`, with `containerd`, a CNI, CoreDNS, and an
ingress controller bundled — so students spend the module on Kubernetes
concepts rather than on cluster bootstrapping. `kubeadm` is the better
choice only if certified-administrator exam preparation is an explicit goal
of your course.

Managed cloud Kubernetes is deliberately out of scope
([01](01-prerequisites-and-scope.md)). Nothing in these manifests depends on
a provider, so they would run on one — the curriculum simply never requires
an account, a console, or a cloud-specific `StorageClass`.

## What's hand-written vs. what's a chart

Deliberate, explicit split — writing 100% of the YAML by hand for
Postgres/Mongo/ClickHouse would be teaching students to reinvent things the
ecosystem has already solved well:

| Component | Approach |
|---|---|
| Django + Next.js app | Hand-written manifests (or a small umbrella Helm chart) under `infra/k8s/` — this is *the* thing students should be able to write from scratch, since it's the actual app they're deploying |
| PostgreSQL | **CloudNativePG** or **Zalando Postgres Operator** — either gives StatefulSet-backed HA (leader + replicas, automatic failover) via a CRD, i.e., Patroni's approach, operationalized. Hand-rolling this is a known trap (documented as one of the harder things to get right on Kubernetes even by experienced teams) |
| Redis | Bitnami Redis Helm chart (or the Redis Operator for HA/Sentinel mode as a stretch) |
| MongoDB | Bitnami MongoDB Helm chart, replica-set mode |
| ClickHouse | Altinity ClickHouse Operator |
| Grafana + Prometheus + Alertmanager + Loki | `kube-prometheus-stack` (Prometheus + Alertmanager + Grafana in one chart) + `loki-stack` — close to the de facto standard for K8s observability, worth teaching as the standard rather than reassembled from scratch |
| Varnish | Hand-written Deployment + Service — small enough, and specific enough to this app's VCL ([08](08-caching.md)), to keep custom |
| Ingress | `ingress-nginx` (matches the Nginx already used in Stage 1, a nice continuity point) + `cert-manager` for automatic TLS via Let's Encrypt |

Writing everything by hand would burn the whole module on YAML mechanics
instead of the actual K8s concepts; using pre-built operators for the
stateful backing services is itself the realistic, production-shaped
choice — that's what real platform teams do too.

## `infra/k8s/` layout

```text
infra/k8s/
├── base/
│   ├── backend-deployment.yaml
│   ├── backend-service.yaml
│   ├── frontend-deployment.yaml
│   ├── frontend-service.yaml
│   ├── varnish-deployment.yaml
│   ├── varnish-service.yaml
│   ├── ingress.yaml
│   ├── configmap-varnish-vcl.yaml   # same default.vcl, now a ConfigMap
│   ├── hpa-backend.yaml
│   └── kustomization.yaml
├── overlays/
│   ├── dev/       # kustomize patch: 1 replica, debug env vars
│   └── prod/      # kustomize patch: real replica counts, resource limits
└── helm/
    └── tartarus/  # optional umbrella chart wrapping base/ + the 3rd-party charts as dependencies
```

Kustomize's base/overlay split is presented explicitly as the same
base/override idea students already learned in [14](14-stage-2-docker.md) (`docker-compose.yml` + `docker-compose.override.yml`) — naming that continuity is worth doing
out loud in class, not left implicit.

## Concepts this stage must land, each with a live demo

- **`Deployment` vs `StatefulSet`** — Django/Next.js as `Deployment`s
  (any replica is interchangeable); Postgres/Mongo/ClickHouse as
  `StatefulSet`s via their operators (stable network identity, one PVC per
  replica, ordered rollout). Kill a backend pod vs. kill a Postgres pod —
  watch the different recovery behavior live.
- **`PersistentVolumeClaim`** and the underlying `StorageClass` — where
  data actually lives, and what happens to it (nothing, on purpose) if a
  pod is deleted and rescheduled.
- **`Service` (ClusterIP) vs `Ingress`** — internal service discovery
  (same `postgres`/`redis`/`mongo` DNS-name pattern from Stages 2–3, now
  via `Service` objects) vs. the one externally-reachable entry point.
- **`HorizontalPodAutoscaler`** on the backend — scale on CPU or (better,
  more realistic) a custom metric via `prometheus-adapter` reading request
  rate from Prometheus. This is the single best pairing with the load
  simulator ([17](17-load-simulator.md)): ramp simulated learners from 200 to
  2,000 live, watch the HPA add backend replicas, watch Grafana's SRE
  dashboard ([12](12-observability-and-slos.md)) show
  it happening.
- **`NetworkPolicy`** — lock down which pods can talk to which (e.g., only
  the backend can reach Postgres; the frontend cannot reach the DB
  directly) — a concrete, checkable "least privilege" lab.
- **Rolling updates and `kubectl rollout undo`** — same lesson as Swarm's
  rollback ([15](15-stage-3-swarm.md)), now with `readinessProbe`/`livenessProbe` doing the
  health-gating instead of a Compose healthcheck, and `maxSurge`/
  `maxUnavailable` instead of Swarm's `update_config`.
- **A real chaos exercise**: delete a random pod (`chaos-mesh` if there's
  time for a dedicated tool, or just a scripted `kubectl delete pod` loop
  if not) while the simulator ([17](17-load-simulator.md)) keeps generating traffic, and read
  the SRE dashboard's ([12](12-observability-and-slos.md)) error-rate panel react in real time. This is the
  single most convincing "why any of this matters" moment in the whole
  four-stage sequence — worth protecting time for.

## Where the Galera elective module (ADR-4) fits

If the class has time for it, Module 4B — the MariaDB Galera multi-master lab ([03](03-architecture-decisions.md) ADR-4) — runs *here*, alongside the main Kubernetes work, using a separate
namespace and a Galera-aware Helm chart (e.g., Bitnami's MariaDB Galera
chart) fronted by ProxySQL. It is explicitly **not** wired into the live
Tartarus application's database connection — it's an isolated lab
environment for teaching multi-master replication and conflict handling on
its own terms, per the reasoning in [03](03-architecture-decisions.md) ADR-3.


## Completion checklist

- [ ] The stack runs; `ci/smoke-test.sh` passes ([09](09-testing-strategy.md)).
- [ ] Images are pulled from the GitLab registry by immutable SHA tag, not
      rebuilt ad hoc and not referenced as `latest` ([10](10-cicd-gitlab.md)).
- [ ] The ETL runs on this stage's own scheduling mechanism (a `CronJob`).
- [ ] Secrets use this stage's mechanism ([11](11-security-and-secrets.md) §11.2).
- [ ] Metrics and logs reach Prometheus/Loki ([12](12-observability-and-slos.md)).
- [ ] A backup/restore drill has been performed ([18](18-operations-and-runbooks.md)).
- [ ] **Every new file meets the commenting standard**
      ([02](02-authoring-standards.md)): header block, a stated reason for
      every non-obvious value, and at least one documented failure mode.
- [ ] The stage README can be followed start-to-finish by someone who has not
      read the other stages.

Next: [17 — Load Simulator and Data Seeder](17-load-simulator.md).
