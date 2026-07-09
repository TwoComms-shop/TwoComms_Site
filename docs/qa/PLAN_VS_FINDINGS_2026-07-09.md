# IMPLEMENTATION_PLAN × Production re-verify (handoff for fix agent)

**Updated:** 2026-07-09 (full re-pass; owner confirmed SSH password rotated)  
**Audience:** next agent that **implements fixes only** from this matrix  
**Sources:**
- Plan: `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` (117 top-level: was 45×`[x]` / 72×`[ ]`)
- Findings: `docs/qa/AUDIT_FINDINGS_2026-07-09.md` (F-001…F-091+)
- Live: `https://twocomms.shop` HTTP
- Code: workspace repo (server SSH **key-only unavailable** after password rotate — DB numbers from last successful shell + code truth)

**Do not trust plan `[x]` alone.** Columns below are the handoff truth.

### Verdict legend
| Verdict | Meaning |
|---------|---------|
| **OK** | Accept criteria hold in code + live (or REPO-only with evidence) |
| **PARTIAL** | Meaningful work done; gaps remain |
| **REOPEN** | Plan said DONE; **must fix again / finish** |
| **OPEN** | Plan still `[ ]` — not implemented |
| **DONE_OWNER** | Owner action complete (this re-pass) |

**Ads gate:** **BLOCKED** until REOPEN P0 attribution + pixel + critical security gaps closed.

---

## 0. Executive counts (this re-pass)

| Bucket | Count | Notes |
|--------|------:|-------|
| Plan top-level DONE claimed | 45 | |
| → reclassified **OK** | ~24 | checkout security, cache, several W1/W3 |
| → reclassified **PARTIAL** | ~12 | normalize, PV hygiene, ADS-1 head PV, W6, W0-3… |
| → reclassified **REOPEN** | **~10** | W2-1/2/3, ADS-2/3 residual, ADS-1 BFCache, W7-1, W3-9 env, mono capture… |
| Plan OPEN | 72 | still to build |
| New/extra gaps found outside plan IDs | yes | `deploy_paramiko.py` secret, mono vs COD tracking asymmetry, title/H1 product pairs |

### P0/P1 fix queue for next agent (ordered)

1. **W1-11 / F-087** — lock down `media/ubd_docs/` (was HTTP 200 for real files when listed on server)
2. **W0-1 REPO tail** — remove/sanitize **`deploy_paramiko.py`** still contains hardcoded SSH password in git (OWNER password already rotated → old secret useless for login but **must leave repo**)
3. **W2-1 / F-071** — `link_order_to_utm`: copy UTM from `analytics_first_touch_data` / `twc_ft`; ensure session on COD; mono: `attach_tracking_to_order` + CheckoutCapture.converted
4. **W2-2 / F-019** — is_converted still 0/1047 historically; fix only works after W2-1
5. **W2-3 / F-083** — purchase UserAction undercount (3 vs 36 paid)
6. **ADS-1 residual / F-030** — define or replace `initializePixelsImmediately()` in `analytics-loader.js` (called, not defined) + collectstatic
7. **ADS-3 / F-001+F-023** — **rewrite Category.seo_title** in DB (full strings); code TITLE_LIMIT=70 alone insufficient (live still ends «від»/«та»/«на»)
8. **ADS-2 residual / F-005** — EN/RU home H1 still Ukrainian hard-coded content
9. **W3-9 / F-088** — set `TELEGRAM_BOT_WEBHOOK_SECRET` on server + re-register webhook
10. **W0-3 / F-090** — enable MySQL backup **cron** (script exists)
11. **F-050** — NP `Kyiv` Latin → 502 (UA Київ 200)
12. Product title≠H1 pairs (e.g. last-breath / death-grabs-ass) — **F-004 family**
13. Then plan OPEN: ADS-4…7, W5-*, W4-*, W2-10 nested, W7 hygiene…

---

## 1. WAVE 0 — Safeguards

| ID | Plan checkbox | Verdict | Evidence | Fix agent action |
|----|---------------|---------|----------|------------------|
| **W0-1** SSH password | was OPEN (REPO verified) | **DONE_OWNER** + **REPO REOPEN tail** | Owner rotated SSH password (confirmed 2026-07-09). Key auth from auditor machine still fails (no key deployed). **Tracked file `deploy_paramiko.py` still has `ssh.connect(..., password="…")`** — remove file or use env; purge from git history if needed | Delete/sanitize `deploy_paramiko.py`; never re-commit host passwords; optional: add deploy key |
| **W0-2** secret rotation | OPEN | **OPEN** | Not re-proven; history may still hold old SECRET_KEY/Redis | Rotate secrets on server; document in OPS |
| **W0-3** MySQL backups | OPEN (script DONE) | **PARTIAL** | `scripts/backup_mysql.sh` in repo; last server check: script present, **no backup cron** (only log rotate cron) | Install crontab + restore drill; verify `~/db_backups` outside webroot |
| **W0-4** money smoke tests | DONE | **OK** | Present: `test_checkout`, `test_monobank_webhook`, `test_cart_sync`, `test_utm_attribution`, `test_utm_normalization` | Keep green before deploys; extend tests for first_touch→Order |
| **W0-5** OPS docs + stash | DONE REPO | **PARTIAL** | `twocomms/docs/OPS.md` exists | OWNER: resolve server git stashes |
| **W0-6** service-account JSON | OPEN | **OPEN** | Prior find under TWC empty; re-run after SSH access restored | Server find + HTTP probe |

---

## 2. WAVE 1 — Money / checkout

| ID | Plan | Verdict | Evidence | Fix agent action |
|----|------|---------|----------|------------------|
| **W1-1** guest COD 500 | DONE | **OK / PARTIAL** | `process_guest_order` gone; `order_create` path; COD UI intentionally off | Optional: `_ensure_session_key` in `create_order` (see F-074) |
| **W1-2** order PII | DONE | **OK** | Live: success-preview → admin login; success/99999 → 404; `_can_view_order` in code | None |
| **W1-3** mono signature | DONE | **OK** | `_verify_monobank_signature`; webhook rejects bad/missing sign; no `or 'success'` unsafe fallback | None |
| **W1-4** promo | DONE | **OK** | `promo_code_id` + `_record_promo_usage_for_order` | None (field name `promo_code` F-070 INFO) |
| **W1-5** checkout errors | DONE | **OK** | missing product / zero total guards in create+mono | None |
| **W1-6** cabinet pay methods | DONE | **OK** | `update_payment_method` / `confirm_payment` restored with owner checks | N/A browser |
| **W1-7** mobile hero CTA | DONE | **OK** | `cls-ultimate.css` has W1-7 mobile `height:auto` / overflow visible comments+rules; critical-home too | Spot-check 360×640 after deploy |
| **W1-8** /test-analytics/ | DONE | **OK** | Live → admin login | None |
| **W1-9** dropship webhook | DONE | **OK** | shared signature verify in dropshipper_views | None |
| **W1-10** profile upload | DONE | **OK** | validation in auth/profile (per plan+code prior) | None |
| **W1-11** ubd_docs public | OPEN | **OPEN — CONFIRMED P1** | Server listing showed real file; HTTP **200** with/without Referer (prior shell). Directory listing now 404; **files still risk if URL known** | Auth-only download; uuid filenames; nginx/LiteSpeed deny raw media path |
| **W1-12** pull-verify amount | DONE | **OK** | `_resolve_retail_invoice_status` + amount checks | None |
| **W1-13** qty cap | DONE | **OK** | `MAX_CART_ITEM_QTY=50` + tests | None |
| **W1-14** double-submit | DONE | **OK** | `_cart_fingerprint` + cart.js submitting lock + tests | None |

### Extra gaps (Wave 1 adjacency)

| Gap | Sev | Evidence | Action |
|-----|-----|----------|--------|
| COD no session ensure | P1 | `create_order` sets `session_key=request.session.session_key` without create; order 275 empty sk | Call `_ensure_session_key` / `session.save()` before Order create |
| Mono no `CheckoutCapture.converted` | P2 | monobank.py has **0** CheckoutCapture refs; checkout COD path sets converted; prod was 0/4 converted | On mono invoice create / paid: mark capture converted by session_key |
| Mono no `attach_tracking_to_order` | P1 | only inline tracking dict; COD uses helper | Use shared `attach_tracking_to_order` for parity |

---

## 3. WAVE 2 — Attribution (CRITICAL REOPEN)

| ID | Plan | Verdict | Evidence | Fix agent action |
|----|------|---------|----------|------------------|
| **W2-1** UTM on orders | DONE | **REOPEN** | Code: fallback session_key → visitor_id → `session['utm_data']` only. **`link_order_to_utm` body has NO `analytics_first_touch_data`**. Tests: COD utm from session data + fbclid synthesis — **no first_touch test**. Prod last shell: **43/43 utm empty**, 0 utm_session FK. Order 276: first_touch UTM in UserAction, Order blank | (1) Fallback #4: first_touch cookie/data → order.utm_*; (2) create UTMSession if needed; (3) COD ensure session; (4) mono attach_tracking; (5) paid canary `utm_source=audit` → Order.utm_source=audit |
| **W2-2** is_converted | DONE | **REOPEN** | `mark_as_converted` only when utm_session linked on lead/purchase. Prod: **0/1047** converted | Depends on W2-1; add regression test |
| **W2-3** purchase layers | DONE | **REOPEN / PARTIAL** | Docs `PURCHASE_DEFINITION.md`; NP purchase path exists; mono records purchase on status transition. Prod: **purchase UA 3 vs paid 36** | Ensure all paid mono/manual policies; backfill optional; never double-fire CAPI |
| **W2-4** bot/PV hygiene | DONE | **PARTIAL** | bot+staff filter, 30m dedupe, SiteSession get_or_create in code | Historical 41k PV noise remains; optional cleanup |
| **W2-5** GTM/loader fast-path | DONE | **OK** | utm/fbclid fastpath in base.html | None |
| **W2-6** TikTok names | DONE | **OK** | CompletePayment mapping in loader + server | Verify Events API in TikTok UI (OWNER) |
| **W2-7** CAPI outside lock | DONE | **PARTIAL / OK leaning** | `utils._dispatch_post_payment_events` + `on_commit`; mono uses utils pattern | Grep remaining in-lock network calls |
| **W2-8** normalize + AI | DONE | **PARTIAL** | aliases include chatgpt.com→chatgpt; middleware calls normalize. Live dirt (prior): still `chatgpt.com` & `ig` rows | Find writers bypassing middleware; backfill optional; add assert on raw hostnames |
| **W2-9** CAPI hygiene | DONE | **OK** | event_source_url shop; no random AddPaymentInfo id in checkout-mono | None |
| **W2-10** bag | OPEN | **OPEN** | Nested open: CRO-033 ATC CAPI, AN-037 first/last touch fields, consent, N+1 reports, AN-001 double gtag, AN-003 item_id, NEW-406 offer_id color, NEW-407 | Implement per nested priority |

### W2-1 failure mode (for implementer)

```
Landing UTM → twc_ft first_touch          ✓ (often)
            → session['utm_data']/UTMSession  ✓ only if session+middleware path runs
Checkout   → link_order_to_utm
            → reads UTMSession / session utm_data only
            → DOES NOT read first_touch        ✗
Result     → Order.utm_* empty; is_converted never; ROAS blind
```

LiteSpeed anon cache + bootstrap-delayed session + AnalyticsExclusion amplify the gap.

---

## 4. WAVE 3 — Reliability

| ID | Plan | Verdict | Evidence | Fix agent action |
|----|------|---------|----------|------------------|
| **W3-1** Celery/Telegram | DONE | **OK / PARTIAL** | `async_enabled=False` default in telegram_notifications | Ensure no critical `.delay()` still drops; CELERY_BROKER may still be set in env |
| **W3-2** error monitoring | DONE | **OK** | client-error API + onerror in base; client_errors.log used | None |
| **W3-3** anon cache cookies | DONE | **OK** | Live anon `/` **zero Set-Cookie**; `/api/bootstrap/` 200 `{"ok":true}` | None |
| **W3-4** CSRF cache | DONE | **OK** | language_switcher empty csrf + cookie fill (prior) | None |
| **W3-5** rate limit / swagger | DONE | **OK** | Live swagger/redoc 404; REMOTE_ADDR middleware | F-007 heavy crawl 429 still possible |
| **W3-6** logs | OPEN REPO done | **PARTIAL** | Prior: log rotate **cron installed** | Confirm archives; large log purge |
| **W3-7** status races | DONE | **OK** | no silent full-save fallback (prior) | None |
| **W3-8** slow log | OPEN | **OPEN** | | SERVER temporary enable |
| **W3-9** TG webhook secret | DONE REPO | **REOPEN env** | Prior prod: **TELEGRAM_BOT_WEBHOOK_SECRET EMPTY** | Set secret + setWebhook; code already warns |
| **W3-10** survey eval | DONE | **OK / WARN** | AST safe eval; residual `eval` count low | Confirm no unsafe eval remains |
| **W3-11** CheckoutCapture | DONE | **PARTIAL** | rate-limit code; converted still broken for mono path | See mono capture gap; run retention cron |
| **W3-12** promo rate limit | DONE | **OK** | 10/m on apply_promo | None |

---

## 5. WAVE ADS

| ID | Plan | Verdict | Evidence | Fix agent action |
|----|------|---------|----------|------------------|
| **ADS-1** early PageView | DONE | **PARTIAL + REOPEN residual** | Live: inline `fbq`+PageView in head ✓; pixel id 823958313630148 present. **analytics-loader still calls `initializePixelsImmediately()` without defining it** (source + hashed staticfiles). Settings `FACEBOOK_PIXEL_ID` was EMPTY (fallback in template) | Fix BFCache handler; set env pixel id; collectstatic/deploy |
| **ADS-2** EN po | DONE | **REOPEN residual** | Live `/en/` and `/ru/` **H1 still Ukrainian** «український streetwear…» | Fix template/content H1 for locales; not only django.po |
| **ADS-3** title truncate | DONE | **REOPEN** | Code `TITLE_LIMIT=70` + `_fit_title`. Live titles still truncated mid-preposition. DB seo_title shorter than migration 0058 full seed (72 chars full vs ~52 live) | **Data fix** Category.seo_title/_uk; stop persisting truncated strings; re-verify all category titles |
| **ADS-4** query dupes | OPEN | **OPEN** | faceted `?color=` still full HTML | noindex faceted query pages |
| **ADS-5** 404 recs cache | OPEN | **OPEN** | fragment cache risk | include publish version in cache key |
| **ADS-6** 500/capacity | OPEN | **OPEN** | F-029 LSAPI, F-031 MySQL gone away history | ops + query hygiene |
| **ADS-7** favicon | OPEN | **OPEN** | Live favicon **200** | minor SERP follow-up |

---

## 6. WAVE 4 — Status / finance

| ID | Plan | Verdict | Action |
|----|------|---------|--------|
| W4-1…W4-6 | all OPEN | **OPEN** | Status history, NP RTS, COGS, custom-print funnel, Meta spend — full implementation per plan |

---

## 7. WAVE 5 — SEO / feed / content

| ID | Plan | Verdict | Evidence / action |
|----|------|---------|-------------------|
| **W5-1** GMC feed | OPEN | **OPEN / NARROWED** | Live feed 200; sample g:link **5/5 → 200** after unescape (F-077). Still: availability/cache/gtin decisions, color landing SEO F-002 |
| **W5-2** pagination | DONE | **OK** | load-more works (F-067) |
| **W5-3…W5-10** | OPEN | **OPEN** | images, size grids, sort, meta batches, stock model, schema, i18n, near-duplicate copy |
| Related F-004 | — | **OPEN** | Live title≠H1: `/product/last-breath/` title «last breath» vs H1 «Череп З Трояндою»; `/product/death-grabs-ass/` similar |
| F-002 color grammar | — | **OPEN** | «Купити чорний футболка…» |
| F-059 alts | — | **OPEN** | ProductImage.alt empty 36/36 (last DB) |
| F-043 help-center | — | **OPEN** | still 404 (no 301→/dopomoga/) |
| F-078 kontakty | — | **OPEN** | 404; `/contacts/` OK |

---

## 8. WAVE 6 — Cart UX

| ID | Plan | Verdict | Evidence |
|----|------|---------|----------|
| **W6-1** mono invoice reset | DONE | **OK** | Tests reset `monobank_invoice_id` on cart update/remove; cart.py handles monobank session keys |
| **W6-2** double-click | DONE | **OK** | cart.js submitting lock |
| **W6-3** badges | DONE | **OK** | per plan |
| W6-4…7 | OPEN | **OPEN** | reviews loop, recs, multidevice, size filter |
| **F-050** NP Latin | — | **OPEN** | Live recheck: `q=Київ` 200; **`q=Kyiv` → 502** |

Cart API (ATC/update/remove/promo/favorites): previously PASS — treat as OK unless regressing.

---

## 9. WAVE 7 — Hygiene

| ID | Plan | Verdict | Evidence / action |
|----|------|---------|-------------------|
| **W7-1** views.py.backup | DONE | **REOPEN residual** | File **still exists**; `views/__init__.py` **still lazy-loads** from backup | Finish migration off backup; delete file |
| W7-6 xlsx | DONE | **OK** | per plan |
| W7-23/24 | DONE | **OK / N/A** | datetime / search pagination |
| W7-2…5,7–22,21 | OPEN | **OPEN** | bulk hygiene — after P0 |

---

## 10. SERVER (S-*) & OWNER (O-*)

All plan checkboxes largely **OPEN** except where noted:

| Item | Status | Note |
|------|--------|------|
| S backup cron | OPEN | script only |
| S-13 TG secret | OPEN | was EMPTY |
| S-14 ubd | OPEN | public file 200 |
| S-7 service account | OPEN | |
| cleanup_analytics cron | OPEN | command may exist, no cron |
| log rotate cron | **PARTIAL DONE** | was installed |
| O-1…O-5 ad cabinets | OPEN | Meta EM, GTM, GSC… |
| O-6 server password | **DONE_OWNER** | rotated |
| O-7 product decisions | OPEN | COD UI, stock, sort, etc. |

---

## 11. Live HTTP re-pass snapshot (2026-07-09)

| Check | Result |
|-------|--------|
| Core pages home/catalog/cart/blog/custom/en/ru | 200 |
| healthz / robots / sitemap / feed / bootstrap / favicon / webmanifest | 200 |
| success-preview / test-analytics | gated (admin login) |
| success/99999 | 404 |
| swagger/redoc | 404 |
| anon home Set-Cookie | none |
| early fbq PageView + pixel id | PASS |
| Category titles complete | **FAIL** (від/та/на) |
| EN/RU H1 localized | **FAIL** (UA text) |
| Color title grammar | **FAIL** |
| Feed sample links | **5/5 200** |
| NP cities Київ | 200 |
| NP cities Kyiv | **502** |
| last-breath title vs H1 | **mismatch FAIL** |
| death-grabs-ass title vs H1 | **mismatch FAIL** |

**SSH:** password auth not used (owner rotated); publickey denied from this environment → no fresh MySQL this pass. Prior DB snapshot remains authoritative for UTM/orders until SSH keys restored.

---

## 12. Code re-pass FAIL list (DONE claims)

```
W2-1  REOPEN  no first_touch → Order.utm_*
W2-1  REOPEN  COD no session ensure
W2-1  REOPEN  mono missing attach_tracking_to_order
W2-1  REOPEN  mono missing CheckoutCapture.converted
W2-2  REOPEN  is_converted dead without utm_session
W2-3  REOPEN  purchase undercount vs paid
ADS-1 REOPEN  initializePixelsImmediately called, not defined
ADS-2 REOPEN  locale H1 still Ukrainian
ADS-3 REOPEN  DB/live titles still truncated
W7-1  REOPEN  views.py.backup still loaded
W0-1  REPO    deploy_paramiko.py still has password literal
W3-9  REOPEN  webhook secret env (server)
W1-11 OPEN    ubd public media
W0-3  PARTIAL backup cron missing
```

---

## 13. Crosswalk F-* ↔ plan (fix agent)

| F-ID | Sev | Plan | Fix? |
|------|-----|------|------|
| F-021/033/071/045 | P0 | W2-1 REOPEN | YES |
| F-019 | P0 | W2-2 REOPEN | YES |
| F-030/079 | P0 | ADS-1 residual | YES |
| F-001/023 | P1 | ADS-3 REOPEN data | YES |
| F-005 | P1 | ADS-2 / W5-9 | YES |
| F-002 | P1 | W5 / color SEO | YES |
| F-004 | P1 | product SEO | YES (last-breath etc.) |
| F-083 | P1 | W2-3 | YES |
| F-084 | P1 | W2-8 | YES |
| F-044/068/073/074 | P1 | W2-1 session | YES |
| F-075 | P2 | mono capture | YES |
| F-050 | P1 | NP Latin | YES |
| F-059 | P1 | alts | YES |
| F-043/078 | P1/P2 | redirects | YES |
| F-087 | P1 | W1-11 | YES |
| F-088 | P1 | W3-9 | YES |
| F-089 | P2 | ADS-1 env | YES |
| F-090 | P2 | W0-3 | YES |
| F-003/027 | P0/P2 | W5-1 | review with F-077 (product links often OK) |
| F-029/031 | P0/P1 | capacity | YES ops |

---

## 14. What is safely OK (do not re-break)

- Order success authorization (W1-2)
- Mono webhook signature + pull-verify amount (W1-3/12)
- Dropship webhook signature (W1-9)
- Promo limits + qty cap + double-submit (W1-4/13/14)
- test-analytics staff (W1-8)
- Early Meta PageView snippet (ADS-1 partial — keep while fixing BFCache)
- Anon page cache / bootstrap CSRF (W3-3/4)
- Swagger closed (W3-5)
- Cart mono invoice session reset tests (W6-1)
- Feed product link resolution (many g:link OK)
- Money smoke test suite files exist (W0-4)

---

## 15. Suggested commit style for fix agent

```
fix(W2-1): copy first_touch UTM onto Order + ensure session on COD
fix(ADS-1): define initializePixelsImmediately for BFCache restore
fix(ADS-3): reseed category seo_title full strings
fix(W1-11): serve ubd_docs only to owner/staff
chore(W0-1): remove deploy_paramiko hardcoded SSH password
```

One plan ID ≈ one commit. No secrets in git. Deploy: production_settings + collectstatic for JS. Acceptance: live curl + Django shell checks listed under each REOPEN.

---

## 16. Document control

| Field | Value |
|-------|--------|
| Purpose | Single handoff for fix agent |
| Owner SSH password | **Rotated — treat W0-1 OWNER as DONE** |
| SSH for auditors | Needs deploy key or new secret via secure channel (not git) |
| Related | `AUDIT_FINDINGS_2026-07-09.md`, `PRE_ADS_MASTER_AUDIT_CHECKLIST.md` |
| Ads gate | **BLOCKED** |

*End of PLAN_VS_FINDINGS_2026-07-09.md — full re-verify pass*
