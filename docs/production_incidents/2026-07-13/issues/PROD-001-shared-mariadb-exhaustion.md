# PROD-001 — Shared MariaDB global connection exhaustion

**Priority:** P0
**State:** confirmed and open
**Owner:** hosting support for root cause; application team for load reduction and graceful degradation
**Fixability:** not fully fixable from this shared-hosting account

## Symptom and impact

Production intermittently cannot open a new database connection. Requests to catalog, products, analytics middleware and management endpoints then fail before or during their first ORM query. The screenshot tracebacks for `/catalog/`, `/en/catalog/`, and `/bot/api/status/` end in:

```text
django.db.utils.OperationalError: (1040, 'Too many connections')
```

This is not a catalog-specific query error. It is a failure to obtain a connection from MariaDB.

## Production evidence

- Effective server: MariaDB `10.6.27-MariaDB-cll-lve`, Django 5.2.11, PyMySQL 1.1.2.
- `max_connections=150`; `Max_used_connections=151` proves the global ceiling was reached.
- `max_user_connections=40`, but no retained error `1203` was found. All capacity events are global `1040`.
- At sampling time: `Connection_errors_max_connections=1007`, `Aborted_connects>9,000`, `Threads_connected` 74–118 and `Threads_running` 13–19.
- The application user could see only one or two of its own sessions. Global `Connections` rose by 434 in 25 seconds, while the site access log contained about 34 requests over a comparable 26-second window. This strongly indicates other shared-host tenants contributed most new connections in that sample; only the host can prove attribution.
- Retained final `OperationalError` lines: 1,580 total — 1,395 code 2006/reset, 133 code 1040, and 52 code 2003/name-resolution or refused-connect variants.
- MariaDB uptime sampled on 13 July implies a restart around 10 July 22:11:13. `/bot/api/status/` failed at 22:10:57, 16 seconds earlier.

### Screenshot correlation

The following 13 July events are directly correlated with complete 1040 tracebacks:

| Incident | Route | Time EEST |
|---|---|---:|
| `2E239FFD` | `/en/catalog/` | 10:12:32 |
| `17EB48B7` | `/catalog/` | 10:12:37 |
| `95B0171D` | `/catalog/` | 12:00:11 |
| `2FC8ABDF` | `/en/catalog/` | 12:00:27 |
| `156C72EF` | `/en/catalog/` | 12:04:17 |
| `87D33BF4` | `/catalog/` | 15:06:41 |

`/bot/api/status/` failed with 1040 at 14:45:32 and 15:45:51. Authentication/session resolution can touch the DB before the status view body, so improving that query alone cannot remove these incidents.

## Effective application configuration

`twocomms/twocomms/production_settings.py` currently produces:

- `CONN_MAX_AGE=0`;
- `CONN_HEALTH_CHECKS=True`;
- connect/read/write timeouts 10/30/30 seconds;
- local MariaDB endpoint.

`CONN_MAX_AGE=0` is the safer setting under a tight shared pool. Increasing it would keep more slots occupied across LSAPI workers. Unlimited retry on 1040 would create a thundering herd.

## Root-cause chain

1. Shared MariaDB reaches its global 150-session ceiling or restarts/resets sockets.
2. Django attempts a fresh connection because persistent connections are disabled.
3. MariaDB rejects or resets the connect before an application query can run.
4. Django emits 500; error rendering or authentication may try the DB again and amplify the failure.
5. GPTBot catalog permutations and request-triggered background work add avoidable local load, but do not explain the much higher global connection rate.

## Implementation plan

### Hosting work

1. Open a support ticket with the time windows and safe counters from the master plan.
2. Request MariaDB error/restart/OOM logs, per-account connection counts, processlist/query digests and slow/lock evidence.
3. Ask whether this DB is shared and what isolation or guaranteed-pool tier is available.
4. Request an explanation for code 2003 name-resolution failures against `localhost` and connection-refused/reset intervals.

### Application mitigation

1. Complete PROD-002 first to eliminate the active crawler amplifier.
2. Make error/404/health responses DB-independent; see PROD-004, PROD-005 and PROD-019.
3. Add metrics for connect failures by MySQL code, host, route, worker and build.
4. Consider a short circuit breaker around dependency health. If a retry is used, allow at most one jittered retry for idempotent GETs; never retry writes blindly.
5. Evaluate a stale/pre-rendered catalog fallback only after canonicalization and with an explicit freshness contract.

## Do not do

- Do not raise `CONN_MAX_AGE` as a speculative fix.
- Do not simply increase LSAPI workers; more simultaneous workers can exhaust DB capacity faster.
- Do not add broad `except Exception` that converts all DB failures to 200.
- Do not claim another tenant is responsible until the host supplies attribution.

## Verification

- A controlled DB outage returns intentional 503/404 responses, not recursive 500s.
- Seven days show zero 1040/reset/refused events under representative traffic.
- Host evidence explains the restart and identifies or eliminates the source of global saturation.
- DB metrics and access events can be joined using timestamp, host, request ID and build.

## Dependencies

PROD-002, PROD-003, PROD-005, PROD-011, PROD-014 and PROD-019.
