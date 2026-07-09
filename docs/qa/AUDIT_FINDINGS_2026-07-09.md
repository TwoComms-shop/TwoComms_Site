# Audit Findings — TwoComms Main Site

**Date:** 2026-07-09  
**Checklist version:** `PRE_ADS_MASTER_AUDIT_CHECKLIST.md` **v2**  
**Auditor (Pass A/B):** agent-pass-a (production HTTP + HTML + sitemap + feed)  
**Confirmer (Pass C):** _pending_  
**Environment:** production `https://twocomms.shop`  
**Scope:** main site only  
**Method:** live curl/python fetch; no code changes; no secrets stored  

## Security

- No SSH/DB/API tokens in this file.
- Pixel IDs below are already public in HTML (`data-meta-pixel-id`).

---

## Executive summary

**Ads launch gate (current, partial Pass A):** **CONDITIONAL → leans BLOCKED for catalog ads** until Pass C confirms **F-003** (Merchant feed URL mangling) and **F-001/F-002** (category/color SEO quality).

**One paragraph:** Core smoke (home, catalog, cart, healthz, robots, sitemap index, www→apex, UTM first-touch cookies, Meta pixel ID + single PageView snippet, cart mini APIs) **works**. Sitemap children load; **65** UK product URLs + locales present. Sample of **22/22** product PDPs returned **200** with Product JSON-LD and UAH prices. **Serious SEO quality bugs** on category titles (truncated mid-phrase), color landings (broken Ukrainian grammar), product title↔H1 mismatches (including Russian leak in H1). **Merchant feed** `g:link` values use HTML-escaped `&amp;` that the site interprets as path `/s/?amp;color=…` instead of proper size/color query — high risk for Shopping/Meta catalog. Aggressive **HTTP 429** rate limiting mid-crawl. RU/EN pages still show **Ukrainian H1** on home/catalog. Pixel **ViewContent/ATC/Purchase E2E** and **Dispatcher/DB funnel** not fully verified yet (need browser + admin/DB).

### Counts (open findings)

| Severity | Open | Confirmed (C) | False positive | Fixed |
|----------|------|---------------|----------------|-------|
| P0 | 1 | 0 | 0 | 0 |
| P1 | 7 | 0 | 0 | 0 |
| P2 | 6 | 0 | 0 | 0 |
| P3 | 2 | 0 | 0 | 0 |

### Pass A coverage (honest)

| Block | Done % | Notes |
|-------|--------|-------|
| 0 Smoke / SEC | ~90% | SMK mostly PASS; full console browser not done |
| 1 Page inventory PG-* | ~45% | Core + static + 22 PDP + 3 cats + colors/themes; not all products×locales |
| 2 SEO deep | ~40% | Titles/canonicals/sitemap structure; full product title DB scan pending |
| 3 GEO | ~30% | H1 leaks confirmed sample; full leak inventory pending |
| 4 CRO funnel | ~10% | Structure mapped; live funnel numbers need Dispatcher/DB |
| 5 CART | ~35% | Empty mini-cart/APIs OK; full ATC→checkout E2E pending |
| 6 UTM / Dispatcher | ~25% | First-touch cookies OK; Dispatcher UI needs auth; order link pending |
| 7 PIX | ~30% | ID + single init/PageView HTML; Events Manager / ATC browser pending |
| 8 TECH | ~35% | 429, favicons, debug login-gated; logs/Telegram pending server |
| 9 FEED | ~50% | Feed live; **link mangling found**; id format TC-* |
| 10 DB | 0% | Needs server MySQL read-only |
| 11 ADS | ~15% | Measurement risks from F-003 |
| 12 DEV browsers | 0% | |

---

## Funnel snapshot (CRO)

**Status:** NOT YET FILLED from production DB/Dispatcher (blocked without admin session / SSH DB).

| Stage | Count | Rate | Notes |
|-------|------:|------|-------|
| sessions | — | | need `get_funnel_stats` |
| product_view | — | | |
| add_to_cart | — | | |
| initiate_checkout | — | | |
| lead | — | | |
| purchase | — | | |

**HTML/API readiness for funnel path:**

- Mini-cart empty: `GET /cart/mini/` → 200, UA empty text «Кошик порожній.»
- `GET /cart/count/` → `{"cart_count": 0}`
- `GET /cart/summary/` → ok count/total 0
- Full cart: `noindex,nofollow`, pay types present: `online_full`, `prepay_200`, monobank + promo + NP signals in HTML

---

## SEO batch results

| Batch | Total | OK | Fail / issue | Notes |
|-------|------:|---:|-------------:|-------|
| Smoke core pages | 11 | 11 | 0 | home, catalog, cart, contacts, blog, etc. |
| Sitemap child files | 8 | 8 | 0 | all 200 |
| Sitemap unique locs (fast crawl) | 489 | 214* | 275×429* | *rate limit; not real 404 |
| UK products (full slow) | 65 | 65 | 0 HTTP; **13 title/H1 name mismatches** | F-004 |
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

- [ ] **Open** · Severity: **P0** · Area: **FEED / ADS** · Checklist: FEED-001–003, ADS-012, PIX-011

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

**Risk of fix:** **HIGH** — feed ID/link changes can break Meta/Google catalogs; need staged regen + re-fetch validation + pixel content_id alignment. **Do not hotfix without Pass C + catalog freeze plan.**

**Pass C:** parse feed with XML lib (so `&amp;`→`&`), GET decoded links; compare final URL to expected size/color; sample content_ids vs pixel.

---

### F-004 — Product title vs H1 mismatch (and RU leak in H1)

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

- [ ] **Open** · Severity: **P2** · Area: **PIXEL** · Checklist: PIX-030

| Field | Value |
|-------|--------|
| Status (B) | SUSPECTED |
| Status (C) | |

`data-tiktok-pixel-id="D43L7DBC77UA61AHLTVG"` present; raw HTML search found **no** `ttq.load` / `ttq('load'`. May load via `analytics-loader.js` after bootstrap — **must verify in browser Network tab**.

---

### F-012 — ViewContent not embedded in PDP HTML (JS-only)

- [ ] **Open** · Severity: **P2** · Area: **PIXEL / CRO** · Checklist: PIX-003, CRO-004

| Field | Value |
|-------|--------|
| Status (B) | SUSPECTED (expected architecture) |
| Status (C) | |

Sample PDPs: no `ViewContent` / `view_item` string in HTML; product-detail.js is the intended path. **Not a bug until browser proves events missing.** Pass C: Meta Pixel Helper on PDP.

---

### F-013 — Category titles vs H1 length strategy inconsistent

- [ ] **Open** · Severity: **P2** · Area: **SEO** · Checklist: SEO-031

Related to F-001: H1s are long complete sentences; titles are shorter and cut. May be intentional length limit with bad truncation.

---

### F-014 — Sitemap product `lastmod` clustered 2026-06-11

- [ ] **Open** · Severity: **P3** · Area: **SEO** · Checklist: SEO-067

Products/variants/categories lastmod in index point to **2026-06-11** while blog newer (2026-06-29). Possible stale lastmod pipeline — Google may under-crawl updates.

---

### F-015 — `manifest.webmanifest` 404 while `site.webmanifest` 200

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

## Sign-off

| Role | Name | Date |
|------|------|------|
| Pass B | agent-pass-a | 2026-07-09 |
| Pass C | | |
| Ads gate owner | | |

---

### F-016 — Variant URL titles work (positive control)

- [x] **Not a bug** · Area: **SEO** · Checklist: SEO-007, SMK-005 partial

Sample **20/20** variant sitemap URLs returned **200** with titles reflecting color/fit, e.g.:

- `/product/classic-tshirt/black/` → `Футболка класична — чорний — TwoComms`  
- `/product/classic-tshirt/black/oversize/` → `… — чорний, оверсайз фіт — TwoComms`  

**Note:** some titles may exceed 65 when both color+fit appended (truncation in SERP only) — minor P3 follow-up.

---

### F-017 — HTML site map (`/mapa-saytu/`) internal links all 200

- [x] **PASS** · Checklist: SEO-071, PG-044

53 unique internal links from mapa page checked slowly → **0 non-200**.

---

## Session changelog

| Time | Action |
|------|--------|
| 2026-07-09 | Pass A started; smoke, SEO sample, sitemap, feed, UTM cookies, findings F-001…F-015 |
| 2026-07-09 | Full 65 UK PDP titles; 13 title/H1 mismatches; 20 variants OK; mapa links OK; F-004 expanded; F-016/F-017 |
| _next_ | Browser pixel ATC/Purchase; Dispatcher/DB funnel; server logs; remaining locales products; full variant sitemap |
