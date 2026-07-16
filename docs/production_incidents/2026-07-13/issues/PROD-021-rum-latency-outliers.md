# PROD-021 — RUM shows severe latency outliers but lacks time/build context

**Priority:** P2 after stability P0/P1 work
**State:** measurement confirms user impact; root attribution incomplete
**Owner:** performance/observability

## Evidence

`rum.log` contains 6,656 records. Examples include:

- admin panel FCP/LCP outliers up to hundreds of seconds and TTFB around 9.4 seconds;
- catalog TTFB around 1.7–1.9 seconds in sampled records;
- mobile custom-print FCP around 5.8 seconds, TTFB around 3.1 seconds and INP around 3.55 seconds;
- bot/in-app catalog LCP around 5.1 seconds and INP around 952 ms.

These measurements are compatible with DB/worker saturation and heavy third-party/client work, but RUM records have no timestamp, revision or sampling metadata. They cannot be correlated with exact 1040/LSAPI windows.

## Implementation plan

1. Add timestamp, revision, host, normalized route, navigation type, device class, connection class and anonymous sample ID.
2. Strip query strings/identifiers and apply bounded sampling/retention.
3. Record server timing/request ID so RUM can join to a safe backend trace.
4. Define route/device budgets for TTFB, LCP, INP and error rate.
5. Build before/after views for PROD-001/002/003/014 changes.
6. Investigate admin/custom-print frontend work only after outage periods are separable.

## Acceptance criteria

- Every RUM record is time/build attributable and privacy-safe.
- Dashboards distinguish backend TTFB from client render/interaction delay.
- P75 and severe-tail budgets are measured by route/device for seven days.
- Stability remediations demonstrate quantified user-impact improvement.
