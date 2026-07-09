# IMPLEMENTATION_PLAN vs Production vs docs/qa Findings

**Date:** 2026-07-09 (late)  
**Sources:**
- Plan: `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` (v2, top-level **117** items: **45 DONE** / **72 OPEN**)
- Audit findings: `docs/qa/AUDIT_FINDINGS_2026-07-09.md` (F-001…F-086)
- Master walk checklist: `docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md` (Pass A complete; Pass C/D not)
- Truth: production `https://twocomms.shop` + MySQL via Django shell + code in repo/server

**Method:** re-check every plan item including those marked `[x]`.  
**Verdict vocabulary:**
| Verdict | Meaning |
|---------|---------|
| **OK** | Code + prod accept criteria hold (or REPO-only item with evidence) |
| **PARTIAL** | Code/work done, but prod accept incomplete / remaining OWNER·SERVER |
| **REOPEN** | Marked DONE in plan but **broken or incomplete** on prod / code gap |
| **OPEN** | Still `[ ]` in plan — not done |
| **N/A VERIFY** | Needs owner UI / paid test / long soak — not fully re-proven here |

**Ads launch gate (still):** **BLOCKED**

---

## Executive summary

| Bucket | Count (top-level) | Notes |
|--------|------------------:|-------|
| Plan DONE claimed | 45 | Many OK for checkout/security |
| Of DONE → **REOPEN** | **8+** | Attribution, titles, pixel BFCache, EN H1, mono capture, backup ops… |
| Of DONE → **PARTIAL** | **~10** | Normalize, PV hygiene, ADS-1, W3-6/9, W0-*, W7-1… |
| Of DONE → **OK** | **~25** | W1 money core, W3 cache/rate-limit, W1-2/3/8/12… |
| Plan OPEN | 72 | Waves 4–7 bulk, ADS-4…7, SERVER, OWNER |
| Nested OPEN (W2-10 / W5-8) | ~15 | ATC CAPI, consent, offer_id… |

**Critical reopen (do before paid ads scale):**

1. **W2-1 / F-071** — `link_order_to_utm` ignores `analytics_first_touch_data` → **0/43** Order.utm  
2. **W2-2 / F-019** — `is_converted` **0/1047**  
3. **W2-3 / F-083** — purchase UserAction **3** vs paid **36**  
4. **ADS-1 residual / F-030** — `initializePixelsImmediately` **called, not defined** (10× client_errors)  
5. **ADS-3 residual / F-001+F-023** — Category.seo_title **still truncated in MySQL** (code TITLE_LIMIT=70 insufficient alone)  
6. **W1-11 CONFIRMED LIVE** — `media/ubd_docs/*` returns **HTTP 200** public (PII)  
7. **Monobank path gaps** — no `CheckoutCapture.converted` (F-075), no `attach_tracking_to_order` in monobank.py  
8. **W0-3 SERVER** — backup script on server, **no backup cron**; only log rotate cron present  

---

## Legend: status columns in matrices

- **Plan** = checkbox in IMPLEMENTATION_PLAN  
- **Code** = local/repo re-check  
- **Prod** = live HTTP / MySQL / server 2026-07-09  
- **docs/qa** = finding IDs  

---

# WAVE 0 — Safeguards

| ID | Plan | Verdict | Code | Prod | docs/qa | Evidence / gap |
|----|------|---------|------|------|---------|----------------|
| **W0-1** SSH password | OPEN (REPO verified) | **PARTIAL** | password not in tracked files | password auth still works for audit SSH | — | REPO clean; **OWNER must rotate password / keys** (still using compromised password path) |
| **W0-2** secret rotation | OPEN | **OPEN** | — | not verified rotated | — | git history secrets; **OWNER/SERVER** |
| **W0-3** MySQL backups | OPEN (REPO done) | **PARTIAL** | `scripts/backup_mysql.sh` exists | script on server; **`crontab` has NO backup**; `~/db_backups` not confirmed scheduled | — | Only log rotate cron at 04:10 |
| **W0-4** money smoke tests | DONE | **OK** | test_checkout / webhook / cart_sync / utm tests present | N/A local CI | — | Accept: green suite in plan journal |
| **W0-5** crontab docs + stash | DONE (REPO) | **PARTIAL** | `docs/OPS.md` | 10 stash OWNER open | — | REPO docs OK |
| **W0-6** service-account JSON | OPEN | **OPEN** | — | `find` returned no `*service*account*.json` in TWC tree (good signal, not full FS audit) | — | Still open until intentional S-7 pass |

---

# WAVE 1 — Money / checkout

| ID | Plan | Verdict | Code | Prod | docs/qa | Evidence / gap |
|----|------|---------|------|------|---------|----------------|
| **W1-1** guest COD 500 | DONE | **OK / PARTIAL** | cart → `order_create`; COD not in UI (decision) | COD order #275 exists (audit test) | F-074 | **Gap:** COD `create_order` **no `_ensure_session_key`** → #275 empty session_key |
| **W1-2** order PII leak | DONE | **OK** | owner/session check; preview staff | `/orders/success-preview/` → admin login; `/orders/success/1/` → 404 | — | Live PASS |
| **W1-3** mono signature | DONE | **OK** | `_verify_monobank_signature` ECDSA | code on server | — | |
| **W1-4** promo COD + limits | DONE | **OK** | `promo_code_id` + `_record_promo_usage_for_order` | API field `promo_code` (F-070 INFO) | F-070 | |
| **W1-5** checkout error states | DONE | **OK** | missing product / zero total guards | mono validates city 400 | F-052 | |
| **W1-6** cabinet pay methods | DONE | **OK** | restored + owner checks | N/A VERIFY (auth UI) | — | |
| **W1-7** mobile hero CTA | DONE | **OK** | CSS overrides 3 places | N/A VERIFY device | — | plan browser accept earlier |
| **W1-8** /test-analytics/ | DONE | **OK** | staff_required | live → admin login | — | |
| **W1-9** dropship webhook sign | DONE | **OK** | shared verify | N/A VERIFY | — | |
| **W1-10** profile upload | DONE | **OK** | size/ImageField | N/A VERIFY | — | |
| **W1-11** ubd_doc public media | OPEN | **OPEN → CONFIRMED FAIL** | files in media/ | **HTTP 200** on `media/ubd_docs/<file>` even with Referer | — | **P1 security live** — fix urgently |
| **W1-12** pull-verify amount | DONE | **OK** | `_resolve_retail_invoice_status` + amount | code present | — | |
| **W1-13** qty cap | DONE | **OK** | `MAX_CART_ITEM_QTY=50` | N/A | — | |
| **W1-14** double-submit | DONE | **OK** | fingerprint 30s | N/A | — | |

### Wave 1 incomplete side-effects (not separate plan IDs)

| Gap | Severity | Evidence |
|-----|----------|----------|
| Monobank **never** marks `CheckoutCapture.converted` | P2 | monobank.py: 0 `CheckoutCapture`; checkout.py COD path sets converted; **prod 0/4 converted** (F-075) even when order+capture share session |
| Monobank **no** `attach_tracking_to_order` | P1 | only inline tracking block; COD uses attach helper |
| COD no session ensure | P1 | F-074 order 275 |

---

# WAVE 2 — Attribution / analytics

| ID | Plan | Verdict | Code | Prod | docs/qa | Evidence / gap |
|----|------|---------|------|------|---------|----------------|
| **W2-1** UTM on any order | DONE | **REOPEN** | fallback session_key → visitor → `session['utm_data']`; **NO first_touch cookie** | **utm empty 43/43**; utm_session 0; order 276 had first_touch UTM in UserAction, Order blank | **F-021, F-033, F-045, F-071** | Accept CRO-050 **FAIL** |
| **W2-2** is_converted | DONE | **REOPEN** | `mark_as_converted` only if utm_session on purchase/lead | **converted 0 / 1047** | **F-019** | Blocked by W2-1 fail |
| **W2-3** purchase definition | DONE | **REOPEN / PARTIAL** | docs + NP path + mono webhook purchase | purchase UA **3** vs paid **36** | **F-083** | Manual orders + older mono miss purchase UA |
| **W2-4** bot filter / PV dedupe | DONE | **PARTIAL** | bot/staff filter + 30m dedupe + SiteSession get_or_create | Historical **41k** PV noise remains; site_session still sparse historically | **F-022, F-076** | Code OK; baseline not clean yet |
| **W2-5** GTM fast-path paid | DONE | **OK** | fbclid/utm in base + loader | early load path present | — | |
| **W2-6** TikTok event names | DONE | **OK** | CompletePayment map client+server | N/A VERIFY EM | — | |
| **W2-7** CAPI outside row-lock | DONE | **OK / PARTIAL** | `transaction.on_commit` + `_dispatch_post_payment_events` in utils | mono uses on_commit pattern via utils | — | verify no remaining in-lock CAPI on all branches |
| **W2-8** utm normalize + AI | DONE | **PARTIAL** | aliases + detect_ai → `chatgpt` | last 3d still **`chatgpt.com`×7** and **`ig`×1** alongside clean instagram/chatgpt | **F-020, F-057, F-084** | New dirt still lands; path without normalize or pre-deploy rows |
| **W2-9** CAPI domain / event_id | DONE | **OK** | twocomms.shop; no random AddPaymentInfo id | N/A VERIFY | — | |
| **W2-10** misc analytics | OPEN | **OPEN** | nested partial done | — | nested below | |

### W2-10 nested

| Sub-ID | Plan | Verdict | Notes / docs/qa |
|--------|------|---------|-----------------|
| AN-014 delivered multi-channel | nested x | PARTIAL | TikTok+UA done; GA4 MP **OWNER**; refund TODO |
| **CRO-033** server ATC CAPI | OPEN | OPEN | blockers lose ATC |
| AN-036 visit window | nested x | OK (code) | |
| **AN-037** first/last touch fields | OPEN | OPEN | **links F-071** — cookie vs session desync |
| **AN-050** cookie consent | OPEN | OPEN | |
| AN-051 cleanup command | nested x | PARTIAL | command exists; **no cleanup cron** on prod |
| AN-038 / DB-005 N+1 reports | OPEN | OPEN | |
| DB-002 index | nested x | OK | |
| DB-003 orphans cleanup | nested x | PARTIAL | needs SERVER run |
| AN-039 search mask | nested x | OK | |
| **AN-001** double gtag/GTM | OPEN | OPEN | |
| **AN-003** payment_type / item_id | OPEN | OPEN | |
| **NEW-406** offer_id ЧЕРНЫЙ | OPEN | OPEN | **F-018** |
| NEW-407 pay_type cod default | OPEN | OPEN | |

### Root-cause diagram (W2-1 REOPEN)

```
twc_ft / analytics_first_touch_data  ──writes──► UserAction.metadata.first_touch  ✓
                                                 payment_payload click-IDs (partial) ✓
                                          ──X──► Order.utm_* / Order.utm_session    ✗
session['utm_data'] / UTMSession row     ──if present──► Order.utm_*                ✓ (rare)
```

LiteSpeed anon cache + delayed session + exclusion IP ⇒ often **only first_touch cookie** has UTM at checkout → **link_order finds nothing**.

---

# WAVE 3 — Reliability

| ID | Plan | Verdict | Code | Prod | docs/qa | Evidence / gap |
|----|------|---------|------|------|---------|----------------|
| **W3-1** Celery dead queue | DONE | **OK / PARTIAL** | sync Telegram; beat removed | `CELERY_BROKER_URL` still **SET** (dead broker residual risk if any `.delay` remains) | — | survey cron SERVER open per OPS |
| **W3-2** error monitoring | DONE | **OK** | client-error + TelegramAlertHandler + handler500 | client_errors.log live (incl. pixel bugs) | F-030 evidence path | |
| **W3-3** anon cache cookies | DONE | **OK** | bootstrap lazy cookies | anon GET `/` **no Set-Cookie** | — | live PASS |
| **W3-4** CSRF cache poison | DONE | **OK** | empty csrf in language forms | related bootstrap works | — | |
| **W3-5** rate limit / swagger | DONE | **OK** | REMOTE_ADDR key; swagger staff | swagger/redoc/schema **404** anon | F-007 | mild burst 20× 200; heavy crawl still 429 |
| **W3-6** log rotate / PII | OPEN (REPO done) | **PARTIAL** | filter + rotate script | **cron runs** `rotate_twocomms_logs.sh` daily | — | SERVER partial done; confirm big log cleanup |
| **W3-7** status races | DONE | **OK** | no silent full save fallback | N/A | — | |
| **W3-8** slow query log | OPEN | **OPEN** | — | — | — | SERVER |
| **W3-9** TG webhook secret | DONE (REPO warn) | **REOPEN / PARTIAL** | warning if empty | **`TELEGRAM_BOT_WEBHOOK_SECRET` EMPTY** on prod | — | Webhook accepts without secret until env set |
| **W3-10** survey eval | DONE | **OK** | AST safe eval | N/A | — | |
| **W3-11** CheckoutCapture limits | DONE | **PARTIAL** | rate-limit + cleanup step | capture empty still **200 ok** (F-051); converted never true (F-075) | F-051, F-075 | retention not cron'd |
| **W3-12** promo bruteforce | DONE | **OK** | 10/m ratelimit | N/A | — | |

---

# WAVE ADS — Pre-launch Meta

| ID | Plan | Verdict | Code | Prod | docs/qa | Evidence / gap |
|----|------|---------|------|------|---------|----------------|
| **ADS-1** early PageView | DONE | **PARTIAL / REOPEN residual** | head fbq init+PageView | early PageView **present**; `FACEBOOK_PIXEL_ID` settings **EMPTY** (hardcoded fallback works); **BFCache: initializePixelsImmediately undefined** | **F-030, F-079, F-042** | 10 client_errors; staticfiles all call missing def |
| **ADS-2** EN po complete | DONE | **PARTIAL / REOPEN residual** | en.po untranslated=0 (per plan) | **`/en/` H1 still Ukrainian** «український streetwear…» | **F-005** | .po ≠ content blocks / hard-coded H1 |
| **ADS-3** category title truncate | DONE | **REOPEN** | `TITLE_LIMIT=70`, `_fit_title` | DB `seo_title` still cut mid-phrase; live title ends with **«від»** | **F-001, F-023** | Seed migration had **full** strings; prod DB shorter → **data not re-seeded / overwritten** after old truncate |
| **ADS-4** query faceted dupes | OPEN | **OPEN** | — | catalog `?color=` 200 indexable risk | F-006 family | |
| **ADS-5** 404 recs cache | OPEN | **OPEN** | fragment cache 3600 | sample recs earlier OK; stale risk remains | F-034 PASS sample only | |
| **ADS-6** 500 crawl | OPEN | **OPEN** | W3-2 helps | LSAPI / capacity history | F-029, F-031 | |
| **ADS-7** favicon SERP | OPEN | **OPEN** | — | `/favicon.ico` **200** (redirect to static) | F-009 | likely minor |

---

# WAVE 4 — Status / finance model

| ID | Plan | Verdict | Notes |
|----|------|---------|-------|
| W4-1…W4-6 | all OPEN | **OPEN** | OrderStatusHistory, NP RTS, COGS, CustomPrintLead funnel, custom checkout, Meta spend import — **not started** |

---

# WAVE 5 — SEO / feed / content

| ID | Plan | Verdict | Prod / docs/qa |
|----|------|---------|----------------|
| **W5-1** GMC feed 301/color | OPEN | **OPEN / NARROWED** | Product g:link after unescape **often 200** with size path (**F-077**); color landings grammar still bad (**F-002**); feed size live ~1.6–2.1MB |
| **W5-2** catalog pagination | DONE | **OK / N/A VERIFY** | load-more works (F-067) |
| **W5-3** image optimize backfill | OPEN | **OPEN** | SERVER `optimize_images` |
| **W5-4** longsleeve size grids | OPEN | **OPEN** | |
| **W5-5** sort / brand mix | OPEN | **OPEN** | OWNER decision |
| **W5-6** meta quality batches | OPEN | **OPEN** | F-008 |
| **W5-7** stock model | OPEN | **OPEN** | OWNER decision |
| **W5-8** misc SEO nested | OPEN | **OPEN** | Indexing quota, schema bloat, lastmod, 410, TikTok sameAs |
| **W5-9** i18n Phase 17 | OPEN | **OPEN** | F-005; ADS-2 residual |
| **W5-10** near-duplicate copy | OPEN | **OPEN** | F-004 family |

Related open from docs/qa not always named in plan: **F-059** empty ProductImage.alt (36/36), **F-043** `/help-center/` 404, **F-078** `/kontakty/` 404 vs `/contacts/`.

---

# WAVE 6 — Cart UX

| ID | Plan | Verdict | Notes |
|----|------|---------|-------|
| W6-1 mono invoice invalidate | DONE | **OK / N/A VERIFY** | code intent; full browser N/A |
| W6-2 double-click remove | DONE | **OK / N/A VERIFY** | |
| W6-3 custom badge counts | DONE | **OK / N/A VERIFY** | |
| W6-4 review loop | OPEN | **OPEN** | |
| W6-5 recommendations | OPEN | **OPEN** | ADS-5 overlap |
| W6-6 multi-device cart | OPEN | **OPEN** | |
| W6-7 size filter | OPEN | **OPEN** | DECISION |

Cart API rechecks from docs/qa: ATC/update/remove/promo/favorites **PASS** (F-024, F-060–064). NP Latin **Kyiv 502** still **F-050**.

---

# WAVE 7 — Repo hygiene

| ID | Plan | Verdict | Notes |
|----|------|---------|-------|
| **W7-1** views.py.backup live runtime | DONE | **PARTIAL / REOPEN residual** | File **still exists**; `__init__.py` **still lazy-loads** from `views.py.backup` | Not fully eliminated |
| W7-6 xlsx costs | DONE | **OK** | removed/secured per plan |
| W7-23 naive datetime | DONE | **OK / N/A** | |
| W7-24 search pagination | DONE | **OK / N/A** | |
| W7-2…5,7–22,21 | OPEN | **OPEN** | bulk hygiene |

---

# SERVER tasks (S-1…S-14)

All plan **OPEN**. Re-check highlights:

| ID | Theme | Prod note |
|----|-------|-----------|
| S-backup / W0-3 | MySQL dump cron | **script yes, cron no** |
| S-7 / W0-6 | service account | none found under TWC (incomplete proof) |
| S-9 | optimize_images | not verified run |
| S-10 | compilemessages | after W5-9 |
| S-13 | TELEGRAM secret | **EMPTY** |
| S-14 / W1-11 | ubd live | **200 PUBLIC — FAIL** |
| cleanup analytics cron | AN-051 | **no cron** |
| log rotate | W3-6 | **cron present** ✓ |

---

# OWNER tasks (O-1…O-7)

All **OPEN**: GTM/GA4/Meta/TikTok/GSC cabinets, SSH password, stash review, product decisions (COD UI, stock, size filter, priority, TikTok handle, unpublished SKUs, GTIN).

---

# docs/qa Pass A/B status (unchanged conclusion)

| Pass | Status |
|------|--------|
| A walk checklist | **COMPLETE** |
| B findings F-001…F-086 | **COMPLETE** |
| C independent confirm | **NOT STARTED** |
| D fixes | **NOT STARTED** (plan has partial historical fixes) |
| Ads gate | **BLOCKED** |

---

# Matrix: Plan DONE but must REOPEN or finish

| Priority | Plan ID | Action |
|----------|---------|--------|
| P0 | **W2-1** | Add first_touch → Order.utm_*; ensure session before order; mono+COD; paid canary |
| P0 | **W2-2** | After W2-1; verify is_converted on purchase |
| P0 | **W2-3 / purchase UA** | Ensure every paid mono path records purchase once |
| P0 | **ADS-1 residual** | Define `initializePixelsImmediately` = deferred init or remove call |
| P1 | **ADS-3** | Re-seed/fix Category.seo_title (+_uk) full strings in MySQL |
| P1 | **ADS-2 residual** | Fix EN home/catalog H1 content not only .po |
| P1 | **W1-11** | Auth-only ubd_docs + random names + block static |
| P1 | **Mono CheckoutCapture** | Mark converted on invoice create / paid |
| P1 | **W3-9** | Set TELEGRAM_BOT_WEBHOOK_SECRET + re-register webhook |
| P1 | **W0-3** | Install backup cron + restore drill |
| P1 | **W2-8** | Find path writing `chatgpt.com`/`ig` without normalize; optional backfill |
| P2 | **W7-1** | Eliminate remaining backup lazy imports |
| P2 | **FACEBOOK_PIXEL_ID** | Set in env (currently EMPTY; HTML fallback only) |
| P2 | **W3-11/AN-051** | Run cleanup cron |

---

# Full OPEN inventory (plan still `[ ]`) — work remaining

### P0/P1 security & ops
W0-1, W0-2, W0-3, W0-6, W1-11, S-*, O-6

### Attribution / ads
W2-10 (nested open), ADS-4, ADS-5, ADS-6, ADS-7, O-1…O-4

### Product/status/finance
W4-1…W4-6

### SEO/content
W5-1, W5-3…W5-10

### UX growth
W6-4…W6-7

### Hygiene
W7-2…5, W7-7…22, W7-21

### Infra
W3-8, remaining W3-6 SERVER polish

---

# Crosswalk: high-signal docs/qa findings ↔ plan

| F-ID | Sev | Plan link | Plan said | This recheck |
|------|-----|-----------|-----------|--------------|
| F-021/033/071/045 | P0 | W2-1 | DONE | **REOPEN** |
| F-019 | P0 | W2-2 | DONE | **REOPEN** |
| F-030/079 | P0 | ADS-1 | DONE | **residual REOPEN** |
| F-001/023 | P1 | ADS-3 | DONE | **REOPEN data** |
| F-005 | P1 | ADS-2 / W5-9 | DONE/OPEN | **EN H1 FAIL** |
| F-003/027/077 | P0/P2 | W5-1 | OPEN | product g:link better; color SEO still open |
| F-083 | P1 | W2-3 | DONE | **REOPEN undercount** |
| F-084 | P1 | W2-8 | DONE | **PARTIAL live dirt** |
| F-044/068/073/074 | P1 | W2-1 | DONE | session_key gaps remain |
| F-075 | P2 | W3-11 / mono | DONE partial | converted still 0 |
| F-050 | P1 | — | not explicit | NP Latin Kyiv 502 |
| F-059 | P1 | W5 / a11y | not explicit | alts empty 36/36 |
| F-029/031 | P0/P1 | ADS-6 / capacity | OPEN | still relevant |
| W1-11 live | P1 | W1-11 | OPEN | **200 public PII** |

---

# Recommended execution order (after this doc)

1. **Security:** W1-11 ubd lockdown; W0-1 password; W3-9 TG secret  
2. **Attribution:** reopen W2-1 (+ first_touch) → W2-2 → purchase UA completeness  
3. **Pixel:** fix initializePixelsImmediately; set FACEBOOK_PIXEL_ID env  
4. **SEO data:** rewrite category seo_title in DB (ADS-3 residual); color grammar F-002  
5. **Ops:** backup cron W0-3; analytics cleanup cron  
6. Then plan OPEN waves ADS-4…, W5, W4  

---

# Appendix A — Prod snapshot 2026-07-09 late

```
Orders total: 43 | utm_source empty: 43 | utm_session: 0
session_key empty: 36 | with session_key: 7
UTMSession is_converted: 0 / 1047
paid/prepaid: 36 | purchase UserAction: 3 | lead: 6
CheckoutCapture: 4 | converted: 0
ProductImage alt empty: 36/36
Category.seo_title: truncated for tshirts/hoodie/long-sleeve
UTM last 3d top: instagram(7), chatgpt.com(7), chatgpt(6), ig(1), audit(1)
initializePixelsImmediately client_errors: ≥10
TELEGRAM_BOT_WEBHOOK_SECRET: EMPTY
FACEBOOK_PIXEL_ID settings: EMPTY
crontab: log rotate only (no backup, no cleanup_analytics)
ubd_docs public: HTTP 200
Server git HEAD (sample): 1743661c …
```

# Appendix B — Live HTTP recheck (sample)

| Check | Result |
|-------|--------|
| success-preview | redirect admin login PASS |
| success/1 | 404 PASS |
| test-analytics | admin login PASS |
| swagger/redoc | 404 PASS |
| early fbq PageView | PASS |
| anon home Set-Cookie | none PASS |
| tshirts title complete | **FAIL** ends «від» |
| en H1 no Ukrainian | **FAIL** |
| color black title grammar | **FAIL** |
| feed 200 | PASS |
| favicon 200 | PASS |
| help-center | 404 (open F-043) |

# Appendix C — Code recheck FAIL list (DONE claims)

```
W2-1  FAIL  first_touch → Order.utm
W2-1  FAIL  COD _ensure_session
ADS-1 FAIL  initializePixelsImmediately defined
(+ prod data FAILs for W2-2, ADS-3, ADS-2 H1, W1-11, W3-9 env)
```

---

## Document control

| Field | Value |
|-------|--------|
| Created | 2026-07-09 |
| Author | agent re-verify pass (plan × prod × docs/qa) |
| Next | Pass C confirm P0 reopen list OR Pass D fixes starting W2-1 + W1-11 + pixel |
| Do not treat plan `[x]` alone as ads-ready |

*End of PLAN_VS_FINDINGS_2026-07-09.md*
