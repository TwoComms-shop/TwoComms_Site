# A2 · Overview tab, console, alerts, cron

**Auditor:** root session, 26–27.09.2026 · **Prod SSH:** read-only (HEAD `c69925d56`, load avg ~5–6 on 20 CPU / 128 GB shared host)

---

## 1. What the Overview tab actually shows (as of 26.09)

`templates/management/bot.html:1265–1312`, JS `render()` at `:1967–2068`, data from `bot_status_api` (`bot_views.py:1061`) → `instagram_bot.status_snapshot()` (`services/instagram_bot.py:~18100–18190`).

**Status card** (9 metrics, all from local DB/cache — no external calls):

| Widget | Source field | Meaning | Owner's confusion |
|---|---|---|---|
| Відповідей | `replies_count` | lifetime counter (from `InstagramBotSettings`) | fine |
| Клієнтів | `unique_senders_count()` | distinct senders, all time | fine |
| **У черзі** | `pending_count()` | pending inbound rows | **R-24 "7 в очереди — очередь чего?"** — ambiguous: this count includes manager-owned and revision-owned preserved evidence, which are *not* runnable. D084 already documents this exact ambiguity (`inbound_pending=7` = preserved evidence, not work). Label must split runnable vs owned. |
| Остання | `last_reply_at` | last outbound reply time | fine |
| Стан зв'язку | `heartbeat_at` | daemon heartbeat | fine |
| **Модель** | `last_gemini_model` (falls back to `gemini_effective_model`) | **the model used by the most recent successful AI call of *any* kind** | **R-24/R-26**: shows e.g. "gemini-3.5-flash-lite" because a background/ordinary call happened last, while a user reading "Модель" assumes it is *the* model answering. It is neither the configured default nor the routing policy — it is a single last-call sample. Misleading. |
| **Рівень міркування** | `last_gemini_reasoning_level` + `_task` | thinking level of the same single last call | only meaningful next to the model and task; tooltip does explain "політика <version>", but the headline number is still a sample of one call |
| Сповіщення | `notification_pending/failed/unknown/dead_letter` | Telegram outbox state | good, actionable |
| Перевірка перед відповіддю | `reply_barrier.waits/aborts` | send-serialisation waits | fine but jargon |

**Diagnostics strip** (`#bot-overview-diagnostics`): configuration warnings (allowlist, missing account id), last error, outbox help, Meta capability (token permission, account access, per-recipient delivery), Meta rate-limit degradation. This part is genuinely useful and honest about uncertainty.

**Console** (`#bot-console`): polls `bot_status_api` every cycle with `after_id`, appends up to 120 newest `InstagramBotLog` rows, keeps last 400 DOM rows. Raw event/level/detail lines — no grouping, no filtering, no "show only problems". Retention in DB: `LOG_KEEP_ROWS = 500` (`instagram_bot.py:70`, pruned at `:3031`).

### Findings

### F3-OPS-01 · "У черзі" mixes runnable work with preserved evidence — P1 · CODE
- **Observed:** `pending_count()` is rendered as a queue length; D084.1 shows the underlying `inbound_pending=7` are "revision-owned preserved evidence", and `customer lane manual=1, manager_owned=12` are counted separately in the deeper health payload but not in this card.
- **Impact:** owner asks "7 in queue — of what?"; a manager cannot tell whether anything needs action. Directly named in the brief (R-24).
- **Recommendation:** split the card into `До відповіді (runnable)` / `Чекає менеджера` / `Збережені джерела (не дія)`, each with an age (oldest seconds). Use the existing lane-health payload instead of the legacy counter. Keep `pending_count` only in a tooltip for compatibility.
- **Plan link:** B03.20, B17.9 (lane health), C19.3 (observability). The 2.0 plan already wants this; the UI just wasn't switched over.

### F3-OPS-02 · "Модель" is a last-call sample presented as current state — P1 · CODE
- **Observed:** `last_gemini_model` is written on every successful provider call (`instagram_bot.py:8434`) regardless of purpose (chat, analysis, image, health). UI shows it under the label "Модель" and "Рівень міркування" next to it.
- **Impact:** the owner reads "3.5 Flash Lite відповідає" and concludes the *sales* model is Lite — while complex/media turns actually route to 3.8 (D082-G). It also silently changes when a background analysis job finishes. Confusing and slightly dishonest as a status signal (R-24, R-26).
- **Recommendation:** show a routing table instead of a single value: per role (`ordinary chat`, `complex/product`, `media/vision`, `analysis`, `presence/health`) the model currently selected *by policy* + the last actual call (model, slot, task, latency, outcome) + quota headroom. Data already exists (`gemini_v2_read_model.build_routes_payload()`, `gemini_health.build_snapshot()`).
- **Risk if unchanged:** owner cannot verify that 3.8 is being used for photos, which is exactly the R-26 requirement.

### F3-OPS-03 · Console is unbounded raw noise; no severity or domain filter — P2 · CODE/UX
- **Observed:** one flat list, newest 120 rows, DB keeps 500; mixed events (daemon_start 20, presence_summary 122, webhook_inbox_committed 93, revision_collaboration_recovery 186 in 7 days per D085 §4.3).
- **Impact:** the console is where a manager looks during an incident; 186 recovery warnings drown the one error that matters. It also can't answer "did we lose a message for client X".
- **Recommendation:** keep the raw console but add (a) level filter chips + "only problems" default toggle, (b) grouping by event with counts and last timestamp, (c) a client-scoped filter (search by client id), (d) sticky "oldest unresolved owed reply" banner. Also raise retention deliberately and store the detail field trimmed (there is already a comment about PII leaking into `detail`, `instagram_bot.py:9765`).
- **Plan link:** C19.3, B16.

### F3-OPS-04 · Alerting is correct-but-narrow: dedupe window 60 min, single alert type per condition — P2 · CODE (design debt)
- **Observed:** `alert_daemon_runtime_health()` (`services/ig_daemon_health.py:151–231`) sends at most one Telegram alert per 60-minute window per `(reason, fingerprint)`; newest commits (`acdb56949`, `c69925d56`, `e273e213b`) already fixed the worst spam (Nova Poshta transport degradation → `degraded` after 3 failures; quiet watchdog start).
- **Gap:** the alert contract covers *daemon/lane/debt* only. It does not cover: quota exhaustion mid-day (R-25), a client waiting > N minutes without reply (the actual conversion-relevant event), repeated provider 5xx for one model, media capture failures, or a manager case left unowned. Config warnings are visible in the UI but never pushed.
- **Recommendation:** define one `attention` event vocabulary (severity, subject, age, dedupe key, deep link to client/tab) and route both UI and Telegram through it. Then: "no more than one alert per condition per hour, but a new *class* of problem always gets its own alert".
- **Plan link:** B17.6, C19.3, D074.

### F3-OPS-05 · Hourly-only alerting means a stall can go unnoticed up to an hour — P2 · DESIGN
- **Observed:** health alert is driven by the periodic jobs cadence (the health job runs each hour for stalled-daemon detection; lane/debt alerts ride the same path).
- **Impact:** during business hours a 10-minute stall is already a lost sale for a waiting customer. Owner's R-27 wants no lost replies.
- **Recommendation:** decay-aware alerting: for a *customer-visible* stall (a client has been waiting and no reply is in flight) alert within ~3–5 min; for background lanes keep the hourly contract. Do not increase cron frequency globally — subscribe by webhook/state-change instead (see F3-OPS-06).
- **Plan link:** D079, B17.6.

### F3-OPS-06 · Cron is still poll-driven where events exist (payments, NP, inbound) — P1 · CODE
- **Observed (server crontab, read-only):** watchdog every minute (`flock -n`), heavy jobs every minute with `flock -w 50`, second heavy lane `flock -n`; the IG coordinator and durable job ride these. Payment truth, NP tracking and inbox refresh are **polled** by periodic jobs (`poll_ig_deal_payments`, `reconcile_*`, `ig_inbox_refresh`), with polling as an explicit fallback in settings (`receive_via_poll`).
- **Impact:** latency + wasted DB work + the 20-connection budget shared with the storefront (D085 §4.1). Owner explicitly asks for webhook-first with polling as safety net (R-31).
- **Recommendation:** keep the poll as reconciliation only (hourly, cursor-based); make the *primary* path event-driven: Monobank webhook → payment event → bot reaction; NP webhook/`TrackingDocument` events → pickup event; Meta webhook already exists for inbound. Record in the decision-log which source confirmed each fact (webhook vs poll) — see 80_… design.
- **Plan link:** B08 (checkout/payment), B10 (service), D085-R1.

---

## 2. Server / cron facts verified 26.09 (read-only SSH)

- HEAD `c69925d566ccbe2109953644e1d1187ecd38260c`, branch `main`, dirty files unrelated to the bot (`passenger_wsgi.py`, `releases/`, `Anl/`, ad-hoc scripts).
- Uptime 213 days; load average ~5.96/4.71/4.19; **20 CPU**, 128 GB RAM total, ~49 GB free + 67 GB cache, ~12 GB used.
- Crontab (redacted): minute-cadence `flock`-guarded watchdog + two heavy lanes; heavy lane 1 waits up to 50 s (`-w 50`), heavy lane 2 fails fast (`-n`). Matches D085 §4.1 narrative — **no random regression to the older config**.
- Retention: `InstagramBotLog` capped at 500 rows + 100 slack; console DOM at 400 lines.

**What this does not prove:** actual peak concurrency, real CPU/RAM/EP quotas of the shared hosting plan (CloudLinux), and whether the 6 LSAPI web slots are enough at 20+ conversations/day. Those remain open (R-36).

---

## 3. Ideas beyond the brief (evaluate before adopting)

1. **"Attention board" as the first screen** (replaces part of Overview): sorted by "who is waiting and for how long", with the reason (no reply yet / manager owes / payment unconfirmed / objection open). One click → that client's chat + funnel. Directly serves R-16 and R-24.
2. **Live conflict resolution panel:** when two clients are waiting, show both with age; today only aggregate counts exist.
3. **Quota headroom widget:** per model/slot rows with "used today / limit" from `gemini_v2_read_model` — makes exhaustion predictable instead of a surprise (R-25).
4. **Console "explain this turn" mode:** pick a message and see the decision chain (intent, route, model, prompt hash, guard verdict, latency, delivery state). This is the seed of the 80_… decision-log and answers "why did the bot say that".
5. **Alert digest:** instead of individual Telegram messages, one rolling digest every N hours plus immediate alerts only for P0 classes. Owner complained about Telegram spam historically (R-36).

---

## 4. Open questions for the owner

- **Q-OPS-01:** Which signals must reach Telegram immediately, and which may wait for a digest? (current: daemon stall/debt only)
- **Q-OPS-02:** Is the console used daily by anyone but you? Should it default to "problems only"?
- **Q-OPS-03:** Preferred waiting threshold before an alert about an unanswered client (5 min? 15 min?) — D057 uses 5 min as an internal target but no alert is attached to it.
