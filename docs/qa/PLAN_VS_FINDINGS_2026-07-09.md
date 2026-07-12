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
| **W7-23** | Residual **`datetime.now()`** still in `orders/dropshipper_views.py:273-274` (non-test). |

### Unchecked in previous re-verify (still `[ ]`)
W2-1, W2-2, W2-3, ADS-1, ADS-2, ADS-3, W7-1, W3-9, W3-11, W0-5 (+ partials documented earlier).

### Still `[x]` after strict pass (~33)
W0-4 · W1-1…W1-14 (except already open W1-11) · W2-4, W2-5, W2-6, W2-8, W2-9 · W3-1…W3-5, W3-7, W3-10, W3-12 · W5-2 · W6-1, W6-2, W6-3 · W7-6, W7-24  

Each KEEP has a **STRICT RE-VERIFY** note in the plan where non-obvious.

---

## 1. Detailed verdict for each remaining `[x]` (before uncheck of W2-7/W7-23)

| ID | Keep? | Live / code proof | Residual nuance (does NOT uncheck unless stated) |
|----|-------|-------------------|--------------------------------------------------|
| **W0-4** | **KEEP** | Test modules present: checkout, monobank_webhook, cart_sync, utm_attribution | Full pytest not re-run (local SECRET_KEY/prod settings) |
| **W1-1** | **KEEP** | `process_guest_order` gone; cart→order_create | COD UI intentionally off; F-074 session ensure still open |
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
| **W2-4** | **KEEP** | bot/staff filter + 30m PV dedupe in code | Historical PV noise remains (data) |
| **W2-5** | **KEEP** | fbclid/utm fastpath in base.html | — |
| **W2-6** | **KEEP** | CompletePayment client+server | TikTok UI not checked (OWNER) |
| **W2-7** | **DROP** | See dual-path analysis §2 | — |
| **W2-8** | **KEEP** | normalize + aliases in middleware/utils | Live dirt rows may still exist (data/backfill) |
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
| **W7-23** | **DROP** | Residual naive `datetime.now()` in dropshipper_views | — |
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
| W2-1 | first_touch not → Order.utm; COD session; mono capture/tracking gaps |
| W2-2 | is_converted 0 on prod |
| W2-3 | purchase UA undercount |
| ADS-1 | early PV OK; BFCache `initializePixelsImmediately` undefined |
| ADS-2 | /en/ H1 still Ukrainian |
| ADS-3 | **RESOLVED `e2558396` (2026-07-12):** guarded DB repair + connector-aware trim |
| W7-1 | views.py.backup still lazy-loaded |
| W3-9 | TG webhook secret empty on prod (was) |
| W3-11 | CheckoutCapture.converted never on mono |
| W0-5 | OPS done; stash OWNER not done |

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
| /en/ H1 Ukrainian | still FAIL (ADS-2 open) |

---

## 5. What was changed in the plan file

1. **`[x]` → `[ ]`:** W2-7, W7-23 (this pass); plus earlier list.  
2. **STRICT RE-VERIFY** notes under KEEP items W0-4, W2-8, W3-7, W6-1, W6-3, W7-24.  
3. Banner mentions dual mono path + datetime residual.

**Rule for future `[x]`:** prove **production path** (not only helper/utils/tests), then live accept.

---

## 6. Fix agent: do not re-mark DONE without

| ID | Minimum accept |
|----|----------------|
| W2-7 | Retail webhook success uses on_commit dispatcher; no long HTTP under row-lock; CAPI still fires once |
| W7-23 | No `datetime.now()` in orders/storefront non-test money/date code (use timezone.now) |
| W2-1 | Paid/canary order has Order.utm_source from first_touch |
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
