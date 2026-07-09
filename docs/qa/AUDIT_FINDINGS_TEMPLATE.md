# Audit Findings — TwoComms Main Site

**Date:** YYYY-MM-DD  
**Checklist version:** `PRE_ADS_MASTER_AUDIT_CHECKLIST.md` **v2**  
**Auditor (Pass A/B):**  
**Confirmer (Pass C):**  
**Environment:** production `https://twocomms.shop` (not local)  
**Scope:** main site only  

## Security

- Do **not** paste SSH/DB/API tokens, full cookies, or `.env`.
- Redact as `***REDACTED***`.

---

## Executive summary

**Ads launch gate:** BLOCKED / CONDITIONAL / CLEAR  

**One paragraph:**  
…

### Counts

| Severity | Open | Confirmed | False positive | Fixed |
|----------|------|-----------|----------------|-------|
| P0 | 0 | 0 | 0 | 0 |
| P1 | 0 | 0 | 0 | 0 |
| P2 | 0 | 0 | 0 | 0 |
| P3 | 0 | 0 | 0 | 0 |

---

## Funnel snapshot (CRO) — fill from Dispatcher / DB

**Period:** week / month · **as of:** YYYY-MM-DD

| Stage | Count | Rate vs sessions | Notes |
|-------|------:|------------------|-------|
| UTM / all sessions | | 100% | |
| product_view (unique sessions) | | % | |
| add_to_cart | | % | |
| initiate_checkout | | % | |
| lead | | % | |
| purchase | | % | |

### Drop-off (worst steps)

| From → To | Drop % | Hypothesis | Checklist IDs |
|-----------|--------|------------|---------------|
| land → product_view | | | CRO-027 |
| product_view → ATC | | | CRO-026 |
| ATC → IC | | | CRO-023 |
| IC → purchase | | | CRO-024 |

### Paid vs organic (if available)

| Segment | Sessions | ATC rate | Purchase rate |
|---------|----------|----------|---------------|
| instagram / facebook paid | | | |
| organic / direct | | | |
| other | | | |

### Top campaigns / creatives (Dispatcher)

| utm_campaign / content | Sessions | ATC | Purchases | Issue? |
|------------------------|----------|-----|-----------|--------|
| | | | | |

---

## SEO batch results

| Batch | Total | OK | Fail | Notes |
|-------|------:|---:|-----:|-------|
| Sitemap locs HEAD | | | | |
| Active categories A–K | | | | |
| Published products titles | | | | |
| Recommended product links | | | | |
| RU/EN leak sample pages | | | | |

Empty `seo_title` count (prod): …  
Duplicate titles count: …  
Contacts 500? (all locales): …

---

## UTM / Pixel canary

| Step | Result | Evidence (redacted) |
|------|--------|---------------------|
| Land with UTM+fbclid | | |
| UTMSession row | | |
| product_view ×2 | | |
| add_to_cart | | |
| Pixel ATC | | |
| Order.utm_* | | |
| Purchase/Lead + event_id | | |
| Dispatcher sees campaign | | |
| Success reload no double Purchase | | |

Orders 30d empty `utm_source` %: …  
`is_converted` vs paid orders: …

---

## Finding entry format

### F-XXX — short title

| Field | Value |
|-------|--------|
| Severity | P0 / P1 / P2 / P3 |
| Area | SEO \| GEO \| CRO \| CART \| UTM \| PIXEL \| TECH \| FEED \| ADS \| OTHER |
| Checklist ID | e.g. SEO-004, CRO-023, CART-006 |
| Status (B) | SUSPECTED / REPRODUCED |
| Status (C) | CONFIRMED / FALSE_POSITIVE / NEEDS_MORE_DATA |
| URL(s) | https://twocomms.shop/… |
| Repro | 1. … 2. … |
| Expected | |
| Actual | |
| Evidence | HTTP / DB count / log **class** / screenshot path — no secrets |
| Business impact | ads waste / index / conversion / trust |
| Fix direction | observation only until Pass C |
| Risk of fix | low / med / high |
| Owner | |

---

## Findings

### F-001 — (delete placeholder)

| Field | Value |
|-------|--------|
| Severity | |
| Area | |
| Checklist ID | |
| Status (B) | |
| Status (C) | |
| URL(s) | |
| Repro | |
| Expected | |
| Actual | |
| Evidence | |
| Business impact | |
| Fix direction | |
| Risk of fix | |
| Owner | |

---

## Pass A coverage

| Block | Done % | Notes |
|-------|--------|-------|
| 0 Smoke / SEC | | |
| 1 Page inventory PG-* | | |
| 2 SEO deep | | |
| 3 GEO | | |
| 4 CRO funnel | | |
| 5 CART | | |
| 6 UTM / Dispatcher | | |
| 7 PIX | | |
| 8 TECH / alerts | | |
| 9 FEED | | |
| 10 DB | | |
| 11 ADS | | |
| 12 DEV browsers | | |

---

## Suspicious / needs re-check (not yet findings)

| Item | Why suspicious | Next check |
|------|----------------|------------|
| | | |

---

## Tech debt / refactor smells (observe only)

| Smell | Path | Risk if touched | Priority |
|-------|------|-----------------|----------|
| | | | |

---

## Telegram / alert noise log

| Alert type | Frequency | Real outage? | Action |
|------------|-----------|--------------|--------|
| | | | |

---

## False positives / intentional

| ID | Why OK |
|----|--------|
| | |

---

## Follow-ups after Pass C only

- [ ] …

---

## Sign-off

| Role | Name | Date |
|------|------|------|
| Pass B | | |
| Pass C | | |
| Ads gate owner | | |
