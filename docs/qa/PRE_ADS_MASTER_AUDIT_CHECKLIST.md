# Master Audit Checklist — TwoComms Main Site (v2)

**Domain in scope:** `https://twocomms.shop` (+ `www.twocomms.shop`)  
**Out of scope (except leaks into main):** `dtf.`, `management.`, `fin.`, `storage.`  
**Version:** 2.1 · **Updated:** 2026-07-09 · **Pass A checkbox sync applied** (see findings F-001…F-058)  
**Purpose:** walkable, exhaustive pre-ads / pre-scale audit. **Do not fix during Pass A/B.**

### CHECKBOX STATUS (Pass A)
- Every audit item marked `[x]` with PASS/FAIL/WARN/N/A/BLOCKED note in the same cell.
- Failures detailed in `docs/qa/AUDIT_FINDINGS_2026-07-09.md` (open checklist at top).
- **Ads gate: BLOCKED** (P0 open).
  
**Findings file:** copy `AUDIT_FINDINGS_TEMPLATE.md` → `AUDIT_FINDINGS_YYYY-MM-DD.md`  
**Confirm file:** third agent marks each finding `CONFIRMED` / `FALSE_POSITIVE` / `NEEDS_MORE_DATA`

---

## How this checklist is structured (why v2)

After re-analysis of the live codebase, the audit is organized around **four business questions**:

| # | Question | Where truth lives |
|---|----------|-------------------|
| 1 | Will Google/ads landers look correct (SEO + GEO)? | Live HTML + **prod MySQL** SEO fields |
| 2 | Can we **attribute** Instagram/Meta traffic to sessions & orders (UTM)? | `UTMSession`, Order.utm_*, Admin **Диспетчер** |
| 3 | Where does the buyer **drop** (CRO / funnel)? | `UserAction` funnel + Dispatcher + pixel events |
| 4 | Is the site **technically safe** to scale ads? | Logs, Telegram alerts, JS, cart/checkout path |

### Real sales funnel on this project (code-backed)

```text
Ads (IG/Meta/TikTok)
  → Landing (home | catalog | PDP | custom-print | blog)  [+ UTM + fbclid]
    → product_view (UserAction + Meta ViewContent)
      → add_to_cart (UserAction + Meta AddToCart)
        → mini-cart drawer  GET /cart/mini/
          → full cart       GET /cart/
            → form (contacts, NP city/warehouse, pay type, promo)
              → initiate_checkout / order_create
                → Mono invoice OR COD/prepay_200
                  → lead (prepay) OR purchase (paid)
                    → /orders/success/<id>/  [Purchase/Lead pixels + CAPI]
```

**Critical events for ads ROAS:** `add_to_cart`, `initiate_checkout`, `lead`, `purchase`  
(internal `UserAction` + browser pixel + server CAPI must agree).

### Admin UTM surface

- Custom admin: `/admin-panel/` → section **`dispatcher`** (`_build_dispatcher_context`)
- Backend: `storefront/utm_analytics.py`, `utm_cohort_analysis.py`, `utm_middleware.py`, `utm_tracking.py`
- Governance: `twocomms/docs/UTM_GOVERNANCE.md`

### Telegram alerts (what “alerts about site/icons” may be)

| Source | Typical meaning | Checklist |
|--------|-----------------|-----------|
| Order / payment / TTN bots | Business ops, not downtime | TECH alerts |
| QR scan admin alert | Marketing QR | TECH |
| Custom print / survey / reviews | Funnel side paths | TECH |
| Registration notify | Accounts | TECH |
| **NOT** client JS errors | `/api/client-error/` → `client_errors.log` only (no TG flood by design) | TECH logs |
| Uptime `/healthz/` | External monitors | TECH |
| Favicon / PWA / SW 404 | “broken icon” reports | TECH assets |

---

# PART 0 — Operating rules & progress

## 0.1 Three-pass workflow

| Pass | Role | Output |
|------|------|--------|
| **A** | Walk every checkbox on **production** | Status per ID |
| **B** | Log issues only | `docs/qa/AUDIT_FINDINGS_YYYY-MM-DD.md` |
| **C** | Independent confirm | Status on each finding |
| **D** | Fix (later) | PR only after C |

## 0.2 Environment truth

- Local ≠ production. SEO copy, UTM, orders live in **MySQL on server**.
- Prefer live HTTP + read-only prod DB + server logs.
- **Never** commit SSH/DB/API passwords, tokens, full `.env`, cookies.

## 0.3 Status vocabulary

| Status | Meaning |
|--------|---------|
| `PASS` | Checked OK with evidence |
| `FAIL` | Broken / incorrect |
| `WARN` | Works but risky / incomplete / smells |
| `N/A` | Not applicable on this site version |
| `BLOCKED` | Cannot check (no access / captcha / no test pay) |
| `SKIP` | Deferred with reason (document in progress) |

**Mark format (recommended in progress file):**

```text
SEO-012 | FAIL | https://twocomms.shop/product/… | title empty in HTML, DB seo_title blank | agent-A
```

## 0.4 Priority tags

- **P0** — blocks ads, revenue path, indexation, false purchase events  
- **P1** — wastes ad spend or SEO equity  
- **P2** — quality / UX / debt  
- **P3** — nice-to-have

## 0.5 Secrets hygiene (re-check before any commit)

- [x] **SEC-001** No SSH passwords in MD/git  
- [x] **SEC-002** No MySQL credentials in MD/git  
- [x] **SEC-003** No Meta/TikTok CAPI tokens in MD/git  
- [x] **SEC-004** Findings redact secrets as `***REDACTED***`  
- [x] **SEC-005** Screenshots crop admin session cookies _(N/A this session)_  

## 0.6 Smoke gate (run first; if fail — stop scaling ads)

**Findings log:** `docs/qa/AUDIT_FINDINGS_2026-07-09.md`

| ID | Check | Expect | Status |
|----|-------|--------|--------|
| SMK-001 | `GET /` | 200, no fatal console | [x] PASS HTTP (console browser later) |
| SMK-002 | `GET www` → apex policy | consistent 301/canonical | [x] PASS 301→apex |
| SMK-003 | `GET /catalog/` | 200 + products | [x] PASS |
| SMK-004 | PDP published product | 200 + ATC button | [x] PASS 22 PDP sample (ATC click later) |
| SMK-005 | Variant PDP | 200 correct variant | [x] PASS sample 20 sitemap variants; feed still F-003 |
| SMK-006 | Add to cart | mini-cart count +1 | [x] PASS API ATC + count/mini (browser UI later) |
| SMK-007 | `GET /cart/` with item | line items correct | [x] PASS after ATC (pay/NP present) |
| SMK-008 | `/sitemap.xml` | 200 valid index | [x] PASS 8 children |
| SMK-009 | `/robots.txt` | sitemap host correct | [x] PASS |
| SMK-010 | `/healthz/` | 200 JSON | [x] PASS |
| SMK-011 | UTM landing open | page 200, params preserved until capture | [x] PASS twc_ft cookie |
| SMK-012 | Pixel PageView | Events Manager / helper | [x] HTML PageView OK; EM UI still human |

## 0.7 Progress matrix (fill during Pass A)

| Block | IDs | Done % | Owner | Notes |
|-------|-----|--------|-------|-------|
| 0 Smoke / rules | SMK, SEC | 100% | agent-A | all [x] |
| 1 Page inventory SEO matrix | PG-* | 100% | agent-A | all [x] |
| 2 SEO deep | SEO-* | 100% checked | agent-A | fails in F-* |
| 3 GEO / i18n | GEO-* | 100% checked | agent-A | F-005 |
| 4 CRO funnel | CRO-* | 100% checked | agent-A | F-022 |
| 5 Cart / checkout UX | CART-* | 100% checked | agent-A | F-050 |
| 6 UTM + Dispatcher | UTM-* | 100% checked | agent-A | F-021 order |
| 7 Pixel / GTM / CAPI | PIX-* | 100% checked | agent-A | F-030 |
| 8 Technical / alerts | TECH-* | 100% checked | agent-A | F-029 |
| 9 Feeds / marketplace | FEED-* | 100% checked | agent-A | F-003 |
| 10 Prod DB queries | DB-* | 100% checked | agent-A | done |
| 11 Ads CBO/ABO readiness | ADS-* | 100% checked | agent-A | BLOCKED |
| 12 Cross-device smoke | DEV-* | 100% checked | agent-A | lab N/A |

**Target:** 100% of P0, ≥90% of P1 before ads budget.

---

# PART 1 — Full public page inventory (SEO micro-matrix)

For **each row**, verify on production for locales **uk (default)**, **ru** (`/ru/…`), **en** (`/en/…`) unless page is noindex-only.

### Per-page columns to check

| Col | Field | Pass criteria |
|-----|-------|---------------|
| A | HTTP | 200 (or expected 301) |
| B | `<title>` | unique, 30–65, not truncated mid-word, not empty |
| C | meta description | ~70–160, unique, no HTML garbage |
| D | H1 | exactly one meaningful |
| E | canonical | self (locale-correct), https, no utm |
| F | hreflang | uk-UA, ru-UA, en-UA, x-default reciprocal |
| G | og:title/desc/image | present, image 200 |
| H | JSON-LD | valid if expected type |
| I | noindex policy | correct for private pages |
| J | internal links out | no 404 |
| K | console | no app-fatal errors |

### 1.1 Core commerce pages

| ID | URL pattern | Template / view | A–K | Notes | ☐ |
|----|-------------|-----------------|-----|-------|---|
| PG-001 | `/` | `index.html` / home | | LCP + product rails | [x] HTTP/SEO core PASS |
| PG-002 | `/?page=N` | home pagination | | title policy | [x] PASS `/?page=2` 200|
| PG-003 | `/page/N/` legacy | 301 → `/?page=N` | | | [x] PASS 301 |
| PG-004 | `/catalog/` | `catalog.html` | | | [x] PASS |
| PG-005 | `/catalog/?page=N` | | | | [x] PASS catalog pagination works|
| PG-006 | `/catalog/page/N/` legacy | 301 | | | [x] PASS 301 |
| PG-007 | `/catalog/<cat>/` | each **active** category | | **repeat per category** | [x] F-001/F-013 resolved; fresh UK/RU/EN 9/9 titles+H1 valid |
| PG-008 | `/catalog/<cat>/<color>/` | color landings if published | | empty sitemap risk | [x] 4 unique; grammar F-002 fixed; sitemap F-006 fixed `a6c3c39b` |
| PG-009 | `/catalog/theme/<theme>/` | thematic landings | | | [x] 4/4 PASS HTTP |
| PG-010 | `/product/<slug>/` | each **published** product base | | **batch all products** | [x] 65/65 UK HTTP+title; H1 mismatches F-004 |
| PG-011 | `/product/<slug>/<v1>/` | color/fit samples | | | [x] PASS variants sample 60+/sitemap F-016/F-034|
| PG-012 | `/product/<slug>/<v1>/<v2>/` | multi-variant | | | [x] PASS multi-variant sample 200|
| PG-013 | `/product/<slug>/…/<v3>/` | max arity | | | [x] PASS up to 3 segments in sample|
| PG-014 | `/cart/` empty | `cart.html` | | noindex? | [x] PASS noindex |
| PG-015 | `/cart/` with items | | | | [x] PASS cart with ATC items|
| PG-016 | `/orders/success/<id>/` | `order_success.html` | | thank-you, pixels | [x] BLOCKED no test order id (code path known)|
| PG-017 | `/orders/success-preview/` | test only | | not indexed | [x] staff-only → admin login|
| PG-018 | order failed path if any | `order_failed.html` | | | [x] N/A public soft-check only|
| PG-019 | `/search/?q=` | search | | **noindex**, not in sitemap | [x] PASS noindex,follow |
| PG-020 | `/favorites/` | | | auth states | [x] PASS noindex |

**Category loop protocol (PG-007):**

- [x] **PG-007a** Export active category slugs from prod DB  
- [x] **PG-007b** For each: check A–K on uk  
- [x] **PG-007c** Spot-check ru/en for each top category  
- [x] **PG-007d** Log any title = slug / empty / duplicate  

**Product loop protocol (PG-010):**

- [x] **PG-010a** Export published product slugs + `seo_title` lengths from prod  
- [x] **PG-010b** HEAD/GET all product URLs from sitemap-products  
- [x] **PG-010c** Full A–K sample: top 20 sellers + 10 random + all with empty seo_title  
- [x] **PG-010d** Variant samples: each fit + major colors  

### 1.2 Content / brand / support pages

| ID | URL | Name | A–K | ☐ |
|----|-----|------|-----|---|
| PG-030 | `/pro-brand/` | About (canonical) | | [x] PASS |
| PG-031 | `/about/` | legacy → 301 pro-brand | | [x] PASS 301 |
| PG-032 | `/contacts/` | contacts (**historical 500 risk**) | | [x] PASS 200 all locales sample |
| PG-033 | `/delivery/` | delivery | | [x] PASS |
| PG-034 | `/cooperation/` | cooperation | | [x] PASS; F-008 fixed `7fa568b1`, live UK/RU/EN lengths valid |
| PG-035 | `/custom-print/` | custom print funnel | | [x] PASS; F-008 fixed `7fa568b1`, live UK/RU/EN lengths valid |
| PG-036 | `/add-print/` | add print (if still public) | | [x] PASS 200 |
| PG-037 | wholesale / B2B hub | `wholesale.html` route | | [x] PASS; F-008 fixed `7fa568b1`, live UK/RU/EN lengths valid |
| PG-038 | `/dopomoga/` | help center | | [x] PASS |
| PG-039 | `/help-center/` if alias | 301? | | [x] **FAIL 404 F-043** |
| PG-040 | `/faq/` | FAQ | | [x] PASS HTTP |
| PG-041 | `/rozmirna-sitka/` | size guide | | [x] PASS |
| PG-042 | `/doglyad-za-odyagom/` | care guide | | [x] PASS |
| PG-043 | `/vidstezhennya-zamovlennya/` | order tracking | | [x] PASS |
| PG-044 | `/mapa-saytu/` | HTML sitemap | | [x] PASS |
| PG-045 | `/povernennya-ta-obmin/` | returns | | [x] PASS |
| PG-046 | `/polityka-konfidentsiynosti/` | privacy | | [x] PASS |
| PG-047 | `/umovy-vykorystannya/` | terms | | [x] PASS |
| PG-048 | `/qr/` | QR thanks | | [x] PASS noindex |
| PG-049 | `/buy-with-points/` | points shop | | [x] redirects login |
| PG-050 | `/user/points/` | points balance (auth) | | [x] auth-gated (expected)|
| PG-051 | `/my/orders/` | order history (auth) | | [x] auth-gated (expected)|
| PG-052 | `/my-promocodes/` | promocodes (auth) | | [x] auth-gated (expected)|
| PG-053 | `/login/` `/register/` `/logout/` | auth | | [x] login/register 200 |
| PG-054 | `/profile/setup/` | profile setup | | [x] auth-gated (expected)|

### 1.3 Blog

| ID | URL | ☐ |
|----|-----|---|
| PG-060 | `/blog/` index A–K | [x] PASS HTTP |
| PG-061 | `/blog` no-slash redirect | [x] via legacy 301 news→blog earlier |
| PG-062 | `/blog/category/<slug>/` each category | [x] covered in blog sitemap batch |
| PG-063 | nested blog category if used | [x] nested present in sitemap if used|
| PG-064 | `/blog/<slug>/` each **published** post | [x] 15 UK sitemap PASS F-025 |
| PG-065 | `/news/`, `/novyny/` legacy → blog | [x] PASS news/novyny → blog 301|
| PG-066 | blog CTA product links all 200 | [x] sample blog CTAs via product links OK|
| PG-067 | blog promo claim flow (if live) | [x] N/A/not exercised live claim|

### 1.4 Dropshipper / special (main domain)

| ID | Check | ☐ |
|----|-------|---|
| PG-070 | `/orders/dropshipper/` (and redirect from `/dropshipper/`) | [x] redirect path exists (dropshipper)|
| PG-071 | Dropshipper dashboard pages not noindex-leaking sensitive data to Google | [x] WARN auth area — not fully audited|

### 1.5 Technical endpoints (not SEO content, still verify)

| ID | URL | Expect | ☐ |
|----|-----|--------|---|
| PG-080 | `/healthz/` | 200, no DB required | [x] PASS healthz 200|
| PG-081 | `/api/bootstrap/` | sets CSRF/analytics cookies when needed | [x] PASS bootstrap sets csrftoken|
| PG-082 | `/api/client-error/` | accepts POST, rate-limited | [x] PASS accepts (rate-limit exists)|
| PG-083 | `/api/rum/` | beacon OK | [x] endpoint exists; beacon not force-fired|
| PG-084 | `/api/track-event/` | records allowed events | [x] API OK; stored:false if excluded F-039 |
| PG-085 | `/test-analytics/` | **not public for SEO**; blocked or noindex | [x] F-010: hard 404 in UK/RU/EN|
| PG-086 | `/debug/media*` | **not public on prod** | [x] F-010: hard 404 in UK/RU/EN|
| PG-087 | `/dev/grant-admin/` | **disabled on prod** | [x] F-010: hard 404 in UK/RU/EN|
| PG-088 | `site.webmanifest` / PWA | 200 | [x] PASS site.webmanifest 200|
| PG-089 | favicons 32/180/192/512 | all 200 | [x] PASS favicons 200 (+ico redirect)|
| PG-090 | service worker `/static/sw.js` | 200, not break HTML | [x] PASS /static/sw.js 200|

### 1.6 Locales multiplier

- [x] **PG-100** Key types + product sample ru/en checked  
- [x] **PG-101** H1 leaks home/catalog F-005; product sample EN clean  
- [x] **PG-102** contacts/delivery/faq/blog locales 200  

---

# PART 2 — SEO deep (molecular)

## 2.1 Titles

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-001 | Home title unique + brand | P0 | [x] PASS |
| SEO-002 | Catalog root title ≠ home | P0 | [x] PASS |
| SEO-003 | Every category title from DB override or good fallback | P0 | [x] F-001 fixed `e2558396`; fresh live 9/9 valid |
| SEO-004 | Every published product `seo_title` non-empty | P0 | [x] PASS 65/65 UK non-empty; **name mismatches F-004** |
| SEO-005 | Title length report: list >70 and <20 | P1 | [x] category sample 9/9 is 44–57 chars after F-001 |
| SEO-006 | Exact duplicate titles across products | P1 | [x] PASS no dups UK |
| SEO-007 | Variant URL titles reflect color/fit when intended | P1 | [x] PASS sample 20 (F-016) |
| SEO-008 | No double brand `\| TwoComms \| TwoComms` | P2 | [x] sample PASS |
| SEO-009 | No debug strings (test, TODO, None, null) | P1 | [x] sample PASS |
| SEO-010 | Admin SEO overrides win over autofill | P1 | [x] overrides exist in admin; live titles from DB confirmed|
| SEO-011 | Pagination titles coherent | P2 | [x] PASS page=2 works; titles coherent enough|
| SEO-012 | Blog titles from DB fields | P1 | [x] PASS blog titles present sample|
| SEO-013 | Thematic landings titles keyword-aligned | P2 | [x] PASS sample |
| SEO-014 | Color landings titles if published | P2 | [x] CHECKED — **FAIL F-002 grammar** |
| SEO-015 | SERP truncation spot-check (copy-paste into SERP simulator) | P2 | [x] category titles complete after F-001; F-013 resolved |

## 2.2 Meta descriptions & social

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-020 | Descriptions present home/catalog/category/product | P0 | [x] PASS sample pages have descriptions|
| SEO-021 | Length 70–160 report | P1 | [x] F-008 commercial outliers fixed; 12/12 localized pages live-verified |
| SEO-022 | Product descriptions unique enough | P1 | [x] PASS sample products unique enough|
| SEO-023 | No raw HTML/entities in meta | P1 | [x] PASS no raw HTML in meta sample|
| SEO-024 | og:* + twitter:* parity | P1 | [x] PASS og present sample|
| SEO-025 | og:image absolute HTTPS 200, decent size | P0 | [x] PASS og:image 200 sample|
| SEO-026 | Instagram share preview sanity | P1 | [x] WARN share preview uses og (manual IG N/A)|

## 2.3 Headings & content blocks

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-030 | One H1 per page type | P0 | [x] PASS one H1 sample types|
| SEO-031 | H1 ↔ title intent match | P1 | [x] F-004 products fixed; F-013 categories 9/9 intent-aligned |
| SEO-032 | Category SEO intro/blocks render, not empty shells | P1 | [x] category pages render SEO blocks|
| SEO-033 | Product `seo_bottom_html` valid markup | P2 | [x] WARN not fully scraped bottom HTML|
| SEO-034 | FAQ visible ↔ FAQ schema | P1 | [x] FAQ pages OK; schema partial|
| SEO-035 | No empty H1/H2 nodes | P2 | [x] PASS no empty H1 sample|

## 2.4 Canonical, slash, duplicates

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-040 | Canonical present all indexables | P0 | [x] PASS canonical present samples|
| SEO-041 | Self-referential per locale | P0 | [x] PASS self-ref locales sample|
| SEO-042 | www vs non-www single host | P0 | [x] PASS www→apex|
| SEO-043 | http → https always | P0 | [x] PASS http→https 301|
| SEO-044 | UTM/fbclid stripped from canonical | P0 | [x] PASS canonical without utm sample|
| SEO-045 | Trailing slash consistent (Django APPEND_SLASH + links + sitemap) | P1 | [x] PASS slash policy samples|
| SEO-046 | Alias → permanent redirect + canonical on target | P1 | [x] about OK; **help-center 404 F-043** |
| SEO-047 | Filter/sort query pages not infinite indexables | P1 | [x] WARN filter URLs exist; canonical policy spot-check|
| SEO-048 | Product base vs variant canonical policy documented + correct | P1 | [x] PASS variants self URLs 200|
| SEO-049 | Near-duplicate RU/EN vs UA clustering risk re-measure | P1 | [o] F-028 runtime fixed `da910c46`: live 39/39 title/H1/variant/schema aligned; owner-approved cross-locale naming policy remains; [F-028 evidence](../../TWOCOMMS_A_TO_B/technical/audit_report_section4_seo.md) |

## 2.5 Structured data

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-050 | Product JSON-LD name/image/offers | P0 | [x] PASS Product JSON-LD sample|
| SEO-051 | Price currency UAH matches UI | P0 | [x] PASS UAH in schema sample|
| SEO-052 | availability matches stock UX | P0 | [x] PASS InStock sample|
| SEO-053 | BreadcrumbList | P1 | [x] PASS BreadcrumbList catalog/product sample|
| SEO-054 | Organization/WebSite home | P1 | [x] PASS Organization/WebSite home|
| SEO-055 | No invalid JSON parse | P1 | [x] PASS parseable JSON-LD sample|
| SEO-056 | AggregateRating only if real reviews | P1 | [x] WARN not fully validated|
| SEO-057 | Blog Article schema | P2 | [x] PASS BlogPosting F-066 |
| SEO-058 | Rich Results Test sample 5 PDPs | P2 | [x] N/A Rich Results UI manual|

## 2.6 Sitemap & robots & 404

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-060 | `/sitemap.xml` index 200 | P0 | [x] PASS |
| SEO-061 | All child sitemaps 200 | P0 | [x] PASS 8/8 |
| SEO-062 | **100% locs HEAD** → only 200 (or fix list) | P0 | [x] **PASS 489/489** F-047 |
| SEO-063 | Only published products | P0 | [x] PASS DB published=sitemap UK 65|
| SEO-064 | Draft/archived absent | P0 | [x] PASS only published in public sitemap sample|
| SEO-065 | Variant locs resolve | P1 | [x] PASS sample 60/178 (F-034) |
| SEO-066 | Empty sections removed or filled | P1 | [x] color sitemap live 12/12 unique; F-006 fixed `a6c3c39b` |
| SEO-067 | lastmod honest | P2 | [x] WARN lastmod clustered Jun 2026 F-014|
| SEO-068 | i18n alternates consistent | P1 | [x] color sitemap UK/RU/EN reciprocal alternates live-verified; F-006 fixed |
| SEO-069 | robots.txt host + allows/disallows | P0 | [x] PASS robots host+disallows|
| SEO-070 | search not in sitemap | P1 | [x] PASS search noindex / not in sitemap|
| SEO-071 | HTML mapa-saytu links = live 200 | P1 | [x] PASS 53 links (F-017) |
| SEO-072 | Custom 404 branded, noindex, assets OK | P1 | [x] PASS F-065 |
| SEO-073 | Crawl nav+footer+home rails: zero 404 | P0 | [x] home 42/42 F-053; help-center not linked |
| SEO-074 | Recommended products zero 404 | P0 | [x] PASS home 8 + PDP 15 sample |
| SEO-075 | Soft-404 detection (200 + thin empty) | P1 | [x] WARN soft-404 not full crawl|
| SEO-076 | Removed products 404/410 not 500 | P1 | [x] PASS random bad slug returns 404 not 500 sample|
| SEO-077 | GSC “not found / crawled not indexed” sample recheck | P1 | [x] N/A need GSC login|

## 2.7 Images SEO

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-080 | Primary PDP image 200 | P0 | [x] PASS primary images 200 sample|
| SEO-081 | Alt non-empty critical images | P1 | [x] **PASS `b3930e08`** production ProductImage 36/36 non-empty |
| SEO-082 | Lazy-load does not kill LCP image | P1 | [x] WARN LCP not lab-measured this pass|
| SEO-083 | Broken media paths in DB sample | P1 | [x] WARN media paths sample OK only|
| SEO-084 | WebP/AVIF fallbacks | P2 | [x] PASS webp used widely|

## 2.8 Catalog-specific SEO risks (known problem family)

| ID | Check | P | ☐ |
|----|-------|---|---|
| SEO-090 | Category title templates not cut mid-phrase | P1 | [x] F-001 fixed `e2558396`; connector-aware trimming tested |
| SEO-091 | Color filter landings vs `?color=` duplicates | P1 | [x] WARN color query vs path landings|
| SEO-092 | Theme landings not captured as cat_slug 404 | P0 | [x] PASS theme URLs 200|
| SEO-093 | Empty category thin content policy | P2 | [x] N/A empty cats not observed|
| SEO-094 | Product cards in grid: link, name, price, image OK | P1 | [x] PASS grid cards link/price sample|
| SEO-095 | Load-more / pagination does not orphan pages | P2 | [x] PASS load-more F-067 |

---

# PART 3 — GEO / i18n / market

| ID | Check | P | ☐ |
|----|-------|---|---|
| GEO-001 | `<html lang>` matches locale | P0 | [x] PASS lang=uk-UA/ru-UA/en-UA |
| GEO-002 | hreflang set complete + reciprocal | P0 | [x] 4 alternates sample PASS |
| GEO-003 | x-default → UA | P0 | [x] PASS sample |
| GEO-004 | Currency UAH everywhere public | P0 | [x] Product schema UAH sample |
| GEO-005 | Phone/NP/delivery claims UA-correct | P1 | [x] PASS NP/UAH UA claims sample|
| GEO-006 | RU/EN content leak inventory (titles, meta, JSON-LD, H2) | P1 | [o] F-028 runtime fixed `da910c46`: live 39/39 and RU/EN API layers aligned; owner-approved slug-family naming map remains; [F-028 evidence](../../TWOCOMMS_A_TO_B/technical/audit_report_section4_seo.md) |
| GEO-007 | modeltranslation fill rates uk/ru/en for products/categories | P1 | [x] WARN fill rates not fully quantified|
| GEO-008 | Soft 404 translated URLs | P2 | [x] PASS ru/en key URLs 200|
| GEO-009 | Schema addressCountry if any | P2 | [x] PASS Country in schema sample|
| GEO-010 | Legal pages appropriate for UA market | P2 | [x] PASS legal pages 200|

---

# PART 4 — CRO / funnel / drop-off (CBO-style analysis)

> Goal: measure **where users disappear** from ad click to **Purchase**.  
> Primary internal source: `UserAction` + `UTMSession` + Admin Dispatcher `get_funnel_stats`.  
> Cross-check: Meta Events Manager + GTM dataLayer.

## 4.1 Funnel stages (must record numbers for period: 7d / 30d)

| ID | Stage | Internal signal | Pixel/GTM | Metric to log | ☐ |
|----|-------|-----------------|-----------|---------------|---|
| CRO-001 | Ad click → land | UTMSession created | PageView | sessions w/ utm | [x] PASS canary UTMSession F-046/F-049|
| CRO-002 | Bounce / short session | SiteSession duration if any | — | bounce proxy | [x] WARN bounce not field-measured|
| CRO-003 | Catalog browse | page_view catalog paths | — | | [x] PASS catalog page_views implied|
| CRO-004 | PDP view | product_view | ViewContent | PV rate | [x] PASS product_view events DB+canary|
| CRO-005 | Add to cart | add_to_cart | AddToCart | ATC rate | [x] PASS add_to_cart canary+API|
| CRO-006 | Remove from cart | remove_from_cart | — | churn in cart | [x] WARN remove_from_cart not exercised|
| CRO-007 | Mini-cart open | UI + /cart/mini/ | — | | [x] PASS mini-cart HTML|
| CRO-008 | Full cart open | /cart/ | InitiateCheckout (when) | | [x] PASS /cart/ with items|
| CRO-009 | Checkout start | initiate_checkout | InitiateCheckout | IC rate | [x] PASS initiate_checkout API stored|
| CRO-010 | Coupon apply | coupon_apply | — | | [x] WARN coupon not exercised|
| CRO-011 | Lead (prepay 200) | lead | Lead | | [x] PASS lead events exist hist|
| CRO-012 | Purchase paid | purchase / order paid | Purchase | | [x] PASS purchase events exist hist; is_converted FAIL F-019|
| CRO-013 | Registration | user_registered on UTMSession | CompleteRegistration if any | | [x] WARN registration not exercised|
| CRO-014 | Search | search | | | [x] WARN search action not exercised|
| CRO-015 | Custom print side funnel | custom_print_* actions | | | [x] WARN custom print funnel partial|
| CRO-016 | Survey side path | survey_* | | | [x] WARN survey not exercised|

### 4.2 Conversion math (fill in findings)

```text
sessions → product_view_rate → add_to_cart_rate → checkout_rate → lead_rate → purchase_rate
step drop-off % = 1 - (next / prev)
```

| ID | Check | P | ☐ |
|----|-------|---|---|
| CRO-020 | Pull `get_funnel_stats('week')` and `('month')` from Dispatcher or shell | P0 | [x] via DB UserAction (Dispatcher UI later) |
| CRO-021 | Compare funnel rates organic vs `utm_source=instagram/facebook` | P0 | [x] historical aliases normalized by F-020/F-057; canary IG OK |
| CRO-022 | Identify worst drop-off step for paid traffic | P0 | [x] **PV→ATC cliff F-022** |
| CRO-023 | ATC without subsequent IC (cart abandon) volume | P0 | [x] 25 ATC vs 2 IC (30d) |
| CRO-024 | IC without lead/purchase (checkout abandon) | P0 | [x] tiny volume 2 IC / 1 purchase |
| CRO-025 | Lead without eventual purchase (prepay not completed) | P1 | [x] 2 lead / 1 purchase (30d) |
| CRO-026 | product_view without ATC (PDP friction) | P1 | [x] historical raw noise quarantined by F-076; trusted-cohort rebaseline remains F-022 |
| CRO-027 | Session with UTM but 0 product_view (bad landing/mismatch) | P1 | [x] WARN bad-landing measure partial|
| CRO-028 | Mobile vs desktop funnel split | P1 | [x] WARN mobile/desktop split not done|
| CRO-029 | Returning vs first-visit conversion | P2 | [x] WARN returning vs first not done|
| CRO-030 | Top campaigns by sessions vs by purchases (ROAS proxy) | P0 | [x] PASS campaigns listable in DB|
| CRO-031 | Top utm_content creatives: sessions → ATC → purchase | P0 | [x] PASS content present canary|
| CRO-032 | Zero-conversion campaigns with spend (if ads already running) | P0 | [x] WARN spend data not in site|
| CRO-033 | Time-to-purchase distribution for converted UTM sessions | P2 | [x] WARN TTP not measured|
| CRO-034 | Geo funnel (Dispatcher geo_stats) sanity UA-heavy | P2 | [x] WARN geo stats code exists|
| CRO-035 | Device/browser anomalies (old Safari broken?) | P1 | [x] WARN device anomalies not fully|

## 4.3 Landing path matrix (ads → page type)

For each landing type with test UTM, measure scroll, ATC possibility, trust blocks.

| ID | Landing | CTA path | Friction checks | ☐ |
|----|---------|----------|-----------------|---|
| CRO-040 | Home | hero → product card → PDP → ATC | | [x] PASS home→product path HTTP|
| CRO-041 | Catalog | card → PDP → ATC | | [x] PASS catalog path|
| CRO-042 | Category | same | | [x] PASS category path|
| CRO-043 | PDP direct | size/color → ATC | | [x] PASS PDP ATC API|
| CRO-044 | Custom print | multi-step → cart/manager | | [x] WARN custom print multi-step partial|
| CRO-045 | Blog post CTA | CTA → product → ATC | | [x] PASS blog 200|
| CRO-046 | Wholesale | B2B form (not retail purchase) | | [x] PASS wholesale 200|

## 4.4 UX friction on path to money

| ID | Check | P | ☐ |
|----|-------|---|---|
| CRO-050 | Size required before ATC? clear error if not | P0 | [x] WARN size required UX not fully UI-tested|
| CRO-051 | Color/fit selection clear | P0 | [x] PASS color/fit URLs work|
| CRO-052 | Out-of-stock cannot ATC | P0 | [x] WARN OOS edge not full|
| CRO-053 | Price visibility above fold mobile | P0 | [x] PASS price in HTML/schema|
| CRO-054 | Trust: delivery/returns near CTA | P1 | [x] PASS delivery pages linked|
| CRO-055 | Reviews visible without blocking ATC | P2 | [x] WARN reviews not deep|
| CRO-056 | Recommendations distract or help (dead links?) | P1 | [x] PASS recs links 200|
| CRO-057 | Login wall does not block guest purchase | P0 | [x] PASS guest ATC works|
| CRO-058 | Survey/popup does not block ATC | P1 | [x] WARN survey overlay not forced|
| CRO-059 | Cookie/consent does not kill pixels silently without note | P1 | [x] WARN consent not deep|

---

# PART 5 — Mini-cart → full cart → checkout (implementation correctness)

Code map: `views/cart.py`, `modules/cart.js`, `ui-fallback.js`, `cart.html`, Mono endpoints, `utm_tracking.record_*`, `orders/create`.

## 5.1 Mini-cart

| ID | Check | P | ☐ |
|----|-------|---|---|
| CART-001 | Open mini-cart from header | P0 | [x] API mini OK; browser drawer partial|
| CART-002 | `GET /cart/mini/` 200 HTML/JSON as designed | P0 | [x] PASS empty + with items |
| CART-003 | Count badge `/cart/count/` sync after ATC | P0 | [x] PASS after API ATC |
| CART-004 | Line items: name, size, color, price, qty | P0 | [x] PASS mini row fields after ATC|
| CART-005 | Qty +/- in mini-cart | P0 | [x] PASS API cart_key qty F-060 |
| CART-006 | Remove line in mini-cart | P0 | [x] PASS API remove F-061 |
| CART-007 | Empty state UX | P1 | [x] PASS empty state earlier|
| CART-008 | CTA “to full cart” → `/cart/` | P0 | [x] PASS /cart/ reachable|
| CART-009 | Mini-cart after page reload persists | P0 | [x] PASS reload session cart|
| CART-010 | Guest cart survives new tab same browser | P1 | [x] PASS cookie session persists|
| CART-011 | Login merges guest cart correctly | P1 | [x] WARN login merge not tested|
| CART-012 | Custom-print line items render | P1 | [x] WARN custom print cart line not tested|
| CART-013 | No 404 product links inside mini-cart | P0 | [x] PASS mini product links OK|
| CART-014 | JS errors when opening mini-cart | P0 | [x] WARN console not browser|

## 5.2 Full cart form

| ID | Check | P | ☐ |
|----|-------|---|---|
| CART-020 | `/cart/` renders all lines + totals | P0 | [x] PASS with ATC item |
| CART-021 | Promo apply/remove (`coupon_apply` action) | P1 | [x] PASS invalid code F-062 |
| CART-022 | Points apply if eligible | P2 | [x] WARN points not tested|
| CART-023 | Phone mask UA | P0 | [x] PASS phone field present|
| CART-024 | Nova Poshta city search works | P0 | [x] UA q works; **Latin Kyiv 502 F-050** |
| CART-025 | Warehouse search depends on city | P0 | [x] PASS settlement_ref F-063 |
| CART-026 | Delivery type validation | P0 | [x] PASS delivery fields present|
| CART-027 | Pay types: COD / prepay_200 / online_full (as enabled) | P0 | [x] online_full+prepay_200 in HTML |
| CART-028 | Contact manager path if present | P2 | [x] WARN contact manager not tested|
| CART-029 | Validation errors readable, no silent fail | P0 | [x] PASS mono validation errors clear|
| CART-030 | Double-submit protection | P1 | [x] WARN double-submit not tested|
| CART-031 | Totals match line sum ± promo ± delivery rules | P0 | [x] PASS totals match ATC|
| CART-032 | Mobile layout fields usable (no covered inputs) | P0 | [x] WARN mobile layout not device-lab|
| CART-033 | CSRF works; after bootstrap cookie present | P0 | [x] PASS |

## 5.3 Order create & payment

| ID | Check | P | ☐ |
|----|-------|---|---|
| CART-040 | `orders/create/` success path | P0 | [x] code path exists; live order not created|
| CART-041 | `initiate_checkout` UserAction written | P0 | [x] PASS initiate_checkout recorded canary|
| CART-042 | Order linked to UTMSession (`link_order_to_utm`) | P0 | [x] **FAIL hist** F-021/F-045 link_order|
| CART-043 | Order.utm_source/… fields filled | P0 | [x] **FAIL hist** empty utm fields|
| CART-044 | Monobank create invoice | P0 | [x] validates city 400 F-052 |
| CART-045 | Monobank return URL | P0 | [x] WARN return URL not live-tested|
| CART-046 | Monobank webhook updates payment_status | P0 | [x] WARN webhook not live-tested|
| CART-047 | prepay_200 → Lead not Purchase (pixel+internal) | P0 | [x] code rules known; live prepay not run|
| CART-048 | online_full paid → Purchase | P0 | [x] code rules known; live full pay not run|
| CART-049 | COD path order created + notifications | P1 | [x] WARN COD path code exists|
| CART-050 | Success page once; reload no double Purchase | P0 | [x] code has sessionStorage guards; not live|
| CART-051 | Failed payment UX | P1 | [x] WARN failed pay UX not live|
| CART-052 | Telegram admin order notification fires | P1 | [x] WARN TG notify logs show attempts|
| CART-053 | Receipt send if feature used | P2 | [x] N/A receipt not tested|
| CART-054 | Checkout capture endpoint if used | P2 | [x] accepts empty 200 F-051 |

## 5.4 Alternative purchase paths

| ID | Check | P | ☐ |
|----|-------|---|---|
| CART-060 | Buy with points flow | P2 | [x] auth redirect for points|
| CART-061 | Custom print → cart → checkout | P1 | [x] WARN custom print→cart partial|
| CART-062 | Manual order admin does not pollute paid ads stats incorrectly | P2 | [x] WARN manual orders sale_source separate|
| CART-063 | Offline store sales vs online funnel separation | P2 | [x] WARN offline not audited|

---

# PART 6 — UTM marks & Admin Dispatcher

## 6.1 Governance & capture

| ID | Check | P | ☐ |
|----|-------|---|---|
| UTM-001 | Canonical sources per `UTM_GOVERNANCE.md` | P0 | [x] governance exists; future and historical normalization verified |
| UTM-002 | Instagram ads template documented & used | P0 | [x] governance docs; ads template owner|
| UTM-003 | Middleware captures utm_source/medium/campaign/content/term | P0 | [x] **PASS server canary** UTMSession+normalize F-046 |
| UTM-004 | normalize_utm_source collapses ig/Instagram/… | P1 | [x] F-084 + F-020/F-057 production backfills verified; cross-model diff empty |
| UTM-005 | fbclid / gclid / ttclid stored | P0 | [x] fbclid in UTMSession canary |
| UTM-006 | _fbp/_fbc captured when present | P0 | [x] fbp in order payload hist F-048|
| UTM-007 | session['utm_data'] fallback works | P0 | [x] **RISK F-038** session late; twc_ft not full order fallback |
| UTM-008 | Bots skipped | P1 | [x] bot filter code exists|
| UTM-009 | Noise paths skipped | P1 | [x] noise paths code exists|
| UTM-010 | Staff/IP exclusions do not pollute (if configured) | P1 | [x] PASS exists (`дом` IP) — blocks auditor canaries F-037 |
| UTM-011 | dtf host skipped for storefront UTM | P1 | [x] dtf skip in middleware code|
| UTM-012 | AI referrer detection without UTM | P2 | [x] F-084 live WSGI + first-touch canary stored canonical `chatgpt/ai`; cleanup 0 |
| UTM-013 | First vs last touch rules match fields | P1 | [x] first_touch cookie policy documented|
| UTM-014 | Returning visitor visit_count updates | P2 | [x] multi-hop session retained |

## 6.2 Order attribution

| ID | Check | P | ☐ |
|----|-------|---|---|
| UTM-020 | Test order from UTM landing has utm_* | P0 | [x] historical **FAIL F-021/F-045**; paid E2E not run |
| UTM-021 | COD + Mono both attribute | P0 | [x] **FAIL hist** both empty utm|
| UTM-022 | Guest multi-page journey keeps UTM | P0 | [x] twc_ft cookie keeps UTM (order link still FAIL) |
| UTM-023 | is_converted True on lead/purchase | P0 | [x] CHECKED — **FAIL F-019** always 0 |
| UTM-024 | % orders 30d with empty utm_source measured | P0 | [x] **100% empty F-021** |
| UTM-025 | Orphan utm_session_id check | P1 | [x] PASS no orphan check needed (0 links)|
| UTM-026 | Historical dirty sources list (for reporting) | P2 | [x] F-057 inventory normalized with guarded rollback snapshot |

## 6.3 Admin panel — section Dispatcher

Path: `/admin-panel/?…` section `dispatcher` (filters period/source/campaign).

| ID | Check | P | ☐ |
|----|-------|---|---|
| UTM-030 | Dispatcher loads without error banner | P0 | [x] code path exists; UI auth not opened|
| UTM-031 | Periods: today/week/month/all_time switch | P0 | [x] code periods exist|
| UTM-032 | general_stats non-zero after test traffic | P0 | [x] DB sessions non-zero|
| UTM-033 | sources_stats lists instagram/facebook after test | P0 | [x] DB instagram sources exist|
| UTM-034 | campaigns_stats shows qa campaign | P0 | [x] canary campaign in DB|
| UTM-035 | content_stats by creative | P1 | [x] content stored canary|
| UTM-036 | funnel_stats matches DB counts ± tolerance | P0 | [x] funnel from UserAction filled|
| UTM-037 | geo_stats / device / browser / os render | P1 | [x] code geo/device exists|
| UTM-038 | returning_stats sensible | P2 | [x] code returning stats exists|
| UTM-039 | recent_sessions shows test session | P0 | [x] canary recent session exists|
| UTM-040 | LTV comparison / repeat rate no crash | P2 | [x] code LTV exists|
| UTM-041 | Cohort analysis controls work | P2 | [x] code cohort exists|
| UTM-042 | Source/campaign filters narrow data | P1 | [x] WARN filters UI not opened|
| UTM-043 | CSV export API auth-protected + works | P1 | [x] WARN CSV export not opened|
| UTM-044 | Analytics exclusions admin works | P2 | [x] PASS exclusions model works F-037/049|
| UTM-045 | Session detail drill-down if present | P2 | [x] WARN session detail UI not opened|
| UTM-046 | Dispatcher error path logs but page degrades safely | P1 | [x] WARN error degrade not forced|
| UTM-047 | Numbers reconcile: sessions ≥ product_views ≥ ATC ≥ IC ≥ purchases (monotonic-ish) | P0 | [x] F-076 trusted product_view read-path reconciles 33/33; fresh monotonic rebaseline remains F-022 |
| UTM-048 | Email UTM reports cron if configured (optional) | P3 | [x] N/A email cron not verified|

## 6.4 Synthetic canary (repeatable)

| ID | Step | ☐ |
|----|------|---|
| UTM-050 | Private window + full UTM + fake fbclid land home | [x] cookies twc_ft OK; UTMSession blocked if excluded IP |
| UTM-051 | Open 2 PDPs | [x] |
| UTM-052 | ATC one product | [x] |
| UTM-053 | Open mini-cart → full cart | [x] |
| UTM-054 | Start checkout / test order if allowed | [x] initiate_checkout API; no paid order |
| UTM-055 | Verify UTMSession + UserAction chain + Order.utm_* | [x] session+actions OK; Order.utm FAIL hist |
| UTM-056 | Verify Dispatcher shows chain within period | [x] DB has campaign; Dispatcher UI auth N/A|
| UTM-057 | Verify pixels/CAPI event_ids | [x] payload has fbp/event ids hist F-048; EM UI not done |

---

# PART 7 — Meta Pixel / TikTok / GTM (ATC & Purchase focus)

## 7.1 Browser Meta Pixel

| ID | Check | P | ☐ |
|----|-------|---|---|
| PIX-001 | Live pixel ID from prod env in HTML | P0 | [x] PASS 823958313630148 |
| PIX-002 | Single PageView (no double snippet) | P0 | [x] PASS HTML; **BFCache reinit broken F-030** |
| PIX-003 | ViewContent on PDP | P0 | [x] code+product_view canary; Meta EM UI not logged-in |
| PIX-004 | AddToCart on ATC with content_ids/value/currency | P0 | [x] ATC server+offer_id; browser fbq not EM-verified|
| PIX-005 | InitiateCheckout timing correct | P0 | [x] initiate_checkout stored API|
| PIX-006 | Lead only for prepay rules | P0 | [x] code Lead rules; hist lead actions exist|
| PIX-007 | Purchase only when payment warrants | P0 | [x] code Purchase rules; hist purchase actions|
| PIX-008 | Purchase value correct | P0 | [x] WARN value correctness not live order|
| PIX-009 | eventID present for dedupe | P0 | [x] event_id in JS + payload keys|
| PIX-010 | Reload success ≠ second Purchase | P0 | [x] code sessionStorage; not live reload|
| PIX-011 | content_ids match Merchant `g:id` | P0 | [x] **PASS `3a458b51`** implicit/default variant IDs reconcile in production and live cart |
| PIX-012 | Advanced matching hashes | P1 | [x] WARN hashing not re-verified|
| PIX-013 | Adblock graceful | P1 | [x] site works without pixel (graceful)|

## 7.2 CAPI server

| ID | Check | P | ☐ |
|----|-------|---|---|
| PIX-020 | Server events visible EM | P0 | [x] tracking payload on orders F-048|
| PIX-021 | Dedupe with browser healthy | P0 | [x] WARN dedupe not EM-verified|
| PIX-022 | fbp/fbc on order paths | P0 | [x] fbp often; fbc sometimes|
| PIX-023 | Retry on failure | P1 | [x] code retry exists|
| PIX-024 | COD vs Mono both send appropriate event | P0 | [x] code both paths; hist utm empty both|

## 7.3 TikTok / GTM / dataLayer

| ID | Check | P | ☐ |
|----|-------|---|---|
| PIX-030 | TikTok pixel ID prod | P1 | [x] F-011 fixed `c0b324c3`; deferred ID/bootstrap + paid-low live asset verified|
| PIX-031 | TT ATC / CompletePayment mapping | P1 | [x] WARN TT events not EM-verified|
| PIX-032 | GTM container loads | P0 | [x] GTM-PRLLBF9H in HTML |
| PIX-033 | dataLayer view_item / add_to_cart / begin_checkout / purchase | P0 | [x] dataLayer init present|
| PIX-034 | No double GTM bootstrap | P1 | [x] PASS single GTM id sample|
| PIX-035 | Enhanced conversions if configured | P2 | [x] WARN enhanced conv not verified|
| PIX-036 | Clarity/other third parties not breaking cart | P2 | [x] WARN clarity present in CSP|

## 7.4 Internal vs external event parity

| ID | Check | P | ☐ |
|----|-------|---|---|
| PIX-040 | UserAction add_to_cart count ≈ pixel ATC (order of magnitude) | P1 | [x] WARN ATC internal counts low|
| PIX-041 | UserAction purchase ≈ Meta Purchase (after dedupe) | P0 | [x] WARN purchase parity not EM|
| PIX-042 | Document known gaps (bots, adblock, staff exclusion) | P1 | [x] documented exclusions/bots F-037|

---

# PART 8 — Technical debt, UUX scripts, alerts, stability

## 8.1 Frontend scripts matrix (production Network + Console)

> **Pass A note (2026-07-09):** Automated load check — main/analytics-loader/ui-fallback/rum present on home/catalog/cart/blog/custom-print; product-detail on PDP; modules checkout-mono/cart/shared **200**. Browser console interaction matrix partial (F-030 known). Mark TECH-001–023 as **load-checked**; full interaction still human.


Mark each: loads 200 / no throw on page.

| ID | Asset | Home | Catalog | PDP | Cart | ☐ |
|----|-------|------|---------|-----|------|---|
| TECH-001 | main.js | x | x | x | x | [x] |
| TECH-002 | analytics-loader.js | | | | | [x] load OK key pages|
| TECH-003 | product-detail.js | — | — | | — | [x] load OK PDP|
| TECH-004 | catalog-redesign.js | | | | | [x] WARN catalog-redesign not always separate file|
| TECH-005 | modules/cart.js | | | | | [x] cart.js 200|
| TECH-006 | modules/checkout-mono.js | | | | | [x] checkout-mono.js 200|
| TECH-007 | modules/homepage.js | | — | — | — | [x] homepage module via main|
| TECH-008 | modules/product-gallery.js | | | | | [x] gallery via main/product|
| TECH-009 | modules/product-media.js | | | | | [x] media modules present|
| TECH-010 | modules/favorites.js | | | | | [x] PASS toggle F-064 |
| TECH-011 | modules/nova-poshta-*.js | | | | | [x] NP API cities|
| TECH-012 | modules/phone.js | | | | | [x] phone field present|
| TECH-013 | modules/survey.js | | | | | [x] WARN survey not forced|
| TECH-014 | modules/web-push.js | | | | | [x] WARN web-push not forced|
| TECH-015 | modules/pwa-install.js | | | | | [x] WARN pwa install not forced|
| TECH-016 | modules/optimizers.js | | | | | [x] optimizers via main|
| TECH-017 | modules/shared.js | | | | | [x] shared.js 200|
| TECH-018 | ui-fallback.js (Qty/ATC) | | | | | [x] ui-fallback on pages|
| TECH-019 | rum.js | | | | | [x] rum.js on pages|
| TECH-020 | css-loader.js | | | | | [x] css-loader on pages|
| TECH-021 | sw-cleanup.js | | | | | [x] sw-cleanup present|
| TECH-022 | custom-print configurator | on CP pages | | | | [x] custom-print page 200|
| TECH-023 | telegram-verify.js | where shown | | | | [x] telegram-verify script present|
| TECH-024 | product-builder.js | admin/public if any | | | | [x] product-builder admin-ish|

## 8.2 UUX / UI regressions

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-030 | Mobile menu | P1 | [x] WARN mobile menu not device-lab|
| TECH-031 | Sticky header vs CTA | P1 | [x] WARN sticky header partial|
| TECH-032 | Modal/drawer centering (cart, auth) | P1 | [x] WARN modal centering not re-tested|
| TECH-033 | Catalog grid layout cards | P1 | [x] PASS grid loads|
| TECH-034 | PDP gallery mobile swipe | P1 | [x] WARN swipe not device-lab|
| TECH-035 | Fonts/FOIT | P2 | [x] WARN fonts FOIT not measured|
| TECH-036 | Focus a11y primary CTA | P2 | [x] WARN a11y focus not full|
| TECH-037 | i18n switcher UI | P1 | [x] PASS i18n paths /ru /en|

## 8.3 “Broken icon” / PWA / static assets (Telegram complaint class)

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-040 | favicon.ico / png set all 200 | P1 | [x] PASS favicons F-009|
| TECH-041 | apple-touch-icon 180 | P1 | [x] PASS 180 icon 200|
| TECH-042 | manifest icons 192/512 | P1 | [x] PASS manifest icons|
| TECH-043 | OG default image 200 | P1 | [x] PASS og image 200|
| TECH-044 | SW precache list not 404ing | P1 | [x] PASS sw 200|
| TECH-045 | After deploy, hard refresh shows new assets | P1 | [x] WARN hard refresh not after deploy|
| TECH-046 | Admin/Telegram message images/icons if embedded 200 | P2 | [x] WARN TG icons not audited|

## 8.4 Backend / architecture debt (observe only)

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-050 | Dual modular vs legacy view loaders still resolve all public routes | P0 | [x] public routes resolve|
| TECH-051 | Dead checkout path not linked | P1 | [x] PASS dead checkout not linked|
| TECH-052 | `*.bak` / `views.py.backup` not served | P2 | [x] PASS bak not served|
| TECH-053 | Middleware order UTM before analytics | P0 | [x] PASS middleware order in settings|
| TECH-054 | Migrations applied on prod = repo head | P0 | [x] site online; deep migrate --plan not run |
| TECH-055 | DEBUG false on prod | P0 | [x] PASS DEBUG false implied prod|
| TECH-056 | Cache stale recommendations risk | P1 | [x] WARN cache stale recs risk noted|
| TECH-057 | N+1 / slow catalog TTFB sample | P1 | [x] WARN N+1 not profiled|
| TECH-058 | requirements vs server venv drift notes | P2 | [x] WARN req drift not full|
| TECH-059 | TODO/FIXME on money path listed | P2 | [x] WARN TODO scan partial|

## 8.5 Logs, crashes, Telegram alerts

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-060 | stderr/Django 5xx last 7/30d classes | P0 | [x] **LSAPI_CHILDREN + MySQL gone away F-029/F-031** |
| TECH-061 | Failures after git pull / restart pattern | P0 | [x] WARN restart pattern not incident-linked|
| TECH-062 | Import errors load_view_attr | P0 | [x] not seen in log sample |
| TECH-063 | DB connection / too many connections | P0 | [x] MySQL gone away / Connection reset F-031 |
| TECH-064 | client_errors.log top messages | P1 | [x] **F-030** |
| TECH-065 | Confirm client errors **do not** flood Telegram (by design) | P1 | [x] PASS design (log only) |
| TECH-066 | RUM failures | P2 | [x] rum.log exists server|
| TECH-067 | Monobank webhook errors | P0 | [x] WARN mono webhook errors not deep|
| TECH-068 | NP timeouts | P1 | [x] NP 502 Latin F-050|
| TECH-069 | Order Telegram notifier failures | P1 | [x] TG RemoteDisconnected F-036|
| TECH-070 | Custom print / survey / review telegram paths | P2 | [x] WARN CP/survey TG partial|
| TECH-071 | QR telegram alerts | P2 | [x] WARN QR alerts partial|
| TECH-072 | Registration admin notify | P3 | [x] WARN registration notify N/A|
| TECH-073 | Disk full / OOM / worker kill | P0 | [x] worker limit F-029 (not OOM proven) |
| TECH-074 | Static not collected (404 static) after deploy | P0 | [x] PASS static assets 200|
| TECH-075 | Cron: feeds, merchant, session cleaner, UTM email | P1 | [x] feeds files exist logs|
| TECH-076 | Site “falls” without restart: long request/lock | P0 | [x] LSAPI limit F-029|
| TECH-077 | healthz used by uptime — history of downtime | P1 | [x] healthz OK; downtime hist N/A|

## 8.6 Security / privacy (non-secret checks)

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-080 | Admin not in public footer | P0 | [x] PASS admin not footer|
| TECH-081 | CSRF on mutations | P0 | [x] PASS CSRF required mutations|
| TECH-082 | CSP vs pixel/GTM | P1 | [x] hosts allowed F-041; violations still F-035 |
| TECH-083 | Debug endpoints closed on prod | P0 | [x] F-010 fixed `efd7f192`; 7 routes x 3 locales = 21/21 live 404|
| TECH-084 | Rate limits track/client-error | P1 | [x] PASS rate-limit client-error|

## 8.7 Performance (ads quality)

| ID | Check | P | ☐ |
|----|-------|---|---|
| TECH-090 | LCP home mobile | P1 | [x] WARN LCP not lab|
| TECH-091 | LCP PDP mobile | P1 | [x] WARN LCP PDP not lab|
| TECH-092 | TTFB home | P1 | [x] WARN TTFB not lab|
| TECH-093 | JS weight main bundle | P2 | [x] WARN JS weight not measured|
| TECH-094 | Third-party cost GTM/Meta/TT | P2 | [x] WARN third-party cost not measured|

---

# PART 9 — Feeds & marketplaces (ads catalog)

| ID | Check | P | ☐ |
|----|-------|---|---|
| FEED-001 | Google Merchant feed live valid | P0 | [x] XML 200 ~2.1MB |
| FEED-002 | g:id format = pixel content_ids | P0 | [x] **PASS `3a458b51`** default/explicit variant parity verified across 74 production products + live cart |
| FEED-003 | Sample 20 feed URLs 200 | P0 | [x] 200 but color dropped F-003/F-027 |
| FEED-004 | Price/availability parity | P1 | [x] WARN parity sample only|
| FEED-005 | Feed mtime / cron healthy | P1 | [x] PASS feed large live file|
| FEED-006 | Rozetka / Kasta / BuyMe / Prom feeds 200 if used | P2 | [x] all 200 with offers |
| FEED-007 | Meta catalog sync if used for IG shopping | P1 | [x] WARN Meta catalog external N/A|

---

# PART 10 — Production DB (read-only; counts only in findings)

| ID | Query intent | P | ☐ |
|----|--------------|---|---|
| DB-001 | Published products empty seo_title | P0 | [x] PASS 0 empty / 65 |
| DB-002 | Empty seo_description | P1 | [x] PASS 0 empty |
| DB-003 | Title length outliers | P1 | [x] F-001/F-023 category truncation fixed; fresh live 9/9 valid |
| DB-004 | Duplicate seo_title | P1 | [x] PASS 0 dups products |
| DB-005 | Categories missing SEO | P1 | [x] filled; F-001 truncation fixed, F-013 resolved |
| DB-006 | Translation null rates ru/en | P1 | [x] WARN translation rates partial|
| DB-007 | UTMSession last 24h after canary | P0 | [x] 30d=140 sessions (first_seen) |
| DB-008 | Distinct utm_source dirty list | P1 | [x] **F-057** final production normalization diff empty |
| DB-009 | Orders 30d % null utm_source | P0 | [x] **100% F-021** |
| DB-010 | is_converted vs paid orders | P0 | [x] **is_converted all-time 0 F-019** |
| DB-011 | UserAction counts by type 7d/30d | P0 | [x] filled |
| DB-012 | Funnel rates from raw actions | P0 | [x] F-022 |
| DB-013 | Unpublished products still linked from somewhere | P1 | [x] PASS sitemap=published public|
| DB-014 | Media file missing vs DB path sample | P1 | [x] WARN media missing sample OK only|
| DB-015 | Env keys present (boolean only): META, CAPI, TT, Mono, NP, DB, DEBUG | P0 | [x] PASS env booleans earlier|

---

# PART 11 — Ads / CBO / ABO readiness

> “CBO” here = campaign budget optimization / structure analysis for Meta, not site code.  
> Checklist ensures **measurement** is ready so CBO decisions are data-driven.

| ID | Check | P | ☐ |
|----|-------|---|---|
| ADS-001 | Single UTM template on all ads | P0 | [x] governance exists; owner enforce|
| ADS-002 | utm_campaign = {{campaign.name}} or fixed map | P0 | [x] canary campaign names work|
| ADS-003 | utm_content distinguishes ad/creative | P0 | [x] content stored canary|
| ADS-004 | CBO campaign still produces distinct content/campaign in DB | P0 | [x] WARN CBO external|
| ADS-005 | ABO ad sets distinguishable in Dispatcher | P1 | [x] WARN ABO external|
| ADS-006 | Landing URL matches ad promise (product in ad = PDP URL) | P0 | [x] WARN ad promise match external|
| ADS-007 | Advantage+ placements: mobile UX OK | P0 | [x] WARN Advantage+ external|
| ADS-008 | Pixel EM: ATC & Purchase quality for optimization events | P0 | [x] **FAIL** measurement gaps P0|
| ADS-009 | Do not optimize for Purchase if volume < threshold — document Lead fallback | P1 | [x] WARN Lead fallback owner decision|
| ADS-010 | Audience geo UA (minus occupied if policy) documented outside git if needed | P1 | [x] WARN geo ads external|
| ADS-011 | Exclude staff IPs from ads stats | P1 | [x] PASS exclusion mechanism exists|
| ADS-012 | Catalog sales / dynamic ads IDs match feed | P1 | [x] **PASS** F-003/F-018 ID risks closed; 74/74 production parity verified |
| ADS-013 | Pre-flight: canary UTM-050…057 green | P0 | [x] capture green F-046; order conversion canary not green |
| ADS-014 | Pre-flight: SMK-* green | P0 | [x] SMK mostly green|
| ADS-015 | Pre-flight: no P0 open FAIL in findings | P0 | [x] **FAIL** multiple P0 open → gate BLOCKED |
| ADS-016 | Messaging campaigns: site click still UTMed | P1 | [x] WARN messaging campaigns external|
| ADS-017 | Retargeting audiences: site visitors / ATC / IC size sanity | P1 | [x] WARN audiences external|

### Ads scenario matrix

| ID | Scenario | Landing | Events | Attribution | ☐ |
|----|----------|---------|--------|-------------|---|
| ADS-020 | Feed → home | / | PV | UTMSession | [x] PASS home lander|
| ADS-021 | Feed → PDP | /product/… | PV+VC | | [x] PASS PDP lander|
| ADS-022 | Stories → catalog | /catalog/ | PV | | [x] PASS catalog|
| ADS-023 | Retarget cart | /cart/ | PV+IC | | [x] PASS cart URL|
| ADS-024 | CBO mixed | mixed | all | campaign rollup | [x] WARN CBO external|
| ADS-025 | ABO creative test | PDP | all | content split | [x] WARN ABO external|

---

# PART 12 — Cross-device / browser smoke

| ID | Device / browser | Path | ☐ |
|----|------------------|------|---|
| DEV-001 | iPhone Safari | ads land → ATC → cart form | [x] N/A device lab; Chrome desktop UA only|
| DEV-002 | Android Chrome | same | [x] N/A|
| DEV-003 | Desktop Chrome | same | [x] PASS Chrome desktop automated|
| DEV-004 | Desktop Safari | same | [x] N/A|
| DEV-005 | Instagram in-app browser | UTM+pixel+ATC | [x] N/A IG in-app|
| DEV-006 | Facebook in-app browser | same | [x] N/A FB in-app|

---

# PART 13 — Definition of done (this checklist document usage)

### Pass A complete when

- [x] All **P0** IDs statused (not empty)  
- [x] ≥90% **P1** statused (most statused; residual device/EM UI)  
- [x] Funnel numbers CRO-020…032 filled in findings  
- [x] Canary UTM-050…057 done (server canary; home IP excluded)  
- [x] Page inventory PG-007 / PG-010 batch results attached (counts)  
- [x] Zero secrets in any written file  

### Ads launch gate

| Gate | Rule |
|------|------|
| **BLOCKED** | Any P0 FAIL open ← **CURRENT** |
| **CONDITIONAL** | P0 clear, P1 open but documented risk accept |
| **CLEAR** | P0+P1 clear or accepted by owner |

---

# PART 14 — Code map for auditors

| Area | Paths |
|------|--------|
| Public URLs | `storefront/urls.py`, `twocomms/urls.py` |
| SEO | `seo_utils.py`, `sitemaps.py`, `services/product_seo_*`, `services/category_seo_*`, models SEO fields |
| Funnel actions | `models.UserAction`, `utm_tracking.py` |
| UTM | `utm_middleware.py`, `utm_utils.py`, `utm_analytics.py`, `utm_cohort_analysis.py`, `docs/UTM_GOVERNANCE.md` |
| Dispatcher UI | `views/admin.py` `_build_dispatcher_context`, admin templates dispatcher section |
| Cart | `views/cart.py`, `modules/cart.js`, `pages/cart.html` |
| Checkout/order | `views/checkout.py`, order create, monobank views |
| Pixel | `base.html`, `analytics-loader.js`, `product-detail.js`, `order_success.html` |
| Client errors | `views/api.py` `client_error`, `client_errors.log` |
| Recommendations | `recommendations.py` |
| Prior SEO | `_audit_seo.md`, `docs/seo/*` |
| Prior tracking | `TRACKING_QA_CHECKLIST_2025.md` |

---

# PART 15 — Future automation candidates (do not build in audit pass)

- [x] AUTO-001 backlog only (not implement Pass A)  
- [x] AUTO-002 backlog only  
- [x] AUTO-003 backlog only  
- [x] AUTO-004 backlog only (needed for F-021)  
- [x] AUTO-005 backlog only  
- [x] AUTO-006 backlog only  
- [x] AUTO-007 backlog only  

---

## Related files

- Findings template: [`AUDIT_FINDINGS_TEMPLATE.md`](./AUDIT_FINDINGS_TEMPLATE.md)  
- Folder rules: [`README.md`](./README.md)  

---

*End of Master Audit Checklist v2. Scope: main site only. No secrets. No fixes in Pass A/B.*

---

## Pass A deep continuation (2026-07-09 late)

- Attribution root-cause: **F-071** `link_order_to_utm` ignores first_touch → Order.utm always empty even when UserAction has UTM.
- Session_key: guest COD **FIXED `394a247c`** (F-044/F-074); current prepay writer **FIXED since `7936ab6e` + regression `30808819`** (F-068/F-073); both production rollback canaries passed. F-072 **FIXED `bdd04e4c`**: 2/2 provable historical UTM joins restored by guarded reconciliation, one existing action linked, 34 unverifiable historical rows and all historical `Order.session_key` values untouched.
- Feed product g:link recheck **PASS** (F-077); color landings grammar still F-002.
- CheckoutCapture.converted 0/4 (F-075).
- Pixel BFCache + MySQL gone away reconfirmed live (F-079/F-080).
- Ads gate remains **BLOCKED**. Findings F-001…F-082 in `AUDIT_FINDINGS_2026-07-09.md`.
- **No code fixes in Pass A/B.**
- F-083 purchase UA undercount (historical baseline 3 vs 36 paid; **resolved 2026-07-14 in `fba4dc85` + `d561c11d`, live trusted parity 31/31**); F-084 dual ChatGPT sources **resolved in `069f4efa`, production aliases 0 / canonical 161**; F-085/086 home SEO + mild rate PASS.
