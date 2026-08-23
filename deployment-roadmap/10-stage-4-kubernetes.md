# 10 — Stage 4: Kubernetes

## Goal

The capstone. Everything from Stage 3 recreated on Kubernetes, but this
time state-aware primitives (`StatefulSet`, `PersistentVolumeClaim`) mean
the honest limitation named in `09` — Swarm can't really do multi-replica
stateful workloads — actually gets resolved, not just talked about.

## Cluster choice for the classroom

Two tracks, similar to Stage 2's split, for a different reason (cost/time,
not a pedagogical either/or):

- **Local, per-student**: `kind` or `k3d` (Kubernetes-in-Docker) — free,
  fast to spin up/tear down, every student gets their own cluster on their
  own laptop for the manifest-writing/debugging labs.
- **Shared, for the live capstone demo**: a real multi-node cluster (a
  managed offering, or self-managed `k3s`/`kubeadm` across the same VMs
  used in Stage 1/3) — this is where the ~1000-learner simulator (`11`)
  and the autoscaling/chaos labs below actually mean something, since a
  3-node `kind` cluster on one laptop can't demonstrate real node-level
  failover.

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
| Varnish | Hand-written Deployment + Service — small enough, and specific enough to this app's VCL (`06`), to keep custom |
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
base/override idea students already learned in `08` (`docker-compose.yml`
+ `docker-compose.override.yml`) — naming that continuity is worth doing
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
  simulator (`11`): ramp simulated learners from 200 to 2,000 live, watch
  the HPA add backend replicas, watch Grafana's SRE dashboard (`05`) show
  it happening.
- **`NetworkPolicy`** — lock down which pods can talk to which (e.g., only
  the backend can reach Postgres; the frontend cannot reach the DB
  directly) — a concrete, checkable "least privilege" lab.
- **Rolling updates and `kubectl rollout undo`** — same lesson as Swarm's
  rollback (`09`), now with `readinessProbe`/`livenessProbe` doing the
  health-gating instead of a Compose healthcheck, and `maxSurge`/
  `maxUnavailable` instead of Swarm's `update_config`.
- **A real chaos exercise**: delete a random pod (`chaos-mesh` if there's
  time for a dedicated tool, or just a scripted `kubectl delete pod` loop
  if not) while the simulator (`11`) keeps generating traffic, and read
  the SRE dashboard's error-rate panel react in real time. This is the
  single most convincing "why any of this matters" moment in the whole
  four-stage sequence — worth protecting time for.

## Where the Galera elective module (`01` §3) fits

If the class has time for it, Module 4B — the MariaDB Galera multi-master
lab — runs *here*, alongside the main Kubernetes work, using a separate
namespace and a Galera-aware Helm chart (e.g., Bitnami's MariaDB Galera
chart) fronted by ProxySQL. It is explicitly **not** wired into the live
Tartarus application's database connection — it's an isolated lab
environment for teaching multi-master replication and conflict handling on
its own terms, per the reasoning in `01`.

Next: [11 — Load Simulator and Data Seeder](11-load-simulator.md).
