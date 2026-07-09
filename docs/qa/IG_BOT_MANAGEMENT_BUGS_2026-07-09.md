# Instagram Direct bot (management) — bug analysis (no fixes)

**Date:** 2026-07-09  
**Scope:** management subdomain bot UI + backend (`management` app)  
**Code focus:** `services/instagram_bot.py`, `bot_views.py`, `ig_bot_models.py`, `templates/management/bot.html`  
**Recent change:** commit `1743661c` — *Upgrade Instagram sales bot automation* (stats, hide/unhide, filters, follow-ups, sales signals)  
**Method:** static code review + tests inventory + last-commit diff. **No production IG API / live inbox access.**  
**Do not implement here** — findings for fix agent only.

---

## 0. Architecture (short)

| Layer | Role |
|-------|------|
| Webhook / poll | `handle_webhook_payload` / `poll_ingest` → enqueue `InstagramBotMessage` |
| Worker | `process_pending` → `_process_one` → Gemini → `send_text` (Graph API) |
| Client CRM | `IgClient` (stage, pause, takeover, **hidden_at**, product pin, sales fields) |
| Management UI | `bot.html` tabs: overview, clients, settings, KB, **stats** |
| APIs | start/stop, clients list/detail, pause/resume, **hide/unhide/lost**, **stats** |

Global bot switch: `InstagramBotSettings.is_enabled` + Start/Stop buttons.  
Per-client mute: `bot_paused` / `manager_takeover` / `is_blocked` / **`hidden_at`**.

---

## 1. Bugs / gaps (by severity)

### IG-001 — Hide UX looks broken (list not refreshed) · **P1**

**Symptom (owner):** «hide не працює».

**Code:**
- API OK: `bot_client_hide_api` sets `hidden_at` + cancels follow-ups (`bot_views.py` ~669–685).
- List filters hidden out of `view=active` (`hidden_at__isnull=True`).
- **UI after Hide:** JS only re-calls `detail(id)` — **does not reload list** and does not switch to «Hidden» filter.

```js
// bot.html — after hide/unhide/lost:
.then(r=>r.json()).then(()=>detail(id))  // list row stays in Active
```

**Effect:** User still sees client in Active list → thinks hide failed. Detail may still open by id (API detail has no hidden filter).

**Also:** Hide/Unhide buttons both always shown; labels **English** (`Hide`/`Unhide`/`Mark lost`).

**Fix direction (for later):** on success → `load(search)` + clear detail or switch `currentView='hidden'`; Ukrainian labels; optional toast.

---

### IG-002 — Hide does not pause bot / takeover (only CRM hide) · **P2**

**Code:** `_client_blocked()` treats `hidden_at` as blocked → bot **skips replies** (good).

**Nuance:** Hide does **not** set `bot_paused` / `manager_takeover`. Semantics: hidden from UI + silent bot, but stage/flags unchanged. If product intent was «hide = archive + stop + manager», incomplete.

**Pause button** only sets `bot_paused` (not takeover). Resume clears both pause and takeover.

---

### IG-003 — No «transfer to manager» button; escalation only via AI tags · **P1 product/UX**

**Owner:** «перенос не працює / кнопки».

**Code:**
- Stage `lead_manager`, `manager_takeover`, `notify_manager(...)` exist.
- Takeover mainly via **echo** (`_handle_echo` when human writes from Page) or model control `[manager]`.
- UI has **Pause / Resume / Hide / Unhide / Mark lost** — **no** explicit «Передати менеджеру» that sets `manager_takeover` + stage + TG notify.

**Effect:** «Transfer» feels broken because **feature is not exposed as a reliable CRM action**.

**Fix direction:** POST API `transfer/` + UA button; set `manager_takeover`, `bot_paused`, stage, notify.

---

### IG-004 — Stats UI English + thin presentation · **P1 UX** (from `1743661c`)

**Location:** `bot.html` Stats module + `bot_stats_api`.

**English hard-coded labels (examples):**
- Filters: `Active`, `Due follow-ups`, `Hidden`, `Spam/Cold`, `Paid`, `Ads`
- Detail: `Readiness`, `intent`, `objection`, `Signals`, `Follow-ups`
- Stats cards: `Conversations`, `Qualified`, `Product matched`, `Checkout/payment`, `Paid`, `Pending follow-ups`, `Discount conversions`, `Manager takeovers`, `Custom print handoffs`
- Tables: `Product/SKU interest`, `Ad/ref performance`, `Objections`, columns `SKU/Product/Chats/Ad ID/Ref/Title/Paid/Revenue/Reason/Count`

**API** returns raw stage/intent/objection **keys** (`new`, `price`, …), not Ukrainian labels (`get_stage_display()` only used on client cards).

**Depth gaps:**
- No time range (today / 7d / 30d) — lifetime only.
- No conversion funnel % (chat→qualified→paid).
- No reply latency / failed sends / Meta error rate.
- No «message request / send blocked» counter.
- Revenue aggregation via `Sum(deals__amount)` can **double-count** multi-deal clients.
- `followup_recoveries` join heuristic is weak.
- UI is plain table grid — not «красива» dashboard.

---

### IG-005 — Message Requests / non-role recipients: bot fails send, manager spam · **P0 ops**

**Owner:** many people write; threads sit in IG **Message Requests**; bot «cannot reply» / «forwarded to manager».

**Root cause (Meta + code):**
1. `send_text` uses `messaging_type: "RESPONSE"` only (no special handling for request folder).
2. Permanent Graph errors classified in `_classify_send_error`:
   - subcode **Advanced Access** → no Advanced Access / non-tester.
   - **#551** → «отримувач недоступний (блокування, деактивація або обмеження діалогу)» — often matches request/restricted threads.
   - 24h window closed.
3. On permanent fail: message `FAILED`, **hourly** `notify_manager` about Advanced Access / cannot reply to non-role users.
4. **No field** on `IgClient` like `in_message_request` / `send_blocked_reason` / last Graph code.
5. CRM does not show why silent or failed.

**Not a pure «bug in if»** — often **Meta policy + pending request state** — but product must:
- detect & **label** «Запит у Message Requests / send blocked»,
- not look like random manager ping without context,
- avoid treating every fail as «передано менеджеру» sales handoff (different from operational alert).

---

### IG-006 — Likes / reactions / some non-text events poorly handled · **P1**

**Code:**
- `is_unsupported` / `is_deleted` → **skipped** (no enqueue).
- Inbound mainly **text + media attachments** (image/share/reel/story).
- No dedicated reaction/like handler → either ignored or odd path through empty text + Gemini.
- Story mentions/reels partially via attachments list.

**Effect:** user actions that are not full DMs produce no useful bot reply / no CRM signal → looks broken.

---

### IG-007 — Permanent send failure path ≠ «передано менеджеру» stage · **P2**

On fail: manager gets Telegram alert, but client stage often **unchanged** (not auto `lead_manager`).  
Owner may interpret alert as handoff; CRM still shows old stage + bot not paused → inconsistent.

---

### IG-008 — False manager takeover on echo · **P2** (partially mitigated)

`_handle_echo` + `_mark_bot_sent` to avoid bot echo → takeover.  
Tests cover some cases (`tests_ig_audit_fixes`). Residual risk if cache miss / multi-instance cache / text normalization mismatch → **false pause**.

---

### IG-009 — Hide/unhide/lost lack automated UI tests · **P2**

`tests_ig_clients_ui.py`: pause/resume covered; **no** hide/unhide/lost/stats filter tests.  
`tests_ig_sales_automation.py` covers automation backend, not hide UX.

---

### IG-010 — Active list filter hides paid/spam by design · **P3 UX**

`view=active` excludes PAID/DONE/SPAM/COLD. Easy to think clients «disappeared» (not hide bug). Needs clearer UA copy.

---

### IG-011 — Global Start/Stop vs per-client pause confusion · **P3**

Start/Stop toggles `is_enabled` globally. Per-client «Стоп бота» is `bot_paused`.  
UI labels OK in Ukrainian for start/stop; Hide still English. Docs for operators missing.

---

### IG-012 — Stats tab may 500 / empty if migration not applied · **P1 deploy**

Commit adds migration `0074_ig_sales_automation.py` and models fields. If management deploy without migrate → hide/stats APIs fail. Verify deploy/migrate on management host separately from main shop audit.

---

### IG-013 — No Message Request flag in data model · **P1 product**

Required product behavior («все що висить у запитах — окремо помічати») **not implemented**:
- no `IgClient.message_request_state`
- no UI filter «У запитах»
- no parse of Graph error → set flag on client

---

### IG-014 — Manager handoff wording vs operational alerts mixed · **P2**

`notify_manager` used for:
- sales escalation (`needs_manager`),
- spam,
- paylink failure,
- **system cannot send**,
- takeover.

Same channel, different meaning → UI/CRM should distinguish «sales transfer» vs «system blocked send».

---

## 2. What last commit `1743661c` actually added

| Area | Added | Risk / unfinished |
|------|--------|-------------------|
| Hide/unhide/lost APIs | yes | List UX not updated after action (IG-001) |
| Client list filters | Active/Due/Hidden/… | Labels English (IG-004) |
| Stats API + tab | yes | English, lifetime-only, thin UX (IG-004) |
| Follow-ups + sales classifier | backend | New complexity; UI English «Follow-ups/Signals» |
| Meta feedback flags | settings | Advanced Access still external |

---

## 3. Likely root-cause map (owner symptoms → code)

| Owner symptom | Likely cause |
|---------------|--------------|
| Hide doesn’t work | List not reloaded after API success (IG-001); or expect hide=pause without understanding silent skip |
| Transfer broken | No transfer button; only AI/echo escalation (IG-003) |
| Stats English / weak | Hardcoded EN strings + raw keys (IG-004) |
| Bot can’t answer many writers | Message Requests + Advanced Access / #551 / 24h window (IG-005); no CRM badge |
| Like/sub weird replies | Unsupported/reaction events skipped or empty AI path (IG-006) |
| «Передано менеджеру» noise | `notify_manager` on permanent send fails ≠ stage handoff (IG-007/014) |

---

## 4. Suggested fix order (for implementer — not done now)

1. **IG-005/013** — capture Graph error on client; flag Message Request / blocked; filter in CRM; quieter alerts.  
2. **IG-001** — hide/unhide refresh list + UA strings.  
3. **IG-003** — explicit transfer-to-manager action.  
4. **IG-004** — full Ukrainian stats + denser metrics + date range.  
5. **IG-006** — reaction/story edge cases.  
6. Tests for hide + stats i18n.

---

## 5. Key file map for fix agent

| File | Why |
|------|-----|
| `management/templates/management/bot.html` | EN labels, hide UX, stats render |
| `management/bot_views.py` | hide/stats APIs, client filters |
| `management/services/instagram_bot.py` | send errors, echo, blocked, process_one |
| `management/ig_bot_models.py` | `hidden_at`, stages, signals |
| `management/urls.py` | hide/unhide/lost/stats routes |
| `management/migrations/0074_ig_sales_automation.py` | must be applied |

---

## 6. Relation to main-site audit

This is **management IG bot**, separate from `twocomms.shop` storefront Pass A.  
Cross-link: main site order UTM issues do not block these CRM bugs; ads ROAS still on main site plan.

---

*Analysis only — no product code changes in this document.*
