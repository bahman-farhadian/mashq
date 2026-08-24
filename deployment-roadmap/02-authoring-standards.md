# 02 — Authoring Standards

> **This document is a completion criterion, not a style guide.**
>
> **An uncommented file is an unfinished file** — however correctly it runs.

---

## 2.1 Why this document exists

This project is an **educational artifact**. Its purpose is not to run
Tartarus — the original already does that. Its purpose is to be *read*, by a
student who is seeing Ansible, Docker, Swarm, or Kubernetes for the first
time, often outside class hours with no instructor to ask.

That gives every file a second job. It must work, and it must **teach**.

### The assumed reader

Write for a student who:

- **can** use a Linux shell, read basic Python/JavaScript, and use `git`;
- **cannot** be assumed to know what a WSGI server is, why a container needs
  a `CMD`, what a Kubernetes `Service` selects on, what a Varnish "ban" is,
  or why anyone would want three copies of a program running at once.

Every unexplained assumption is a student stuck, silently, at 11pm.

### The consequence

Comment density in this project is **far higher than production code would
justify** — deliberately. In a production repo, a comment explaining what
`ExecStart=` does would be noise. Here it is the entire point. Do not
"clean up" the comments later; they are the deliverable.

---

## 2.2 The standard

### R1 — Every file opens with a header block

Before any code, state five things:

1. **What** this file is.
2. **Which session/module** it belongs to.
3. **What it depends on** (must exist first).
4. **What consumes it** (what breaks if it is wrong).
5. **Which roadmap document** explains the concept in depth.

### R2 — Explain *why*, never merely *what*

The code already says what it does. The comment must say why it does it that
way, and what would happen otherwise.

```yaml
# BAD  — restates the code, teaches nothing
# set the worker count
workers: 9

# GOOD — explains the reasoning and where the number came from
# Gunicorn's docs recommend (2 x CPU cores) + 1 as a starting point: enough
# workers that one blocked on I/O doesn't idle the core, without so many that
# they thrash. This host has 4 cores -> (2*4)+1 = 9. This is a STARTING point,
# not a tuned value: session 18 has you re-derive it from real load data.
workers: 9
```

### R3 — No unexplained magic

Every port, flag, path, timeout, version pin, and default gets a reason.
If you cannot explain why a value is what it is, that is a signal the value
is wrong — or that you have found something worth researching before
teaching.

### R4 — Progressive disclosure

Explain a concept **in full the first time it appears**, then cross-
reference it afterwards. Files are read in course order; the fifth Ansible
role should not re-explain what a handler is, it should say
*"handler — see `roles/common/tasks/main.yml`, session 13."*

This keeps later files readable without turning every file into a textbook.

### R5 — Tag the session

A student who opens a file mid-course must be able to locate themselves.
Reference the session or module number where the concept is taught.

### R6 — Comment the failure mode, not just the happy path

Where something is easy to get wrong, say so, and say what the symptom looks
like. This is the single highest-value comment type in infrastructure code,
because infrastructure failures are usually silent or cryptic.

```yaml
# NOTE: 'depends_on' alone only waits for the container to START, not to be
# READY. Postgres accepts TCP connections several seconds before it will
# accept queries, so without 'condition: service_healthy' below, the backend
# starts, fails its first query, and crash-loops. That failure looks like a
# bug in the app; it isn't. Session 14.
```

### R7 — This roadmap holds itself to the same standard

Every fenced code block in these markdown files is commented to the depth it
demands of real files. A snippet that would be rejected in `infra/` is
rejected here too.

---

## 2.3 Worked examples

The bar, shown rather than asserted.

### Example A — an Ansible task

**Before** (functional, and useless as teaching material):

```yaml
- name: Install postgres
  apt:
    name: postgresql-16
    state: present
```

**After** (meets the standard):

```yaml
# ---------------------------------------------------------------------------
# Install the PostgreSQL server package.
#
# Session:      13 (Stage 1 — VM deployment)
# Depends on:   the 'common' role having run (adds the PGDG apt repository)
# Consumed by:  the 'backend' role, which cannot migrate without a live DB
# Concept doc:  03-architecture-decisions.md (why PostgreSQL, not MariaDB)
# ---------------------------------------------------------------------------
- name: Install the PostgreSQL server
  ansible.builtin.apt:
    # Pinned to the exact major version, NOT 'postgresql' (a meta-package that
    # tracks whatever the distro currently considers default). An unpinned
    # major version means a future 'apt upgrade' could move the cluster to a
    # new major release, which requires a data-directory migration and WILL
    # take the service down. Pin majors; let minors float for security fixes.
    name: postgresql-16
    state: present
    # Refresh the package index only if it is older than an hour. Without this,
    # a cold host fails with "no installation candidate" because the PGDG repo
    # added by the 'common' role was never indexed.
    update_cache: true
    cache_valid_time: 3600
  become: true                     # package installation requires root
  notify: restart postgresql       # handler in this role's handlers/main.yml
```

### Example B — a Dockerfile stanza

**Before**:

```dockerfile
FROM python:3.12
COPY . .
RUN pip install -r requirements.txt
CMD ["gunicorn", "config.wsgi"]
```

**After**:

```dockerfile
# ---------------------------------------------------------------------------
# Stage 1 of 2: BUILD.
# Compiles Python dependencies (some need a C toolchain) into a virtualenv we
# copy into the slim runtime image later. Nothing from this stage ships, so the
# compilers never reach production. Session 14.
# ---------------------------------------------------------------------------
# '-slim' is Debian-based without the ~700MB of build tooling in the default
# tag. NOT '-alpine': Alpine uses musl libc, which forces slow source builds of
# many Python wheels and has bitten teams with subtle DNS differences.
FROM python:3.12-slim AS builder

# Dependencies are copied and installed BEFORE the application source. Docker
# caches each layer and invalidates every layer after the first change: source
# changes on every commit, requirements change rarely. In this order, a normal
# code edit reuses the cached dependency layer. Reverse them and every build
# reinstalls every package. This single line is most of your build-time budget.
COPY requirements.txt .

RUN python -m venv /opt/venv && \
    # --no-cache-dir: pip's download cache is useless in an image that is
    # thrown away, and costs ~50MB in the layer.
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
```

---

## 2.4 What CI can enforce

Judgement cannot be automated; presence and structure can. These become
pipeline jobs in [10](10-cicd-gitlab.md):

| Check | Rule |
|---|---|
| Header block present | Every shipped file starts with a comment block naming its session |
| Ansible tasks named | Every task has a `name:` (also required for readable output) |
| No unnamed Compose/K8s services | Every service/resource carries an explanatory comment |
| No `TODO`/`FIXME` | Unfinished markers must not ship in teaching material |
| Comment ratio floor | A crude heuristic (e.g. ≥25% comment lines in `infra/`) that catches wholesale omissions, not a quality measure |

The ratio check is deliberately blunt. It cannot tell a good comment from a
bad one — it only catches files where someone forgot entirely. Quality is a
review responsibility, and belongs in the stage completion checklists.

---

## 2.5 Completion checklist (repeated in every stage document)

A stage is done when:

- [ ] It runs, and the smoke test in [09](09-testing-strategy.md) passes.
- [ ] Every new file has a header block (R1).
- [ ] Every non-obvious value has a stated reason (R2, R3).
- [ ] Concepts new to this stage are explained in full; prior concepts are
      cross-referenced, not repeated (R4).
- [ ] At least one likely failure mode is documented with its symptom (R6).
- [ ] The stage README can be followed start-to-finish by someone who has
      not read the other stages.

Next: [03 — Architecture Decisions](03-architecture-decisions.md).
