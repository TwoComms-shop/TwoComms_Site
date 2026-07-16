# IMPLEMENTATION_PLAN — strict re-verify of every remaining `[x]`

**Updated:** 2026-07-09 (strict pass #2 on *remaining* DONE only)  
**Plan:** `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md`  
**Current counts after this pass:** **`[x]` ≈ 33** · **`[ ]` ≈ 84**

**Method:** code inspection + live HTTP for each still-checked item.  
If accept criteria fail or only half the production path was fixed → **`[x]` removed**.

---

## 0. Result in one glance

### Newly unchecked this strict pass
| ID | Why uncheck |
|----|-------------|
| **W2-7** | **Dual path.** `utils._record_monobank_status_locked` has `on_commit` + CAPI dispatch, but **live retail webhook** (`monobank.py:1611`) calls **`_apply_monobank_status`**, which runs Telegram + `record_order_action` **synchronously** and **never** calls `_dispatch_post_payment_events` / Meta CAPI. W2-7 accept not met for the path that handles paid webhooks. |
| **W7-23** | Historical strict-pass finding; **resolved `3df4c2fc`** with one Kyiv-local reporting date and boundary regression. |

### Unchecked in previous re-verify (still `[ ]`)
W2-2, ADS-1, W7-1, W0-5 (+ partials documented earlier). W2-1, W2-3,
ADS-2, ADS-3, W3-9 and W3-11 were subsequently resolved with production
evidence recorded below.

### Still `[x]` after later production closures
W0-4 · W1-1…W1-14 (W1-11 closed by `ead5fd70` + `e89fd17d`) · W2-1, W2-3, W2-4, W2-5, W2-6, W2-8, W2-9 · W3-1…W3-5, W3-7, W3-9, W3-10, W3-11, W3-12 · W5-2 · W6-1, W6-2, W6-3 · W7-6, W7-24

Each KEEP has a **STRICT RE-VERIFY** note in the plan where non-obvious.

---

## 1. Detailed verdict for each remaining `[x]` (before uncheck of W2-7/W7-23)

| ID | Keep? | Live / code proof | Residual nuance (does NOT uncheck unless stated) |
|----|-------|-------------------|--------------------------------------------------|
| **W0-4** | **KEEP** | Test modules present: checkout, monobank_webhook, cart_sync, utm_attribution | Full pytest not re-run (local SECRET_KEY/prod settings) |
| **W1-1** | **KEEP** | `process_guest_order` gone; cart→order_create; `394a247c` ensures a durable guest session before Order persistence | COD UI intentionally off; F-074 residual resolved by production rollback canary on 2026-07-14 |
| **W1-2** | **KEEP** | Live: preview→login, `/orders/success/1/`→404 | — |
| **W1-3** | **KEEP** | Signature verify in code; POST webhook without sign → **400**; no `status or 'success'` | — |
| **W1-4** | **KEEP** | `promo_code_id` + `_record_promo_usage_for_order` | — |
| **W1-5** | **KEEP** | Missing product / zero-total guards | — |
| **W1-6** | **KEEP** | `update_payment_method` / `confirm_payment` present | Not re-UI-tested |
| **W1-7** | **KEEP** | Theme CSS hero mobile fix; **live** `cls-ultimate.*.css` contains fix | — |
| **W1-8** | **KEEP** | Live test-analytics gated | — |
| **W1-9** | **KEEP** | Dropship uses signature verify | — |
| **W1-10** | **KEEP** | FILE_UPLOAD limits / validation paths | — |
| **W1-12** | **KEEP** | `_resolve_retail_invoice_status` + amount | — |
| **W1-13** | **KEEP** | `MAX_CART_ITEM_QTY = 50` | — |
| **W1-14** | **KEEP** | `_cart_fingerprint` dedupe | — |
| **W2-4** | **KEEP / CLOSED `fdf6563a`** | bot/staff filter + 30m PV dedupe retained; product writer now requires committed PageView proof | Historical raw PV retained for audit, but null/bot/zero-pageview rows are quarantined from product and UTM business metrics; production 1713/1713 parity |
| **W2-5** | **KEEP** | fbclid/utm fastpath in base.html | — |
| **W2-6** | **KEEP** | CompletePayment client+server | TikTok UI not checked (OWNER) |
| **W2-7** | **DROP** | See dual-path analysis §2 | — |
| **W2-8** | **DONE `069f4efa` + `f42b537a`** | shared source/medium normalizer plus guarded AI and all-source history commands | Production alias diff is empty across UTM/first-touch/orders; test-source residue removed |
| **W2-9** | **KEEP** | twocomms.shop + server add_payment_event_id | — |
| **W3-1** | **KEEP** | `async_enabled=False` default | CELERY_BROKER may still be set in env |
| **W3-2** | **KEEP** | client-error endpoint live POST ok; onerror in base | — |
| **W3-3** | **KEEP** | Anon `/` **0 Set-Cookie**; bootstrap present | — |
| **W3-4** | **KEEP** | language_switcher no baked long csrf value | — |
| **W3-5** | **KEEP** | swagger/redoc 404; REMOTE_ADDR rate limit | — |
| **W3-7** | **KEEP** | No obvious `update_fields`→bare `.save()` fallback in mono/utils/np | Medium confidence without full suite |
| **W3-10** | **KEEP** | AST `_safe_eval`; bare eval ≤2 | — |
| **W3-12** | **KEEP** | apply_promo ratelimit | — |
| **W5-2** | **KEEP** | Live `/load-more-products/?page=2` → 200; homepage paginator code | — |
| **W6-1** | **KEEP** | `_reset_monobank_session` on cart mutate; tests assert keys cleared | — |
| **W6-2** | **KEEP** | cart.js submit lock | — |
| **W6-3** | **KEEP** | Badge markup + tests cart_count includes custom_print | Not full visual browser pass |
| **W7-6** | **KEEP** | No cost/purchase xlsx in tree scan | — |
| **W7-23** | **KEEP / CLOSED `3df4c2fc`** | Uses one `timezone.localdate()` for the payout reporting period | Kyiv New Year boundary + AST hygiene regression; local/server 2/2 |
| **W7-24** | **KEEP** | Live `/search/?q=test` and `page=2` → 200 | — |

---

## 2. Why W2-7 must not stay checked (important)

```
Retail Monobank webhook (production path)
  monobank_webhook
    → _resolve_retail_invoice_status (pull-verify)     ✓ W1-12
    → _apply_monobank_status                            ← THIS PATH
         · order.save(update_fields=…)
         · record_order_action('purchase')              sync
         · TelegramNotifier.send_admin…                 sync
         · NO transaction.on_commit
         · NO _dispatch_post_payment_events
         · NO Meta CAPI / TikTok from this function

Alternate path (API/poll)
  utils._record_monobank_status_locked
    → transaction.on_commit(_dispatch_post_payment_events)  ← W2-7 fix lives HERE
```

**Conclusion:** W2-7 fixed the **utils** path, not the **webhook** path that production retail orders use. Marking the whole item done was incorrect → **`[ ]` again**.

**Fix agent should:** route webhook `_apply_monobank_status` success transitions through the same on_commit dispatcher (or call shared helper), without double Telegram/purchase.

**Resolution (2026-07-12):** fixed in `78814344`. The retail webhook now uses
the shared post-commit dispatcher under an atomic row lock. Focused server tests
passed **29/29**; the production MySQL canary observed one post-commit dispatch,
zero duplicate dispatches, one purchase action, and clean canary removal.

---

## 3. Previously unchecked (still false done) — recap

| ID | One-line |
|----|----------|
| W2-1 | **RESOLVED 2026-07-16:** COD/prepay session/UTM/tracking acceptance plus the final CheckoutCapture conversion residual all passed production verification |
| W2-2 | is_converted 0 on prod |
| W2-3 | **RESOLVED `fba4dc85` + `d561c11d` (2026-07-14):** DB-backed purchase idempotency, all confirmed writers, safe historical reconciliation; production trusted parity 31/31, 0 missing/duplicates |
| ADS-1 | early PV OK; BFCache `initializePixelsImmediately` undefined |
| ADS-2 | **RESOLVED `d773bee6` (2026-07-13):** missing RU/EN home/catalog translations added; server 2/2 + live H1 4/4 |
| ADS-3 | **RESOLVED `e2558396` (2026-07-12):** guarded DB repair + connector-aware trim |
| W7-1 | views.py.backup still lazy-loaded |
| W3-9 | **DONE `d7c6812a` + server config**: fail-closed webhook, secret_token registered, live header probes passed |
| W3-11 | **RESOLVED `a90191ea..1962b488`:** strict capture validation, terminal COD/Mono transition, MariaDB canary, live 6/6 negative matrix and 4/4 historical match reconciliation |
| W0-5 | OPS done; stash OWNER not done |

**W2-3 resolution evidence (2026-07-14):** migration 0083 created the real
MariaDB unique key `(action_type, order_id)`; a guarded backfill restored exactly
26 missing trusted purchases without rewriting the five existing rows or the
seven ambiguous legacy manual orders. Repeated apply created 0 rows. Focused
tests passed 172/172 locally and 186/186 on the server; a MariaDB rollback
canary and a live forged-event HTTP check also passed. The documented GA4
Measurement Protocol owner dependency and refund/cancel follow-up remain
explicit gaps, not falsely claimed implementations.

**F-044/F-074 / W1-1 residual evidence (2026-07-14):** `394a247c` creates a
durable anonymous session before the COD Order writer and shares the invariant
with Monobank/UTM code. At production HEAD `bb217bd9`, the rollback canary
matched the response cookie, Order and UTMSession key, preserved first-touch
`utm_source`, created the order-linked action, and left zero rows after cleanup.
This closes the COD-session residual. The prepay acceptance is documented
below; the final CheckoutCapture conversion gap was closed on 2026-07-16 by
`de7f7efc` + `1962b488`, completing W2-1.

**F-068/F-073 prepay evidence (2026-07-14):** Git history proves that the old
writer created the Order before the later tracking block established a session
external ID; `7936ab6e` added the pre-create ensure and Order field write.
Regression `30808819` covers a truly lazy guest session. Its production
rollback canary matched cookie, Order, UTMSession and
`tracking.external_id=session:<key>`, sent a 20,000-minor-unit mocked invoice,
marked UTM as `lead`, and left zero DB or session-cache traces. The historical
19/19 session-key cohort was not changed. F-072 was subsequently closed by
`bdd04e4c`: a guarded dry-run/rollback/apply flow restored the only two exact
surviving Order→UTM links, linked one existing purchase action, preserved every
historical `Order.session_key`, created no actions, and left the other 34
unverifiable orders untouched. Repeated apply changed 0 rows and three
non-candidate digests remained identical.

---

## 4. Live checks this strict pass

| Check | Result |
|-------|--------|
| success-preview / success/1 | gated / 404 |
| mono webhook POST no sign | **400** |
| test-analytics | gated |
| anon home Set-Cookie | **0** |
| client-error POST | 200/4xx ok |
| swagger/redoc | 404 |
| load-more page=2 | 200 |
| search + page=2 | 200 |
| live cls-ultimate hero fix | present |
| category titles mid-phrase | **PASS 2026-07-12:** 9/9 UA/RU/EN live pages, migration `0081` |
| /en/ H1 Ukrainian | **PASS 2026-07-13:** ADS-2 resolved in `d773bee6`; RU/EN home/catalog live H1 4/4 |
| guest COD session/first-touch join | **PASS 2026-07-14:** `394a247c`; cookie = Order.session_key = UTMSession.session_key, rollback cleanup 0 |
| guest prepay session/tracking join | **PASS 2026-07-14:** `30808819`; cookie = Order = UTMSession = tracking external session, amount 20,000; DB/cache cleanup 0 |

---

## 5. What was changed in the plan file

1. **`[x]` → `[ ]`:** W2-7, W7-23 during this historical pass; both were subsequently resolved.
2. **STRICT RE-VERIFY** notes under KEEP items W0-4, W2-8, W3-7, W6-1, W6-3, W7-24.  
3. Banner mentions dual mono path + datetime residual.

**Rule for future `[x]`:** prove **production path** (not only helper/utils/tests), then live accept.

---

## 6. Fix agent: do not re-mark DONE without

| ID | Minimum accept |
|----|----------------|
| W2-7 | Retail webhook success uses on_commit dispatcher; no long HTTP under row-lock; CAPI still fires once |
| W7-23 | **PASS `3df4c2fc`:** one Kyiv-local date, boundary regression and AST guard; local/server 2/2 |
| W2-1 | **PASS 2026-07-16:** Monobank and COD persist terminal CheckoutCapture markers; server 137/137, MariaDB rollback canary and historical 4/4 reconciliation passed |
| ADS-1 | No client_error for initializePixelsImmediately; BFCache restore works |
| ADS-3 | Live category titles do not end with від/та/на |

**ADS-3 resolution (2026-07-12):** fixed in `e2558396`. Production migration
`storefront.0081` repaired only exact damaged values; the server regression
suite passed 5/5 and all nine localized category pages returned complete titles.

---

## 7. Instagram bot (management) — separate bug pack

Deep analysis (no code fixes): **[`IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`](./IG_BOT_MANAGEMENT_BUGS_2026-07-09.md)**

Covers post-commit `1743661c` sales automation: Hide UX, missing transfer action, English stats UI, Message Requests / Graph send failures, likes/reactions, manager-notify noise.

---

## 8. Document control

| Field | Value |
|-------|--------|
| Pass | Strict DONE audit #2 + IG bot analysis |
| Plan checkboxes | Updated in IMPLEMENTATION_PLAN.md |
| Ads gate | **BLOCKED** |
| SSH | Owner password rotated; no DB shell this pass |
| IG bot | Analysis-only findings file linked above |

*End of strict DONE re-verify*
