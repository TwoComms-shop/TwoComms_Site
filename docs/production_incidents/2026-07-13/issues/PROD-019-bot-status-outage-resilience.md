# PROD-019 — Bot status polling amplifies dependency outages

**Priority:** P1 resilience
**State:** confirmed/open
**Owner:** management bot UI/API

## Evidence

- `/bot/api/status/`: 34×500 and four 503 across available access logs.
- On 13 July: 503 at 14:41:55, 500 at 14:45:32 and 15:45:51.
- The two 500s end in MySQL 1040. Failure may occur while `login_required` resolves `request.user`, before the view body.
- `management/bot_views.py:319-345` queries `InstagramBotLog` on every status poll.
- 13 July produced thousands of status requests, consistent with an open management page polling repeatedly.

The 14:41 503 overlaps the Fable mixed-deploy event in PROD-009. The later 500s are DB exhaustion from PROD-001.

## Problem

A normal polling client continues requesting during DB/process outages. Every failure produces an expensive traceback/alert and the UI lacks a defined degraded-state/backoff contract.

## Implementation plan

1. Measure current poll interval, concurrent tabs and query count; consolidate duplicate browser timers.
2. Return a small controlled JSON 503 with `Retry-After` for dependency-unavailable cases after authentication can be resolved safely.
3. Implement exponential client backoff with jitter, pause when the tab is hidden/offline and reset after success.
4. Cache the last safe bot-state snapshot briefly; label it stale rather than returning false “healthy”.
5. Separate a DB-independent process liveness endpoint from authenticated business status.
6. Reduce query shape and add indexes only after profiling; the current root is connection acquisition, not proven slow SQL.
7. Suppress repeated same-fingerprint polling failures into one incident/digest.

## Tests

- DB unavailable -> controlled JSON 503, no HTML error page and bounded logging.
- browser uses `Retry-After`/backoff and one timer per page.
- stale snapshot is visibly stale and never interpreted as current success.
- recovery resumes normal interval without reload.
- authentication/permissions remain enforced.

## Acceptance criteria

- Repeated status polling cannot create a new 5xx/Telegram flood during a DB or deploy outage.
- The UI clearly distinguishes current, stale and unavailable state.
- Recovery is automatic, and request/query volume remains within a documented budget.
