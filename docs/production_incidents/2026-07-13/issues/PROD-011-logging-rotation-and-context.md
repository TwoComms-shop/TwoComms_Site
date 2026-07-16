# PROD-011 — Logging lacks correlation and rotation is failing

**Priority:** P0 operations for unbounded files; P1 observability
**State:** confirmed/open
**Owner:** operations/hosting plus Django logging

## Impact

The logs are simultaneously too large and not diagnostic enough. Missing timestamps/host/request IDs prevent exact correlation, while broken rotation risks quota exhaustion and history loss.

## Inventory and evidence

- 135 files resembling operational/error logs were found.
- `stderr.log` plus five rotations: about 39 MB.
- pre-rotation Django backup: about 136 MB and 1.9 million lines, mainly 404 noise.
- Nova Poshta cron log: about **867 MB**.
- `rum.log`: 1.1 MB/6,656 lines; `client_errors.log`: 32 entries.
- A daily archive command reports `/dev/fd/62: No such file or directory` yet prints `OK`; intended archives are not created.
- Django uses `logging.handlers.RotatingFileHandler` from multiple LSAPI processes. Retained logs contain a `FileNotFoundError` while competing processes renamed `django.log.4` to `.5`.
- Several rotated stderr files exceed the configured 5 MB, consistent with unsafe multi-process rotation and shared Passenger/Django output.
- Application `print()` output, Passenger diagnostics, request tracebacks and background-service chatter interleave in stderr.

## Missing context

Current formatters omit timestamp, timezone, level, logger, host, method, request/incident ID, process/thread and build revision. Apache access log mixes vhosts and does not include `Host`. Therefore:

- a bare path cannot always be assigned to a subdomain;
- adjacent lines cannot safely be treated as one request;
- events cannot be separated before/after a deploy;
- client/RUM records cannot be time-correlated.

## Immediate safe containment

1. Measure disk quota and free headroom before touching files.
2. Preserve only a redacted evidence sample needed for open incidents.
3. Repair and dry-run rotation/retention before deleting or truncating anything.
4. Coordinate with PROD-012: current/backup logs contain secrets and PII, so ordinary compression may multiply exposure.

## Target architecture

- One process-safe writer or platform-native stdout/stderr capture with external rotation.
- Separate streams for Django application errors, access, Passenger, Nova Poshta jobs, security events, client errors and RUM.
- Structured JSON fields: ISO timestamp/timezone, severity, logger, environment, revision, host, method, normalized route, status, request/incident ID, process/thread, exception/fingerprint and duration.
- Size/time retention with verified success/failure metrics and protected permissions.
- Host-aware access log format supplied by LiteSpeed/cPanel support if not user-configurable.

## Implementation plan

1. Add characterization tests for redaction and structured formatter output.
2. Remove application `RotatingFileHandler` from multi-process files; choose a single-writer or OS/platform rotation mechanism.
3. Repair the archive job without process-substitution paths that disappear in cron; make failure exit non-zero and alert once.
4. Set explicit maximum retention and compression for every stream.
5. Move Nova Poshta informational per-order output to bounded structured summaries.
6. Add rotation smoke test: force threshold concurrently, prove no rename race/data loss and verify retention count.
7. Add external monitoring for file growth, quota percentage and last successful rotation.

## Acceptance criteria

- Rotation completes successfully for seven consecutive scheduled runs.
- Nova/Django files remain within documented bounds; no `/dev/fd` or rename exception recurs.
- One production request can be joined across access, Django and Telegram by request/incident ID.
- Logs identify host and build, and no concurrent traces are ambiguous.
- Retention and backups pass the secret/PII scan in PROD-012.
