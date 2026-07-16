# Production Error Remediation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` and execute one linked issue at a time. Re-read the issue file before changing code. After every slice: run its tests, inspect the narrow diff, deploy only when explicitly authorized, and verify against production evidence.

**Audit date:** 2026-07-13
**Evidence cutoff:** approximately 16:15 Europe/Kiev
**Production revision at cutoff:** `7dfd0597a410f0759b1d707674ad18c4334cd06d`
**Mode of this run:** read-only production investigation plus local documentation. No application fix, configuration change, service restart, migration, log deletion, Git pull, commit, push, or deploy was performed.

## Goal

Remove the root causes behind recurring 500/502/503 responses and Telegram noise, while preserving enough evidence and implementation detail for another agent to continue safely.

## Scope and evidence

The audit covered all retained production sources available to the hosting account:

- Apache access logs from 2026-05-31 22:03 through 2026-07-13 about 16:05 EEST;
- `stderr.log` and five rotations, about 39 MB, covering approximately late June through 13 July;
- `django.log` and rotations, the 136 MB pre-rotation backup, `client_errors.log`, `rum.log`, and the Nova Poshta cron log;
- the four Telegram screenshots supplied for 13 July;
- effective production settings, current server source, process state, MariaDB variables visible to the account, cache contents, and local source/history at the same revision.

Across the accessible HTTP logs there are **3,481 5xx responses**: 3,153×500, 51×502, and 277×503. They span 1,523 paths, so a raw path count is misleading: several independent root causes produce the same generic 500.

The retained Python exception totals are also not equivalent to distinct user incidents. Each wrapped PyMySQL error is normally logged twice, multiple workers interleave in one file, and some application errors are caught after being logged. Counts below use the final Django exception line when possible.

## Executive conclusion

There is no single “catalog bug.” The production failures form five interacting layers:

1. The shared MariaDB server reaches its global 150-connection ceiling and also resets/restarts connections. This is the direct cause of the catalog and bot-status tracebacks in the screenshots.
2. GPTBot discovered a combinatorial color-filter graph and crawled almost every ordered permutation. It generated 80–90% of daily traffic, invalidated the file cache, and amplified local DB/process load.
3. LiteSpeed/LSAPI repeatedly saturates its child limit and kills workers. Hosting-only LVE evidence is required to separate memory, entry-process, CPU, and deploy restarts.
4. Ordinary 404s on `management` and `fin` fail while rendering the shared storefront template, turning harmless browser/scanner probes into bare 500 alerts. A second DB-backed redirect middleware can turn any non-DTF 404 into 500 during a DB outage.
5. The observability path duplicates, truncates, suppresses, and interleaves errors. It also leaked a Telegram credential and personal data to retained logs, so security containment precedes ordinary cleanup.

## Confidence legend

- **Confirmed:** reproduced or tied to a complete production traceback/access event.
- **Strong inference:** production logs omit `Host` or timestamps, but behavior was reproduced and alternatives were checked.
- **Needs hosting:** the application account cannot access the authoritative server/LVE/MariaDB evidence.
- **Historical/resolved:** present in retained logs but absent after a known deployed change; keep a recurrence test.

## Screenshot-to-root-cause map

All times are Europe/Kiev.

| Screenshot event | What was established | Root issue |
|---|---|---|
| `/catalog/` and `/en/catalog/`, 10:12, 12:00, 12:04, 15:06 | Every mapped incident ends in MySQL `1040 Too many connections`; every catalog 500 on 13 July was requested by GPTBot | [PROD-001](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md), [PROD-002](../production_incidents/2026-07-13/issues/PROD-002-gptbot-catalog-crawl-trap.md) |
| `/bot/api/status/`, 14:45 and 15:45 | MySQL 1040, often before the view body while resolving the authenticated user | [PROD-001](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md), [PROD-019](../production_incidents/2026-07-13/issues/PROD-019-bot-status-outage-resilience.md) |
| `/bot/api/status/`, 14:41 | 503 during an in-place Fable5 deploy/mixed worker state | [PROD-009](../production_incidents/2026-07-13/issues/PROD-009-non-atomic-deploy-mixed-code.md) |
| `/robots.txt`, WP/GravitySMTP probes, `/.env` | External scanner/crawler traffic. The site bug is that expected cheap 403/404 responses can become 500 | [PROD-004](../production_incidents/2026-07-13/issues/PROD-004-subdomain-404-becomes-500.md), [PROD-005](../production_incidents/2026-07-13/issues/PROD-005-db-dependent-404-redirects.md), [PROD-008](../production_incidents/2026-07-13/issues/PROD-008-scanner-probes-edge-filtering.md) |
| missing category icon, 13:04 | Referer identifies management; the requested optimized file is absent and two templates hardcode its stale name | [PROD-006](../production_incidents/2026-07-13/issues/PROD-006-stale-category-icon.md) |
| favicon and Apple icons, 13:30 | Standard browser fallback requests; subdomains do not expose uniform aliases, and the burst coincided with worker termination | [PROD-003](../production_incidents/2026-07-13/issues/PROD-003-lsapi-worker-saturation.md), [PROD-007](../production_incidents/2026-07-13/issues/PROD-007-cross-subdomain-standard-assets.md) |
| paired full traceback plus `Incident XXXXXXXX` | One exception is logged twice at ERROR level; both consume a shared 5/10-minute Telegram budget | [PROD-010](../production_incidents/2026-07-13/issues/PROD-010-telegram-alert-pipeline.md) |

The access log mixes hosts and does not record `Host`. Therefore, the exact subdomain for a bare path-only alert is sometimes a strong inference rather than a direct access-log fact. Do not correlate a bare stderr line with the adjacent traceback: multiple processes write concurrently.

## Confirmed incident correlation for 13 July

| Incident | Route | Time | Final cause |
|---|---|---:|---|
| `2E239FFD` | `/en/catalog/` | 10:12:32 | MySQL 1040 |
| `17EB48B7` | `/catalog/` | 10:12:37 | MySQL 1040 |
| `95B0171D` | `/catalog/` | 12:00:11 | MySQL 1040 |
| `2FC8ABDF` | `/en/catalog/` | 12:00:27 | MySQL 1040 |
| `156C72EF` | `/en/catalog/` | 12:04:17 | MySQL 1040 |
| `87D33BF4` | `/catalog/` | 15:06:41 | MySQL 1040 |

## Master remediation checklist

Checkboxes below describe remediation, not this completed investigation. Each root problem has its own implementation file.
`[x]` is production-verified, `[o]` is partially fixed with a named residual,
and `[ ]` remains open.

### Phase 0 — contain security exposure

- [ ] **P0** Rotate the exposed Telegram bot credential, re-register dependent webhooks, and invalidate the old credential. Do not paste either value into a ticket or commit. [PROD-012](../production_incidents/2026-07-13/issues/PROD-012-secret-and-pii-leakage.md)
- [ ] **P0** Securely remove/redact retained credential and contact payloads after preserving a non-sensitive incident record. Include large backups and cron logs. [PROD-012](../production_incidents/2026-07-13/issues/PROD-012-secret-and-pii-leakage.md)
- [ ] **P0** Set a webhook secret in production, call Telegram `setWebhook` with `secret_token`, then enforce fail-closed validation. [PROD-013](../production_incidents/2026-07-13/issues/PROD-013-telegram-webhook-authentication.md)

### Phase 1 — stop the active load amplifier and false 500s

- [x] **P0** Canonicalize color filters as a deduplicated stable set and redirect non-canonical orderings. **FIXED `20079875`:** production redirects noisy/unknown filters to one sorted URL and resets pagination only when filter identity changes. [PROD-002](../production_incidents/2026-07-13/issues/PROD-002-gptbot-catalog-crawl-trap.md)
- [x] **P0** Generate cache keys from canonical filters; do not hash raw query order. **FIXED `20079875`:** repeated, duplicate and reordered color parameters share one semantic anonymous-cache identity. [PROD-002](../production_incidents/2026-07-13/issues/PROD-002-gptbot-catalog-crawl-trap.md)
- [x] **P0** Make multi-select permutations non-crawlable and add an immediate edge/robots/rate control for GPTBot query variants. **FIXED `20079875`:** multi-select is `noindex, nofollow`, facet links are `nofollow`, and the live GPTBot block disallows catalog `color` query variants. [PROD-002](../production_incidents/2026-07-13/issues/PROD-002-gptbot-catalog-crawl-trap.md)
- [x] **P0** Give `management` and `fin` autonomous, DB-free 404/500 handlers and regression tests across every host. **FIXED `013f6f9d`**; server error/middleware suite 5/5. [PROD-004](../production_incidents/2026-07-13/issues/PROD-004-subdomain-404-becomes-500.md)
- [x] **P1** Make redirect fallback fail open on DB errors and skip hosts/paths that do not need DB redirects. **FIXED `59d1ad1f`**; routed subdomains skip DB and DB errors preserve 404. [PROD-005](../production_incidents/2026-07-13/issues/PROD-005-db-dependent-404-redirects.md)
- [ ] **P2** Reject `.env`, WordPress and backup probes before Passenger, consistently for every vhost. [PROD-008](../production_incidents/2026-07-13/issues/PROD-008-scanner-probes-edge-filtering.md)

### Phase 2 — hosting escalation in parallel

- [ ] **P0 / hosting** Open a ticket for global MariaDB exhaustion, resets and the 10 July restart using the evidence packet in PROD-001. [PROD-001](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md)
- [ ] **P0 / hosting** Request MariaDB per-tenant connection attribution and an isolated/guaranteed database option. [PROD-001](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md)
- [ ] **P1 / hosting** Request CloudLinux LVE and LiteSpeed reasons for child-limit events, SIGTERM/SIGKILL/SIGABRT and LSAPI file errors. [PROD-003](../production_incidents/2026-07-13/issues/PROD-003-lsapi-worker-saturation.md)
- [ ] **Guardrail** Do not raise `CONN_MAX_AGE`, add unlimited DB retries, or merely increase worker count before the DB and LVE constraints are known. [PROD-001](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md)

### Phase 3 — make deploys and infrastructure failure-safe

- [ ] **P1** Replace live `git pull` over running workers with a preflighted controlled restart or atomic release switch. [PROD-009](../production_incidents/2026-07-13/issues/PROD-009-non-atomic-deploy-mixed-code.md)
- [ ] **P1** Add deploy gates for migrations, imports, `check --deploy`, static collection/compression and worker readiness. [PROD-009](../production_incidents/2026-07-13/issues/PROD-009-non-atomic-deploy-mixed-code.md)
- [ ] **P1** Move Nova Poshta scheduling out of request middleware; bound eligible shipments and quarantine invalid TTNs. [PROD-014](../production_incidents/2026-07-13/issues/PROD-014-nova-poshta-fallback-and-upstream.md)
- [ ] **P1** Add timeout/cache fallback for the city lookup paths that produced all 51 observed 502s. [PROD-014](../production_incidents/2026-07-13/issues/PROD-014-nova-poshta-fallback-and-upstream.md)
- [ ] **P1** Make `/bot/api/status/` return a controlled JSON 503 with client backoff during dependency outages. [PROD-019](../production_incidents/2026-07-13/issues/PROD-019-bot-status-outage-resilience.md)
- [o] **P2** Serve robots, favicon, Apple aliases and ordinary static/media responses outside Django and consistently across hosts. **PARTIAL `169e6032`:** main favicon is direct 200; Apple aliases, every subdomain and edge/static ownership remain open. [PROD-007](../production_incidents/2026-07-13/issues/PROD-007-cross-subdomain-standard-assets.md)
- [ ] **P1** Replace the stale ignored-media category icon with a tracked or model-derived asset plus fallback. [PROD-006](../production_incidents/2026-07-13/issues/PROD-006-stale-category-icon.md)

### Phase 4 — fix application data errors

- [x] **P1** Align `UserProfile.pay_type` choices and column length with canonical payment values; migrate existing data safely. **FIXED `ed4aeb8d`**; migration `accounts.0028`, production inventory and 130/130 server tests. [PROD-015](../production_incidents/2026-07-13/issues/PROD-015-userprofile-pay-type-overflow.md)
- [x] **P1** Validate and sanitize all cart session identifiers before bulk ORM calls; drop only malformed items. **FIXED `ed4aeb8d` + test portability `569626c3`**; 130/130 server tests and live cart smoke. [PROD-016](../production_incidents/2026-07-13/issues/PROD-016-malformed-cart-session.md)
- [ ] **P2** Fix the confirmed `isStatsTab` JavaScript reference and add timestamps/build IDs to client-error telemetry. [PROD-017](../production_incidents/2026-07-13/issues/PROD-017-client-javascript-errors.md)
- [ ] **P2** Correlate Google `invalid_grant` callbacks without logging authorization codes; handle replay/expiry as an expected login failure. [PROD-018](../production_incidents/2026-07-13/issues/PROD-018-google-oauth-invalid-grant.md)

### Phase 5 — rebuild observability

- [ ] **P1** Emit one Telegram event per incident, keyed by fingerprint/host/severity, with a digest of suppressed events. [PROD-010](../production_incidents/2026-07-13/issues/PROD-010-telegram-alert-pipeline.md)
- [ ] **P1** Put the exception type/message and last project frame in Telegram instead of the first 1,000 traceback characters. [PROD-010](../production_incidents/2026-07-13/issues/PROD-010-telegram-alert-pipeline.md)
- [ ] **P0 ops** Repair the failing archive cron and safely reduce the 867 MB Nova Poshta log and 136 MB Django backup. [PROD-011](../production_incidents/2026-07-13/issues/PROD-011-logging-rotation-and-context.md)
- [ ] **P1** Replace multi-process `RotatingFileHandler` with a single-writer/platform rotation strategy. [PROD-011](../production_incidents/2026-07-13/issues/PROD-011-logging-rotation-and-context.md)
- [ ] **P1** Add structured timestamp, timezone, host, request ID, process, status, exception and build revision to every production event. [PROD-011](../production_incidents/2026-07-13/issues/PROD-011-logging-rotation-and-context.md)
- [ ] **P2** Add timestamp/build/navigation context to RUM and establish budgets for the severe latency outliers. [PROD-021](../production_incidents/2026-07-13/issues/PROD-021-rum-latency-outliers.md)

### Phase 6 — preserve resolved fixes

- [ ] Keep a deploy smoke test that renders compressed pages and verifies the offline manifest after every release. [PROD-020](../production_incidents/2026-07-13/issues/PROD-020-compressor-manifest-drift-resolved.md)
- [ ] Keep the 8 July Nova Poshta DB connection-boundary regression test; do not mistake historical `InterfaceError` counts for current behavior. [PROD-014](../production_incidents/2026-07-13/issues/PROD-014-nova-poshta-fallback-and-upstream.md)

## Root-cause issue index

| ID | Priority | State at cutoff | Ownership | Issue |
|---|---:|---|---|---|
| PROD-001 | P0 | Confirmed/open | Hosting + app mitigation | [Shared MariaDB global connection exhaustion](../production_incidents/2026-07-13/issues/PROD-001-shared-mariadb-exhaustion.md) |
| PROD-002 | P0 | Partial `20079875`; app/robots fixed, traffic monitoring and edge rate control open | Application + edge | [GPTBot catalog crawl trap and file-cache churn](../production_incidents/2026-07-13/issues/PROD-002-gptbot-catalog-crawl-trap.md) |
| PROD-003 | P1 | Confirmed/open; cause needs hosting | Hosting + capacity | [LSAPI worker saturation and forced termination](../production_incidents/2026-07-13/issues/PROD-003-lsapi-worker-saturation.md) |
| PROD-004 | P0 | Fixed `013f6f9d` | Application | [Subdomain 404 becomes 500](../production_incidents/2026-07-13/issues/PROD-004-subdomain-404-becomes-500.md) |
| PROD-005 | P1 | Fixed `59d1ad1f` | Application | [404 redirect fallback depends on DB](../production_incidents/2026-07-13/issues/PROD-005-db-dependent-404-redirects.md) |
| PROD-006 | P1 | Confirmed/open | Application/content | [Stale category icon reference](../production_incidents/2026-07-13/issues/PROD-006-stale-category-icon.md) |
| PROD-007 | P2 | Partial `169e6032`; edge/subdomains open | Web server + application | [Cross-subdomain standard assets](../production_incidents/2026-07-13/issues/PROD-007-cross-subdomain-standard-assets.md) |
| PROD-008 | P2 | Confirmed hardening gap | Web server/hosting | [Scanner probes reach Passenger](../production_incidents/2026-07-13/issues/PROD-008-scanner-probes-edge-filtering.md) |
| PROD-009 | P1 | Confirmed/open deploy risk | Operations | [Non-atomic deploy creates mixed code](../production_incidents/2026-07-13/issues/PROD-009-non-atomic-deploy-mixed-code.md) |
| PROD-010 | P1 | Confirmed/open | Application/observability | [Telegram alert pipeline loses signal](../production_incidents/2026-07-13/issues/PROD-010-telegram-alert-pipeline.md) |
| PROD-011 | P0 ops | Confirmed/open | Operations/hosting | [Logging context and rotation failure](../production_incidents/2026-07-13/issues/PROD-011-logging-rotation-and-context.md) |
| PROD-012 | P0 security | Confirmed/open | Security/operations | [Secrets and PII leak into logs](../production_incidents/2026-07-13/issues/PROD-012-secret-and-pii-leakage.md) |
| PROD-013 | P0 security | Confirmed/open | Security/operations | [Telegram webhook is unauthenticated](../production_incidents/2026-07-13/issues/PROD-013-telegram-webhook-authentication.md) |
| PROD-014 | P1 | Partially fixed/open | Application/operations | [Nova Poshta fallback and upstream failures](../production_incidents/2026-07-13/issues/PROD-014-nova-poshta-fallback-and-upstream.md) |
| PROD-015 | P1 | Fixed `ed4aeb8d` | Application/data | [UserProfile payment value overflows DB](../production_incidents/2026-07-13/issues/PROD-015-userprofile-pay-type-overflow.md) |
| PROD-016 | P1 | Fixed `ed4aeb8d` | Application | [Malformed cart session causes 500](../production_incidents/2026-07-13/issues/PROD-016-malformed-cart-session.md) |
| PROD-017 | P2 | Mixed open/resolved/noise | Frontend/observability | [Client JavaScript errors](../production_incidents/2026-07-13/issues/PROD-017-client-javascript-errors.md) |
| PROD-018 | P2 | Needs correlation | Authentication | [Google OAuth invalid_grant](../production_incidents/2026-07-13/issues/PROD-018-google-oauth-invalid-grant.md) |
| PROD-019 | P1 | Confirmed/open resilience gap | Management app | [Bot status polling during outages](../production_incidents/2026-07-13/issues/PROD-019-bot-status-outage-resilience.md) |
| PROD-020 | Monitor | Historical/resolved | Deployment | [Compressor manifest drift](../production_incidents/2026-07-13/issues/PROD-020-compressor-manifest-drift-resolved.md) |
| PROD-021 | P2 | Confirmed measurement gap | Performance/observability | [RUM latency outliers](../production_incidents/2026-07-13/issues/PROD-021-rum-latency-outliers.md) |

## Hosting support evidence packet

Do not include application secrets, SSH credentials, customer identifiers, raw webhook payloads, or authorization codes in the ticket.

Request investigation for these windows:

- 2026-07-10 22:05–22:20 EEST;
- 2026-07-13 10:10–10:15;
- 2026-07-13 11:58–12:13;
- 2026-07-13 13:03–13:31;
- 2026-07-13 14:40–14:46;
- 2026-07-13 15:05–15:07;
- 2026-07-13 15:44–15:46.

Safe facts to include:

- MariaDB `max_connections=150`, `max_user_connections=40`, `Max_used_connections=151`;
- `Connection_errors_max_connections=1007`, more than 9,000 aborted connects at sampling time;
- observed error is global `1040`; no retained `1203` per-user-limit error;
- global connection creation increased by about 434 in 25 seconds while the site received about 34 requests in 26 seconds;
- MariaDB uptime indicates a restart around 2026-07-10 22:11:13, with a site DB failure 16 seconds earlier;
- 22,242 max-child-limit messages, 243 SIGTERM, 48 SIGKILL, four SIGABRT and 15 LSAPI file errors in retained stderr.

Ask the host for:

1. MariaDB error, restart and OOM logs for the windows above.
2. Time series for `Threads_connected`, `Threads_running`, `Max_used_connections`, refused/reset connects and connection count by hosting account.
3. Top tenants, processlist snapshots, sleep age, query digests, slow queries and lock waits.
4. Confirmation that the database is shared and the options for isolated MariaDB or a guaranteed per-account pool.
5. CloudLinux EP/NPROC/PMEM/CPU/IO faults and LVE graphs for this account.
6. LiteSpeed/LSWSGI reason codes for the SIGTERM/SIGKILL/SIGABRT events and max-child saturation.
7. Whether access logging can include the request `Host` and whether static/scanner rules can be enforced consistently for every vhost.

## Global completion criteria

The remediation program is complete only when all of the following hold for at least seven days of representative traffic:

- no unexplained 500/502/503 cluster and no MySQL 1040/reset event;
- catalog filter URLs are canonical and crawl volume remains bounded;
- unknown URLs on every host return deliberate 403/404 without DB access or Telegram ERROR;
- one backend exception produces one correlated incident with a complete structured record;
- no credential, phone, email, chat/user identifier, OAuth code, or raw webhook payload appears in current or rotated logs;
- deploy smoke tests prove model registry, migrations, static/compressor assets and worker readiness before traffic switches;
- log rotation succeeds, retention is bounded, and no multi-process rename exception occurs;
- Nova Poshta jobs run from a scheduler, not user requests, and city lookup failures degrade predictably;
- support either identifies and resolves the shared DB/LVE constraints or the service is migrated to an isolated capacity tier.
