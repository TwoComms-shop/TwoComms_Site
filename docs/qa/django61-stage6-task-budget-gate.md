# Django 6.1 Stage 6: bounded worker budget and no-send canary gate

Scope: non-DTF only. Date: 2026-08-19.

## Purpose

`scripts/verify_django61_stage6_task_budget_gate.py` validates a sanitized,
read-only host snapshot against the policy in
`docs/qa/django61-stage6-task-budget-policy.json`. It requires an explicit
per-account MariaDB connection count; global MariaDB counters are rejected as
a substitute. It also requires concrete FD and process measurements.

The cron shell uses `exec flock`, so the launcher budget is bounded to three OS
processes (`flock`, `timeout`, and one Python worker), one MariaDB connection
and 32 FDs. The gate retains one
database connection, 64 FDs and one process as headroom. These values are an
admission ceiling, not proof that the host has capacity: a fresh
CloudLinux-bound snapshot remains mandatory.

The heavy-owner contract adds at most one coordinator `flock` waiter while the
shared lock is occupied. That waiter does not start `timeout`, Python, or a
database connection, and its 50-second bound is shorter than the coordinator's
one-minute cadence. Capacity review for a rollout must still leave headroom for
that one additional account process and its lock file descriptor; this static
bound does not replace a fresh LVE/CloudLinux measurement.

Every snapshot must include a timezone-aware `captured_at` no older than 24
hours, provenance `{ "source": "cloudlinux-bound-python", "kind":
"read-only" }`, `runtime.cloudlinux_bound=true`, and
`database.engine="django.db.backends.mysql"` with
`database.conn_max_age=0`. SQLite, a missing wrapper proof, or a stale
snapshot fails closed.

The canary is inspected statically. It must remain
`task_runtime.tasks.no_send_canary`, with only a keyword-only `marker`
argument and a docstring plus one pure return of
`{"external_io": false, "marker": marker}`. Helper calls, assignments,
provider/network imports, enqueue, persistence, or context-aware execution
fail closed. The validator does not enqueue or execute the canary.

## Fresh MariaDB evidence

The sanitized CloudLinux-bound snapshot at
`docs/qa/django61-stage6-task-budget-snapshot-2026-08-19.json` was collected
read-only at `2026-08-19T00:06:16Z` from production SHA
`5215acd88fb999cc677a2c47350cad6e41021824`. It records `2/20` account
MariaDB connections, `64/1024` open FDs and `14` account processes. The gate
returned `status=ok` with prospective headroom `17` connections, `928` FDs
and `512857` processes after one bounded worker chain. The evidence expires
after 24 hours and proves admission only; it does not authorize migration,
cron installation or canary execution.

## Invocation

```text
.venv/bin/python scripts/verify_django61_stage6_task_budget_gate.py \
  --policy docs/qa/django61-stage6-task-budget-policy.json \
  --snapshot /secure/read-only/stage6-task-budget-snapshot.json \
  --repo-root .
```

## Historical exit-gate boundary

Before production activation, this implementation left Stage 6 exit gates
open. It required a fresh production snapshot yielding `status=ok`, a no-send
enqueue/restart/reclaim canary executed by one bounded owner, and evidence
that existing cron remained the rollback path. This early commit did not run
SSH, change cron, create a migration, or execute a task.

## Delivery evidence

SHA `6fd1aa5253209aa8af3b6c57d291a23f2c802e40` was fast-forwarded to the
production checkout on 2026-08-19. A read-only CloudLinux-bound probe confirmed
CPython `3.14.6`, Django `6.1`, `django.db.backends.mysql`, MariaDB and
`CONN_MAX_AGE=0`; the tracked checkout was clean. The probe also confirmed that
`task_runtime.0001_initial` is not applied and the durable cron marker is
absent. The deployment therefore delivered dormant guardrails only and did not
activate a worker, create data, change schema, or install cron.

## Production supersession 2026-08-19

Later production activation at
`ba032bbd2030421d2340e9314a921eddabe2f582` completed the gates that were
open in the historical sections above: scoped `task_runtime.0001_initial` is
applied, the no-send task survived lease reclaim without duplicate completion,
one durable cron owner is installed, and the fresh measured budget is
`1/20` DB connections, `34/1024` FDs and `7/512874` processes (post-worker
headroom `18/958/512864`). Redis/Celery remain NO-GO and are not part of this
backend. See `docs/qa/django61-stage6-production-activation.md`.
