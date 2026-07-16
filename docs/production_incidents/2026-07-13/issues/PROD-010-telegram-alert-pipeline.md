# PROD-010 — Telegram alert pipeline duplicates, truncates and suppresses signal

**Priority:** P1
**State:** confirmed/open
**Owner:** observability/application

## User-visible behavior

The screenshots show a full `Internal Server Error` traceback, a second `Incident XXXXXXXX` alert for the same request, then a global rate-limit warning. The traceback is cut before the final MySQL exception, which is the most useful line.

## Root problems

### Duplicate ERROR per exception

Django logs `Internal Server Error` with traceback. `twocomms/error_views.py` logs another ERROR with the incident code. Both use the `django.request` Telegram handler. One request therefore consumes two of five slots.

### Global lossy limiter

`twocomms/twocomms/log_handlers.py:62-81` uses one cache key for all hosts/routes/exceptions. A scanner-induced 500 can suppress checkout or DB incidents. `cache.get` plus `cache.set` is non-atomic, the effective backend is file based, and cache failure is fail-open flood behavior. Suppressed events are not summarized by fingerprint/count.

### Wrong traceback slice

Lines 83-85 keep the first 1,000 characters. Python tracebacks put final exception type/message at the end, so screenshots stop inside Django/PyMySQL frames before `1040 Too many connections`.

### Unreliable delivery

Lines 92-103 launch a daemon thread. Worker recycle/SIGTERM/SIGKILL can terminate before delivery. Failures are swallowed and there is no sent/failed/suppressed metric. `TelegramNotifier` defaults to HTML while raw exception/path content is not escaped.

## Target design

One request produces one incident object:

- stable random incident/request ID;
- environment, revision, timestamp, host, method and normalized route;
- exception class/message, last project frame and fingerprint;
- severity/customer impact;
- full traceback in structured storage;
- one concise Telegram card with a pointer to the full event.

Limiter keys should include environment, host, severity and exception fingerprint. Use an atomic counter in a concurrency-safe backend, reserve capacity for P0 routes, and emit a periodic digest with suppressed counts and first/last time.

## Implementation plan

1. Write tests proving one exception -> one incident -> one Telegram send.
2. Stop logging a second ERROR from `handler500`; attach the incident code to the original record/request context.
3. Format Telegram from exception tail and project frame rather than raw first 1,000 chars.
4. Escape HTML or use plain text.
5. Replace daemon fire-and-forget with a bounded reliable outbox/worker or a host-supported delivery mechanism.
6. Instrument sent, failed, retried and suppressed totals.
7. Exclude expected 404/security probes from ERROR alerting after PROD-004/008.

## Acceptance criteria

- One synthetic backend exception generates one Telegram card and one full structured traceback.
- Ten repeated same-fingerprint events produce one initial card plus a digest, without hiding a different P0 exception.
- Telegram shows final root cause and project frame.
- Delivery survives a graceful worker recycle and exposes failures.
- No token, PII or raw OAuth/webhook data enters alert content; see PROD-012.
