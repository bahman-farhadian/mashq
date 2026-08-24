# 10 — CI/CD with Self-Hosted GitLab CE

## 10.1 Scope

**Assumed to exist** ([01](01-prerequisites-and-scope.md)): a running GitLab
CE instance, at least one registered runner capable of building container
images, and the Container Registry enabled.

**In scope:** the pipeline — `.gitlab-ci.yml`, its jobs, the image build and
publish flow, and the deployment gates.

**Out of scope:** installing, upgrading, or operating GitLab itself.

## 10.2 Why CI/CD belongs before the deployment stages

The pipeline is what makes the four stages *the same artifact deployed four
ways* rather than four hand-built environments. Once images are built once
and published to a registry, every later stage becomes "pull this exact
image and run it with this configuration" — which is invariant I1
([04](04-repository-layout.md)) made operational.

Teach it here, and Stages 2–4 each get shorter and more honest.

## 10.3 Pipeline shape

```mermaid
flowchart LR
    lint --> test --> build --> scan --> publish --> deploy --> verify
```

| Stage | Jobs | Gate |
|---|---|---|
| `lint` | ruff, black, eslint, ansible-lint, yamllint, hadolint, I1 check, comment-standard check | auto |
| `test` | unit, integration, parity, migration, molecule | auto |
| `build` | backend, frontend, etl, varnish images | auto |
| `scan` | Trivy per image, pip-audit, npm audit, SBOM | auto (fails on HIGH/CRITICAL) |
| `publish` | push images to the GitLab registry | auto on default branch |
| `deploy` | deploy-vm / deploy-compose / deploy-swarm / deploy-k8s | **manual** |
| `verify` | `ci/smoke-test.sh`, Playwright e2e | auto after deploy |

Deployment is a **manual gate** on purpose. Fully automatic
production deployment is a defensible real-world choice, but in a classroom
a student must press the button and watch it happen — and the gate is where
you teach approval flows, GitLab Environments, and rollback.

## 10.4 Illustrative `.gitlab-ci.yml` (annotated to the [02](02-authoring-standards.md) standard)

```yaml
# ===========================================================================
# Tartarus CI/CD pipeline.
#
# Session:      10 (CI/CD)
# Depends on:   a GitLab runner with the Docker executor; Container Registry
#               enabled on this project (see 01 — Prerequisites)
# Consumed by:  every deployment stage (13-16) pulls the images this publishes
# Concept doc:  10-cicd-gitlab.md (this file)
#
# Pipeline order is declared once here; every job below picks one of these.
# A job with no matching stage silently never runs, which is a confusing
# failure mode -- keep this list and the job definitions in sync.
# ===========================================================================
stages: [lint, test, build, scan, publish, deploy, verify]

variables:
  # Two coordinates for every image: the immutable one (commit SHA) and the
  # moving one (branch/tag). Deployments ALWAYS reference the SHA tag --
  # 'latest' is ambiguous the moment two pipelines run close together, and
  # "which build is actually running in prod?" must have exactly one answer.
  IMAGE_BASE: $CI_REGISTRY_IMAGE
  IMAGE_TAG: $CI_COMMIT_SHORT_SHA
  # Postgres settings shared by the service container in the test jobs below.
  POSTGRES_DB: tartarus_test
  POSTGRES_USER: tartarus
  # Real credentials NEVER live here. This is a throwaway value for an
  # ephemeral test database that exists for ~60 seconds. Production secrets
  # come from masked+protected CI/CD variables -- see 11-security-and-secrets.
  POSTGRES_PASSWORD: test-only-not-a-secret

# ---------------------------------------------------------------------------
# LINT
# ---------------------------------------------------------------------------
lint:python:
  stage: lint
  image: python:3.12-slim
  script:
    - pip install ruff black
    - ruff check backend/ data-platform/ simulator/
    # --check means "report, don't rewrite". A formatter that edits files in
    # CI would produce a green pipeline for code that was never committed.
    - black --check backend/ data-platform/ simulator/

lint:invariants:
  stage: lint
  image: alpine:3.20
  script:
    - apk add --no-cache bash
    # Invariant I1: no application source under infra/ (see 04).
    - bash ci/check-no-app-code-in-infra.sh
    # Invariant I3: teaching material must actually be commented (see 02).
    - bash ci/check-comment-standard.sh

# ---------------------------------------------------------------------------
# TEST
# ---------------------------------------------------------------------------
test:backend:
  stage: test
  image: python:3.12-slim
  # 'services' starts sibling containers on the job's network. We use REAL
  # Postgres and REAL Redis rather than mocks: fakeredis and sqlite hide
  # exactly the TTL, transaction, and serialisation bugs these tests exist to
  # catch. See 09 -- Testing Strategy.
  services:
    - postgres:16
    - redis:7
  variables:
    # Hostnames here are the service names above, resolved on the job network.
    DATABASE_URL: postgres://tartarus:test-only-not-a-secret@postgres:5432/tartarus_test
    REDIS_URL: redis://redis:6379/0
  script:
    - pip install -r backend/requirements-dev.txt
    - pytest backend/tests -v --junitxml=report.xml
  artifacts:
    when: always          # publish the report even (especially) on failure
    reports:
      junit: report.xml

test:parity:
  stage: test
  image: python:3.12-slim
  services: [postgres:16, redis:7]
  script:
    # THE GATE (09 §"Parity"). Drives the Django engine and the frozen
    # legacy/ engine through an identical answer sequence and asserts the
    # resulting learner state matches field-for-field. If this fails, the
    # port has silently changed the learning algorithm -- nothing downstream
    # should be deployed.
    - pip install -r backend/requirements-dev.txt
    - pytest backend/tests/test_parity.py -v

test:ansible:
  stage: test
  image: python:3.12
  # Molecule is slow and only meaningful when the roles changed. 'rules'
  # replaces the deprecated only/except -- don't mix the two syntaxes.
  rules:
    - changes: [ansible/**/*]
  script:
    - pip install molecule molecule-plugins[docker] ansible-lint
    - ansible-lint ansible/
    # Each scenario applies the role, verifies it, then applies it AGAIN and
    # asserts zero changes -- the idempotency check (09).
    - cd ansible && molecule test --all

# ---------------------------------------------------------------------------
# BUILD  (one job per image; shown once, the rest are identical in shape)
# ---------------------------------------------------------------------------
build:backend:
  stage: build
  image:
    # Kaniko builds OCI images WITHOUT a Docker daemon, so the runner does not
    # need privileged mode. Privileged runners can escape to the host, which
    # is a poor thing to hand a classroom full of students.
    name: gcr.io/kaniko-project/executor:debug
    entrypoint: [""]
  script:
    - /kaniko/executor
      --context "$CI_PROJECT_DIR"
      --dockerfile "$CI_PROJECT_DIR/infra/docker/images/backend/Dockerfile"
      # --no-push here; publishing is a separate, gated stage so that an
      # image which fails the scan below never reaches the registry.
      --no-push
      --tarPath image.tar
      --destination "$IMAGE_BASE/backend:$IMAGE_TAG"
  artifacts:
    paths: [image.tar]
    expire_in: 1 day

# ---------------------------------------------------------------------------
# SCAN  (detail in 11 -- Security and Secrets)
# ---------------------------------------------------------------------------
scan:trivy:
  stage: scan
  image: aquasec/trivy:latest
  script:
    # Fail the pipeline on HIGH/CRITICAL. --ignore-unfixed avoids blocking on
    # CVEs with no available patch, which would otherwise make the pipeline
    # permanently red through no fault of ours.
    - trivy image --input image.tar --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed

# ---------------------------------------------------------------------------
# DEPLOY  (manual gate; one job per stage, k8s shown)
# ---------------------------------------------------------------------------
deploy:k8s:
  stage: deploy
  image: bitnami/kubectl:latest
  # GitLab Environments track what is deployed where and give you the
  # rollback button in the UI. Without this block you lose that history.
  environment:
    name: k8s
    url: https://tartarus.k8s.lab
  rules:
    # Manual, and only from the default branch: a feature branch must never
    # be deployable to a shared environment by accident.
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: manual
  script:
    # Pin the deployment to THIS pipeline's immutable SHA tag, never 'latest'.
    - kubectl set image deployment/backend backend=$IMAGE_BASE/backend:$IMAGE_TAG
    # Block until the rollout succeeds; without --timeout the job can hang
    # for the runner's full timeout on a crash-looping pod.
    - kubectl rollout status deployment/backend --timeout=300s
```

## 10.5 Image tagging and versioning

| Tag | When | Used for |
|---|---|---|
| `<short-sha>` | every build | **what deployments reference** — immutable, unambiguous |
| `<branch>` | branch builds | convenience for local testing |
| `latest` | default branch only | convenience only — **never** referenced by a deploy |
| `v<semver>` | git tags | releases, rollback targets |

The rule to state plainly: **deployments reference immutable tags.** If a
student ever cannot answer "which commit is running right now?", the tagging
scheme has failed.

## 10.6 What each stage's deploy job actually does

| Stage | Deploy mechanism |
|---|---|
| 1 — VM | `ansible-playbook playbooks/deploy-app.yml` against the inventory |
| 2 — Docker | SSH + `docker compose pull && docker compose up -d` |
| 3 — Swarm | `docker stack deploy -c stack.yml tartarus` (Swarm rolls it out) |
| 4 — Kubernetes | `kubectl set image` + `kubectl rollout status` |

Four mechanisms, one artifact. Putting them side by side in one table is one
of the more effective single slides in the course.

## 10.7 Failure modes worth teaching

- **A green pipeline that deployed nothing.** A `rules:` typo makes a job
  silently never run. Teach reading the pipeline graph, not just its colour.
- **Cache poisoning across branches.** A shared `cache:key` lets one branch
  serve another's stale dependencies. Key caches on the lockfile hash.
- **Secrets in job logs.** An unmasked variable echoed by a script leaks
  into logs any developer can read — see [11](11-security-and-secrets.md).
- **The scan that everyone ignores.** A permanently-red vulnerability job
  gets muted within a week. Discuss `--ignore-unfixed`, triage, and the
  organisational failure mode of alert fatigue.

## 10.8 Completion checklist

- [ ] Pipeline runs green end to end on the default branch.
- [ ] Images are published with SHA tags and pulled by at least one stage.
- [ ] Both invariant checks (I1, comment standard) run in `lint` and can be
      demonstrated *failing* on a deliberately bad commit.
- [ ] Deploy jobs are manual, environment-tracked, and rollback-capable.
- [ ] `.gitlab-ci.yml` meets the commenting standard ([02](02-authoring-standards.md)).

Next: [11 — Security and Secrets](11-security-and-secrets.md).
