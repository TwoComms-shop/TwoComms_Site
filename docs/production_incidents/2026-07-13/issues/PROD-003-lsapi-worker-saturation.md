# PROD-003 — LSAPI worker saturation and forced termination

**Priority:** P1, potentially P0 during active saturation
**State:** saturation confirmed; termination reason needs hosting evidence
**Owner:** hosting/operations with application load reduction

## Symptom and impact

Unrelated dynamic, static-like and API paths receive 500/503 responses in clusters. Retained stderr repeatedly reports every LSAPI child busy and shows workers being terminated. A killed worker can drop an in-flight request and also lose daemon-thread Telegram alerts.

## Evidence

- `Reached max children process limit`: 22,242 retained entries.
- Frequent state: six configured children plus two extras, all eight busy. Later messages show other current/extra counts during reconfiguration/restarts.
- Worker terminations: 243 SIGTERM, 48 SIGKILL and four SIGABRT.
- 15 `OSError: LSAPI: File error` entries plus `start_response() return NULL`/bad gateway messages.
- Current configuration observed `LSAPI_CHILDREN=10`; the historical messages prove effective limits varied or extra-child semantics applied.
- A favicon/Apple-icon burst around 13:30 coincided with SIGTERM/SIGKILL events.
- The account runs inside a CloudLinux LVE, but its PMEM/EP/NPROC/CPU/IO counters are not readable by the SSH user.

## What is and is not proven

Confirmed: processes saturate and are terminated. Not proven: whether SIGKILL came from LVE memory, entry-process limits, LiteSpeed policy, deploy scripts or another host action. Host-wide free memory is not evidence for the account's LVE allowance.

Increasing children is not a safe standalone remedy: current workers consumed roughly 127–212 MB RSS each, and more concurrency can worsen both memory and MariaDB connection pressure.

## Contributors to reduce first

- PROD-002 high-cardinality GPTBot crawl.
- PROD-014 request-triggered Nova Poshta scheduling and log churn.
- PROD-011 file-cache/log filesystem churn.
- expensive error rendering and DB-backed 404s in PROD-004/005.

## Hosting request

Ask for per-minute LVE EP/NPROC/PMEM/CPU/IO faults, LSWSGI queue depth, worker RSS/termination reason, configured hard/soft child limits and 503 reason codes for the incident windows in the master plan.

## Implementation plan

1. Add worker/build/request identifiers and latency to structured logs.
2. Remove known load amplifiers before capacity tuning.
3. Establish a DB connection budget per possible worker and background thread.
4. With hosting evidence, choose one of:
   - right-size child count within memory/DB budgets;
   - upgrade LVE resources;
   - move to an isolated deployment model;
   - put static/media and bot filtering at the edge.
5. Ensure graceful restart drains traffic and alert delivery rather than killing arbitrary workers.
6. Add an external synthetic check that distinguishes queue timeout, app 500 and DB-unavailable 503.

## Verification

- Seven days without max-child saturation, unexpected signal termination or LSAPI file error.
- P95/P99 request queue latency and worker RSS stay within documented budgets.
- A deploy produces only expected graceful termination and zero dropped synthetic requests.
- Capacity changes do not increase DB 1040 frequency.
