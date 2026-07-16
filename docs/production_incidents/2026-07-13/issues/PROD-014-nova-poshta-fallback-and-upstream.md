# PROD-014 — Nova Poshta request-triggered scheduler, stale shipments and upstream 502s

**Priority:** P1
**State:** stale-connection bug fixed; architecture/upstream handling open
**Owner:** orders/operations

## Historical fix that worked

Before 8 July, background threads reused unsafe/stale Django connections. Retained final counts include 4,471 `django.db.utils.InterfaceError (0, '')` and most code-2006 resets inside Nova Poshta service. The 8 July change added `close_old_connections()` at thread boundaries. Current stderr contains zero new InterfaceError, so do not reopen that exact bug without new evidence.

## Remaining problems

### Request middleware is the scheduler

`orders/nova_poshta_middleware.py` checks update age on ordinary non-DTF requests, acquires a cache lock and launches a background thread around every 15 minutes. Under GPTBot traffic, many requests log the same “last update” warning before lock resolution. There is no independent cron for the fallback, so Passenger traffic controls scheduling and worker lifecycle can interrupt it.

### Repeated stale/invalid shipments

Runs repeatedly process about nine old TTNs and often finish `0/9 updated`. Logs contain invalid-phone warnings, deleted/not-found documents, remote disconnects, SSL EOF and read timeouts. Retained summaries include thousands of failed update applications. Per-order INFO/print output created an 867 MB cron/service log.

### City lookup 502 cluster

All 51 observed HTTP 502s belong to:

- `/cart/delivery/cities/`: 33;
- `/ru/cart/delivery/cities/`: 18.

Bursts occurred on 7 June, 30 June and 9 July, consistent with upstream timeout/failure without a sufficient cached fallback.

### Shared DB interaction

Current fallback can still encounter global 1040/reset errors from PROD-001. It is not the sole global connection source, but avoidable threads/API calls add pressure during an outage.

## Implementation plan

1. Move tracking updates to a cron/scheduled management command with one explicit process and overlap lock.
2. Remove scheduling/logging from request middleware after cutover.
3. Define eligible shipments by age and active terminal states; stop polling delivered/deleted/not-found legacy rows indefinitely.
4. Add failure state, attempt count, exponential backoff and quarantine/manual-review reason for invalid TTNs/phones.
5. Use bounded API batches, explicit timeouts and summary metrics; avoid per-request/per-order INFO noise.
6. Close DB connections at job boundaries and before/after external network waits; retain the existing regression protection.
7. For city lookup, cache successful directory data, serve a documented stale fallback during transient upstream failure, and return structured retryable status rather than opaque proxy 502 where possible.

## Verification

- Scheduler runs at defined times without site traffic and never overlaps.
- No request starts a tracking thread.
- Terminal/invalid rows leave the hot polling set.
- Seven days show bounded log volume, zero InterfaceError and measurable upstream success/latency/error categories.
- Simulated Nova Poshta timeout makes city UI degrade/retry predictably without losing checkout state.
