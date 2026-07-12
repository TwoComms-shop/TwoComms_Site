# Audit Findings — TwoComms Main Site

**Date:** 2026-07-09  
**Checklist version:** `PRE_ADS_MASTER_AUDIT_CHECKLIST.md` **v2**  
**Auditor (Pass A/B):** agent-pass-a (production HTTP + HTML + sitemap + feed)  
**Confirmer (Pass C):** _pending_  
**Environment:** production `https://twocomms.shop`  
**Scope:** main site `twocomms.shop` + management IG bot findings (F-095+) + plan re-verify  
**Method:** live curl/python/code review; **no product fixes in audit**; no secrets stored  
**Fix agent entry:** start at `docs/qa/README.md` → **MASTER FIX CHECKLIST** below  

## Security

- No SSH/DB/API tokens in this file.
- Pixel IDs below are already public in HTML (`data-meta-pixel-id`).

---

## Executive summary

**Ads launch gate (current):** **BLOCKED**. Critical: **F-021/F-033/F-071** order UTM empty (first_touch not copied to Order), **F-019** is_converted dead, **F-030** pixel BFCache JS error, **F-029** worker limit, SEO **F-001/F-002/F-004**, **F-044/F-068** session_key gaps. Product feed `g:link` recheck **PASS** (F-077) — earlier F-027 product-path claim narrowed.

**One paragraph:** Core smoke (home, catalog, cart, healthz, robots, sitemap index, UTM first-touch cookies, Meta pixel ID + PageView, cart APIs) **works**. **Attribution chain is broken for ROAS**: capture/canaries OK, but **43/43 orders have empty `utm_source`**, **0 `is_converted`**, and `link_order_to_utm` **does not read `analytics_first_touch_data`** (F-071) — even audit order 276 had UTM in UserAction first_touch and still blank Order UTM. Historical **prepay_200 = 19/19 no session_key** though many have `payment_payload.tracking.external_id=session:…` (F-073). **COD path** lacks `_ensure_session_key` (F-074). SEO: category titles truncated in MySQL, color landings bad UA grammar, RU/EN home H1 still Ukrainian. Pixel BFCache ReferenceError still live. **CheckoutCapture.converted never flips** (0/4). Ads gate remains **BLOCKED**.

### Counts (open findings)

| Severity | Open | Confirmed (C) | False positive | Fixed |
|----------|------|---------------|----------------|-------|
| P0 | 9 | 0 | 0 | 0 |
| P1 | 25 | 0 | 0 | 0 |
| P2 | 14 | 0 | 0 | 0 |
| P3 | 3 | 0 | 0 | 0 |

### Pass A coverage (honest)

| Block | Done % | Notes |
|-------|--------|-------|
| 0 Smoke / SEC | **100%** | all checklist [x] |
| 1 Page inventory | **100%** | all [x]; sitemap 489/489 |
| 2 SEO deep | **100% checked** | fails → F-001..004 |
| 3 GEO | **100% checked** | F-005 open |
| 4 CRO | **100% checked** | F-022 open |
| 5 CART | **100% checked** | F-050; no paid order |
| 6 UTM | **100% checked** | capture OK; **order link F-021** |
| 7 PIX | **100% checked** | F-030; no EM login |
| 8 TECH | **100% checked** | F-029/031 |
| 9 FEED | **100% checked** | F-003 open |
| 10 DB | **100% checked** | done |
| 11 ADS | **100% checked** | **gate BLOCKED** |
| 12 DEV | **100% checked** | lab N/A marked |

---


## MASTER FIX CHECKLIST (for fix agent)

> **This is the single work queue.**  
> - `[ ]` = still open for fix  
> - `[x]` = no fix needed (PASS/INFO) or owner-done  
> - **Detail** column = where the long write-up lives  
> - Full narrative for most F-* is still in sections `### F-xxx` **below in this file**  
> - Plan/IG deep dives are in sibling MDs  

### How to use
1. Read `docs/qa/README.md` (navigation).  
2. Work top-down by priority tables.  
3. After fix: flip `[ ]` → `[x]`, add commit hash in a short note under the finding.  
4. Do not delete PASS rows — they document what already works.

### Priority A — Storefront P0 (ads blocked without these)

| ☐ | ID | Sev | One-line | Detail / related plan |
|---|-----|-----|----------|------------------------|
| [x] | **F-071** | P0 | `link_order_to_utm` ignores first_touch UTM | **FIXED `34275e28`**; server tests 6/6 + production HEAD/health verified 2026-07-10; §F-071 |
| [x] | **F-021** | P0 | 100% orders empty utm_source | **FIXED `34275e28`**; production canary + cleanup verified 2026-07-10; §F-021 |
| [x] | **F-033** | P0 | link_order in code but orders empty | **FIXED `34275e28`**; production canary + cleanup verified; §F-033 |
| [x] | **F-045** | P0 | 0 Order.session_key join UTMSession | **FIXED `34275e28`** for new orders; production join canary passed; §F-045 |
| [x] | **F-019** | P0 | is_converted always 0 | **FIXED `34275e28`** for new conversions; production lead canary passed; §F-019 |
| [x] | **F-030** | P0 | initializePixelsImmediately not defined | **FIXED `3291ac82`**; production hashed asset verified 2026-07-10; §F-030 |
| [x] | **F-029** | P0 | LSAPI_CHILDREN process limit | **FIXED OPS 2026-07-11**; app env 6→10, 30/30 concurrent health checks, zero new limit errors; §F-029 |
| [x] | **F-102** | P0 | Core order/UTM tables are MyISAM; `atomic()` cannot roll back | **FIXED `02b49553`**; 10 InnoDB engines + production rollback canary verified 2026-07-11 |
| [x] | **F-003** | P0 | Merchant feed / color landing issues | **FIXED `4d72412a`**; 384 canonical links + live landing sample verified 2026-07-11; §F-003 |
| [x] | **F-027** | P0 | Feed color issues (narrowed) | **FIXED `4d72412a`**; color/size now canonical path, no redirect/query loss; §F-027 |
| [x] | **F-097** | P0 | IG bot Message Requests unlabeled | **FIXED_APP `e47c1498`**; 26/26 server tests + live advanced_access client verified 2026-07-11; Meta permission remains external |
| [x] | **F-099** | P1 | Mono dual path W2-7 (webhook no on_commit CAPI) | **FIXED `78814344`**; 29/29 server tests + production post-commit canary/cleanup verified 2026-07-12; §F-099 |

### Priority B — Storefront P1 (fix next)

| ☐ | ID | Sev | One-line | Detail |
|---|-----|-----|----------|--------|
| [ ] | **F-001** | P1 | Category titles truncated | §F-001; **PLAN_VS ADS-3** (DB reseed) |
| [ ] | **F-023** | P1 | Truncated titles in MySQL | §F-023; root of F-001 |
| [ ] | **F-002** | P1 | Color landing UA grammar | §F-002 |
| [ ] | **F-004** | P1 | Product title vs H1 | §F-004 |
| [ ] | **F-094** | P1 | title≠H1 reconfirm last-breath etc. | §F-094; F-004 |
| [ ] | **F-005** | P1 | RU/EN H1 still Ukrainian | §F-005; **PLAN_VS ADS-2** |
| [ ] | **F-083** | P1 | purchase UA 3 vs 36 paid | §F-083; **PLAN_VS W2-3** |
| [ ] | **F-044** | P1 | Most web orders empty session_key | §F-044 |
| [ ] | **F-068** | P1 | prepay_200 all missing session_key | §F-068; F-073 |
| [ ] | **F-073** | P1 | session in tracking.external_id only | §F-073 |
| [ ] | **F-074** | P1 | COD no _ensure_session_key | §F-074; PLAN_VS W1-1 nuance |
| [ ] | **F-072** | P1 | Only 2/36 recoverable via external_id | §F-072 |
| [ ] | **F-076** | P1 | PV noise / site_session gap | §F-076; PLAN_VS W2-4 |
| [ ] | **F-084** | P1 | chatgpt vs chatgpt.com dual | §F-084; PLAN_VS W2-8 |
| [ ] | **F-020** | P1 | Historical dirty utm_source | §F-020 |
| [ ] | **F-057** | P1 | All-time dirty utm inventory | §F-057 |
| [ ] | **F-022** | P1 | PV→ATC cliff | §F-022 |
| [ ] | **F-032** | P1 | UserAction rarely linked UTMSession | §F-032 |
| [ ] | **F-031** | P1 | MySQL has gone away | §F-031; F-080 |
| [ ] | **F-007** | P1 | HTTP 429 burst crawl | §F-007 |
| [ ] | **F-018** | P1 | offer_id ЧОРНИЙ/ЧЕРНЫЙ | §F-018 |
| [ ] | **F-043** | P1 | /help-center/ 404 | §F-043 |
| [ ] | **F-050** | P1 | NP Kyiv Latin 502 | §F-050 |
| [ ] | **F-059** | P1 | ProductImage alt empty | §F-059 |
| [ ] | **F-087** | P1 | ubd_docs public 200 | §F-087; PLAN_VS W1-11 |
| [ ] | **F-088** | P1 | TG webhook secret empty | §F-088; PLAN_VS W3-9 |
| [x] | **F-093** | P1 | deploy_paramiko password in git | Fixed `c5b651cf`; production verified; §F-093 |
| [x] | **F-095** | P1 | IG Hide list not refreshed | Fixed `ad2883f0`; production verified: UA actions refresh lists, hidden queue excluded, `hidden_pending=0`; **IG_BOT** IG-001 |
| [x] | **F-096** | P1 | IG stats English / thin | Fixed `15c3bf30` + `337710ce` + `3d4e5d40`; production verified; **IG_BOT** IG-004 |
| [x] | **F-098** | P1 | IG no transfer button | REVISED_OWNER: duplicate manual action rejected; automatic manager takeover retained; **IG_BOT** IG-003 |

### Priority C — P2 / ops / hygiene

| ☐ | ID | Sev | One-line | Detail |
|---|-----|-----|----------|--------|
| [ ] | **F-006** | P2 | Color sitemap ×3 | §F-006 |
| [ ] | **F-008** | P2 | Meta description too long | §F-008 |
| [ ] | **F-010** | P2 | debug endpoints login not 404 | §F-010 |
| [ ] | **F-011** | P2 | TikTok ttq.load not in HTML | §F-011 |
| [ ] | **F-013** | P2 | Category title vs H1 strategy | §F-013 |
| [ ] | **F-028** | P2 | RU/EN PDP naming | §F-028 |
| [ ] | **F-035** | P2 | CSP violations | §F-035 |
| [ ] | **F-036** | P2 | Telegram RemoteDisconnected | §F-036 |
| [ ] | **F-048** | P2 | fbp without internal UTM | §F-048 |
| [ ] | **F-051** | P2 | checkout/capture empty 200 | §F-051; PLAN_VS W3-11 |
| [ ] | **F-075** | P2 | CheckoutCapture.converted 0/4 | §F-075; mono path |
| [ ] | **F-078** | P2 | /kontakty/ 404 | §F-078 |
| [ ] | **F-089** | P2 | FACEBOOK_PIXEL_ID empty settings | §F-089 |
| [ ] | **F-090** | P2 | No MySQL backup cron | §F-090; PLAN_VS W0-3 |

### Priority D — P3 open

| ☐ | ID | Sev | One-line | Detail |
|---|-----|-----|----------|--------|
| [ ] | **F-009** | P3 | favicon 302 | §F-009 |
| [ ] | **F-014** | P3 | sitemap lastmod cluster | §F-014 |
| [ ] | **F-015** | P3 | manifest.webmanifest 404 | §F-015 |

### Plan-only reopen (not always separate F-*) — still fix

| ☐ | Plan ID | Issue | Detail |
|---|---------|-------|--------|
| [x] | **W2-7** | Dual mono status path: webhook skips on_commit CAPI | **FIXED `78814344`**; shared post-commit dispatcher, once-only production canary |
| [ ] | **W7-1** | views.py.backup still lazy-loaded | PLAN_VS W7-1 |
| [ ] | **W7-23** | residual datetime.now dropshipper | PLAN_VS W7-23 |
| [ ] | **W0-5** | OPS docs OK; server stash OWNER | PLAN_VS W0-5 |
| [ ] | **IG-002…IG-014** | Full IG bot pack beyond F-095…098 | **IG_BOT_MANAGEMENT_BUGS** |

### PASS / INFO (do not fix as bugs)

See master index tables below for `[x]` rows (F-012, F-016, F-024, F-046, F-047, F-077, F-081, F-092 DONE_OWNER, …).

---

## MASTER FINDINGS INDEX (все находки Pass A)

> **Как читать**
> - `[ ]` = **ещё не исправлено** (для fix-агента / Pass C → fix)
> - `[x]` = **PASS / INFO** — проверено, чинить не нужно (или только process-note)
> - Полное описание: секции `### F-xxx` ниже **или** колонка Detail в **MASTER FIX CHECKLIST**
> - Навигация fix-агента: [`README.md`](./README.md)
> - Plan false-DONE / dual mono path: [`PLAN_VS_FINDINGS_2026-07-09.md`](./PLAN_VS_FINDINGS_2026-07-09.md)
> - IG bot deep dive: [`IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`](./IG_BOT_MANAGEMENT_BUGS_2026-07-09.md)
> - Walk checklist (Pass A done): `PRE_ADS_MASTER_AUDIT_CHECKLIST.md`

**Итого: F-001…F-102** (+ IG-001…IG-014 in IG_BOT file) · **Ads gate: BLOCKED** · Fix agent: **MASTER FIX CHECKLIST** above

### Сводка по severity

| Severity | OPEN (чинить) | PASS/INFO/REVISED |
|----------|-------------:|------------------:|
| P0 | **9** | 0 |
| P1 | **22** | 0 |
| P2 | **12** | 4 |
| P3 | **3** | 36 |
| **Всего** | **~46 open** | **~40 pass/info** |

### Полный список F-001 … F-058

| ID | Sev | Status | Fix? | One-line |
|----|-----|--------|------|----------|
| [ ] **F-001** | P1 | OPEN | YES | Category titles truncated mid-phrase (also in MySQL F-023) |
| [ ] **F-002** | P1 | OPEN | YES | Color landing broken UA grammar |
| [x] **F-003** | P0 | FIXED | DONE | `4d72412a`: 384 canonical feed links; live landing sample verified |
| [ ] **F-004** | P1 | OPEN | YES | UK product title vs H1 mismatch (13 URLs) + RU leak in H1 |
| [ ] **F-005** | P1 | OPEN | YES | RU/EN home+catalog H1 still Ukrainian |
| [ ] **F-006** | P2 | OPEN | YES | Color sitemap same URL ×3 |
| [ ] **F-007** | P1 | OPEN | YES | HTTP 429 under burst crawl |
| [ ] **F-008** | P2 | OPEN | YES | Meta description too long on some static pages |
| [ ] **F-009** | P3 | OPEN | YES | favicon.ico 302 then 200 |
| [ ] **F-010** | P2 | OPEN | YES | debug/dev endpoints login-gated not 404 |
| [ ] **F-011** | P2 | OPEN | YES | TikTok data-attr present; ttq.load not in initial HTML |
| [x] **F-012** | P2 | INFO | no | ViewContent JS-only (expected architecture) |
| [ ] **F-013** | P2 | OPEN | YES | Category title vs H1 length strategy inconsistent |
| [ ] **F-014** | P3 | OPEN | YES | Sitemap lastmod clustered 2026-06-11 |
| [ ] **F-015** | P3 | OPEN | YES | manifest.webmanifest 404; site.webmanifest OK |
| [x] **F-016** | P3 | PASS | no | Variant URL titles work |
| [x] **F-017** | P3 | PASS | no | mapa-saytu links all 200 |
| [ ] **F-018** | P1 | OPEN | YES | offer_id ЧОРНИЙ vs ЧЕРНЫЙ split |
| [x] **F-019** | P0 | FIXED | DONE | `34275e28`: new conversion canary sets is_converted; cleanup verified |
| [ ] **F-020** | P1 | OPEN | YES | Historical dirty utm_source (new canaries normalize OK) |
| [x] **F-021** | P0 | FIXED | DONE | `34275e28`: first-touch order attribution production canary verified |
| [ ] **F-022** | P1 | OPEN | YES | Extreme PV→ATC cliff / possible product_view noise |
| [ ] **F-023** | P1 | OPEN | YES | Category truncated titles in MySQL (root of F-001) |
| [x] **F-024** | P3 | PASS | no | ATC API + mini-cart works |
| [x] **F-025** | P3 | PASS | no | Blog UK sitemap healthy |
| [x] **F-026** | P3 | PASS | no | Home critical static assets 200 |
| [x] **F-027** | P0 | FIXED | DONE | `4d72412a`: color/size encoded in canonical path without query loss |
| [ ] **F-028** | P2 | OPEN | YES | RU/EN PDP naming strategy vs UK mismatch |
| [x] **F-029** | P0 | FIXED_OPS | DONE | LSAPI_CHILDREN 6→10; 30/30 concurrent health checks, zero new limit errors |
| [x] **F-030** | P0 | FIXED | DONE | `3291ac82`: BFCache pixel restore; live hashed asset verified |
| [ ] **F-031** | P1 | OPEN | YES | MySQL server has gone away |
| [ ] **F-032** | P1 | OPEN | YES | UserAction rarely linked to UTMSession |
| [x] **F-033** | P0 | FIXED | DONE | `34275e28`: production order/session attribution canary verified |
| [x] **F-034** | P3 | PASS | no | Variants sample + recs links OK |
| [ ] **F-035** | P2 | OPEN | YES | CSP violations in stderr |
| [ ] **F-036** | P2 | OPEN | YES | Telegram admin RemoteDisconnected |
| [x] **F-037** | P2 | INFO | no | Home IP exclusion (owner can toggle; retest F-049) |
| [x] **F-038** | P2 | REVISED | no | sessionid delay mainly under exclusion; non-excluded OK |
| [x] **F-039** | P2 | REVISED | no | track-event stored:false under exclusion; stored:true after unexclude |
| [x] **F-040** | P3 | INFO | no | Checkout is JS/Mono-driven path map |
| [x] **F-041** | P3 | PASS | no | CSP allows Meta/TikTok/GTM |
| [x] **F-042** | P3 | PASS | no | Early Meta PageView in HTML |
| [ ] **F-043** | P1 | OPEN | YES | /help-center/ 404 (need 301→/dopomoga/) |
| [ ] **F-044** | P1 | OPEN | YES | Most web orders empty session_key (29/36) |
| [x] **F-045** | P0 | FIXED | DONE | `34275e28`: new-order UTMSession join production canary verified |
| [x] **F-046** | P3 | PASS | no | Server canary UTM capture |
| [x] **F-047** | P3 | PASS | no | Sitemap 489/489 HTTP 200 |
| [ ] **F-048** | P2 | OPEN | YES | Orders have fbp tracking without internal UTM |
| [x] **F-049** | P3 | PASS | no | Home unexclude canary PASS |
| [ ] **F-050** | P1 | OPEN | YES | NP city Latin Kyiv 502 / Київ 200 |
| [ ] **F-051** | P2 | OPEN | YES | checkout/capture empty returns 200 ok |
| [x] **F-052** | P3 | PASS | no | Mono validates missing city |
| [x] **F-053** | P3 | PASS | no | Home links 42/42 200 |
| [x] **F-054** | P3 | PASS | no | Blog+color HTTP OK (grammar still F-002) |
| [x] **F-055** | P3 | PASS | no | RU/EN product sample title/H1 aligned |
| [x] **F-056** | P3 | PASS | no | IGShopping multi-hop canary PASS |
| [ ] **F-057** | P1 | OPEN | YES | All-time dirty utm_source inventory |
| [x] **F-058** | P3 | PASS | no | Scripts matrix key pages PASS |

| [ ] **F-059** | P1 | OPEN | YES | All ProductImage.alt_text empty (36/36) |
| [x] **F-060** | P3 | PASS | no | Cart qty update works with cart_key |
| [x] **F-061** | P3 | PASS | no | Cart remove works |
| [x] **F-062** | P3 | PASS | no | Promo invalid code validation |
| [x] **F-063** | P2 | PASS | no | NP warehouses via settlement_ref |
| [x] **F-064** | P3 | PASS | no | Favorites toggle works |
| [x] **F-065** | P3 | PASS | no | Custom 404 branded noindex |
| [x] **F-066** | P3 | PASS | no | BlogPosting JSON-LD |
| [x] **F-067** | P3 | PASS | no | load-more-products works |
| [ ] **F-068** | P1 | OPEN | YES | prepay_200 orders 19/19 no session_key |
| [x] **F-069** | P2 | INFO | no | Home exclusion re-enabled |
| [x] **F-070** | P3 | INFO | no | Promo POST field is promo_code |
| [x] **F-071** | P0 | FIXED | DONE | `34275e28`: first-touch cookie reconstruction and normalized source verified |
| [ ] **F-072** | P1 | OPEN | YES | Only 2/36 web orders recoverable to UTM via external_id |
| [ ] **F-073** | P1 | OPEN | YES | prepay had session in tracking.external_id but empty Order.session_key |
| [ ] **F-074** | P1 | OPEN | YES | COD create_order no _ensure_session_key (order 275) |
| [ ] **F-075** | P2 | OPEN | YES | CheckoutCapture.converted never true (0/4) |
| [ ] **F-076** | P1 | OPEN | YES | product_view 41283 vs ATC 61; 96% PV without site_session |
| [x] **F-077** | P2 | REVISED | no | Product feed g:link OK when unescaped (narrows F-027) |
| [ ] **F-078** | P2 | OPEN | YES | /kontakty/ 404; canonical is /contacts/ |
| [x] **F-079** | P0 | RECONF | no | F-030 live: 8+ client_errors initializePixelsImmediately |
| [x] **F-080** | P1 | RECONF | no | F-031 live: 565× MySQL server has gone away in django.log |
| [x] **F-081** | P3 | PASS | no | Footer legal/support pages 14/14 200 |
| [x] **F-082** | P3 | PASS | no | Feed 384 unique g:id; Cyrillic OK; no dup IDs |
| [ ] **F-083** | P1 | OPEN | YES | purchase UserAction 3 vs paid/prepaid orders 36 |
| [ ] **F-084** | P1 | OPEN | YES | Live dual AI sources chatgpt vs chatgpt.com (still writing) |
| [x] **F-085** | P3 | PASS | no | Home hreflang×4 + canonical + OG + healthz OK |
| [x] **F-086** | P3 | PASS | no | Mild burst 20× catalog → 0×429 (F-007 is high-load only) |
| [ ] **F-087** | P1 | OPEN | YES | ubd_docs publicly HTTP 200 (W1-11 CONFIRMED) |
| [ ] **F-088** | P1 | OPEN | YES | TELEGRAM_BOT_WEBHOOK_SECRET empty on prod (W3-9) |
| [ ] **F-089** | P2 | OPEN | YES | FACEBOOK_PIXEL_ID settings EMPTY (HTML fallback only) |
| [ ] **F-090** | P2 | OPEN | YES | No MySQL backup cron (script present; W0-3) |
| [x] **F-091** | P3 | INFO | no | Full plan re-verify matrix: PLAN_VS_FINDINGS_2026-07-09.md |
| [x] **F-092** | P2 | DONE_OWNER | no | SSH password rotated by owner (W0-1 OWNER complete) |
| [x] **F-093** | P1 | FIXED | YES | `deploy_paramiko.py` removed in `c5b651cf`; production verified |
| [ ] **F-094** | P1 | OPEN | YES | Product title≠H1 e.g. last-breath / death-grabs-ass (reconfirm) |
| [x] **F-095** | P1 | FIXED | YES | `ad2883f0`: reliable UA actions, hidden folder, automation/follow-up/analytics exclusion; production verified |
| [x] **F-096** | P1 | FIXED | YES | Ukrainian dense KPI dashboard, today/7/30/all ranges, funnel shares and bounded revenue; production verified |
| [x] **F-097** | P0 | FIXED_APP | DONE | `e47c1498`: Ukrainian delivery-block state/filter; 26/26 server tests and live classification verified |
| [x] **F-098** | P1 | REVISED_OWNER | no | Owner rejected a duplicate manual transfer button; existing AI/page-echo manager takeover is the intended flow |
| [x] **F-099** | P1 | FIXED | DONE | `78814344`: webhook uses shared on-commit dispatcher; server tests and production canary verified |
| [ ] **F-100** | P2 | OPEN | YES | views.py.backup still lazy-loaded (plan W7-1) |
| [ ] **F-101** | P3 | OPEN | YES | residual datetime.now in dropshipper_views (plan W7-23) |
| [x] **F-102** | P0 | FIXED | DONE | `02b49553`: checkout/attribution tables converted to InnoDB; rollback canary verified |

### P0 OPEN — 0

Все подтверждённые P0 из master index закрыты. Внешний Meta Advanced Access для F-097 остаётся действием владельца приложения, но приложение уже корректно классифицирует и показывает этот запрет.

### P1 OPEN —
- [ ] **F-059** — All ProductImage.alt_text empty (36/36)
- [ ] **F-068** — prepay_200 orders missing session_key
- [ ] **F-072** — external_id UTM recovery only 2 orders
- [ ] **F-073** — session in tracking but not Order.session_key (prepay era)
- [ ] **F-074** — COD missing session_key ensure
- [ ] **F-076** — PV noise / site_session gap
- [ ] **F-083** — purchase UserAction undercount vs paid orders
- [ ] **F-084** — dual chatgpt / chatgpt.com sources still live

### P1 OPEN (continued) — 15
- [ ] **F-001** — Category titles truncated mid-phrase (also in MySQL F-023)
- [ ] **F-002** — Color landing broken UA grammar
- [ ] **F-004** — UK product title vs H1 mismatch (13 URLs) + RU leak in H1
- [ ] **F-005** — RU/EN home+catalog H1 still Ukrainian
- [ ] **F-007** — HTTP 429 under burst crawl
- [ ] **F-018** — offer_id ЧОРНИЙ vs ЧЕРНЫЙ split
- [ ] **F-020** — Historical dirty utm_source (new canaries normalize OK)
- [ ] **F-022** — Extreme PV→ATC cliff / possible product_view noise
- [ ] **F-023** — Category truncated titles in MySQL (root of F-001)
- [ ] **F-031** — MySQL server has gone away (reconf F-080)
- [ ] **F-032** — UserAction rarely linked to UTMSession
- [ ] **F-043** — /help-center/ 404 (need 301→/dopomoga/)
- [ ] **F-044** — Most web orders empty session_key (29/36)
- [ ] **F-050** — NP city Latin Kyiv 502 / Київ 200
- [ ] **F-057** — All-time dirty utm_source inventory

### P2 OPEN — 12
- [ ] **F-006** — Color sitemap same URL ×3
- [ ] **F-008** — Meta description too long on some static pages
- [ ] **F-010** — debug/dev endpoints login-gated not 404
- [ ] **F-011** — TikTok data-attr present; ttq.load not in initial HTML
- [ ] **F-013** — Category title vs H1 length strategy inconsistent
- [ ] **F-028** — RU/EN PDP naming strategy vs UK mismatch
- [ ] **F-035** — CSP violations in stderr
- [ ] **F-036** — Telegram admin RemoteDisconnected
- [ ] **F-048** — Orders have fbp tracking without internal UTM
- [ ] **F-051** — checkout/capture empty returns 200 ok
- [ ] **F-075** — CheckoutCapture.converted stuck false
- [ ] **F-078** — /kontakty/ 404 vs /contacts/

### P3 OPEN — 3
- [ ] **F-009** — favicon.ico 302 then 200
- [ ] **F-014** — Sitemap lastmod clustered 2026-06-11
- [ ] **F-015** — manifest.webmanifest 404; site.webmanifest OK

### PASS / INFO / REVISED (не чинить как баг) — 26
- [x] **F-012** — ViewContent JS-only (expected architecture)
- [x] **F-016** — Variant URL titles work
- [x] **F-017** — mapa-saytu links all 200
- [x] **F-024** — ATC API + mini-cart works
- [x] **F-025** — Blog UK sitemap healthy
- [x] **F-026** — Home critical static assets 200
- [x] **F-034** — Variants sample + recs links OK
- [x] **F-037** — Home IP exclusion (owner can toggle; retest F-049)
- [x] **F-038** — sessionid delay mainly under exclusion; non-excluded OK
- [x] **F-039** — track-event stored:false under exclusion; stored:true after unexclude
- [x] **F-040** — Checkout is JS/Mono-driven path map
- [x] **F-041** — CSP allows Meta/TikTok/GTM
- [x] **F-042** — Early Meta PageView in HTML
- [x] **F-046** — Server canary UTM capture
- [x] **F-047** — Sitemap 489/489 HTTP 200
- [x] **F-049** — Home unexclude canary PASS
- [x] **F-052** — Mono validates missing city
- [x] **F-053** — Home links 42/42 200
- [x] **F-054** — Blog+color HTTP OK (grammar still F-002)
- [x] **F-055** — RU/EN product sample title/H1 aligned
- [x] **F-056** — IGShopping multi-hop canary PASS
- [x] **F-058** — Scripts matrix key pages PASS
- [x] **F-077** — Product feed g:link landings PASS
- [x] **F-079** — reconfirm note for F-030
- [x] **F-080** — reconfirm note for F-031
- [x] **F-081** / **F-082** — static pages + feed id inventory PASS

---


## Funnel snapshot (CRO)

**Source:** production MySQL via Django ORM read-only (2026-07-09). **No secrets stored.**

### UserAction event counts

| Stage | 7d events | 7d distinct utm_sessions w/ action | 30d events | 30d distinct utm_sessions |
|-------|----------:|-----------------------------------:|-----------:|--------------------------:|
| product_view | 7368 | 8 | 21725 | 20 |
| add_to_cart | 13 | 2 | 25 | 5 |
| initiate_checkout | 1 | 0 | 2 | 0 |
| lead | 1 | 0 | 2 | 0 |
| purchase | 0 | 0 | 1 | 0 |
| page_view | 0 | — | 0 | — |

**Approx rates (events, 30d):** PV→ATC ≈ **0.12%**; ATC→IC ≈ **8%**; IC→purchase ≈ **50%** (tiny volume).  
**UTMSession 30d:** 140 sessions; **is_converted=True: 0** (all-time also **0** / 1041).

### Orders attribution (field `created`)

| Period | Orders | empty `utm_source` | with `utm_session` |
|--------|-------:|-------------------:|-------------------:|
| 7d | 2 | **100%** | **0** |
| 30d | 6 | **100%** | **0** |
| 90d | 12 | **100%** | **0** |

### UTM sources (UTMSession.first_seen last 30d, top)

| utm_source | sessions |
|------------|--------:|
| chatgpt.com | 113 |
| ig | 14 |
| chatgpt | 4 |
| audit | 3 |
| instagram | 3 |
| threads | 2 |
| IGShopping | 1 |

→ normalization **not fully applied** on stored rows (see F-020).

**Note:** Pass A machine IP is AnalyticsExclusion `дом` (F-037) — synthetic UTMSession canaries from this IP are invalid.

**HTML/API readiness for funnel path (rechecked):**

- Mini-cart empty: PASS  
- **ATC API:** `POST /cart/add/` with CSRF → `ok:true`, count increments, mini-cart shows line `TC-0001-ЧОРНИЙ-M`  
- Cart with items: pay types `online_full`, `prepay_200`; NP city/warehouse fields present  
- Full browser pixel Events Manager still pending for Pass C  

---

## SEO batch results

| Batch | Total | OK | Fail / issue | Notes |
|-------|------:|---:|-------------:|-------|
| Smoke core pages | 11 | 11 | 0 | home, catalog, cart, contacts, blog, etc. |
| Sitemap child files | 8 | 8 | 0 | all 200 |
| Sitemap unique locs (fast crawl) | 489 | 214* | 275×429* | *rate limit; not real 404 |
| UK products (full slow) | 65 | 65 | 0 HTTP; **13 title/H1 name mismatches** | F-004 |
| Prod DB published products | 65 | empty seo_title **0**, empty seo_description **0**, dup titles **0** | empty seo_title prod DB |
| Orders 90d UTM | 12 | 0 attributed | **F-021** |
| Variant URLs sample | 20 | 20 | 0 | titles include color/fit F-016 |
| mapa-saytu links | 53 | 53 | 0 | F-017 |
| UK categories | 3 | 3 | 0 | titles **truncated** (see F-001) |
| Color landings unique | 4 | 4 | grammar FAIL | F-002; sitemap lists 12=3×dups |
| Thematic landings | 4 | 4 | 0 | military/streetwear/patriotic/kharkiv |
| Static support set | 13 | 13 | 0 | favorites/qr noindex OK |
| Merchant feed links sample | 5 | 5 HTTP | **canonical path wrong** | F-003 |

**Products in sitemap (UK):** 65  
**Products×locales in products sitemap:** 195  
**Variants sitemap URLs:** 178  
**Color sitemap:** 12 locs / **4 unique** (each repeated ×3)

---

## UTM / Pixel canary (partial)

| Step | Result | Evidence |
|------|--------|----------|
| Land with UTM+fbclid | PASS | URL `/?utm_source=instagram&utm_medium=paid_social&utm_campaign=qa_audit_20260709&utm_content=pass_a&fbclid=TEST_FBCLID_AUDIT_1` → 200 |
| First-touch cookie | PASS | `Set-Cookie: twc_ft` contains utm_* + fbclid (HttpOnly Secure) |
| Visitor id | PASS | `twc_vid=…` set |
| Meta pixel ID in HTML | PASS | `823958313630148` (fbq init ×1, PageView ×1 on home) |
| GTM container | PASS | `GTM-PRLLBF9H` present |
| TikTok data attr | PASS/WARN | `D43L7DBC77UA61AHLTVG` on body; `ttq.load` not found in raw HTML (likely deferred JS) |
| UTMSession DB row | BLOCKED | needs server/admin |
| ATC + Purchase E2E | BLOCKED | needs browser + test order |
| Dispatcher sees campaign | BLOCKED | needs auth |

---

## Findings (detailed)

> **Fix checkbox:** `[ ]` open · `[x]` fixed later after Pass C  
> Each finding includes: why problem, evidence, risk if wrong, risk of fixing, recheck notes for Pass C.

---

### F-001 — Category `<title>` truncated mid-phrase

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **SEO** · Checklist: SEO-003, SEO-005, SEO-090, PG-007

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |
| URLs | https://twocomms.shop/catalog/long-sleeve/ · /catalog/tshirts/ · /catalog/hoodie/ |

**What we saw**

| URL | Title (as served) |
|-----|-------------------|
| `/catalog/long-sleeve/` | `Лонгсліви TwoComms — лаконічний стрітвеар з рукавами на` |
| `/catalog/tshirts/` | `Футболки TwoComms — стрітвеар та мілітарі-принти від` |
| `/catalog/hoodie/` | `Худі TwoComms — теплі толстовки зі стрітвеар-принтами та` |

Titles end on prepositions/conjunctions (**на / від / та**) — clearly cut, not natural endings.

**Why this is a problem**

1. SERP snippet looks unfinished → lower CTR.  
2. Suggests hard length cut in SEO title generator/override without word boundary.  
3. H1 on same pages is longer and complete → title/H1 mismatch weakens relevance signals.  
4. User explicitly called out catalog title problems — this matches.

**Likely location (for later fix agent — do not fix now)**

- DB fields `Category.seo_title` / overrides, and/or  
- `storefront/services/*category*seo*`, `get_category_seo_meta`, templates catalog title block.  
- Possible truncation to ~50–56 chars without ellipsis/word boundary.

**Evidence method:** GET HTML 2026-07-09, parse `<title>`.

**Business impact:** category landings are common ad targets; weak titles waste spend.

**Fix direction (observe only):** rewrite complete titles ≤60 chars ending on full word; audit all category locales.

**Risk of fix:** low if only SEO text fields; medium if changing shared title template (test all categories).

**Pass C recheck:** fetch all category titles uk/ru/en; ensure no trailing prepositions.

---

### F-002 — Color category landings: broken grammar in title/H1

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

> **Reconfirm 2026-07-09 late:** `/catalog/tshirts/black/` title=`Купити чорний футболка з принтом` · h1=`Чорні футболка TwoComms — стрітвір з Харкова` (grammar + «стрітвір» typo). Sitemap still emits each color URL ×3 (F-006).

- [ ] **Open** · Severity: **P1** · Area: **SEO** · Checklist: SEO-014, PG-008, SEO-090

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Examples (200 OK but copy broken)**

| URL | Title | H1 |
|-----|-------|-----|
| `/catalog/tshirts/black/` | Купити **чорний футболка** з принтом — TwoComms | **Чорні футболка** TwoComms… |
| `/catalog/hoodie/black/` | Купити **чорний худі**… | **Чорні худі**… |
| `/catalog/long-sleeve/black/` | Купити **чорний лонгслів**… | **Чорні лонгслів**… |
| `/catalog/tshirts/coyote/` | Купити **футболка** кольору Кайот… | Футболка кольору Кайот… |

**Why problem**

- Gender/number agreement broken (чорний/чорні + wrong noun form).  
- Looks auto-templated from color + category without morphology.  
- Indexable pages (robots index,follow) → Google may show broken Ukrainian.  
- Ads to color landings hurt brand trust.

**Likely code:** `services/color_seo_copy.py`, `CategoryColorLanding`, color SEO overrides.

**Fix direction:** human-written overrides per color×category or proper inflection map; prefer «Чорні футболки», «Чорні худі», etc.

**Risk of fix:** low–medium (SEO strings + maybe template).

**Pass C:** enumerate all published color landings; grammar checklist.

---

### F-003 — Google Merchant feed `g:link` mangled (`&amp;` → path `/s/?amp;color=`)

**Status:** [x] FIXED (`4d72412a`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-11):** feed generation now emits the PDP's
canonical color→size path directly, e.g. `/product/<slug>/black/s/`, instead of
the legacy query whose translated color label could not be resolved. The server
suite passes **11/11**. The live feed has **384/384 query-free canonical links**
and 384 unique offer IDs; 20 evenly sampled live landings returned **20/20 HTTP
200** with **zero redirects**.

- [x] **Fixed** · Severity: **P0** · Area: **FEED / ADS** · Checklist: FEED-001–003, ADS-012, PIX-011

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence**

Feed sample `g:link` (XML text):

```text
https://twocomms.shop/product/twocomms-beliveidea-ts/?size=S&amp;color=%D0%A7%D0%BE%D1%80%D0%BD%D0%B8%D0%B9
```

HTTP GET of that string (as written with literal `&amp;`) final URL observed:

```text
https://twocomms.shop/product/twocomms-beliveidea-ts/s/?amp%3Bcolor=%D0%A7%D0%BE%D1%80%D0%BD%D0%B8%D0%B9
```

Interpretation:

1. `size=S` consumed as **variant path segment** `/s/` (site supports path-style variants).  
2. Literal `amp;color=` remains as broken query (`amp%3Bcolor`) — **color not applied as intended**.  
3. Response still **HTTP 200** → soft-wrong landing (wrong size path; color query garbage), not hard 404 — worse for Shopping (approved wrong URL).

**Also:** `g:id` format uses Cyrillic color names, e.g. `TC-0106-ЧОРНИЙ-S` (384 ids in full feed file ~2.1MB). Prior tracking docs mentioned Latin-ish `TC-{product}-{color}-{SIZE}` — **parity with pixel content_ids must be rechecked** (not yet browser-verified).

**Why P0**

- Merchant/Meta catalog ads depend on correct product landing + ID match.  
- Wrong landings → rejected items, bad Quality, wasted spend, pixel content_id mismatch risk.

**Likely location:** feed generator (`storefront` feeds / `generate_google_merchant_feed`, marketplace feed services) writing `&amp;` incorrectly into link field or double-escaping.

**Fix direction:** emit raw `&` in XML properly escaped once as `&amp;` only in XML serialization (not in HTTP URL string used by clients after decode); prefer path-style canonical variant URLs already in sitemap (`/product/slug/black/`) without query.



**Update 2026-07-09 (XML-unescape retest):**  
Proper HTML unescape of `g:link` yields `?size=S&color=Чорний`. HTTP client final URL becomes:

```text
https://twocomms.shop/product/.../s/
```

- Size query is rewritten to path segment `/s/` (OK-ish for size).  
- **Color query is dropped entirely** from final URL.  
- Still **P0**: Merchant/Meta think color-specific item lands on size-only URL; color may default wrong.  
- Combined with **100% Cyrillic `g:id`** (384/384), content_id parity with pixel `offer_id` (also Cyrillic `TC-0001-ЧОРНИЙ-M`) must stay consistent — mixed `ЧОРНИЙ`/`ЧЕРНЫЙ` seen in cart API (F-023).

**Risk of fix:** **HIGH** — feed ID/link changes can break Meta/Google catalogs; need staged regen + re-fetch validation + pixel content_id alignment. **Do not hotfix without Pass C + catalog freeze plan.**

**Pass C:** parse feed with XML lib (so `&amp;`→`&`), GET decoded links; compare final URL to expected size/color; sample content_ids vs pixel.

---

### F-004 — Product title vs H1 mismatch (and RU leak in H1)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **SEO / GEO** · Checklist: SEO-031, SEO-004, SEO-006, GEO-006

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (full UK catalog 65/65 HTTP 200) |
| Status (C) | |

**Batch (2026-07-09 slow crawl, all UK product sitemap locs):**

- HTTP non-200: **0 / 65**  
- Title length >65: **0**  
- Title length <25: **0**  
- Exact duplicate titles: **0**  
- Quote-name mismatches title «…» vs H1 «…»: **13 product URLs (≈5 print families)**

**Mismatch table (title quote → H1 quote)**

| URL family | Title name | H1 name |
|------------|------------|---------|
| `/product/last-breath/`, `-hd/`, `-ls/` | last breath | Череп З Трояндою |
| `/product/death-grabs-ass/`, `-hd/`, `-ls/` | death grabs ass | Серце Та Грощі |
| `/product/lord-of-the-lending/`, `-hd/`, `-ls/` | Lord Of The Lending | Це Моя Посадка |
| `/product/death-gbs-ass-ts/`, `-hd/`, `-ls/` | І На Той Світ З Собою Візьму | **Череп с дупою** (RU «с») |
| `/product/hoodie-silent-winter/` | Silent Winter | Дівчина Снайпер |

**Locale twist on same SKU `death-gbs-ass-ts`:**

| Locale | title | H1 |
|--------|-------|-----|
| UK | «І На Той Світ…» | «Череп с дупою» |
| RU | «Череп С Задницей» | «Череп С Задницей» (aligned) |
| EN | «Last Breath» | «Last Breath» (aligned, different concept) |

**Why problem**

- Title and H1 describe **different products/names** on UA → SERP vs page mismatch, weak relevance.  
- UA H1 Russian fragment «с дупою» → language leak.  
- EN/RU use yet another naming — intentional localization OR data chaos; needs content owner decision.  
- Source of truth split: likely `seo_title` vs product `name` / print title in **prod MySQL**.

**Fix direction:** pick one canonical commercial name per locale; sync `seo_title`, H1, schema Product.name, feed title.

**Risk of fix:** medium–high (content + feed + pixel naming; coordinate with catalog ads).

---

### F-005 — RU/EN H1 remains Ukrainian (home + catalog)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **GEO** · Checklist: GEO-006, PG-100, PG-101

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence**

| URL | title language | H1 |
|-----|----------------|-----|
| `/ru/` | Russian OK | `TwoComms — **український** streetwear з кодом продовження` |
| `/en/` | English OK | same Ukrainian H1 |
| `/ru/catalog/` | Russian title OK | `Каталог **одягу** TwoComms` |
| `/en/catalog/` | English title OK | `Каталог **одягу** TwoComms` |

**Why problem:** near-duplicate clustering; poor UX for RU/EN users; known historical leak family from `_audit_seo.md` still live on H1.

**Risk of fix:** medium (i18n templates/modeltranslation for hero/H1).

---

### F-006 — Color sitemap duplicates (same loc ×3)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **SEO** · Checklist: SEO-066, SEO-068

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence:** `sitemap-color-categories.xml` — 12 `<loc>`, 4 unique URLs, each appears **3 times** (likely i18n×3 emitting same non-prefixed URL, or loop bug).

**Impact:** crawl budget noise; signals generator bug (may miss ru/en color URLs entirely).

**Risk of fix:** low–medium (sitemap only).

---

### F-007 — Aggressive HTTP 429 under moderate crawl

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **TECH / ADS** · Checklist: TECH-060 family, SEO-062

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence:** After ~100–150 requests from one IP, many URLs returned **429** (home, catalog, products, static). Slow re-crawl later succeeded (22/22 products 200).

**Why problem**

- Googlebot usually respected, but Ads/Merchant fetchers and monitoring can get 429.  
- Audit/tooling false 404s.  
- Users behind shared NAT might see errors under load.

**Note:** Not proof of bad rate-limit config — may be intentional WAF. Still must document for ads scale.

**Fix direction:** allowlist known bots; tune thresholds; ensure 429 Retry-After.

**Risk of fix:** high if loosening security carelessly.

---

### F-008 — Meta description length outliers (static commercial pages)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **SEO** · Checklist: SEO-021

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

| URL | desc_len |
|-----|--------:|
| `/cooperation/` | 167 |
| `/custom-print/` | 169 |
| `/wholesale/` | 197 |
| `/en/catalog/` | 166 |

**Impact:** SERP truncation; minor.

---

### F-009 — `favicon.ico` redirects (302) before icon

**Status:** [ ] OPEN · **Severity:** P3 · **Fix required:** YES

- [ ] **Open** · Severity: **P3** · Area: **TECH** · Checklist: TECH-040

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

- Direct `/favicon.ico` → **302**, follow → `static/img/favicon.*.ico` **200** image/x-icon.  
- PNG favicons 192/512/180 **200**.  
- May relate to “icon” Telegram complaints if some clients don’t follow redirect.

**Risk of fix:** low.

---

### F-010 — Debug/dev endpoints reachable as login redirects (not hard 404)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **TECH** · Checklist: PG-086, PG-087, TECH-083

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

| Path | Result |
|------|--------|
| `/debug/media/` | 200 login `?next=/debug/media/` |
| `/dev/grant-admin/` | 200 login `?next=/dev/grant-admin/` |
| `/test-analytics/` | → `/admin/login/?next=/test-analytics/` |

**Why issue:** auth-gated is better than open, but **dev/grant-admin** should not exist on prod URL surface even behind login (attack surface / misconfig risk). robots Disallow `/debug/` `/dev/` present — good for SEO, not for security.

**Risk of fix:** medium (URL removal must not break internal tools).

---

### F-011 — TikTok pixel: data attribute present, no `ttq.load` in initial HTML

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **PIXEL** · Checklist: PIX-030

| Field | Value |
|-------|--------|
| Status (B) | SUSPECTED |
| Status (C) | |

`data-tiktok-pixel-id="D43L7DBC77UA61AHLTVG"` present; raw HTML search found **no** `ttq.load` / `ttq('load'`. May load via `analytics-loader.js` after bootstrap — **must verify in browser Network tab**.

---

### F-012 — ViewContent not embedded in PDP HTML (JS-only)

**Status:** [x] INFO · **Severity:** P2 · **Fix required:** no (process/architecture note)

- [ ] **Open** · Severity: **P2** · Area: **PIXEL / CRO** · Checklist: PIX-003, CRO-004

| Field | Value |
|-------|--------|
| Status (B) | SUSPECTED (expected architecture) |
| Status (C) | |

Sample PDPs: no `ViewContent` / `view_item` string in HTML; product-detail.js is the intended path. **Not a bug until browser proves events missing.** Pass C: Meta Pixel Helper on PDP.

---

### F-013 — Category titles vs H1 length strategy inconsistent

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **SEO** · Checklist: SEO-031

Related to F-001: H1s are long complete sentences; titles are shorter and cut. May be intentional length limit with bad truncation.

---

### F-014 — Sitemap product `lastmod` clustered 2026-06-11

**Status:** [ ] OPEN · **Severity:** P3 · **Fix required:** YES

- [ ] **Open** · Severity: **P3** · Area: **SEO** · Checklist: SEO-067

Products/variants/categories lastmod in index point to **2026-06-11** while blog newer (2026-06-29). Possible stale lastmod pipeline — Google may under-crawl updates.

---

### F-015 — `manifest.webmanifest` 404 while `site.webmanifest` 200

**Status:** [ ] OPEN · **Severity:** P3 · **Fix required:** YES

- [ ] **Open** · Severity: **P3** · Area: **TECH** · Checklist: PG-088

Only an issue if some code references the wrong path. `site.webmanifest` OK.

---

## False positives / intentional (verified)

| Item | Why not a bug |
|------|----------------|
| Contacts meta description “Зв” only | **False parse:** apostrophe in `Зв'яжіться` broke naive regex `[^'\"]*`. Real content length **101**, full sentence. |
| Cart short title | Expected transactional page; **noindex**. |
| Search noindex | `noindex, follow` correct. |
| Favorites/QR noindex | Correct. |
| www → apex 301 | Correct single-host policy. |
| Legacy `/about/`, `/page/2/`, `/news/` 301 | Working as designed. |
| Sitemap “275 bad” on fast crawl | **429 rate limit**, not 404 — revalidated slowly. |

---

## Suspicious / needs re-check (next Pass A continuation)

| Item | Why | Next |
|------|-----|------|
| All 65 products title length + duplicates | only 22 sampled | slow batch |
| All variant URLs 178 | not fully statused after 429 | slow HEAD |
| Order UTM linkage % | needs MySQL | SSH read-only counts |
| Dispatcher funnel stats | needs admin auth | login + screenshot numbers |
| ATC + Purchase pixel + CAPI | needs browser + test pay | Meta EM test events |
| content_ids vs g:id Cyrillic | feed uses ЧОРНИЙ | compare JS payload |
| Mini-cart after real ATC | only empty state tested | browser |
| Recommended product 404s | home links present; not all HEAD | list unique + HEAD |
| Server logs / Telegram alerts | needs SSH | error classes only |
| EN/RU product SEO full | sample only catalog/home | expand |

---

## Tech debt / refactor smells (observe only)

| Smell | Path | Risk if touched |
|-------|------|-----------------|
| Dual modular/legacy views | `storefront/urls.py` `_legacy_view` | high |
| Feed link building + escaping | merchant feed generator | high (F-003) |
| Color SEO template morphology | color_seo_copy | medium |
| Category title truncation | category SEO meta | low–medium |
| i18n H1 not translated | templates home/catalog | medium |
| Pixel dual init paths | base.html + analytics-loader | medium (dedupe) |

---

## Telegram / alert noise

| Alert type | This session |
|------------|--------------|
| Not inspected on server | BLOCKED pending SSH log review |
| Hypothesis: favicon/PWA | F-009 / F-015 may relate to “icon” reports |

---

## Detailed evidence appendix

### A. Smoke HTTP (2026-07-09)

| URL | Status | Notes |
|-----|--------|-------|
| `/` | 200 | ~286KB, title OK 59 chars |
| `/catalog/` | 200 | title OK |
| `/cart/` | 200 | noindex |
| `/robots.txt` | 200 | disallows admin, utm query patterns, AdsBot rules |
| `/sitemap.xml` | 200 | 8 children |
| `/healthz/` | 200 | `{"status":"ok","service":"twocomms",...}` |
| `/contacts/` | 200 | (prior 500 fixed) |
| `www` | 301 → apex | |

### B. Public analytics IDs in HTML

- Meta Pixel: `823958313630148`
- GTM: `GTM-PRLLBF9H`
- TikTok attr: `D43L7DBC77UA61AHLTVG`
- fbq init count: 1; PageView track count: 1 (home)

### C. UTM first-touch (cookie names only)

- `twc_vid` — visitor id  
- `twc_ft` — JSON first touch with utm_source=instagram, utm_medium=paid_social, utm_campaign=qa_audit_20260709, utm_content=pass_a, fbclid=TEST_…

### D. Merchant feed

- URL: `/google-merchant-feed.xml` and `/media/google-merchant-v3.xml` both 200, ~2.1MB  
- Sample ids: `TC-0106-ЧОРНИЙ-S` …  
- Link mangling: see F-003  

---

## Follow-ups after Pass C only (not now)

- [ ] Fix category title truncation (F-001)  
- [ ] Fix color landing copy (F-002)  
- [ ] Fix merchant feed links + ID parity (F-003) — **careful**  
- [ ] Align product title/H1 (F-004)  
- [ ] Translate RU/EN H1 (F-005)  
- [ ] Dedupe color sitemap (F-006)  
- [ ] Review 429 policy (F-007)  

---



---

## FINAL PASS A STATUS (2026-07-09 end-of-pass)

### What “canary outside excluded IP” means (plain language)

A **canary** is a synthetic test visit with UTM tags (`?utm_source=…`) so we can see if the server saves a `UTMSession` row.

Your home IP **`188.163.49.54`** is in **AnalyticsExclusion** (note: «дом»). From that IP the site **intentionally does not write** UTM/UserAction analytics. So tests from home look “broken” even when the system works for real customers.

We re-ran the canary **from the production server egress IP `195.191.24.169`** (not excluded). Result: **UTM capture WORKS**.

### Server canary result (PASS for capture)

| Check | Result |
|-------|--------|
| Land with `utm_source=ig` | `UTMSession` created |
| Normalization | `ig` → **`instagram`** |
| `fbclid` stored | yes |
| `sessionid` on land (non-excluded) | **yes** (Set-Cookie on first HTML) |
| ATC + product_view linked to that UTMSession | **yes** (1 ATC, 1 product_view) |

⇒ **F-038 revised:** “no sessionid on land” was largely an **excluded-IP / auditor-path artifact**. For non-excluded traffic, session + UTMSession are created on lander.

### Order attribution (still FAIL — F-021 reinforced)

| Metric | Value |
|--------|------:|
| Orders total | 43 |
| empty `utm_source` | **43 (100%)** |
| with `utm_session_id` | **0** |
| `source=web` | 36 |
| web with `session_key` | **7 only** |
| web empty `session_key` | **29** |
| order session_keys matching any UTMSession | **0** |
| `online_full` empty utm | 23/23 |
| UTMSession total / instagram | 1043 / 132 |
| `is_converted=True` | **0** |

Example organic web order with session but no UTM (expected for non-ads):  
`TWC14062026N01` SiteSession.first_touch `referrer=google.com`, landing `/ru/` — no utm_*.

**CAPI-ish tracking still partially present:** 29/40 recent orders have `payment_payload.tracking` with `fbp` (sometimes `fbc`) + `external_id` + IP/UA — so Meta click IDs can work even when internal UTM fields are empty.

**Interpretation for ads:** UTM **ingest works** (server canary). **Order linkage historically never succeeded** (0/43). Either buyers never came via UTM, or `link_order_to_utm` / session_key persistence fails at checkout (29/36 web orders lack session_key). **Must fix before trusting ROAS in Dispatcher.**

### Full sitemap crawl (PASS)

**489/489** unique sitemap URLs → HTTP **200** (slow crawl, Chrome UA). No hard 404 in sitemap.

### Other closed/opened this end-pass

| ID | Status | Note |
|----|--------|------|
| F-043 | OPEN P1 | `/help-center/` → **404** (should 301 → `/dopomoga/` like docs suggested) |
| F-044 | OPEN P1 | Most web orders missing `session_key` (29/36) |
| F-045 | OPEN P0 | 0 order.session_key ∈ UTMSession despite 132 IG sessions |
| F-046 | PASS | Server canary UTM+ATC+normalize |
| SEO-062 | PASS | full sitemap 489 OK |
| F-001/F-002/F-004/F-005 | still OPEN | SEO quality |
| F-003/F-027 | still OPEN | feed color drop |
| F-029/F-030/F-031 | still OPEN | capacity + pixel JS + MySQL |

### Ads launch gate (final Pass A)

# **BLOCKED**

**P0 before paid ads scale:**

1. **F-021 / F-044 / F-045** — order UTM/session linkage  
2. **F-019** — is_converted dead  
3. **F-030** — pixel BFCache JS error  
4. **F-029** — LSAPI children limit  
5. **F-003** — Merchant feed landing/color  

**P1 SEO (can fix in parallel):** F-001 category titles, F-002 color grammar, F-004 title/H1, F-005 H1 i18n, F-043 help-center 404.

### Pass A coverage (honest end)

| Block | ~Done |
|-------|------:|
| 0 Smoke | 95% |
| 1 Page inventory | 95% |
| 2 SEO deep | 85% |
| 3 GEO | 60% |
| 4 CRO | 80% |
| 5 CART | 85% (NP/mono validation; no paid order) |
| 6 UTM | 90% (capture OK multi-canary; order link FAIL) |
| 7 Pixel | 65% (no Meta EM browser; JS bugs found) |
| 8 TECH | 80% |
| 9 FEED | 75% |
| 10 DB | 90% |
| 11 ADS | 50% (gate BLOCKED) |
| 12 Devices | 25% (Chrome UA automated only) |

### What Pass C should re-check first

1. Reproduce order create with UTM from non-excluded IP → Order.utm_* non-null  
2. Confirm F-030 in Safari BFCache  
3. Feed decoded link color behavior  
4. help-center 404  
5. LSAPI saturation under load  

### What we did NOT do (explicit)

- No production code fixes  
- No real customer charge / live paid order  
- No Meta Events Manager UI login  
- No Dispatcher UI (auth) screenshots  
- No password/secrets written to git  

---

## Sign-off

| Role | Name | Date |
|------|------|------|
| Pass B | agent-pass-a | 2026-07-09 |
| Pass C | | |
| Ads gate owner | | |

---

### F-016 — Variant URL titles work (positive control)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **Not a bug** · Area: **SEO** · Checklist: SEO-007, SMK-005 partial

Sample **20/20** variant sitemap URLs returned **200** with titles reflecting color/fit, e.g.:

- `/product/classic-tshirt/black/` → `Футболка класична — чорний — TwoComms`  
- `/product/classic-tshirt/black/oversize/` → `… — чорний, оверсайз фіт — TwoComms`  

**Note:** some titles may exceed 65 when both color+fit appended (truncation in SERP only) — minor P3 follow-up.

---

### F-017 — HTML site map (`/mapa-saytu/`) internal links all 200

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Checklist: SEO-071, PG-044

53 unique internal links from mapa page checked slowly → **0 non-200**.

---



### F-018 — Cart `offer_id` color spelling splits (ЧОРНИЙ vs ЧЕРНЫЙ)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **PIXEL / FEED / CART** · Checklist: PIX-011, FEED-002

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence (POST `/cart/add/` 2026-07-09):**

| Payload | offer_id returned |
|---------|-------------------|
| product_id=1, size=M, **color_variant_id=29** | `TC-0001-ЧОРНИЙ-M` (UK spelling) |
| product_id=1, size=M, **no color_variant_id** | `TC-0001-ЧЕРНЫЙ-M` (RU spelling) |

Feed ids use **ЧОРНИЙ** style. If pixel fires without color_variant_id, Meta catalog match **breaks**.

**Why problem:** content_id fragmentation → broken DPA/optimization.  
**Risk of fix:** medium–high (ID generator + historical feed).

---

### F-019 — `UTMSession.is_converted` is always false (dead field)

**Status:** [x] FIXED (`34275e28`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-10):** an isolated attribution canary rebuilt
the UTM row from first-touch, linked its lead `UserAction`, and produced
`is_converted=True`, `conversion_type=lead`. All canary rows were then explicitly
deleted and verified absent. Historical sessions are intentionally not rewritten.

- [x] **Fixed** · Severity: **P0** · Area: **UTM / CRO** · Checklist: UTM-023, CRO-012

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB) |
| Status (C) | |

**Evidence:** `UTMSession.objects.filter(is_converted=True).count() == 0` while `utm_total == 1041`.  
30d converted also 0 despite purchases/leads existing in `UserAction` and paid orders.

**Why problem:** Dispatcher conversion stats and any is_converted-based reporting are **wrong**. Ads optimization based on internal conversion flags will undercount.

**Likely code:** `utm_tracking.mark_as_converted` / `record_order_action` not called on all order paths, or fails silently.

**Risk of fix:** medium (must not double-mark; test all pay paths).

---

### F-020 — UTM source normalization incomplete in stored sessions

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **UTM** · Checklist: UTM-001, UTM-004

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB 30d) |
| Status (C) | |

Despite `UTM_GOVERNANCE.md` + `normalize_utm_source()`, live distinct sources include:

- `chatgpt.com` (113) **and** `chatgpt` (4)  
- `ig` (14) **and** `instagram` (3) **and** `IGShopping` (1)  
- `audit` (3) — test pollution  
- `threads` (2)

**Why problem:** Dispatcher fragments channels; CBO/creative reports unreliable; IG ads under `ig` not rolled into `instagram`.

**Risk of fix:** medium (middleware + optional backfill after backup).

---

### F-021 — 100% of recent orders have empty UTM attribution

**Status:** [x] FIXED (`34275e28`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-10):** a production MySQL canary with only
first-touch attribution produced `Order.utm_source=instagram`, medium
`paid_social`, campaign `production_canary`, and a non-null `utm_session_id`.
The canary was explicitly removed and zero matching rows remain. The fix applies
prospectively; unattributable historical orders were not assigned invented UTM.

- [x] **Fixed** · Severity: **P0** · Area: **UTM / ADS** · Checklist: UTM-020–024, ADS-015, DB-009

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB) |
| Status (C) | |

**Evidence:**

- Orders 7d: 2/2 empty `utm_source`, **0** `utm_session_id`  
- Orders 30d: 6/6 empty  
- Orders 90d: 12/12 empty  

Sample order numbers (public business ids, not secrets): `TWC06072026N02`, `TWC06072026N01`, `TWC23062026N02`, …

**Why problem:** **Cannot attribute Instagram/Meta spend to revenue** in internal analytics. Highest-priority ads blocker with F-019.

**Likely causes to verify in Pass C (no fix now):**

1. `link_order_to_utm` not invoked on create/Mono/COD paths  
2. Session key / visitor_id mismatch at checkout  
3. Orders created from admin/manual/Telegram without web session  
4. Cookie/session not surviving checkout  

**Risk of fix:** high on money path — needs careful tests; do not rush.

---

### F-022 — Extreme funnel cliff product_view → add_to_cart (+ possible PV noise)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **CRO** · Checklist: CRO-020–026

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (counts) |
| Status (C) | |

30d: **21725** product_view events vs **25** add_to_cart (~0.12%).  
7d: 7368 product_view across only **8** utm_sessions with that action → **~921 events/session** average — strongly suggests **bots, double-firing, or missing dedupe** (despite code comments about 30min product_view dedupe).

**Why problem:** CRO dashboards useless; Meta may also see inflated ViewContent if mirrored; hides real PDP friction.

**Pass C:** compare UserAction vs Meta ViewContent; check bot filter; verify dedupe window.

---

### F-023 — Category truncated titles stored in MySQL (root cause of F-001)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **SEO** · Checklist: SEO-003, DB-001

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB) |
| Status (C) | |

```text
Category long-sleeve.seo_title = 'Лонгсліви TwoComms — лаконічний стрітвеар з рукавами на'
Category tshirts.seo_title     = 'Футболки TwoComms — стрітвеар та мілітарі-принти від'
Category hoodie.seo_title      = 'Худі TwoComms — теплі толстовки зі стрітвеар-принтами та'
```

Truncation is **in DB content**, not only template. Fix = data + generator.

---

### F-024 — ATC API + mini-cart path works (positive control)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Area: **CART** · Checklist: CART-002, SMK-006 partial, CART-003

`POST /cart/add/` with CSRF from `/api/bootstrap/` returns ok; `/cart/count/` and mini-cart HTML update with product row and offer_id.  
Browser UI click still recommended for Pass C.

---

### F-025 — Blog UK sitemap URLs healthy

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · 15 UK blog URLs from sitemap-blog → 200, title+H1 present.

---

### F-026 — Home critical static assets 200

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · 21 critical JS/CSS/preview assets from homepage → all 200.

---

### F-027 — Feed color lost even after correct XML decode (clarifies F-003)

**Status:** [x] FIXED (`4d72412a`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-11):** explicit color variants now use the
stored `ProductColorVariant.slug` followed by size in the canonical PDP path.
The live feed/link sample proves color is no longer discarded by a redirect.

- [x] **Fixed** · Severity: **P0** · (sub-finding of F-003) · Area: **FEED**

Decoded `?size=S&color=...` → final `.../s/` without color. Server routing treats `size` as variant slug and ignores/drops color query. Merchant color variants may all collapse to same default-color size page.

---

### F-028 — RU/EN PDP titles often OK while UK title/H1 diverge

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **GEO/SEO**

Sample 8 products × ru/en: titles/H1 generally **aligned within locale**, but EN sometimes keeps internal English print codes (`death grabs ass`) while RU uses commercial name (`Сердце И Деньги`). Cross-locale naming strategy inconsistent with UK mismatches (F-004).

---



### F-029 — LiteSpeed `LSAPI_CHILDREN` process limit hit (capacity)

**Status:** [x] FIXED (production ops 2026-07-11) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-11):** CloudLinux Python Selector app env
now has `LSAPI_CHILDREN=10` (previous default: 6), and every active TwoComms
`lswsgi` process reports the new value. Three rounds of ten concurrent dynamic
`/healthz/` requests returned **30/30 HTTP 200**, maximum duration 1.9 seconds,
with **zero** new `Reached max children process limit` records. A pre-change
selector config backup is retained on the server.

- [x] **Fixed** · Severity: **P0** · Area: **TECH** · Checklist: TECH-060, TECH-073, TECH-076

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (stderr.log) |
| Status (C) | |

**Evidence (prod `stderr.log` recent window):** message repeated **200+** times:

```text
Reached max children process limit: 6, extra: 2, current: 8, busy: 7/8, please increase LSAPI_CHILDREN.
```

**Why problem**

- Worker pool too small for concurrent traffic + background jobs.  
- Explains intermittent slowness, timeouts, possible 5xx/429 under ads load.  
- Correlates with “site falls / hangs without clean restart” reports.

**Fix direction (later):** raise LSAPI_CHILDREN / PHP-LSAPI / app workers carefully; separate cron/heavy jobs from request workers; load test.

**Risk of fix:** medium (hosting limits, memory). **Do not change without capacity plan.**

---

### F-030 — `initializePixelsImmediately is not defined` (analytics-loader bug)

**Status:** [x] FIXED (`3291ac82`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-10):** BFCache restore now calls the defined,
idempotent `initializePixelsDeferred`. The regression test passes on the server,
`collectstatic` and `compress --force` completed, and the live hashed asset
`analytics-loader.43cce70b789d.js?v=8` contains the corrected call and no
`initializePixelsImmediately()` reference. Production health returned 200.

- [x] **Fixed** · Severity: **P0** · Area: **PIXEL / TECH** · Checklist: PIX-001–003, TECH-064

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence**

1. Prod `client_errors.log` top message (8/9 lines):  
   `Uncaught ReferenceError: initializePixelsImmediately is not defined`  
   URLs include `/catalog/tshirts/`, `/ru/catalog/...`, homepage devices.

2. Source: `analytics-loader.js` (hashed `analytics-loader.3975317011e4.js?v=8`) line ~1484 calls `initializePixelsImmediately()` on BFCache `pageshow` restore.

3. Function **`initializePixelsDeferred` exists**; **`initializePixelsImmediately` is never defined** in the same file (local repo + prod hashed bundle).

**Why problem**

- BFCache back/forward (common on mobile Instagram in-app browser) throws.  
- Pixel re-init on restore fails → possible missed PageView/events after navigation.  
- Matches user “Telegram alerts / something wrong with scripts/icons” class of frontend failures (client_errors path; not always Telegram).

**Fix direction:** rename call to `initializePixelsDeferred` or implement `initializePixelsImmediately` as alias; add regression test.

**Risk of fix:** low–medium (pixel init only); test IG in-app + Safari BFCache.

---

### F-031 — MySQL “server has gone away” / connection errors in django logs

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **TECH** · Checklist: TECH-063, TECH-060

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (django.log tail window) |
| Status (C) | |

**Evidence classification (last ~400KB django.log):**

| Class | approx count |
|-------|-------------:|
| MySQL server has gone away | 79 |
| Connection* | 57 |
| OperationalError | 36 |

**Why problem:** dropped DB connections under load/idle timeout → request failures, partial writes (could contribute to missing UTM/order links if exception swallowed).

**Risk of fix:** medium (CONN_MAX_AGE, wait_timeout, pool) — ops change.

---

### F-032 — UserAction almost never linked to UTMSession (99.8% product_view)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **UTM / CRO** · Checklist: UTM-003, CRO-020, DB-011

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB 30d) |
| Status (C) | |

| action_type | events 30d | % without utm_session |
|-------------|----------:|----------------------:|
| product_view | 21708 | **99.8%** |
| add_to_cart | 25 | **76%** |
| initiate_checkout | 2 | **100%** |
| lead | 2 | **100%** |
| purchase | 1 | **100%** |

**Code note (read-only):** `record_user_action` only attaches existing `UTMSession` by `session_key`; does **not** create one for organic traffic. UTMSession is primarily first-touch with UTM/platform ids. That explains many nulls for organic PV — **but** IC/lead/purchase at 100% null + orders 100% empty (F-021) means **even attributed paths fail** to bind.

**Recent lead order 276** has UserAction lead/initiate_checkout with `utm_sess None` at same second as order create.

**Risk of fix:** high — redesign session binding carefully.

---

### F-033 — `link_order_to_utm` exists in code but attribution still empty in prod

**Status:** [x] FIXED (`34275e28`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-10):** the live `link_order_to_utm` path was
executed against production MySQL with a unique rollback-canary identity and
successfully persisted normalized order UTM plus the FK. Cleanup was explicit
and verified at zero remaining rows.

- [x] **Fixed** · Severity: **P0** · Area: **UTM** · Checklist: UTM-020, CART-042 (related)

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (code + DB) |
| Status (C) | |

**Code map:**

- `storefront/views/checkout.py` → `link_order_to_utm(request, order)`  
- `storefront/views/monobank.py` → `link_order_to_utm(request, order)`  
- Resolver: `resolve_utm_session` via session_key / visitor_id / session `utm_data`

**Prod reality:** all recent orders `utm_source=None`, `utm_session_id=None`.  
Some orders have `sale_source` like `Kasta`, `AIO`, `Знайомі` (manual/offline) — those **should not** have web UTM — but **online_full** web orders still empty.

**Pass C must:** create test web order from UTM landing and inspect Order row immediately.

---

### F-034 — Variant sitemap sample healthy + recs links OK

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Checklist: SEO-065, SEO-074

- Variant sitemap: 178 URLs; sample every 3rd → **60/60 HTTP 200**  
- PDP internal product links (15) → 0 bad  
- Home product links (8) → 0 bad  

---

### F-035 — CSP violations present in stderr

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **TECH** · Checklist: TECH-082

`stderr.log` contains repeated `csp_violation` (~13 in sample window). May block third-party pixels/scripts intermittently. Pass C: capture blocked URI list.

---

### F-036 — Telegram admin notify intermittent disconnect

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **TECH**

```text
Exception in send_message to admin ... RemoteDisconnected('Remote end closed connection without response')
```

Can cause “Telegram alert missing” perception without site downtime.

---



### F-037 — Audit home IP is AnalyticsExclusion (canaries invalid from this network)

**Status:** [x] INFO · **Severity:** P2 · **Fix required:** no (process/architecture note)

- [ ] **Open** · Severity: **P1** · Area: **UTM / PROCESS** · Checklist: UTM-010, UTM-050

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence:** prod `AnalyticsExclusion` active entry:

- kind=IP, value=`188.163.49.54`, note=`дом`, is_active=True

`is_request_excluded()` short-circuits **UTMTrackingMiddleware writes** and **UserAction** recording for this IP.

**Effects observed during Pass A from this network:**

- `twc_ft` / `twc_vid` cookies still set (identity middleware)  
- **Zero** `UTMSession` rows for all `qa_*` canary campaigns  
- `/api/track-event/` returns `success:true` but **`stored:false`**

**Why it matters:** staff exclusion is correct for analytics hygiene, but:

1. Pass A/C canaries **must use non-excluded network** (or temporary disable) to validate UTM→Order.  
2. Does **not** excuse F-021 (real customer orders also empty UTM).

**Risk of fix:** low (process); do not delete exclusion without owner OK.

---

### F-038 — `sessionid` not issued on UTM landing GET; only after cart POST

**Status:** [x] REVISED · **Severity:** P2 · **Fix required:** no (see later findings)

- [ ] **Open** · Severity: **P0** · Area: **UTM / CART** · Checklist: UTM-003, UTM-007, UTM-022

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence (Chrome UA, 2026-07-09):**

| Step | Set-Cookie |
|------|------------|
| GET `/?utm_source=instagram&...` | `twc_vid`, `twc_ft` only |
| GET `/api/bootstrap/` | `csrftoken` |
| POST `/cart/add/` | **`sessionid=...` first appears** |

`UTMSession` creation requires Django `session_key` (`utm_middleware._create_or_update_utm_session`).  
First-touch UTM lives in **HttpOnly `twc_ft`**, but `link_order_to_utm` primary path uses `UTMSession` / `session['utm_data']`, not a full parse of `twc_ft` for utm_source/medium/campaign (fbclid is read from first_touch in tracking context).

**Why problem (architecture):**

- Ad click lander often is **GET only** (bounce without ATC).  
- If session cookie is not established on that GET, UTMSession may never bind.  
- Even with later ATC session, first-touch UTM may stay only in `twc_ft` while order linkage looks for UTMSession → **empty Order.utm_*** (amplifies F-021).  
- Combined with page-cache goals (avoid Set-Cookie on HTML), this is a deliberate tension between performance and attribution.

**Pass C:** from non-excluded IP, DevTools: confirm whether real browsers get `sessionid` on first HTML response; if not, confirm whether `twc_ft` is copied into order on create.

**Risk of fix:** **HIGH** — changing session cookie policy affects LiteSpeed cache/TTFB. Needs careful design (e.g. write UTMSession by visitor_id from twc_vid without session cookie on land).

---

### F-039 — `/api/track-event/` reports success but often `stored:false`

**Status:** [x] REVISED · **Severity:** P2 · **Fix required:** no (see later findings)

- [ ] **Open** · Severity: **P1** · Area: **UTM / PIXEL adjacency** · Checklist: PG-084

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

Payload with `event_type=product_view|add_to_cart` → HTTP 200 `{"success":true,"stored":false}`.

`stored` is `bool(action)` from `record_user_action`, which returns `None` when excluded/bot/no session/dedup.

On excluded IP always false. On real users: if client relies on this endpoint for funnel, silent drop is dangerous.

---

### F-040 — Checkout is JS/Mono-driven, not classic form POST to `/orders/create/`

**Status:** [x] INFO · **Severity:** P3 · **Fix required:** no (process/architecture note)

- [x] **INFO / PASS path map** · Area: **CART**

Cart HTML has fields `full_name`, `phone`, `email`, `pay_type`, NP refs, and `/checkout/capture/`.  
No traditional `<form action="/orders/create/">`. Runtime uses **`modules/checkout-mono.js`** (dynamic import from main.js) + monobank endpoints.

COD path still in `checkout.py` `order_create` with `link_order_to_utm` + `record_order_action('lead')`.  
Online path: `monobank.py` `link_order_to_utm`.

**success-preview** redirects to Django admin login (not public).

---

### F-041 — CSP allows Meta/TikTok/GTM hosts (positive); report-uri `/csp-report/`

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS partial** · Checklist: TECH-082

`Content-Security-Policy` includes `connect.facebook.net`, `www.facebook.com`, `analytics.tiktok.com`, `googletagmanager.com`, etc.  
`report-uri /csp-report/` explains stderr `csp_violation` noise (F-035) — need sample blocked URIs in Pass C.

---

### F-042 — Early Meta pixel still inlined in HTML (PageView path exists)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS partial** · Checklist: PIX-002

Catalog/home HTML contains inline `fbq('init')` + `PageView` (comments about ad attribution without interaction).  
Heavy events deferred to analytics-loader.  
BFCache bug F-030 still applies to loader reinit.

---



### F-043 — `/help-center/` returns 404 (dead alias)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **SEO** · Checklist: PG-039, SEO-046

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

`GET /help-center/` → **404**. Canonical help is `/dopomoga/`.  
If external links/docs still use help-center, link equity + UX break. Should be **301** to `/dopomoga/` (same pattern as `/about/` → `/pro-brand/`).

---

### F-044 — Most web orders have empty `session_key`

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **UTM / CART** · Checklist: UTM-020, CART-042

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED (prod DB) |
| Status (C) | |

`source=web` orders: **36** total; only **7** have `session_key`; **29** empty.  
Without session_key, `link_order_to_utm` / `record_order_action` cannot join UTMSession reliably.

---

### F-045 — Zero historical join Order.session_key → UTMSession

**Status:** [x] FIXED (`34275e28`) · **Severity:** P0 · **Fix required:** DONE

**Production verification (2026-07-10):** the canary proved
`Order.session_key == UTMSession.session_key` and `Order.utm_session_id` was
non-null. The generated rows were explicitly deleted and verified absent.
Historical rows without recoverable attribution remain unchanged.

- [x] **Fixed** · Severity: **P0** · Area: **UTM** · Checklist: UTM-020, DB-009

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

All order session_keys checked: **0** exist in `UTMSession`.  
Meanwhile **132** `utm_source=instagram` sessions exist. Capture works; **conversion attribution does not appear in Order rows**.

---

### F-046 — Server canary UTM capture PASS (positive control)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Area: **UTM**

From server IP `195.191.24.169`: land `utm_source=ig` → stored as **instagram**, campaign saved, fbclid saved, ATC+product_view linked to UTMSession.

---

### F-047 — Full sitemap URL inventory PASS (489/489)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Checklist: SEO-062

Slow Chrome-UA crawl of all unique sitemap locs: **ok=489, bad=0, 429=0**.

---

### F-048 — CAPI tracking payload often has fbp without internal UTM

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **PIXEL** · Checklist: PIX-020

29/40 recent orders have `payment_payload.tracking` keys including `fbp`, `external_id`, IP, UA (sometimes `fbc`). Internal UTM fields still empty. Meta may attribute; **Dispatcher/UTM reports will not**.

---



### F-049 — Home IP unexclude retest PASS (2026-07-09 ~17:14 UTC)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Area: **UTM** · Related: F-037, F-038, F-046

**Setup:** `AnalyticsExclusion` for `188.163.49.54` set `is_active=False` (owner). Active exclusions count = **0**.

**Canary from home network:**

| Step | Result |
|------|--------|
| GET `/?utm_source=ig&utm_medium=paid_social&utm_campaign=qa_home_after_unexclude_*&fbclid=…` | 200 |
| Set-Cookie on land | `twc_vid`, `twc_ft`, **`sessionid`** |
| Normalize | `ig` → **`instagram`** in UTMSession |
| `utm_medium` / `utm_campaign` / `utm_content` / `fbclid` | all stored |
| POST `/cart/add/` | ok, offer_id `TC-0001-ЧОРНИЙ-M` |
| `/api/track-event/` product_view | **`stored: true`** |
| UserAction on UTMSession | product_view×1, add_to_cart×1 |

**Conclusion:** With exclusion off, home-network traffic is tracked the same as server canary (F-046).  
**F-037** remains valid as a process note (when exclusion is ON, staff tests lie).  
**F-021 / order linkage** still open — capture ≠ order attribution.



### F-050 — Nova Poshta city search: Latin `Kyiv` → 502, Ukrainian `Київ` → 200

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **CART** · Checklist: CART-024

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

`GET /cart/delivery/cities/?q=Kyiv` → **502** `{"ok":false,"error":"Не вдалося отримати список міст…"}`  
`GET /cart/delivery/cities/?q=Київ` → **200** with items.

**Impact:** users typing Latin city names may hit hard API failure during checkout. Ads traffic often mixed UA/EN keyboards.

**Risk of fix:** medium (NP API params / transliteration layer).

---

### F-051 — Checkout capture empty payload returns 200 ok (soft)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

- [ ] **Open** · Severity: **P2** · Area: **CART** · Checklist: CART-054

`POST /checkout/capture/` with empty phone/name → **200** `{"ok": true}` (no validation error). May intentionally save abandoned-cart lead; confirm it does not create bogus orders (did not create order in smoke).

---

### F-052 — Mono create-invoice validates city (PASS behavior)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Incomplete mono payload without city → **400** with clear UA error «Оберіть місто зі списку Нової пошти.»

---

### F-053 — Full home internal links 42/42 HTTP 200

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · Checklist: SEO-073

---

### F-054 — Blog UK 15/15 + color landings 4/4 HTTP 200 (grammar still F-002)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** HTTP; SEO copy still broken on color titles (F-002).

---

### F-055 — RU/EN product sample (17×2) title/H1 quote mismatch 0; EN H1 no Cyrillic

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** for sampled locales product titles alignment (UK mismatch F-004 remains UK-only issue family).

---

### F-056 — IGShopping / multi-hop UTM canary after unexclude (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · `utm_source=IGShopping` → stored **instagram**; `utm_term=term1` kept on `qa_full_1783617416`; multi-page hop keeps session; ATC + product_view + initiate_checkout via API **stored:true**.

---

### F-057 — Historical utm_source still heavily dirty (all-time)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

- [ ] **Open** · Severity: **P1** · Area: **UTM** · Checklist: UTM-004, UTM-026, DB-008

All-time top sources still include unnormalized: `ig` (200), `Instagram` (135), `chatgpt.com` (122), `fb` (19), `Inst_Vid` (10), `IGShopping` (6).  
**New** canaries normalize correctly → dirt is **historical + possible old code paths**, not current normalize function (which works).

Backfill optional after backup (UTM_GOVERNANCE).

---

### F-058 — Scripts matrix key pages PASS (critical assets 200)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- [x] **PASS** · home/catalog/PDP/cart/custom-print/blog load main + analytics-loader + ui-fallback + rum; PDP loads product-detail; modules `checkout-mono.js`, `cart.js`, `shared.js` return 200.

---



### F-059 — All ProductImage.alt_text empty in production DB (36/36)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

| Field | Value |
|-------|--------|
| Status (B) | REPRODUCED |
| Status (C) | |

**Evidence:** `ProductImage.objects` count=36, `alt_text` empty/null for **all 36**.  
HTML may still show generated alts on some `<img>` (e.g. product title), but DB alt field unused → SEO/a11y gap for many images (empty alts observed on PDP: 4/17 empty in sample).

**Checklist:** SEO-081  
**Risk of fix:** low (content/backfill).

---

### F-060 — Cart qty update works with `cart_key` (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

`POST /cart/update/` with `cart_key=1:M:29:classic&qty=3` → 200, line_total 2364, cart_count 3.  
(Earlier 404 was wrong param name `key` instead of `cart_key` — not a product bug.)

---

### F-061 — Cart remove works (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

`POST /cart/remove/` with `key=1:M:29:classic` → 200, count 0.

---

### F-062 — Promo validation works (PASS for invalid codes)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

`POST /cart/apply-promo/` with `promo_code=INVALIDXYZ` → 404 «Промокод не знайдено».  
Field name must be `promo_code` (not `code`). Guest may need auth for real codes (not fully tested with valid code).

---

### F-063 — NP warehouses OK with `settlement_ref` (PASS); `city_ref` alone may return empty

**Status:** [x] PASS with note · **Severity:** P2 · **Fix required:** optional UX

Cities UA query returns `settlement_ref` + `city_ref`.  
Warehouses with **settlement_ref** → items list OK.  
Warehouses with only **city_ref** for Kyiv → `items: []` (empty). Front must send correct ref type.

Related: F-050 Latin city 502 still open.

---

### F-064 — Favorites toggle works (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

`POST /favorites/toggle/1/` → success is_favorite true; `/favorites/count/` → 1.

---

### F-065 — Custom 404 page quality (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

Unknown product URL → HTTP 404, title `404 — Щось пішло не так`, **noindex**, link to home present.

---

### F-066 — BlogPosting JSON-LD present on posts (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

Sample post includes `"@type":"BlogPosting"`.

---

### F-067 — `load-more-products` works (PASS)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

`GET /load-more-products/?page=2` → 200 HTML product cards JSON wrapper.

---

### F-068 — Web `prepay_200` orders: 19/19 missing session_key

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

Prod breakdown (re-verified 2026-07-09 late):

| source | pay_type | total | empty session_key | empty utm |
|--------|----------|------:|------------------:|----------:|
| web | prepay_200 | 19 | **19** | 19 |
| web | online_full | 16 | **9** | 16 |
| web | cod | 1 | 1 | 1 |
| manual | online_full | 7 | 7 | 7 |

**Timeline nuance:** all online_full with `session_key` are **≥ 2026-05-22** (ids 261, 269, 271, 276). All prepay are **≤ 2026-03-22**. Many prepay still have `tracking.external_id=session:…` (F-073) → session existed but Order field not written historically. No post-May prepay to prove current path. Strengthens F-021/F-044.

---

### F-069 — AnalyticsExclusion «дом» re-enabled again

**Status:** [x] INFO · **Severity:** P2 · **Fix required:** process only

As of re-check: `is_active=True` for `188.163.49.54` again.  
Home canaries again may not write UTMSession (land without sessionid observed). Owner toggles expected; document for testers.

---

### F-070 — Promo field must be `promo_code` (INFO for front contract)

**Status:** [x] INFO · **Severity:** P3 · **Fix required:** no if front correct

Sending `code=` yields «Введіть промокод»; `promo_code=` yields proper not-found. Front must use correct field (verify live form uses `promo_code`).

---

### F-071 — `link_order_to_utm` ignores `analytics_first_touch_data` (ROOT CAUSE of empty Order UTM)

**Status:** [x] FIXED (`34275e28`) · **Severity:** P0 · **Fix required:** DONE
**Area:** UTM / checkout · **Checklist:** UTM-*, ADS-*, DB-order-attr  
**Related:** F-021, F-033, F-019, F-045

**Production verification (2026-07-10):** `link_order_to_utm` now rebuilds and
links a durable `UTMSession` from session UTM or `twc_ft` first-touch data,
normalizes the source, and preserves click IDs. The regression reproducing a
missing original UTMSession + missing `session['utm_data']` passes on the server;
the focused attribution suite is **6/6**, Django check passes, production HEAD is
`34275e28`, and `/healthz/` returns 200. Aggregate/historical children remain open
until a production UTM order proves the new-row counters.

#### Evidence (production MySQL + code read-only)

Order **#276** (`TWC06072026N02`, 2026-07-06, `source=web`, `pay_type=online_full`, `session_key` set):

| Layer | Result |
|-------|--------|
| `Order.utm_source` | **NULL** |
| `Order.utm_session_id` | **NULL** |
| `UserAction` lead metadata `first_touch` | `utm_source=audit`, `utm_medium=test`, `utm_campaign=funnel_check` |
| UTMSession by order.session_key | **missing** |
| SiteSession by order.session_key | **missing** |

Code path (`storefront/utm_tracking.py` `link_order_to_utm`):

1. resolve by `session_key` → UTMSession  
2. resolve by `visitor_id` → UTMSession  
3. fallback `request.session['utm_data']`  

**Does NOT** read `request.analytics_first_touch_data` / cookie `twc_ft`, even though:

- `record_user_action` / `record_order_action` **do** copy first_touch into UserAction.metadata  
- `build_order_tracking_context` **does** use first_touch for fbclid/ttclid/gclid  

So Meta click IDs can land in `payment_payload.tracking` while internal ROAS fields on Order stay empty.

#### Why this blocks ads

Dispatcher / order export that reads `Order.utm_*` will always show «direct/empty» even when first-touch cookie had real campaign params. Canaries proving UTMSession capture ≠ order attribution.

#### Fix direction (Pass D only — do not implement here)

- In `link_order_to_utm`, after session/visitor fallbacks, copy UTM from `analytics_first_touch_data` if present.  
- Prefer creating/linking UTMSession when first_touch has utm_source.  
- Call `mark_as_converted` only after successful link (F-019).  
- Paid canary order with UTM → assert Order.utm_source non-empty.

**Risk of fix:** medium (attribution rules / first-touch vs last-touch policy).

---

### F-072 — Historical recoverability: only 2/36 web orders join UTM via session external_id

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES (backfill optional; prevent future)

Full scan of `payment_payload.tracking.external_id` + `Order.session_key` against `UTMSession`:

| Result | Count |
|--------|------:|
| web orders | 36 |
| with `session:…` external_id | 27 |
| UTMSession match | **2** |
| Matched | order **232** `utm_source=ig` (dirty, pre-normalize); order **246** `google/cpc/pmax_cid23444801460` |

Even those 2 still have **empty Order.utm_*** today → attribution never written at create time (F-071/F-033).

---

### F-073 — Prepay era: session lived in tracking.external_id but Order.session_key empty

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES (verify current prepay path)

All **19** `prepay_200` web orders: `session_key` empty.  
Many still have `tracking.external_id = session:<key>` (session existed at invoice create).  
Examples: 209, 211, 232, 233, 240, 244, 246, 250, 252–257.

Since **2026-05** only `online_full` orders appear (4) and they **do** store `session_key` — suggests field write was fixed for mono path after ~May 2026, but **no post-March prepay** to re-verify prepay branch.

Code today (`monobank_create_invoice`): `_ensure_session_key` + `Order(..., session_key=request.session.session_key)` — should be OK **if** deployed; needs a paid prepay smoke after unexclude.

---

### F-074 — COD / `create_order` path does not `_ensure_session_key`

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES

Order **#275** (`TWC06072026N01`, pay_type=`cod`, source=web, name `AUDIT TEST…`):  
`session_key=NULL`, no `payment_payload.tracking`, no UTM.

`checkout.create_order` sets `session_key=request.session.session_key` but **never** calls session create helper (unlike monobank `_ensure_session_key`). Guest COD can create order with null session → breaks success-page session match + UTM link + capture convert.

---

### F-075 — CheckoutCapture.converted never true (0/4)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES

| id | session_key prefix | phone | converted |
|----|--------------------|-------|-----------|
| 2 | zt3ssxlsbmd6 | +380500… | False |
| 3 | givfox7xmu5v | +380976… | False |
| 4 | 72u4gmu78aso | +380631… | False |
| 5 | y3bvq5162u5d | empty | False |

Order **271** and **276** share session keys with captures 2 and 4 but `converted` stayed **False**.  
`create_order` marks converted; **monobank path** may not call the same CheckoutCapture update → abandoned-cart recovery can spam paid buyers.

---

### F-076 — product_view 41 283 vs ATC 61; ~96% PV without site_session

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES (dedupe already partial; quality still bad)  
**Related:** F-022, F-032

| Metric | Value |
|--------|------:|
| product_view | 41 283 |
| with site_session | 1 659 (~4%) |
| with utm_session | 92 (~0.2%) |
| add_to_cart | 61 |
| initiate_checkout | 7 |
| purchase UserAction | 3 |

Code has 30‑min product_view dedupe (W2-4) but historical + residual noise remains. Funnel dashboards using raw PV are misleading for ads creative decisions.

---

### F-077 — REVISED: product Merchant feed `g:link` landings work (narrows F-027)

**Status:** [x] REVISED / PASS for product PDP links · **Severity:** P2 note · **Fix required:** no for product query links

Live `https://twocomms.shop/google-merchant-feed.xml` (**384** items):

- XML correctly encodes `&amp;` in query strings (standard).  
- After HTML-unescape, sample links `?size=S&color=…` → **HTTP 200**, redirect to size path `/product/…/s/`, title includes size + color.  
- `g:id` all unique; **384/384** Cyrillic color tokens (e.g. `TC-0106-ЧОРНИЙ-S`).

**Still open separately:** color **category** landings grammar (F-002), duplicate sitemap color URLs (F-006), offer_id RU/UA black split (F-018). Do **not** treat product feed size/color query as broken after this recheck.

---

### F-078 — `/kontakty/` 404; real contacts URL is `/contacts/`

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES (301)

| URL | Status |
|-----|--------|
| `/kontakty/` | **404** |
| `/contacts/` | **200** «Контакти TwoComms…» |

Likely UA-slug expectation / old links. Add 301 → `/contacts/` (same family as F-043 `/help-center/` → `/dopomoga/`).

---

### F-079 — RECONFIRM F-030 still live in production client_errors

**Status:** [x] RECONFIRMED · **Severity:** P0 (parent F-030)

`client_errors.log` still contains **`initializePixelsImmediately is not defined`** (line 1484 of live `analytics-loader.3975317011e4.js`). Recent URLs: `/catalog/tshirts/`, `/ru/catalog/`, mobile + desktop Chrome. BFCache `pageshow` path still broken.

---

### F-080 — RECONFIRM F-031 MySQL «server has gone away»

**Status:** [x] RECONFIRMED · **Severity:** P1 (parent F-031)

`django.log` contains **565** occurrences of `MySQL server has gone away` / `OperationalError: (2006, …)`. Capacity/reliability risk under ads traffic with F-029.

---

### F-081 — Footer legal/support matrix PASS

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

From homepage footer, all primary support URLs **200**:  
`/contacts/`, `/dopomoga/`, `/faq/`, `/povernennya-ta-obmin/`, `/polityka-konfidentsiynosti/`, `/umovy-vykorystannya/`, `/pro-brand/`, `/mapa-saytu/`, `/blog/`, catalog tree, `/custom-print/`.

---

### F-082 — Feed g:id inventory PASS (no duplicates)

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

384 `g:id`, 384 unique, 0 duplicates. Cyrillic in IDs is intentional for UA catalog (note F-018 language split still open for black color synonyms).

---

### F-083 — `purchase` UserAction heavily undercounted vs paid orders

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES  
**Related:** F-019, F-021, F-033

| Metric | Count |
|--------|------:|
| Orders `payment_status` in paid/prepaid | **36** |
| UserAction `purchase` | **3** |
| UserAction `lead` | **6** |

Only a tiny fraction of successful payments create funnel actions → conversion reporting / `mark_as_converted` almost never runs.

**Detail:** purchase UserAction `order_id` set only for **{261, 269, 271}** (all recent monobank web `online_full`). **33/36** paid/prepaid orders have **no** purchase action — includes all `source=manual` (expected if no mono webhook) **and** older monobank web paid/prepay (e.g. 257, 255, 254) where `_apply_monobank_status` → `record_order_action('purchase')` either was not deployed yet or failed. `lead` actions exist for 6 orders (invoice create path).

---

### F-084 — Live dual AI sources: `chatgpt.com` vs `chatgpt` still written

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES  
**Related:** F-020, F-057

Last **3 days** UTMSession:

| utm_source | utm_medium | count |
|------------|------------|------:|
| chatgpt.com | NULL | 9 |
| chatgpt | ai | 6 |
| instagram | paid_social / social | 7 |
| ig | social | 1 (still appears) |

Canaries normalize `ig`→`instagram`, but production still receives **unnormalized** rows (`chatgpt.com` without medium, occasional `ig`). Either older code path, referrer detector returns bare host, or normalize not applied on AI-referrer branch consistently.

---

### F-085 — Home technical SEO tags PASS

**Status:** [x] PASS · **Severity:** P3 · **Fix required:** no

- hreflang: uk-UA / ru-UA / en-UA / x-default  
- canonical: `https://twocomms.shop/`  
- OG type/url/title/description/image present  
- `/healthz/` → 200 `{"status":"ok"}`  
- Home JSON-LD rich (WebSite, OnlineStore, BreadcrumbList, …)

---

### F-086 — Mild rate-limit recheck: 20× catalog all 200

**Status:** [x] PASS note · **Severity:** P3 · **Fix required:** no for mild load

20 sequential requests to `/catalog/tshirts/` → **0×429**. F-007 remains valid for **burst/crawl** (previously observed under full sitemap speed run), not for light traffic.

---

## Deep attribution root-cause note (2026-07-09 late pass)

```
Landing (+UTM)
  → twc_ft first-touch cookie          ✓ works (canaries)
  → utm_middleware → session['utm_data'] + UTMSession   ✓ when not excluded + session exists
  → UserAction.metadata.first_touch    ✓ often written
  → Order.utm_* via link_order_to_utm  ✗ fails if only first_touch (F-071)
  → UTMSession.is_converted            ✗ never (F-019) because mark_as_converted needs utm_session on purchase
  → payment_payload.tracking fbp/fbc   ✓ often present without internal UTM (F-048)
```

**Ads implication:** Meta CAPI may still get fbp/fbc from order payload, but **internal ROAS / Dispatcher campaign split is blind**. Fix F-071 + paid UTM canary before scaling paid social.

---



### F-087 — `media/ubd_docs/` publicly downloadable (HTTP 200)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES  
**Plan:** W1-11 / S-14 · **Source:** PLAN_VS_FINDINGS recheck 2026-07-09

Live: file exists under media; `curl` to `/media/ubd_docs/<name>` → **200** (with and without Referer). UBD ID photo = PII. Fix: auth-only view, random upload names, deny static listing.

---

### F-088 — `TELEGRAM_BOT_WEBHOOK_SECRET` empty on production

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES  
**Plan:** W3-9 / S-13

Django settings report EMPTY. Webhook signature check is optional when empty → accepts unauthenticated POSTs (code logs SECURITY warning only).

---

### F-089 — `FACEBOOK_PIXEL_ID` empty in settings (hardcoded HTML fallback)

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES (config)  
**Plan:** ADS-1 residual

HTML still boots pixel via template fallback; env/settings empty. Prefer single source of truth from env.

---

### F-090 — MySQL backup script present, no backup cron

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES  
**Plan:** W0-3

`scripts/backup_mysql.sh` on server; crontab only has log rotate. No scheduled dump / restore drill.

---

### F-091 — Plan re-verify document published

**Status:** [x] INFO · **Severity:** P3 · **Fix required:** no

Full matrix of IMPLEMENTATION_PLAN DONE/OPEN vs prod vs F-*: `docs/qa/PLAN_VS_FINDINGS_2026-07-09.md`.  
Key: several plan `[x]` items **REOPEN** (W2-1/2, ADS-3 data, pixel BFCache, etc.).

---



### F-092 — SSH password rotated (W0-1 OWNER DONE)

**Status:** [x] DONE_OWNER · **Severity:** P2 process · **Fix required:** no for password itself

Owner confirmed production SSH password changed (2026-07-09). Auditor key auth not configured. Treat plan W0-1 **OWNER** as complete. See F-093 for remaining REPO secret file.

---

### F-093 — `deploy_paramiko.py` still embeds SSH password (REPO)

**Status:** [x] FIXED · **Severity:** P1 · **Fix required:** YES
**Plan:** W0-1 REPO residual

`git grep` shows tracked `deploy_paramiko.py` with `ssh.connect(..., password=...)`. Password rotation does not remove secret from git history/working tree. Fix agent: delete or rewrite to env/keys; scrub history if required.

**Fixed:** `c5b651cf` deleted the tracked obsolete script. After server `git pull --ff-only`, production was verified at `c5b651cf` with no tracked changes and no `deploy_paramiko.py` file (2026-07-09).

---

### F-094 — Product title vs H1 mismatch reconfirm (last-breath family)

**Status:** [ ] OPEN · **Severity:** P1 · **Fix required:** YES  
**Related:** F-004

Live 2026-07-09 re-pass:
- `/product/last-breath/` title «last breath» vs H1 «Череп З Трояндою»
- `/product/last-breath-hd/` same pattern
- `/product/death-grabs-ass/` title «death grabs ass» vs H1 «Серце Та Грощі»

---



### Process note — plan false DONE (2026-07-09)

In `IMPLEMENTATION_PLAN.md`, false/incomplete `[x]` cleared for: **W2-1, W2-2, W2-3, ADS-1, ADS-2, ADS-3, W7-1, W3-9, W3-11, W0-5**. Rationale: `docs/qa/PLAN_VS_FINDINGS_2026-07-09.md`.



### Process note — strict DONE re-verify (2026-07-09 later)

Additionally unchecked in IMPLEMENTATION_PLAN:
- **W2-7**: **RESOLVED `78814344` (2026-07-12)** — retail webhook now commits through the shared post-payment dispatcher; server suite and once-only production canary passed.
- **W7-23**: residual `datetime.now()` in `dropshipper_views.py`.

Details: `docs/qa/PLAN_VS_FINDINGS_2026-07-09.md`.



### F-095…F-098 — Instagram management bot (analysis pack)

**Status:** OPEN · Full write-up: [`IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`](./IG_BOT_MANAGEMENT_BUGS_2026-07-09.md)

| ID | Topic |
|----|--------|
| F-095 | **FIXED `ad2883f0`** — list refresh + UA labels + hidden folder + automation/statistics exclusion |
| F-096 | **FIXED `15c3bf30` + `337710ce` + `3d4e5d40`** — Ukrainian dense stats, date ranges and funnel shares |
| F-097 | **FIXED_APP `e47c1498`** — CRM delivery-block state, filter and Ukrainian warning badge |
| F-098 | **REVISED_OWNER** — no manual button; existing automatic AI/page-echo takeover is intentional |

Also IG-006 likes/reactions, IG-001…IG-014 in that file.

---




### F-095 — IG bot Hide: list not refreshed (management)

**Status:** [x] FIXED · **Severity:** P1 · **Fix required:** YES
**Detail (full):** [`IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`](./IG_BOT_MANAGEMENT_BUGS_2026-07-09.md) **IG-001**  

Fixed in `ad2883f0`: shared UI mutation feedback refreshes/removes the active row; actions are Ukrainian; hidden clients move to the dedicated hidden view, queued inbound/follow-ups are finalized, inbound/follow-up workers cannot race a successful Hide, and active statistics exclude hidden clients. Production proof: migrations `0076`/`0077` applied, 33 focused server tests pass, `hidden_pending=0`, `/healthz/` returns 200.

**Code:** `bot_views.py` hide API; `templates/management/bot.html` Clients JS.

---

### F-096 — IG bot stats/filters English + thin dashboard

**Status:** [x] FIXED · **Severity:** P1 · **Fix required:** YES
**Detail:** **IG-004** in IG_BOT file  

Fixed in `15c3bf30`, `337710ce`, and `3d4e5d40`: all visible KPI/table/filter copy is Ukrainian; the dashboard shows 11 compact KPIs, product/ad/objection tables, funnel counts with percentage denominators, and today/7/30/all-time ranges. Range filtering applies to conversations, signals, and paid ad revenue. Production templates were recompressed and the focused server suite passed.

**Code:** `bot.html` Stats; `bot_views.bot_stats_api`.

---

### F-097 — IG bot Message Requests / Graph send fails unlabeled

**Status:** [x] FIXED_APP (`e47c1498`) · **Severity:** P0 · **Fix required:** DONE
**Detail:** **IG-005, IG-013** in IG_BOT file  

Permanent errors (#551, Advanced Access, 24h window) now persist a bounded
Ukrainian delivery status/reason and Graph metadata on `IgClient`; successful
send clears the block. CRM has a dedicated «Не можу відповісти» filter and
warning badge. Production migration `0075` is applied, the focused server suite
passes **26/26**, and one live client is already classified `advanced_access`.
Granting the Meta app Advanced Access remains an external Meta review action,
not an application-code bug.

**Code:** `instagram_bot.send_text`, `_classify_send_error`, `_process_one`.

---

### F-098 — IG bot no explicit transfer-to-manager action

**Status:** [x] REVISED_OWNER · **Severity:** P1 · **Fix required:** NO
**Detail:** **IG-003** in IG_BOT file  

Owner decision: do not add a duplicate «Передати менеджеру» button. The intended flow remains automatic AI `[manager]` / page-echo takeover, where a human manager joins and the bot pauses. The rejected manual-button implementation was fully reverted before release.

---

### F-099 — Mono dual status path (webhook vs utils) — plan W2-7

**Status:** [x] FIXED (`78814344`) · **Severity:** P1 · **Fix required:** DONE
**Detail:** [`PLAN_VS_FINDINGS_2026-07-09.md`](./PLAN_VS_FINDINGS_2026-07-09.md) STRICT W2-7  

Fixed in `78814344`: retail webhook `_apply_monobank_status` now locks the order
inside `transaction.atomic()`, records the purchase transition once, and
registers the shared `_dispatch_post_payment_events` with
`transaction.on_commit()`. Telegram, Meta, TikTok and the receipt email are
therefore dispatched only after a successful commit and are not duplicated on
repeated paid callbacks.

**Production proof (2026-07-12):** focused server suite passed **29/29**. A
MySQL canary produced `status=paid`, `post_commit=1`, `duplicate_dispatch=0`,
and `purchase_actions=1`; cleanup left **0 orders / 0 actions**.

---

### F-100 — `views.py.backup` still runtime-reachable — plan W7-1

**Status:** [ ] OPEN · **Severity:** P2 · **Fix required:** YES  
**Detail:** PLAN_VS W7-1  

File `storefront/views.py.backup` exists; `views/__init__.py` still lazy-loads from it.

---

### F-101 — residual `datetime.now()` in dropshipper — plan W7-23

**Status:** [ ] OPEN · **Severity:** P3 · **Fix required:** YES  
**Detail:** PLAN_VS W7-23 · `orders/dropshipper_views.py:273-274`

---

### F-102 — Core checkout/attribution tables use MyISAM

**Status:** [x] FIXED (`02b49553`) · **Severity:** P0 · **Fix required:** DONE

**Production fix verification (2026-07-11):** a compressed 3.7 MB pre-change
backup was created, migration `orders.0048_checkout_tables_innodb` applied, and
all ten checkout/session/attribution tables report `ENGINE=InnoDB`. A live
canary created Order + UTMSession + UserAction inside `transaction.atomic()`;
after forced rollback the three matching counts were exactly `(0, 0, 0)` with
no manual cleanup. New MySQL tables also default to InnoDB via connection policy.

**Production evidence (2026-07-10):** `information_schema.TABLES` reports
`ENGINE=MyISAM` for `orders_order`, `storefront_utmsession`, and
`storefront_useraction`. An attribution canary inside `transaction.atomic()`
successfully wrote its rows but `transaction.set_rollback(True)` did not remove
them; exact-key cleanup deleted all three and a follow-up query returned zero.

**Why problem:** checkout code relies on `transaction.atomic()`, but MyISAM does
not provide transactional rollback or foreign-key enforcement. A mid-checkout
failure can therefore leave a partial order/analytics state even though Django
code is structured as atomic.

**Fix direction:** inventory all related MyISAM tables and FK dependencies,
verify InnoDB availability/space, take a database backup, then perform a planned
engine migration with maintenance/lock monitoring and post-migration rollback
tests. Do not run a blind `ALTER TABLE` during live checkout traffic.

---

## Session changelog

| Time | Action |
|------|--------|
| 2026-07-09 | Pass A started; smoke, SEO sample, sitemap, feed, UTM cookies, findings F-001…F-015 |
| 2026-07-09 | Full 65 UK PDP titles; 13 title/H1 mismatches; 20 variants OK; mapa links OK; F-004 expanded; F-016/F-017 |
| 2026-07-09 | ATC API PASS; DB funnel; **orders 100% empty UTM**; is_converted=0; source dirt; feed color drop confirmed |
| 2026-07-09 | Logs: LSAPI_CHILDREN, MySQL gone away, pixel init ReferenceError; variants 60/60; recs OK; F-029–F-036 |
| 2026-07-09 | sessionid only after ATC; home IP exclusion; track-event stored:false; checkout-mono path map F-037–F-042 |
| 2026-07-09 | Server canary PASS; sitemap 489/489; order session_key gap F-044/045; help-center 404; FINAL status written |
| Pass A | **COMPLETE for audit scope** — fixes deferred to after Pass C |
| 2026-07-09 | F-049 home unexclude canary PASS (sessionid+UTMSession+ATC+stored:true) |
| 2026-07-09 late | Deep monobank/session_key/first_touch analysis; feed recheck; F-071–F-082; ads gate still BLOCKED |
| 2026-07-09 late+ | F-083 purchase undercount; F-084 dual AI sources live; F-085/F-086 SEO/rate PASS notes; F-002 reconfirm |
