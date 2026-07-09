# IMPLEMENTATION_PLAN — re-verify of «DONE» checkboxes

**Updated:** 2026-07-09 (dedicated pass: *is [x] really done?*)  
**Plan file mutated:** `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md`  
**After uncheck:** top-level **`[x]` ≈ 35** · **`[ ]` ≈ 82** (was ~45 / ~72)

**Goal of this pass:** for every plan item marked done, decide:
1. keep `[x]` (really done),
2. keep `[x]` + **nuance** (mostly done, residual risk),
3. **remove `[x]`** (false done / accept criteria fail).

**Truth sources:** repo code + live `https://twocomms.shop` HTTP.  
**SSH:** owner rotated password; auditor has no key → no fresh MySQL this pass (order/UTM numbers from last shell still cited where relevant).

---

## 0. Short answer

**Нет — не всё «выполненное» выполнено.**

| Категория | Кол-во plan IDs | Что сделали |
|-----------|----------------:|-------------|
| **Сняли `[x]` (false / incomplete)** | **10** | W2-1, W2-2, W2-3, ADS-1, ADS-2, ADS-3, W7-1, W3-9, W3-11, W0-5 |
| **Оставили `[x]` + нюанс** | **~10** | W1-1, W1-7, W2-4/7/8, W3-1/10, W6-3, W7-23/24… |
| **Оставили `[x]` чисто** | **~25** | W1-2…6,8–10,12–14, W2-5/6/9, W3-2…5,7,12, W5-2, W6-1/2, W0-4, W7-6… |

**W0-1 OWNER (SSH password):** done by owner — **not** a false code fix; REPO still dirty (`deploy_paramiko.py`).

---

## 1. Снятые галочки — почему (обязательно читать fix-агенту)

### 1.1 Полный false DONE → снова `[ ]`

#### W2-1 — UTM на заказ — **СНЯТО**
- **Plan claimed:** fallback session_key → visitor → `utm_data`; COD+Mono linked.
- **Reality:**
  - `link_order_to_utm` **does not** read `analytics_first_touch_data` / `twc_ft`.
  - COD `create_order` **no** `_ensure_session_key`.
  - Mono: **no** `attach_tracking_to_order`, **no** `CheckoutCapture.converted`.
  - Prod (last DB): **43/43** `Order.utm_source` empty; order 276 had UTM in UserAction first_touch, Order blank.
  - Tests cover session utm_data / fbclid — **not** first_touch → Order.
- **Accept CRO-050:** FAIL → **cannot stay [x]**.

#### W2-2 — is_converted — **СНЯТО**
- Code calls `mark_as_converted` only if `utm_session` present on lead/purchase.
- Prod: **0 / 1047** `is_converted=True`.
- Depends on W2-1 → **false done**.

#### W2-3 — единый purchase — **СНЯТО (partial)**
- Docs + NP path + mono webhook path exist.
- Prod: **purchase UserAction = 3**, **paid/prepaid orders = 36**.
- Accept «all layers consistent» not met → uncheck; partial work remains in code/docs.

#### ADS-3 — category titles — **СНЯТО**
- Code: `TITLE_LIMIT = 70`, `_fit_title` improved.
- **Live still FAIL:**
  - tshirts: `…принти **від**`
  - hoodie: `…принтами **та**`
  - long-sleeve: `…рукавами **на**`
- Root: **MySQL `Category.seo_title` still truncated** (seed 0058 had full strings ~72 chars; live ~52).
- Code-only fix ≠ accept «no mid-phrase titles» → **uncheck**.

#### W7-1 — views.py.backup not live — **СНЯТО**
- `twocomms/storefront/views.py.backup` **still exists**.
- `views/__init__.py` **still lazy-loads** from it.
- Claim «не живой рантайм» is **false** → uncheck.

---

### 1.2 Partial work → сняли полный `[x]`, оставили пояснение

#### ADS-1 — early PageView — **СНЯТО с partial**
| Часть | Статус |
|-------|--------|
| Inline `fbq` + PageView in `<head>` | **DONE live** |
| BFCache `initializePixelsImmediately()` | **BROKEN** — called in live `analytics-loader.3975317011e4.js`, **function not defined** |
| `FACEBOOK_PIXEL_ID` in settings | was **EMPTY** (template fallback) |

Early PageView alone does not equal full ADS-1 accept → uncheck full item; implement BFCache fix before re-`[x]`.

#### ADS-2 — EN translations — **СНЯТО с partial**
- django.po may be clean (per earlier plan journal).
- **Live `/en/` H1:** still `TwoComms — **український** streetwear…` (Ukrainian).
- Accept «no Ukrainian on /en/ key blocks» **FAIL** → uncheck.

#### W3-9 — Telegram webhook secret — **СНЯТО с partial**
- REPO: warns if secret empty — OK.
- Prod: **`TELEGRAM_BOT_WEBHOOK_SECRET` was EMPTY** → webhook effectively open.
- Accept «secret configured» not met → uncheck full done.

#### W3-11 — CheckoutCapture limits — **СНЯТО с partial**
- Rate-limit + cleanup command: code OK.
- **`converted` never true** on mono path (mono has zero CheckoutCapture refs; prod 0/4) → incomplete.

#### W0-5 — OPS + stash — **СНЯТО с partial**
- `twocomms/docs/OPS.md` written — REPO OK.
- OWNER: 10 server git-stashes **not** resolved → full item oversold.

---

## 2. Оставлены `[x]` — но с нюансами (галочку НЕ снимали)

| ID | Keep [x]? | Nuance (must not ignore) |
|----|-----------|---------------------------|
| **W1-1** | yes | COD 500 fixed; UI COD off by decision. **Still:** no session ensure on COD create (F-074). |
| **W1-7** | yes | Hero CSS fix in theme; confirm **collectstatic** if prod CSS stale. |
| **W2-4** | yes | Bot/dedupe code OK; **historical** product_view noise remains. |
| **W2-7** | yes | `on_commit` dispatch exists; re-grep any remaining in-lock CAPI. |
| **W2-8** | yes | Normalize code OK; live still saw `chatgpt.com` / `ig` — not 100% clean. |
| **W3-1** | yes | Telegram sync default OK; `CELERY_BROKER` may still be set. |
| **W3-10** | yes | AST safe eval; low residual risk. |
| **W6-3** | yes | Badges claimed; not re-browser-tested this pass. |
| **W7-23/24** | yes | Claimed fixes; not every call site re-audited. |

If fix agent needs strict «zero residual», treat **OK_NUANCE** as follow-ups, not as reopen of the main bug.

---

## 3. Оставлены `[x]` чисто (re-check OK)

W0-4 · W1-2 · W1-3 · W1-4 · W1-5 · W1-6 · W1-8 · W1-9 · W1-10 · W1-12 · W1-13 · W1-14 · W2-5 · W2-6 · W2-9 · W3-2 · W3-3 · W3-4 · W3-5 · W3-7 · W3-12 · W5-2 · W6-1 · W6-2 · W7-6  

**Live confirms (sample):** success-preview/test-analytics gated; success/1 → 404; early PageView present; anon home no Set-Cookie; swagger closed; feed links sample OK.

**Do not re-break these** while fixing REOPEN items.

---

## 4. Live evidence snapshot (this pass)

| Check | Result | Affects |
|-------|--------|---------|
| Category titles mid-phrase | **FAIL** `від`/`та`/`на` | ADS-3 uncheck |
| `/en/` H1 Ukrainian | **FAIL** | ADS-2 uncheck |
| Early fbq PageView | PASS | ADS-1 partial |
| Loader BFCache def | **FAIL** called, not defined | ADS-1 uncheck |
| success-preview / test-analytics | PASS gated | W1-2, W1-8 keep |
| last-breath title vs H1 | **FAIL** mismatch | F-004 (not plan DONE) |
| NP `Kyiv` | **502** | F-050 OPEN |
| NP `Київ` | 200 | |

---

## 5. What changed in IMPLEMENTATION_PLAN.md

1. Inserted banner: **RE-VERIFY PASS 2026-07-09**.
2. Set **`[ ]`** on: W0-5, W2-1, W2-2, W2-3, W3-9, W3-11, ADS-1, ADS-2, ADS-3, W7-1.
3. Under each: blockquote **`RE-VERIFY <ID>:`** with reason.
4. Under remaining nuanced DONE: **`RE-VERIFY` nuance** notes (W1-1, W1-7, W2-4, W2-7, W2-8, W3-1).

**Rule for re-checking done later:** only mark `[x]` when **live accept** in the plan item is proven (not only unit tests / partial code).

---

## 6. Fix agent priority (after uncheck)

1. **W2-1** first_touch → Order.utm_* + COD session + mono tracking/capture  
2. **W2-2** is_converted (after W2-1)  
3. **W2-3** purchase UA completeness  
4. **ADS-1** define/fix `initializePixelsImmediately` + collectstatic  
5. **ADS-3** reseed Category.seo_title in DB  
6. **ADS-2** locale H1/content not only .po  
7. **W7-1** eliminate views.py.backup lazy load  
8. **W3-9** set TG webhook secret on server  
9. **W3-11** mono CheckoutCapture.converted  
10. **W0-1 REPO** remove `deploy_paramiko.py` password  
11. **W1-11** ubd public media  
12. Remaining OPEN plan waves  

---

## 7. Related findings IDs

| Topic | F-ID |
|-------|------|
| Order UTM empty / first_touch | F-021, F-033, F-071, F-045 |
| is_converted | F-019 |
| purchase undercount | F-083 |
| pixel BFCache | F-030, F-079 |
| category titles | F-001, F-023 |
| EN H1 | F-005 |
| ubd public | F-087 |
| TG secret | F-088 |
| backup cron | F-090 |
| deploy_paramiko secret | F-093 |
| title≠H1 products | F-004, F-094 |
| SSH owner done | F-092 |

---

## 8. Document control

| Field | Value |
|-------|--------|
| Purpose | Honest DONE audit + plan checkbox correction |
| Plan file | `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` (checkboxes updated) |
| This file | Explanation of every uncheck / nuance |
| Ads gate | **BLOCKED** (W2-1/2 and ADS residuals open again) |

*End — re-verify of completed work only*
