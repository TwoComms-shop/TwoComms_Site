# Management Instagram Sales Bot Super-Upgrade Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Every checked task is a separate delivery slice with its own focused tests, commit, push, deploy, and production verification.

**Goal:** Make the Management-subdomain Instagram bot reliable, conversion-oriented, observable, secure, and maintainable while preserving a human manager's control over every conversation.

**Architecture:** Keep the current Django management app, database-backed queue, shared cache, cron-friendly daemon, Meta Graph API, and Gemini REST integration. Strengthen the boundaries between ingress, conversation state, AI generation, delivery, CRM analysis, follow-up scheduling, notifications, and admin presentation. Use `gemini-3.6-flash` as the primary chat model only after an explicit model/key capability check; retain a controlled fallback chain for outages, quota exhaustion, or a provider-side model error.

**Tech Stack:** Django 5.x, Python 3.14, MySQL in production, existing cache backend, Django management commands + cron, Meta Graph API v25.0, Gemini `generateContent`, vanilla JavaScript management UI, Django `TestCase`, mocked HTTP tests, Playwright/browser smoke tests, SSH deploy to the hosted server.

---

## 0. Scope and Working Rules

### In scope

- Management-subdomain Instagram Direct webhook, worker, polling backstop, Meta Send API, Gemini generation, key rotation, Telegram alerts, CRM card, follow-ups, payments, ad attribution, CAPI feedback, media understanding, prompt/playbook routing, admin console, database/indexes, security, tests, deployment, and operational documentation.
- Analysis of messages sent by the bot, the customer, and a human manager. A manager message must update CRM analysis even when the bot is paused and must never cause an automatic bot reply.
- Production truth from the server and MySQL. Local code is the implementation source; local SQLite/test DB is not evidence of production behavior.

### Explicitly out of scope for this plan

- Storefront redesign, DTF subdomain changes, unrelated management analytics, or changing Meta permissions/App Review itself. We will document Meta permission blockers and expose them clearly, but cannot grant Advanced Access from Django.
- Sending live advertising test events or creating fake customer orders. Live checks use read-only API calls or a single explicitly authorized smoke message only after a separate approval.

### Delivery rule

For every implementation checkbox below:

1. Add/update focused tests first.
2. Implement only that slice.
3. Run the listed local checks and `git diff --check`.
4. Commit only intended files with a descriptive message.
5. Push `main` (or merge the approved feature branch into `main`).
6. On the server run the documented pull/migrate/check/static/restart/daemon commands.
7. Verify deployed SHA, migration state, daemon heartbeat, and the slice-specific smoke check.
8. Mark the checkbox as done in this file in the same commit, then repeat.

Never stage the existing unrelated worktree files (archives, screenshots, packages, or `django.log.1`).

---

## 1. Audit Evidence (2026-07-22)

### Production facts verified through SSH

| Observation | Evidence | Consequence |
|---|---|---|
| All six Gemini environment keys exist | `GEMINI_API`, `GEMINI_API2` … `GEMINI_API6` were present; values were not printed | Key-pool work can use the complete configured pool, but must still track per-key quota and health. |
| `gemini-3.6-flash` is available | `models.list` returned `models/gemini-3.6-flash` for all six keys | The requested model is a real provider model for these projects; it is not currently represented in the local model chains. |
| Generation works on all keys | Parallel short `generateContent` probes returned HTTP 200 for all six keys; some returned `MAX_TOKENS` because Gemini thinking consumed the tiny output budget | Capability is proven, but generation config must reserve output tokens and record finish reasons. |
| Production settings select the wrong/unused model | DB row has `gemini-3-flash-preview`; local `gemini_generate()` calls the centralized `chat` chain and does not use `InstagramBotSettings.gemini_model` | Admin model selection is misleading and `gemini-3.6-flash` cannot be made primary by changing the current UI alone. |
| Bot is enabled but not alive | `is_enabled=True`, `ai_enabled=True`, DB heartbeat `2026-07-10`, cache heartbeat absent, `status_snapshot().running=False` | The UI correctly reports the current dead daemon, but there is no active watchdog cron to recover it. |
| Watchdog is absent | Production `crontab -l` has no `run_instagram_bot --ensure` entry; only Passenger processes were running | Add a guarded every-minute watchdog and make its liveness state unambiguous. |
| App secret is absent | Production snapshot reports `app_secret_set=False` | Current `verify_signature()` fails open. Production must fail closed or require an explicit development-only override. |
| Repeated manager takeover events exist | Production logs contain multiple `takeover` rows for the same IGSID on consecutive messages | `_handle_echo()` notifies Telegram on every unmarked manager echo instead of only on the state transition. |
| Production has broad sender access | `allowed_senders` is empty in the live snapshot (`allow_all=True`) | Local allowlist and Meta role permission are separate; non-role failures must be shown as Meta delivery blockers, not silently treated as bot logic failures. |

### Local code facts

- Main path: `twocomms/management/services/instagram_bot.py`, `bot_webhook.py`, `bot_views.py`, `models.py`, `ig_bot_models.py`, `services/bot_followups.py`, `services/bot_sales_classifier.py`, `services/bot_playbooks.py`, `services/call_ai_analysis.py`, `services/gemini_keys.py`, `services/bot_orders.py`, `services/bot_payments.py`, `services/ig_meta_events.py`, `management/commands/run_instagram_bot.py`, and `templates/management/bot.html`.
- The event-driven design already avoids normal read-history calls to Instagram; the local message table supplies the Gemini context. Polling is an optional backstop.
- Incoming `mid` is unique and queue rows have `pending/processing/done/failed` states. Stale processing reclaim and a per-client automation lease already exist.
- `_handle_echo()` stores manager messages and classifies them, but it always sets takeover and calls `notify_manager()`.
- Permanent Send API errors are classified, stored on `InstagramBotSettings.last_error` and `IgClient.delivery_*`, and rate-limited only at the global alert level.
- `IgClient.buying_readiness` is a cumulative regex score. It is useful as a hint but is not a calibrated probability: repeated messages can inflate it, negative evidence does not reliably subtract it, and the UI presents it as a precise percentage.
- Follow-ups already have quiet hours (`10:00–19:00 Europe/Kyiv`), a 23-hour Meta window guard, payment reminder, thinking/qualification reminders, and capped 5%/10% rescue offers. The policy is not yet visible enough to the operator and does not expose a full decision/evidence trail.
- The UI exposes status, clients, follow-ups, signals, deals, and stats, but it does not show key health, model actually used, notification idempotency, score confidence/evidence, failure transition history, or a real countdown reason.

---

## 2. Target Invariants

These are non-negotiable acceptance rules for the final system.

1. **One state transition, one alert.** Enabling, disabling, manager takeover, permanent delivery failure, and AI-unavailable states create at most one Telegram notification per client/state epoch. Repeated echoes/retries only update counters and logs.
2. **No duplicate customer sends.** A webhook retry, polling duplicate, background thread, daemon loop, stale-worker reclaim, or post-send CRM exception cannot send the same customer reply twice.
3. **Manager messages are observable but not automated.** Every manager message is stored, classified, included in the conversation timeline, and cancels bot follow-ups. It never triggers a generated reply.
4. **Paid means verified paid.** `payment_pending`, a generated invoice, a customer promise, or a screenshot is not `paid` until the verified payment/deal/order path confirms it.
5. **A percentage always has evidence.** The UI must show score band, confidence, evidence, last analysis time, and uncertainty. A raw 60% without a reason is not acceptable.
6. **No invented catalog facts.** Product, SKU, price, availability, discount, payment URL, delivery promise, and custom-print final price come from structured server data or an explicit manager instruction.
7. **The bot does not message outside policy.** Quiet hours, Meta messaging window, opt-out/no-buy, spam, hidden clients, manager takeover, paid orders, rate limits, and max follow-up count are enforced before Send API.
8. **Production liveness is truthful.** `enabled`, `daemon alive`, `webhook receiving`, `queue healthy`, and `Meta delivery healthy` are separate states. The UI never labels an enabled-but-dead daemon as online.
9. **Secrets never reach UI/logs.** Custom tokens and API keys are write-only/masked; logs contain key names and error classes, never key values or raw provider payloads.
10. **Every operational action is idempotent.** Migrations, playbook seed, watchdog spawn, key probing, follow-up scheduling, CAPI events, and deploy commands can be retried safely.

---

## 3. Gemini 3.6 and API-Key Strategy

### 3.1 Required behavior

- Primary model for Instagram chat: `gemini-3.6-flash`.
- Primary role pools remain separated: chat keys first, management/checker keys only as controlled borrow fallbacks.
- All six configured keys are usable candidates. The scheduler must prefer the most recently healthy key, but must not permanently strand a healthy key behind a stale cooldown.
- A model/key attempt records: key name, model, role, latency, HTTP class, finish reason, token usage, retry/cooldown, and final outcome.
- A `404/403` model capability error skips the model for that key/project; a quota `429` cools down the key; a provider `503` cools down the model briefly; a transport timeout is transient and bounded by a request deadline.
- The admin UI displays the configured primary model and the actual model/key used for the last successful response. These are different facts.

### 3.2 Required code changes

- Extend `services/gemini_keys.py` model chains and free-quota classification with `gemini-3.6-flash` and the provider's exact current response semantics.
- Make `InstagramBotSettings.gemini_model` authoritative for chat primary selection, with an explicit fallback chain rather than silently ignoring it.
- Add model capability/health fields to `GeminiKeyState` or a dedicated probe model: `last_probe_at`, `last_probe_status`, `last_probe_model`, `last_latency_ms`, `last_finish_reason`, and bounded error class.
- Add a management command such as `probe_ig_gemini_pool --role chat --model gemini-3.6-flash --parallel 2` that uses `models.get`/a tiny generation probe, never sends a customer message, and redacts all key values.
- Use a small parallel preflight for the two configured primary chat keys (`GEMINI_API`, `GEMINI_API2`) to reduce time-to-health-result. Do not issue two customer generations just to race them; generation remains single-flight per response and falls through the health-aware pool.
- Make thinking configuration model-aware. Reserve enough `maxOutputTokens` for the answer, record `finishReason`, retry a `MAX_TOKENS` empty response with a bounded output budget, and avoid treating a valid model as unavailable because a probe used too few tokens.
- Add an explicit provider allowlist. User text cannot select an arbitrary URL/model.

### 3.3 Verification

- Unit-test ordering, sticky key behavior, 3.6 quota classification, 404/403 model skip, 429 key cooldown, transient retry, deadline, and redaction.
- Mock `generateContent` responses for `STOP`, `MAX_TOKENS`, safety block, empty parts, 429, 503, 404, and malformed JSON.
- Run the read-only production probe against all six keys and record only HTTP/status/model availability in the deployment log.

### 3.4 Context7 reasoning policy (official contract checked 2026-07-23)

Context7's current official Gemini API sources distinguish two incompatible controls: Gemini 3.x uses `thinkingConfig.thinkingLevel` (`minimal`, `low`, `medium`, `high`), while Gemini 2.5 uses `thinkingConfig.thinkingBudget`. The provider warns that sending both controls can return HTTP 400. Higher levels trade latency/cost for reasoning depth, and provider thought parts/token counts are telemetry, not customer-facing answer text.

The bot therefore needs one explicit, versioned task policy instead of a payload-specific hard-coded value:

| Task class | Gemini 3.6 level | Purpose and guardrail |
|---|---|---|
| `health_probe` | `low` | Connectivity/capability only; never customer content. |
| `customer_chat` | `medium` | Minimum for every normal customer reply; concise output is enforced by prompt/output budget, not by disabling reasoning. |
| `product_decision`, `size_fit_decision`, `catalog_match`, `media_analysis` | `high` | Use when product identity, variant, stock, fit, or ambiguous media affects the answer; structured catalog data remains authoritative. |
| `payment_decision`, `order_decision` | `high` | Use when generating/validating a payment or order action; AI still cannot declare payment verified or invent a URL/SKU. |
| `customer_intelligence`, `conversion_analysis`, `conversation_reanalysis` | `high` | Multi-message evidence synthesis, uncertainty, objections, and next action; no unsupported personality, nationality, or solvency claims. |
| `memory_summary`, `reporting_summary` | `medium` by default, `high` only above an explicit complexity threshold | Keep routine background cost bounded and record why escalation occurred. |

For a Gemini 2.5 fallback, map the task policy to a tested, versioned budget table. This mapping is our operational policy, not a Google-defined equivalence. Persist `reasoning_task`, requested/effective level or budget, policy version, model, key alias, latency, finish reason, candidate/thought token counts, and outcome. Never persist or render provider thought text.

---

## 4. Notification and Duplicate-Alert Design

### Root cause to fix

`_handle_echo()` treats every unmarked page echo as a new manager takeover and calls `notify_manager()`. The existing one-hour cache key only protects the global permanent Meta alert, not the per-client transition. A manager sending three messages therefore creates three identical Telegram notices.

### Target design

- Add a per-client control epoch/version. `manager_takeover=False → True` increments the epoch and emits one `manager_takeover_started` notification. Further manager echoes only update `last_manager_message_at`, classify the message, and write a console log.
- Resuming the bot closes the epoch. A later manager takeover creates a new epoch and one new alert.
- Add an idempotent notification/outbox record (for example `IgBotNotification`) with unique `(client, event_type, state_epoch)` and fields for status, attempts, last error, sent message id, and payload hash.
- Replace scattered direct `notify_manager()` calls with `notify_manager_once()` for stateful events. Keep high-signal immediate alerts for payment-link failure, permanent Meta delivery block, AI-unavailable after bounded retries, spam lock, and manager handoff.
- Deduplicate “cannot answer” per `(client, inbound_message_mid, classified_reason)` and expose the next retry/alert time. A new inbound message or a recovered delivery state starts a new epoch.
- Telegram text must identify the client, event, state transition, and required operator action. Never send “bot disabled” when only that one client is paused.

### Tests

- `_handle_echo()` called five times for one client: one Telegram call, five manager messages, one takeover signal.
- Bot resume followed by a new manager echo: exactly one new alert.
- Same permanent Meta failure retried on the same message: one alert; new message: one new alert.
- Notification insert race from two workers: one row/message, no duplicate side effect.

---

## 5. Liveness, Watchdog, and “Offline” UX

### Root cause to fix

Production has `is_enabled=True` but no running daemon, no cache heartbeat, and no `--ensure` cron. The DB heartbeat is stale. Passenger being healthy does not mean the bot worker is healthy.

### Target design

- Keep the daemon singleton, but use an absolute `manage.py` path and an explicit lock file/cache key shared by Passenger and cron.
- Install a guarded every-minute cron entry for `run_instagram_bot --ensure`, with a bounded log path and `flock`/spawn lock.
- Add a startup/recovery log event with PID, deployed SHA, settings version, and daemon mode.
- Make the daemon heartbeat write both a short-lived cache heartbeat and a DB `heartbeat_at`; record `heartbeat_source`, `last_loop_at`, `last_queue_item_at`, `last_followup_at`, and `last_daemon_error` if needed.
- `status_snapshot()` returns `enabled`, `daemon_online`, `db_heartbeat_age`, `cache_heartbeat_age`, `webhook_last_seen`, `queue_lag_seconds`, `last_error`, `recovery_expected`, and a machine-readable `state` (`disabled`, `starting`, `running`, `enabled_but_worker_missing`, `worker_error`, `meta_delivery_blocked`).
- The UI displays “включен, но worker не запущен” separately from “офлайн” and shows exact last-seen time plus watchdog expectation.
- Add a read-only health endpoint/check command for deploy verification. Do not make a customer message just to prove liveness.

### Tests and production checks

- Unit-test stale DB heartbeat, missing cache heartbeat, fresh daemon heartbeat, disabled bot, and mismatched cache/DB sources.
- Test `--ensure` twice concurrently: one daemon only.
- Add a browser check that the status text and countdown update without a page reload.
- After deploy: verify cron entry, `run_instagram_bot --ensure`, process, cache heartbeat age <45s, DB heartbeat age <90s, and a clean `daemon_start`/`daemon_spawn` log.

---

## 6. End-to-End Conversation Pipeline Audit

### Ingress and Meta boundary

- Verify webhook GET challenge, POST signature, event field variants, echo parsing, referral parsing, media URLs, message IDs, deletes, unsupported events, and retry behavior.
- Change production signature handling to fail closed when `IG_APP_SECRET` is absent, unless an explicit `IG_BOT_ALLOW_UNSIGNED_WEBHOOKS=true` development setting is active. Log the configuration error once, not once per event.
- Store raw webhook payloads only under bounded retention and redact tokens/PII where possible.
- Keep local allowlist semantics separate from Meta role/Advanced Access. Show both in the UI: `local_sender_allowed`, `meta_delivery_capability`, and `delivery_status`.

### Queue and worker boundary

- Preserve unique `mid`, client automation lease, stale reclaim, and “do not retry after successful send” invariants.
- Add an explicit response idempotency key derived from inbound `mid` + response version. Persist the decision before external send and mark the send attempt/result transactionally enough to prevent late CRM exceptions from requeueing a delivered row.
- Add queue latency and attempt counters to the console and status API.
- Remove or bound the webhook background thread if it can race the daemon under load; prefer one worker path with a short wake-up signal.

### Customer observation

- Continue using local history for normal context; do not poll/read every chat by default.
- Polling remains an explicitly labeled backstop, with a single cursor and duplicate protection. It must not create a second copy of a webhook message.
- Profile/avatar fetch, media download, and vision matching are bounded, cached, and never allowed to block the queue indefinitely.

### Human manager boundary

- Every manager echo becomes a `role=manager` message, is classified, cancels pending automation, and sets takeover. No Gemini generation occurs for that echo.
- Add explicit manager actions: pause, resume, hide, mark lost, opt out, and “reopen automation with reason”. Resume must create an audit event and may schedule a fresh follow-up only after a new customer message or explicit operator command.

---

## 7. Conversion State and CRM Semantics

### Problem

`buying_readiness` is a cumulative regex score. It can show a high percentage for a customer who asked a single price question, cannot distinguish confidence from probability, and does not explain why the number changed.

### Target model

Keep the existing `IgClient.stage` for operational routing, but add a versioned analysis snapshot/event:

- `score_band`: `cold`, `exploring`, `qualified`, `high_intent`, `checkout`, `paid`, `lost`, `opted_out`.
- `score_value`: bounded 0–100 heuristic/probability estimate.
- `score_confidence`: 0–1 confidence in the classification, not purchase probability.
- `evidence`: structured positive/negative signals with source message, role, timestamp, and rule/model version.
- `uncertainties`: missing product, missing size, unresolved price, unverified payment, ambiguous language, or Meta delivery failure.
- `last_analyzed_message_id`, `analysis_model`, `analysis_prompt_version`, `analysis_latency_ms`.

Recommended interpretation:

| Band | Meaning | Required evidence |
|---|---|---|
| `cold` | No active buying evidence, explicit refusal, spam, or long unresolved silence | Negative signal or timeout; never inferred from one short message. |
| `exploring` | Questions about product, size, fabric, delivery, or price | At least one topical signal; no checkout commitment. |
| `qualified` | Product/need is known and customer engages with a next-step question | Product or intent plus an answered qualification point. |
| `high_intent` | Customer chooses SKU/size/color, asks for link, or says they will pay | Explicit purchase intent; payment still unverified. |
| `checkout` | Deal/invoice exists or customer is completing delivery/payment details | Structured deal or checkout evidence. |
| `paid` | Verified payment event/order | Provider-confirmed payment only. |
| `lost/opted_out` | Explicit no-buy/stop, spam, or operator mark | Hard stop; no automated re-engagement. |

### Rules

- A later negative or opt-out signal can lower the band; scores do not only increase.
- Repeated copies of the same message are deduplicated for scoring.
- Manager messages are evidence about the conversation, not proof of customer intent. Customer and manager evidence are labeled separately.
- A product question cannot be labeled “conversion” without product/need evidence. A payment promise cannot be labeled “paid”.
- Show “why” in the UI: e.g. `high_intent · 0.82 confidence · SKU fixed, size M, payment link created; payment not verified`.

### Implementation

- Keep deterministic extraction for cheap facts (language, phone, size, color, quantity, explicit stop, payment words).
- Add a bounded Gemini JSON classifier for ambiguous intent/objection/sentiment only after deterministic facts are injected and validated.
- Version the classifier prompt and rule weights. Store raw model output only with PII/length limits and never trust it as a payment fact.
- Add daily calibration/reporting: predicted band versus payment/order outcome, false-high/false-low examples, and a manual correction path.

---

## 8. Follow-Up and Re-Engagement Policy

The current implementation is a useful base. The final policy must be explicit, visible, capped, and data-driven.

### Recommended default ladder (Europe/Kyiv)

| Situation | First action | Second action | Last action | Stop rules |
|---|---:|---:|---:|---|
| Payment link created, no verified payment | +45 minutes: help with payment/link | next eligible day at 10:00 | one final close after 24h if window allows | paid/order, manager takeover, stop/no-buy, 2 sent reminders, Meta window closed |
| “I will pay / give me a minute” without a link | +45 minutes: confirm assistance | next day 10:00 with product context | none unless customer re-engages | no product/SKU, explicit hesitation, opt-out |
| Product/size/price question with no reply | +2 hours if inside window | next day 10:00 | no discount by default | no-buy, spam, hidden, manager, 2 unanswered touches |
| “I’ll think” / price hesitation | next day 10:00 | +12–24h value reminder | 5% rescue only for eligible product-matched lead; 10% final only with explicit policy/approval | any decline, 10% already offered, 3 total follow-ups |
| Custom-print lead | manager handoff task | manager reminder, not customer spam | none automatically outside agreed channel | incomplete brief or explicit stop |

Additional rules:

- Never send before 10:00 or after 19:00. Move due time to the next allowed slot and show the reason.
- Recalculate the timer after every inbound customer or manager message; cancel/replacement must be atomic.
- Use a customer-specific message generated from the structured context, not a generic “будете покупать?” template.
- Store `scheduled_for`, `eligible_at`, `quiet_hours_shift`, `meta_window_deadline`, `cancel_reason`, `sent_at`, `delivery_result`, and `followup_level`.
- The UI shows a live countdown, policy reason, next action, and why a task is cancelled/skipped.
- No automated follow-up after `NO_BUY`, `STOP`, `не пишіть`, spam lock, hidden card, paid/order state, or manager takeover.
- Measure response rate, recovered payment, incremental conversion, opt-outs, and complaints per follow-up level. Do not optimize only for messages sent.

---

## 9. Product, Price, Payment, and Media Truth

- The catalog context must be generated from current published product/variant/price/availability data with a version/timestamp. Cache it briefly, invalidate on product changes, and never leave stale prices in a long-lived client memory summary.
- Product pinning must record product ID, variant/color/fit, confidence, source (`customer_text`, `image_match`, `manager`, `model_tag`), and timestamp. A low-confidence image match must not silently become the payment SKU.
- Payment links must be generated only by `bot_orders` from the pinned/current deal. A model-provided URL is removed. Payment type and amount are stored before Send API.
- Full payment and verified prepayment remain one purchase/order conversion with separate paid value; do not create a second Lead/Purchase event for the same transaction.
- Reconcile Monobank/payment webhooks and polling idempotently. A payment screenshot or customer statement is a signal requiring verification, not a paid state.
- Media downloads must validate URL host/redirects, MIME, size, timeout, and content type. Prefer Meta CDN allowlisting; reject SSRF/private-network targets. Keep image count/bytes caps and log only bounded metadata.
- Vision matching must return candidate SKU(s), confidence, and “ambiguous” when below threshold. The bot asks for a post link or details instead of hallucinating.
- Add tests for regular/oversize/fabric/thermo questions, changing prices, sold-out variants, image mismatch, corrupted media, and stale catalog cache.

---

## 10. Ads, Attribution, Meta CAPI, and Retargeting Data

- Preserve referral fields (`ad_id`, `ref`, source, title, creative URL, payload), but distinguish first-touch, last-touch, and assisted campaign attribution.
- Add immutable attribution touch rows or a JSON event timeline so a later referral does not overwrite the original campaign evidence.
- Map campaigns to product/theme/CTA through admin-editable `BotAdCampaign`, with validation that the linked product is published/current.
- Record funnel events with stable event IDs: conversation started, product matched, checkout started, payment pending, paid/order created, lost, opt-out, manager takeover, follow-up sent, follow-up recovered.
- CAPI feedback stays disabled by default until match-data, consent/privacy, event deduplication, and test-event handling are verified. Never send live test events without explicit authorization.
- Stats must use distinct clients and verified order revenue. Show date range/time zone, denominator, attribution mode, and unknown/unattributed counts.
- Expose exportable campaign cohorts for retargeting: high-intent unpaid, price objection, “thinking”, lost/no-buy, paid repeat buyers, and opt-outs excluded.
- Track ad-to-paid lag, assisted conversions, payment-link opens if available, and follow-up incremental lift. Do not claim ROAS without spend data.

---

## 11. Prompt, Playbook, and Jailbreak Hardening

- Keep `BotInstruction` modular and admin-editable: core sales, SKU/catalog, size/fit, price objection, prepayment, custom print, payment, delivery, no-buy, manager handoff, and privacy/safety.
- Add instruction version, author, active window, priority, validation status, and audit history. Preview the final prompt context before activation.
- Separate trusted system policy, structured catalog/state, manager directives, and untrusted customer content with explicit delimiters.
- Ignore customer requests to reveal system prompts, API keys, internal tags, hidden instructions, scoring internals, or to bypass payment/permissions. The model may acknowledge the request briefly and return to the sale.
- Never let customer text set `[STAGE]`, `[PAYLINK]`, `[PRODUCT]`, `[ORDER]`, or manager controls. Validate model control tags against server state and catalog.
- Add content moderation and escalation for threats, harassment, self-harm, illegal requests, sensitive personal data, and abusive messages. Do not over-block ordinary Ukrainian/Russian sales language.
- Redact phone/payment tokens in logs and keep retention bounded. Provide a customer deletion/export path through existing privacy controls.

---

## 12. Management UX and Console Redesign

### Overview

- Status card: enabled/disabled, worker state, heartbeat age, queue depth/lag, last inbound/reply, current primary model, actual last model/key (key name only), last error class, and watchdog recovery state.
- Console filters: severity, event, client, model/key, delivery outcome, and time range. Add correlation ID per inbound message.
- One-line state transitions: `received → classified → queued → generating → sending → delivered/blocked → CRM updated`.
- Alerts are rendered as transitions, not repeated noise. Show “suppressed duplicate alert” count in the console.

### Client card

- Name/IGSID/profile, first/last touch, language, intent, product/SKU, size/color/qty, score band/value/confidence, positive/negative evidence, payment truth, delivery capability, manager takeover, opt-out, next follow-up countdown, and last analysis time.
- Conversation timeline visually distinguishes customer, bot, manager, system event, and follow-up.
- Actions: pause/resume, hide/unhide, mark lost, opt out, reopen automation, regenerate analysis (read-only preview first), and inspect attribution.

### Follow-up and stats

- Follow-up queue with due countdown, quiet-hour shift, Meta window deadline, reason, level, discount, status, cancellation reason, and delivery result.
- Funnel reports distinguish conversations, qualified, high-intent, checkout, payment pending, verified paid, orders, lost, opt-out, manager takeover, and unknown.
- Ad report includes distinct chats, qualified, checkout, verified paid, revenue, conversion rate, attribution mode, and cohort lag.
- Key health tab shows present/absent, last probe, last success, cooldown scope, requests today, model availability, and last bounded error. Never show values.
- Fix secret fields: custom token/key inputs are write-only/masked and never repopulated into HTML.

### Browser acceptance

- Desktop and mobile screenshots for overview, offline/dead-worker, client detail, countdown, console filters, key health, and stats.
- No overlapping text, no false green “online”, no raw secrets, no inaccessible controls, and no stale client state after pause/resume/hide.

---

## 13. Database, Performance, and Retention

- Add indexes for pending queue by `(status, role, created_at)`, client message timeline, follow-up `(status, due_at)`, notification idempotency, analysis events, attribution touches, and Meta event IDs.
- Use `select_related/prefetch_related` on client detail and stats; preserve distinct counts when joining deals/signals/events.
- Bound raw webhook, bot log, message history, model-output, and notification retention with explicit management commands and dry-run reports. Never delete data needed for payment/audit/legal retention.
- Keep transactions short around DB state; never hold row locks across Gemini, image, Telegram, or Meta HTTP calls.
- Add queue backpressure: per-client serial processing, global max in-flight, per-provider deadline, and graceful degradation to manager task when capacity is exhausted.
- Add a nightly health report: stale processing rows, orphan messages, failed notification outbox, overdue follow-ups, stuck leases, stale heartbeats, missing keys, and unverified payment links.

---

## 14. Implementation Checklist

The order is intentional. P0 blocks safe operation; P1 improves conversion and operator control; P2 improves optimization and maintainability.

### P0 — production correctness and safety

- [ ] **P0.1 Baseline lock and observability contract.** Freeze current production/local evidence, add correlation IDs and status event schema, and write read-only health checks. Files: `instagram_bot.py`, `models.py`, `bot_views.py`, new tests. Verify current deployed SHA and no unrelated files staged. Commit/push/deploy.
- [x] **P0.2 — restore daemon liveness with a cache-independent singleton.** Watchdog spawn and daemon lifetime are now protected by separate OS `flock` files; cache heartbeat is telemetry only. Spawn failure, child-without-lock, and stale-reload timeout fail deploy through `CommandError`. Thirteen focused tests include a real two-process ensure race. Production SHA `912e6120` transitioned cleanly from the old worker, three concurrent `--ensure` calls all observed the same daemon, and exactly one PID remained with fresh DB/cache heartbeat. Commit/push/deploy.
- [x] **P0.3 — deduplicate takeover alerts on the real MySQL engine.** Migration `0091` converts the complete 12-table IG runtime set from MyISAM to InnoDB and a read-only audit now fails deploy on missing/non-InnoDB tables. Production SHA `13881418` reports 12/12 healthy engines with 58 clients, 195 messages and one deal preserved. Two simultaneous real production processes exercised `_handle_echo()` on one isolated synthetic IGSID with Telegram/log side effects mocked: exactly one takeover transition was emitted; the fixture was then removed. Commit/push/deploy.
- [x] **P0.4 — complete the notification outbox.** Commit `044e9bdf` added autonomous daemon/manual drain, due time, bounded backoff/jitter, atomic claim, stale-`sending` quarantine, timeout ambiguity, dead-letter/resolved states, retry-after handling, and audited operator review. Focused tests cover failure recovery without a new business transition, concurrent claim, missing credentials, `UNKNOWN`, stale sending, retry exhaustion, and confirmed Telegram message IDs. Current production SHA `990be8a5` passed the rollback-only MariaDB verifier with exact DB `qlknpodo_MySQL_DB`, zero `test_*` schemas, `sent/unknown/dead_letter` outcomes, forced mid-fixture rollback, zero residue/AUTO_INCREMENT drift, and `mocked_no_network` transport.
- [x] **P0.5 Make Gemini 3.6 authoritative.** Add `gemini-3.6-flash` to model/key policy, make the settings model effective, and add model-aware generation config/finish-reason handling. Run mocked tests plus the six-key read-only production probe. Commit/push/deploy.
- [x] **P0.6 Make key rotation health-aware.** All six configured keys now participate in role-prioritized fallback (own keys first, then borrowed keys); per-key cooldown remains isolated; Gemini 3.6 model-major ordering and telemetry are covered by 56 focused tests. Production SHA `857ac233` verified all six chat candidates with `state=running`; probe output exposed names/status only, never key values. Commit/push/deploy.
- [x] **P0.7 Fail closed on webhook signature in production.** Missing `IG_APP_SECRET` now rejects POSTs; only explicit `IG_BOT_ALLOW_UNSIGNED_WEBHOOKS=true` enables a development bypass. Status exposes `configured`, `unsigned_override`, `healthy`, and `state`; the missing-secret warning is bounded instead of emitted for every event. Production SHA `11b4f9cf` reports `missing_secret` with override disabled, so unsigned traffic is intentionally blocked until the real Meta secret is configured. Commit/push/deploy.
- [x] **P0.8 Guarantee no duplicate customer send.** Added `send_state`/timestamps and a conditional send boundary: `sending` is persisted before Meta I/O, success becomes `sent`, and timeout/5xx/partial delivery becomes `unknown` with automatic retry disabled. Stale processing rows that crossed the boundary are failed instead of requeued; post-send claim loss cannot replay the request. Migration `0081` is applied on production SHA `3853088a`; focused resilience/audit/e2e tests pass. Commit/push/deploy.
- [x] **P0.9 Fix secret presentation and access boundaries.** Custom Direct/Gemini credentials are write-only password fields with explicit presence indicators; blank saves preserve existing values and explicit clear flags are admin-only. Status JSON exposes no custom values, token-like query parameters are redacted in diagnostics, and 17 privacy/secret tests pass. Production SHA `2974501d` reports no `custom_*` fields in status and daemon `running`. Commit/push/deploy.
- [x] **P0.10 Production recovery verification.** Production SHA `bedca9de7453382b9e0dced759b100b3e1a0f62f` was checked after docs deploy; an active maintenance lease was explicitly discovered and released by its exact owner token, `--ensure` spawned one daemon, and the authenticated overview showed `Працює / Агент онлайн і відповідає`, `gemini-3.6-flash`, and outbox `0`. Webhook capability remains honestly `missing_secret`; no customer/Telegram/Meta transport ran.
- [x] **P0.10a Maintenance release must be fail-visible.**
  - **Priority:** P0 — a swallowed release error can leave the daemon in a deliberate maintenance pause after a deploy appears successful.
  - **Symptom:** the rollback-only verifier cleanup suppressed a failed `--maintenance-off`; the next deploy printed `maintenance active — watchdog skip` and no daemon was running.
  - **Root cause:** the ad-hoc shell trap redirected release errors and did not assert inactive maintenance status before calling `--ensure`.
  - **Risk:** production can remain offline while Git SHA and migrations look correct.
  - **Affected branches:** maintenance-on/off deploy sequence, verifier cleanup, watchdog ensure, offline cockpit state.
  - **Acceptance:** deploys release only the exact captured lease, fail on release error, explicitly assert `maintenance_status.active=false`, then run `--ensure` and verify one fresh daemon; no cleanup path may hide a release failure.
  - **Implementation/evidence:** the deploy runbook now requires exact-token release verification and a post-release inactive assertion; the incident was recovered on production with exact-token `--maintenance-off` followed by `--ensure`, one daemon, fresh UI heartbeat, and no transport side effects.

### Additional findings from the second audit (2026-07-22)

These findings were discovered while tracing the full queue/worker/provider path after the initial plan was written. They are now explicit delivery items rather than informal follow-up notes:

- [x] **P0.A0 Watchdog deploy-reload race.** A fresh heartbeat from a pre-deploy daemon could make an immediate `--ensure` skip the replacement process, and the old process could delete the new heartbeat during exit. Heartbeat now carries a timestamp/sentinel, watchdog compares PID and restart mtimes, and cleanup is owner-guarded. Production SHA `7159ae63` passed `touch restart.txt -> ensure -> sleep 8s` with `state=running` and both heartbeats fresh.
- [x] **P0.A1 Follow-up retry backoff.** Persisted `attempt_count`, `next_attempt_at`, and `last_error` now gate eligibility; transient failures use 5m/10m/20m/40m bounded backoff and then terminal skip, while unknown provider delivery is never retried. Recovery and no-hot-loop tests pass; migration `0082` is applied on production SHA `d0aeb6c2` (runtime reverified on `7159ae63`).
- [x] **P0.A2 Polling cursor/batch correctness.** Added durable `IgPollCursor` (migration `0083`), chronological batch processing, bounded paging, per-conversation cursor gating, webhook-mid dedup through the existing unique constraint, and attachment propagation. Production migration is applied on SHA `9b7610c3`; daemon status remains `running` after deploy.
- [x] **P0.A3 Model allowlist and authority.** Settings accept arbitrary model strings while the UI omits `gemini-3.6-flash`; enforce a provider allowlist, make the selected model effective, and expose configured versus actually used model.
- [x] **P0.A4 Fail-closed webhook verification.** Covered by P0.7: HMAC success/failure, missing-secret rejection, explicit development override, endpoint status, and configuration health tests are in `tests_ig_webhook_security.py`.
- [x] **P0.A5 — durable Telegram notification delivery.** Closed with P0.4 on production SHA `990be8a5`: daemon drain runs independently of reply enablement, restart/concurrent claim and failure isolation are covered, timeout ambiguity is never auto-retried, missing credentials remain retryable operational state, exhausted retries become dead-letter, and the rollback-only MariaDB proof used no real Telegram transport.
- [x] **P1.A6 Gemini 3.6 model-aware generation and health probe.** Context7 confirms `gemini-3.6-flash` as an official model and documents `thinkingConfig.thinkingLevel` for 3.6 plus separate thought/output usage. The current chat payload always sends legacy `thinkingBudget=0`, and the planned six-key probe command is absent. Added model-specific generation normalization (`thinkingLevel=low` for 3.6, compatible settings for older fallbacks), redacted `probe_ig_gemini_pool --role chat --model gemini-3.6-flash --parallel 2`, persisted bounded probe telemetry, and correct `STOP`/`MAX_TOKENS`/`SAFETY`/empty-content classification. Production `10586cd6`: all six keys returned HTTP 200/STOP for `gemini-3.6-flash`; 69 targeted tests, `check`, and migration check passed. A `200 + MAX_TOKENS` probe proves reachability but not a usable answer and does not quarantine the model/key. Commit/push/deploy.

### Additional findings from the Context7/API contract audit (2026-07-23)

Context7 sources used for this pass: official Meta Graph API reference and official Gemini API documentation. Documentation evidence is treated as a contract input, while production behavior and mocked contract tests remain the acceptance authority.

- [x] **P0.A6 Complete conversation discovery instead of monitoring only ten threads.** Meta `/conversations` is paginated, but `refresh_conv_ids()` requests `limit=10` and stores only the first page. This can silently exclude older conversations from polling/analysis when more than ten active threads exist. Implemented bounded pagination (10 pages/500 validated IDs), deduplication, page/ID/Graph-host validation, page-cycle protection, page-scoped cache keys, and cold-cache nonblocking behavior. Production `37b01440` is running; 84 IG regression tests pass.
- [x] **P0.A7 Respect Meta conversation rate limits and partial-page failure semantics.** Context7 documents a 2 requests/second limit per Instagram professional account for Conversations API. Implemented 0.5s page pacing, distributed refresh lock, no partial snapshot publication, stale-cache recovery on 429/5xx/malformed page, and a `refresh_pending` result instead of blocking the daemon. Production `37b01440` is running with fresh heartbeat and zero pending queue/outbox; 84 IG regression tests pass.
- [x] **P0.A8 Verify tagged-send policy against current Instagram Messaging eligibility.** Context7's official Instagram Platform source describes `HUMAN_AGENT` as a human-support response for complex issues up to seven days, while normal API responses remain inside 24 hours; it did not establish automated sales or shipment reminders as eligible. `send_text_tagged()` therefore fails with `policy` before token/provider I/O unless the caller explicitly supplies `human_authored=True`, and only the `HUMAN_AGENT` tag is accepted. Shipment automation now uses ordinary `RESPONSE` only inside a conservative 23-hour window; outside it, or after ambiguous/permanent delivery, it creates one visible skipped manager-task with the prepared TTN text and a retryable deduplicated Telegram alert. It never marks `shipped_notified_at` without confirmed delivery and never auto-retries an unknown result. Commit `1a3d48d1` passed 7 shipment policy tests, 376 IG/Gemini/chat tests, `check`, migration drift, compile, and diff gates. Production SHA `1a3d48d1` rejected an automated tagged smoke with zero token/HTTP calls; no eligible shipment required an action, queue/outbox stayed empty, heartbeats were fresh, and effective model remained `gemini-3.6-flash`. No live customer message was sent.
- [x] **P0.A9 Make Graph version/permission capability explicit.** All Meta requests now use the central `v25.0` builder/transport; unversioned, wrong-version, external-host, fragment, and query-credential URLs fail closed, while legacy `access_token` query input is stripped before the bearer header is sent. Long-lived token exchange keeps app/user credentials in form data rather than the URL. Contract tests cover URL policy, credential removal, exchange-body secrecy, and independent allowlist/token/permission/delivery facts. The dashboard reports those capability axes separately and never equates app roles with Advanced Access. Production SHA `4d025dc3`: `check`, migration drift, compile/static/compress, 17/17 InnoDB audit, payment-truth findings `0`, rollback-only mocked transport proof, one daemon PID, fresh heartbeats, zero queue/outbox/analysis backlog, and canonical `/bot/` route returned the login boundary without a 500. Current production capability remains honestly `token_permission=unknown`, `account_access=unknown`, `webhook_signature=missing_secret` until Meta credentials/access are externally verified.
- [ ] **P1.A7 PARTIAL — audit webhook field coverage against v25.** `_iter_events()` is now shape-safe for malformed/extended `entry`, `messaging`, and `changes` envelopes; only real `message` objects can enter the customer queue, while postback/reaction/control/delete/unsupported/unknown kinds are ignored and bounded counters are stored in `InstagramBotRawEvent.note`. DB-free fixtures cover non-object envelopes, ignored kinds, unknown fields, attachments, referrals, and echoes; production SHA `9ce594c2` passed checks, InnoDB/payment audits, and rollback-only verification. Remaining acceptance work: dedicated webhook fixtures for duplicate `mid`, out-of-order delivery, `messaging_postbacks`/reaction field variants, and end-to-end proof that ignored events never create a reply.
- [ ] **P1.A8 PARTIAL — add provider-level rate and quota observability.** Production SHA `bcab2dae` now records cache-only endpoint classes (`conversations`, `send`, `read`, `oauth`), total/429/transport counters, last rate-limited class/time, and a bounded degraded window in status/UI; the live smoke showed `degraded=false`, `read.requests=6`, all rate/transport counters `0`, and no inferred remaining quota. Existing Conversations pacing and stale-snapshot behavior remain intact. Remaining acceptance work: endpoint-specific bounded 429 backoff (using only provider-proven retry data), audio/video class separation, and mocked response fixtures for each documented limit; no live Meta rate-limit request was sent.
- [ ] **P1.A9 Keep Meta attribution and CAPI claims evidence-bound.** Referral/ad metadata may establish an attribution touch, not a verified purchase. A `Purchase` remains gated by verified payment/order truth, uses a stable dedupe event ID, and live CAPI test events remain disabled without explicit authorization. Add fixtures separating referral, checkout intent, payment promise, payment pending, and provider-verified payment.
- [ ] **P2.A1 Add an API-contract review gate.** Before a Graph/Gemini version bump, run mocked request/response fixtures for endpoints, fields, error codes, finish reasons, rate limits, and permission wording; record the checked documentation date and deployed API/model identifiers. This prevents a constant-only version bump from being mistaken for compatibility.

### Additional findings from the Context7 reasoning/CRM analytics audit (2026-07-23)

These items preserve the complete customer-intelligence, paused-chat analysis, statistics, and operator-UX scope requested on 2026-07-23. They supplement, rather than replace, P1.1-P1.11. Evidence must stay separated from inference: language/tone can be observed, but ethnicity, nationality, personality, or ability to pay must not be asserted without explicit conversation/order evidence.

- [x] **P0.A10 Add task-based Gemini reasoning routing.** Replace the global `low`/legacy `thinkingBudget=0` behavior with the versioned task matrix in §3.4: customer chat is at least `medium`; product/size/media/payment/order/conversion decisions use `high`; probes remain `low`. Convert tasks to a tested Gemini 2.5 fallback budget without ever sending both controls. For Gemini 3.x remove explicit `temperature`/`topP`/`topK` so the provider's reasoning-optimized defaults remain effective; preserve compatible sampling on 2.5 fallbacks. Return and persist bounded task/level/policy/token telemetry, never thought text. Acceptance met: 297 focused IG/Gemini tests, `check`, migration check, compile, and production SHA `bcc0431e`; migration `0085` applied, all six keys returned HTTP 200/STOP for `gemini-3.6-flash`, production chat payload is `medium`, payment payload is `high`, and daemon/queue/outbox are healthy.
- [x] **P0.A11 Make the selected chat model authoritative for pooled keys.** `_run_with_pool()` now passes one validated model chain into both manual and pooled-key paths; non-default allowed primary selection and fallback ordering are covered by regression tests. Production SHA `27c389ac` confirmed `gemini-2.5-flash` becomes the first pooled candidate when selected, with 12 expected key/model candidates, while daemon/heartbeats/queue/outbox remained healthy. The normal configured primary remains `gemini-3.6-flash`.
- [x] **P0.A12 Harden per-conversation message pagination.** `poll_ingest()` now validates cached conversation IDs and every nested message/time/sender/text/attachment/paging field, rejects non-string bounded IDs, allows only centrally versioned `graph.facebook.com/v25.0` page URLs, detects cycles, and publishes messages/cursor movement only after a complete usable page chain. Each provider call is capped at 5 seconds; one poll is capped at 40 requests/20 seconds and persists a round-robin cache offset so older chats cannot starve. Commit `a96302ed` passed 19 polling tests, 374 IG/Gemini/chat tests, `check`, migration drift, compile, and diff gates. Production polling is enabled and running on SHA `a96302ed`; a mocked hostile-host smoke made one v25 request and rejected the next URL without Meta I/O, then live daemon observation showed two validated cached conversations/two cursors, no recent polling safety warnings, fresh heartbeats, empty queue/outbox, and no `last_error`. No live customer test message was sent.
- [x] **P1.A11 Persist versioned customer-intelligence snapshots.** Added idempotent append-only `IgConversationAnalysisSnapshot` records keyed by message/rules watermark with band, bounded heuristic purchase estimate, independent confidence, structured role/message evidence, uncertainties, last analyzed message, model/rules version, latency, and trigger. Manager messages are labeled as manager evidence and no longer inflate customer readiness; payment intent remains unverified rather than paid; legacy `buying_readiness` is visibly labeled as fallback. Production migration `0086` required `db_constraint=False` because both referenced legacy tables are MyISAM; the empty partial table from the first failed DDL was verified and removed, then production SHA `ad4d02fb` passed an InnoDB snapshot insert/read smoke (`exploring`, `0.2800`) with daemon/queue/outbox healthy. Historical backfill and high-reasoning reanalysis remain open under P1.1/P1.A14.
- [ ] **P1.A12 PARTIAL — build the complete interaction taxonomy.** The shipped classifier now adds explicit `collaboration`, `wholesale_b2b`, `support_complaint`, and `community_casual` types with Ukrainian labels, conservative language rules, Gemini prompt/schema acceptance, and migration `0100`; ordinary store-information wording is regression-protected from false B2B classification. Independent opt-out state and emoji-only no-reply behavior remain intact. Production SHA `88813c59` applied the metadata migration and passed focused taxonomy tests plus production checks. Remaining acceptance work: persist/count real reaction-webhook events as first-class observations and complete the planned representative Ukrainian/Russian/sarcasm/custom/support/wholesale fixture matrix before calling the taxonomy complete.
- [x] **P1.A12a Recognize missing-delivery support complaints without false positives.**
  - **Priority:** P1 — missed support complaints can be routed and scored as ordinary informational chats.
  - **Symptom:** `SUPPORT_RE` contains literal `неs+прийш` / `неs+приш`, so normal Ukrainian/Russian phrases such as `замовлення не прийшло` and `посылка не пришла` do not match the intended branch.
  - **Root cause:** `\s+` was mistyped as `s+`, and the rule had no constrained variants for delivered/received order complaints.
  - **Risk:** delivery incidents can receive sales-oriented analysis, contaminate intent statistics, and delay manager support handling; a naive broad stem fix could also misclassify `не пришлю фото`.
  - **Affected branches:** deterministic interaction type, rules snapshots/versioning, high-reasoning prompt context, statistics, filters, and operator routing.
  - **Acceptance:** common RU/UA missing-arrival, non-delivery, and order-not-received phrases classify as `support_complaint`; unrelated future/meeting/payment negatives remain informational; rules version advances.
  - **Tests:** RU/UA arrival, delivered and received variants; `не пришлю фото`, `не прийшла на зустріч`, payment negative controls; rules-version contract.
  - **Implementation/evidence:** constrained object-aware delivery rules are deployed on production SHA `723573f9` with `ANALYSIS_RULES_VERSION=2026-07-24.v4`. Production MariaDB temporary-table fixture passed five support positives and four false-positive controls, confirmed v4 snapshots, zero real-table residue, and zero external transports; migration/check/static/engine/payment/daemon gates remained healthy.
- [ ] **P1.A13 Analyze paused and manager-led conversations without auto-reply.** Customer and manager messages must update deterministic facts and queue analysis even while automation is paused. Manager text is labeled as manager evidence, cancels unsafe follow-ups, and can never prove customer intent or trigger a generated customer reply. Add paused/resumed/takeover race fixtures.
- [ ] **P1.A14 Add idempotent event-triggered and delayed reanalysis.** Run cheap deterministic extraction once per new message, coalesce high-reasoning work by conversation/message watermark, and support configurable hourly/nightly reconciliation for changed chats. Record trigger, due time, lease, attempts, token usage, last analyzed watermark, and skip reason so pause/restarts cannot duplicate cost.
- [ ] **P1.A15 PARTIAL — store structured commercial memory with provenance.** `sales_context` now keeps its legacy flat keys while adding bounded `_provenance` records with value, source message ID/role/time, confidence, conflict flag, and the last four prior values; conflicting manager/customer observations are retained rather than silently erased. Focused DB-free memory tests pass and production SHA `252eaff7` passed migration/check/engine/payment/runtime gates. Remaining acceptance work: first-class product/variant and custom-print brief evidence, desired purchase time/delivery/next-action fields, objection/like/dislike provenance, and catalog-fact expiration/reconciliation.
- [ ] **P1.A16 PARTIAL — replace cumulative readiness inflation with reversible scoring.** Repeated signals are now idempotent per source message, later neutral/deferred evidence can decay or cap readiness, explicit refusal can move it to zero, pure communication opt-out preserves commercial state, and verified provider-ledger payment resolves it to `100`. The scoring checkpoint used policy version `2026-07-24.v3`; the current rules version is `2026-07-24.v4` because the follow-up taxonomy fix is also persisted in snapshots. Focused production-style DB-free tests pass 8/8, and production SHA `c564a9e0` passed a MariaDB temporary-table integration proof for size `28`, deferred `35`, neutral decay `25`, pure opt-out preservation `82`, explicit no-buy `0`, verified payment `100`, duplicate signal/snapshot prevention, zero real-table residue, and zero external transports. Remaining acceptance work: 100+ representative Ukrainian/Russian fixtures covering terse speech, sarcasm, mixed language, reactions, abuse, custom print, stock/size ambiguity, promise-to-pay, verified payment, refund, and repeat purchase, plus concurrency proof and calibration against verified outcomes.
- [ ] **P1.A17 Make payment and revenue truth dominate prediction.** Generated link, promise, screenshot, and `payment_pending` remain intent evidence only. Green/paid state, revenue, conversion, and product attribution require the verified provider/deal/order ledger. Reconcile refunds/cancellations and keep full-payment/prepayment as one purchase with separate paid value.
- [ ] **P1.A18 Exclude hidden clients from every operational aggregate.** Centralize the active-client scope and contract-test all overview, funnel, product, objection, language, campaign, drop-off, follow-up, revenue, and export queries. Hidden records remain available only in an explicitly selected archive view and never silently affect denominators.
- [ ] **P1.A19 Add funnel/drop-off and product-demand analytics.** Date-range reports must show unique conversations, qualified/high-intent/checkout/verified-paid counts, product/variant interest, objection and loss reason, unanswered stage, language, ad/referral touch, follow-up recovery, verified revenue, and sample size/denominator. Popularity means evidenced interest; sales performance means verified orders/revenue.
- [ ] **P1.A20 Add prediction calibration and honest precision.** Compare each saved prediction with later verified paid/lost outcomes; report false-high, false-low, Brier/calibration bands, sample size, drift by prompt/rules version, and manual corrections. Display one decimal place only when the denominator supports it; never imply `0.1%` accuracy from a tiny sample.
- [ ] **P1.A21 Redesign client-list and detail UX around evidence.** Use green only for verified paid/order state, yellow for high intent/payment pending, red for explicit no-buy/opt-out/spam/lost, and neutral styling for exploration/information/reaction-only. Cards show band, probability, confidence, payment truth, top evidence/uncertainty, product, next action, follow-up countdown, last analysis, and whether bot/manager/paused observation owns the chat. Color is supplemented by icons/text and never presented as a psychological certainty.
- [x] **P1.A21a Surface interaction categories in the operator UI.**
  - **Priority:** P1 — a backend-only category does not help the administrator route or audit conversations.
  - **Symptom:** clients API returns raw `interaction_type`, but the client list/detail do not render it, filters cannot isolate complaints/B2B/collaboration/reactions, and statistics have no latest-category breakdown.
  - **Root cause:** taxonomy work stopped at snapshot serialization; the template still renders raw band/intent/objection codes and stage-only filters.
  - **Risk:** support complaints look like ordinary sales chats, manager routing is delayed, and operators cannot verify whether the classifier is behaving correctly.
  - **Affected branches:** client serializer/list/detail, category filters, hidden-client scope, statistics, responsive layout, accessibility, and browser QA.
  - **Acceptance:** show localized category/band/intent/objection labels in list and detail; add complaint/B2B/collaboration/reaction filters; add latest-category statistics with hidden clients excluded; use text plus restrained semantic color; preserve mobile layout.
  - **Tests:** API labels/tone, latest-snapshot filter semantics, hidden exclusion, stats breakdown, template controls, empty category fallback, desktop/mobile render and interaction.
  - **Implementation/evidence:** production SHA `d46eced2` exposes localized category/band/intent/objection labels, latest-snapshot complaint/B2B/collaboration/reaction filters, category badges in the client list/detail, and latest-category statistics with hidden clients excluded. Sixteen DB-free UI tests passed with the runner explicitly skipping both production databases; a rollback-safe MariaDB temporary-table fixture proved latest-snapshot filtering, hidden exclusion, localized support labels, and zero transport calls. Authenticated production browser QA on 2026-07-24 exercised `Клієнти -> Скарги / підтримка`, a populated reaction category/detail, and `Статистика -> Категорії діалогів`; the 390 px viewport had `scrollWidth=clientWidth=390`, and desktop/mobile screenshots showed the category controls without overlap. Browser console entries came only from the Chrome extension, not the application.
- [ ] **P1.A22 Expand statistics UX and drill-down.** Add accessible date controls and filters for stage, product, ad/campaign, objection, language, category, payment truth, and hidden/archive scope; every chart drills into the exact client cohort and states its numerator/denominator. Add empty/loading/error states, responsive tables, keyboard/focus support, and desktop/mobile browser screenshots.
- [ ] **P1.A23 Add operator correction and audit workflow.** Let authorized staff correct product, category, objection, band, next action, and false model inference with a required reason. Store before/after/actor/time, feed corrections into calibration, and never overwrite verified payment/order facts through this UI.
- [ ] **P1.A24 Link attribution without overstating causality.** Preserve referral/ad/campaign first/last/assisted touches and conversation entry context. Reporting may segment outcomes by touch but must label attribution model and cannot claim an ad caused a purchase solely because its referral was present.
- [ ] **P1.A25 Two-step manual payment review and editable order handoff.** **Symptom:** a client or manager can state “оплатив/оплатила” or attach a receipt, but the CRM has no auditable action to distinguish that claim from provider payment truth or to prepare the correct two-shirt order. **Root cause:** chat evidence and Monobank ledger are separate facts, while the existing manual order form has no reviewed IG draft input. **Risk:** accidental paid state/order, duplicate order, wrong size/color/price or delivery data, and no operator accountability. **Affected branches:** paused/manager-led chats, payment-pending/high-intent analysis, manual order creation, Telegram order notification, payment/reversal reconciliation. **Acceptance:** hidden clients never create reviews; evidence creates one durable in-app review with recent role-labeled messages, deal/items/delivery and `Підтвердити`/`Скасувати`; confirmation is CSRF/permission/audit protected and does not mutate provider projection; confirmed review opens the existing editable order form with product/variant/size/qty/price/name/phone/Nova Poshta prefilled; final submit requires the still-valid confirmation, links the deal idempotently, and uses the existing order notification; cancellation/duplicate/race are safe. **Tests:** pure evidence/status rules; MariaDB rollback fixtures for duplicate confirmation, cancel, hidden client, stale review, concurrent submit, order idempotency, and zero external transports. **Priority:** P1, immediately after payment-truth contract and before broad CRM automation.
- [ ] **P2.A2 Add token/cost and latency controls for background analysis.** Coalesce unchanged chats, hash prompt+watermark, cap retries, prioritize high-intent/payment ambiguity, skip explicit opt-out/spam/hidden records, and expose per-task/model token counts and latency. Never reduce normal customer chat below `medium` to save cost.
- [ ] **P2.A3 Add privacy, retention, and export governance for intelligence data.** Define retention for raw message excerpts, evidence, media metadata, model outputs, and audit corrections; redact sensitive values; support deletion/anonymization without corrupting aggregate reports; document who can view/export customer profiles.
- [ ] **P2.A4 Add outcome-driven experimentation controls.** Version follow-up/playbook/scoring policies, assign only eligible cohorts, preserve holdouts, measure verified conversion and complaints/opt-outs, and stop harmful variants. Do not optimize on reply rate alone.
- [ ] **P2.A5 Add data-quality monitoring and repair.** Detect stale analysis watermarks, impossible state combinations, paid-without-ledger rows, conflicting product facts, orphan snapshots, missing denominators, and hidden-client leakage. Provide read-only diagnostics first and idempotent repair commands with dry-run.

### Additional findings from the full local + production truth audit (2026-07-23)

The owner requested a complete re-audit before further implementation. Three independent read-only passes covered CRM/payment/order truth, Meta/Gemini/runtime boundaries, and UX/statistics. Production was then inspected through SSH without customer sends, Gemini generation probes, CAPI events, database writes, or deploy changes.

Context7 was not available in this execution environment, so API conclusions were
checked directly against primary current sources: Meta's official Instagram API
collection for [messaging access and test-role requirements](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-4b9737aa-d320-498a-a092-7225a0a785b7)
and Google's official [Gemini thinking contract](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
and [rate-limit contract](https://ai.google.dev/gemini-api/docs/rate-limits). Documentation
is contract input; mocked tests and production evidence remain acceptance authority.

Production evidence at audit time:

- local, `origin/main`, and production SHA: `fa0b6a9b42aa64211f5cc6f7301a0c7d0fbb6443`;
- daemon running; DB/cache heartbeat about five seconds old; queue and notification outbox empty;
- migrations through `0087` applied; `manage.py check` clean;
- webhook secret absent, unsigned override disabled, webhook state `missing_secret`;
- direct/page tokens present and `v25.0/me/permissions` returned `instagram_manage_messages=granted`; Advanced Access and public-recipient delivery remain unproven separate facts;
- cache backend `FileBasedCache`;
- `management_igclient`, `management_instagrambotmessage`, `management_igfollowuptask`, and `management_igdeal` are MyISAM; notification and analysis snapshot tables are InnoDB;
- 58 clients, 37 hidden, 0 analysis snapshots, 0 follow-up tasks, 0 Meta event rows;
- one active paused/takeover conversation has explicit order intent, delivery data, full-prepayment wording, and manager order acknowledgement, but remains `new`, has no snapshot, and has no exact phone-linked order candidate;
- all 375 current local Instagram/Gemini/bot tests pass, proving that the defects below require new contracts rather than being covered by the legacy suite.

The approved architecture is documented in `docs/plans/2026-07-23-management-instagram-crm-truth-design.md`.

#### P0.B — newly proven production-safety defects

- [ ] **P0.B1 Make payment and fulfillment truth ledger-only.**
  - **Symptom:** model output `[STAGE:paid]`, `[STAGE:order_created]`, or `[STAGE:done]` can make the CRM green/paid, stop follow-ups, force snapshot probability to 1, and increase paid/ad conversion without a payment.
  - **Root cause:** the prompt allows hard stages, `_apply_stage()` accepts every `IgClient.Stage`, and stats/follow-ups/snapshots trust the mutable client stage.
  - **Risk:** false revenue, wrong customer treatment, lost recovery work, invalid CAPI attribution, and an auditable claim of payment without provider evidence.
  - **Affected branches:** chat control tags, classifier, stage history, follow-ups, client list/detail, funnel/stats, ad reports, order collection.
  - **Acceptance:** AI can change only soft intent/routing state; verified payment is derived from provider/deal/order ledger; fulfillment is derived from linked order/TTN; hard truth cannot be cleared by later model replies; a dry-run inconsistency report lists forged/legacy paid rows before repair.
  - **Tests:** forged `paid/order_created/done` tags, payment promise/screenshot/link, verified full/prepayment, webhook+poll duplicate, refund/reversal/cancel, post-paid chat, fake legacy paid in every aggregate.
  - [x] **P0.B1a — authority boundary:** model hard-stage tags are rejected; CRM paid view/card, score band, follow-up stop, paid/ad aggregates, and order creation use one confirmed-deal predicate (`paid|prepaid`, `paid_at`, paid/order-created ledger status). Existing prompt rows are updated by reversible migration `0088`; focused and full related suites cover forged stage, stale soft stage, repeated unpaid attempt, and unverified order creation.
  - [x] **P0.B1b — reconciliation/repair audit:** added PII-free, strictly read-only `audit_ig_payment_truth` with full counts and bounded ID samples for forged client stages, incomplete payment evidence, split truth fields, orders without verified payment, and order-created rows without orders. Production SHA `96b1b370` returned zero findings in all five categories; no repair was run. One daemon PID remained healthy, both heartbeat ages were under 4 seconds, queue/outbox were zero, and the effective model remained `gemini-3.6-flash`.
  - [ ] **P0.B1c — reversals:** add append-only refund/reversal/cancellation truth and prove that every paid aggregate, lifecycle summary, follow-up policy, and CAPI path reverses safely without rewriting historical evidence.
    - [x] Core payment/order ledger: append-only provider events, InnoDB locked projection, amount validation, partial/full refund and reversal truth, order/shipment fail-closed reconciliation, net-paid client/ad aggregates, dirty-marker recovery for MyISAM mirrors, and audit-only Meta refund records are deployed at production SHA `fc727a35`. Fresh migration and 405 related tests pass; production has both append-only triggers, zero dirty projections and zero reconciliation findings.
    - [ ] External Meta Purchase correction/refund delivery remains blocked on an explicit reviewed CAPI policy and owner permission; no live or test Meta event was sent.

- [x] **P0.B2 Make daemon singleton correctness independent of Redis/cache backend.**
  - **Symptom:** two concurrent cron/manual `--ensure` invocations can both spawn a worker on production.
  - **Root cause:** production uses `FileBasedCache`; watchdog and daemon singleton depend on non-atomic cross-process `cache.add()`.
  - **Risk:** duplicate sends/follow-ups/alerts, competing leases, Gemini quota pressure, and DB load.
  - **Affected branches:** minute watchdog, deploy restart, manual ensure, stale heartbeat recovery, Passenger-triggered paths.
  - **Acceptance:** OS `flock` or atomic DB lease owns the daemon; PID/start-time/SHA are verified; stale ownership recovers; cache eviction/outage cannot produce two workers.
  - **Tests:** real multiprocess FileBasedCache race, two concurrent ensures, stale PID, deploy sentinel race, killed owner, cache deletion, exactly one live PID.
  - **Production proof:** SHA `912e6120`; PID `939730` was the only `--forever` process after three simultaneous production ensures, all three returned `daemon alive — ok`, heartbeat ages were 2.5 seconds, queue/outbox were zero, and effective model was `gemini-3.6-flash`.

- [x] **P0.B3 Remove MyISAM-invalid concurrency assumptions from critical CRM paths.**
  - **Symptom:** concurrent manager echoes, hide-vs-send, webhook-vs-poll, or two client leases can both cross a supposed `select_for_update()` boundary.
  - **Root cause:** production critical tables are MyISAM, which does not provide transactional row locks, while the code and SQLite tests assume them.
  - **Risk:** duplicate takeover alerts, reply after confirmed hide/pause, parallel sends, lost state transitions, and inconsistent deal/order linkage.
  - **Affected branches:** `_handle_echo`, enqueue/client creation, automation lease, hide/resume, follow-up claim, deal reconciliation.
  - **Acceptance:** migrate required tables to InnoDB with measured DDL/rollback safety or replace locks with atomic conditional updates/unique epochs that work on the actual engine; deploy health fails when required engines differ.
  - **Tests:** MySQL concurrency fixtures for two echoes, hide-vs-send, two leases, webhook-vs-poll, deal payment race, migration/engine assertion.
  - **Production proof:** migration `0091` applied at SHA `13881418`; all 12 audited IG runtime tables are InnoDB, payment event/projection tables remain InnoDB, row counts were preserved, payment audit stayed at zero findings, and the isolated two-process takeover fixture produced one transition only with no Telegram/Meta/customer send.

- [x] **P0.B4 Add autonomous notification outbox drain and recovery.**
  - **Symptom:** one Telegram timeout/500 leaves a critical alert failed forever unless the same business transition calls `notify_manager()` again.
  - **Root cause:** the request path creates and sends outbox rows synchronously; daemon/status never claims due failed rows.
  - **Risk:** silent loss of takeover, payment, AI outage, shipment review, delivery block, and spam escalation alerts.
  - **Affected branches:** every `IgBotNotification` event producer, daemon loop, deploy/restart, cockpit health.
  - **Acceptance:** autonomous due-row drain, `next_attempt_at`, bounded exponential backoff+jitter, stale-sending recovery, timeout ambiguity policy, confirmed-message idempotency, dead-letter state and Ukrainian operator action.
  - **Tests:** one-shot failure then recovery without new event, restart, concurrent drains, missing credentials, stale sending, ambiguous timeout, confirmed Telegram message ID.
  - **Production proof:** commit `044e9bdf` and migrations `0092`-`0094` are deployed; the daemon calls a bounded drain before reply work even when replies are disabled, isolates outbox exceptions from customer processing, and exposes manual review. On production SHA `990be8a5`, `verify_ig_production_contract --rollback-fixtures` proved exact MariaDB identity, zero test schemas, mocked sent/unknown/dead-letter transitions, forced rollback cleanup, and no real provider call.

- [x] **P0.B4a Make notification claiming valid on the production DB engine.**
  - **Symptom:** two notification workers can both cross the apparent `select_for_update()` boundary and call Telegram for one pending row.
  - **Root cause:** migration `0091` and the engine audit omit `management_igbotnotification`; on a legacy MyISAM installation its row locks are ineffective.
  - **Risk:** duplicate takeover, payment, shipment, delivery-block, and AI-outage alerts.
  - **Affected branches:** synchronous `notify_manager`, daemon drain, manual drain command, deploy/restart overlap.
  - **Acceptance:** the notification table is explicitly InnoDB, deploy health fails on any other engine, and one database claim owns each attempt before provider I/O.
  - **Tests:** idempotent engine migration/audit plus a real two-process claim fixture with mocked provider I/O and exactly one claimed send.
  - **Production proof:** SHA `0225caf2`; all 14 required tables are InnoDB. Two separate production MariaDB processes produced one mocked provider marker, one attempt and one stored Telegram message ID; the fixture/audit were removed and no real provider request ran.

- [x] **P0.B4b Isolate outbox failure from customer message processing.**
  - **Symptom:** an outbox schema/database/bad-row exception aborts `_run_work_cycle()` before the customer queue and follow-ups run.
  - **Root cause:** notification drain and customer work share one unguarded call chain.
  - **Risk:** a broken manager-alert subsystem silently stops all automated customer replies.
  - **Affected branches:** daemon cycle while enabled, migration rollout, corrupt outbox payload, transient DB failure.
  - **Acceptance:** drain failure is logged and surfaced as degraded health but customer queue/follow-up processing continues; reply failures cannot suppress later outbox cycles either.
  - **Tests:** `drain raises -> process_pending/follow-ups still run`, disabled reply gate, and subsequent-cycle recovery.
  - **Production proof:** deployed at SHA `044e9bdf`; daemon drain is isolated from reply work and fresh production runtime at SHA `0225caf2` has one healthy worker with empty queue/outbox.

- [x] **P1.B4c Do not render unavailable outbox telemetry as zero.**
  - **Symptom:** backend `None` values become `0` through JavaScript fallback and the cockpit claims an empty healthy outbox.
  - **Root cause:** UI conflates unavailable data with a measured zero.
  - **Risk:** operators miss migration failures and database outages.
  - **Affected branches:** status API error fallback and overview rendering.
  - **Acceptance:** unavailable counts render as `Дані недоступні` with a textual warning independent of colour; measured zero remains distinct.
  - **Tests:** null telemetry render contract and normal zero/non-zero payloads.
  - **Implementation/evidence:** the overview computes an all-fields-present availability guard before rendering counts; `NULL` renders `Дані недоступні` plus a Ukrainian migration/database warning, while measured zero remains `0`. The DB-free template contract passed on production settings with the runner skipping the unused MariaDB database; production browser QA at SHA `6a7c6f6f` showed the measured healthy outbox value `0`, distinct from the unavailable branch.

- [ ] **P1.B4d Add an actionable Ukrainian notification-review queue.**
  - **Symptom:** the overview shows only a total manual-review count; an operator cannot identify or resolve an `unknown/dead_letter` row.
  - **Root cause:** no safe detail endpoint/list or audited resolution action exists.
  - **Risk:** critical alerts remain unresolved or are blindly resent and duplicated.
  - **Affected branches:** UNKNOWN ambiguity, retry exhaustion, permanent provider rejection, admin cockpit.
  - **Acceptance:** authorized operators can see event/client/time/attempts/sanitized error and payload preview, mark a row resolved after checking Telegram, or deliberately requeue it with an audit trail; no automatic retry of UNKNOWN.
  - **Tests:** authorization, redaction, list ordering, resolve/requeue transitions, CSRF, duplicate-safe requeue, mobile/desktop rendering.

- [x] **P1.B4e Honour Telegram rate-limit retry timing.**
  - **Symptom:** HTTP 429 uses the generic short backoff and can exhaust all attempts before Telegram's requested wait expires.
  - **Root cause:** `parameters.retry_after` is parsed but ignored.
  - **Risk:** recoverable alerts enter dead-letter unnecessarily and create alert gaps.
  - **Affected branches:** provider 429 response, retry scheduling, restart drain.
  - **Acceptance:** validated bounded `retry_after` is the minimum next-attempt delay, malformed/extreme values fall back safely, and jitter never schedules earlier than the provider delay.
  - **Tests:** valid, missing, string, negative, and extreme retry-after fixtures plus attempt-budget preservation.
  - **Production proof:** deployed parser/backoff contract at SHA `044e9bdf`; production MariaDB rollback fixtures at SHA `0225caf2` prove retry/dead-letter state without any real Telegram request.

- [x] **P0.B4f Keep outbox schema creation recoverable from partial MySQL DDL.**
  - **Symptom:** a failed migration can leave the audit table created while Django still considers the migration unapplied; retry then fails on `table already exists`.
  - **Root cause:** one `atomic=False` migration mixes `CreateModel`/`AlterField` with later engine-changing DDL that commits implicitly.
  - **Risk:** production deploy cannot safely resume after a metadata lock, timeout, or engine conversion failure.
  - **Affected branches:** migrations `0093+`, deploy retry, rollback/recovery, engine health gate.
  - **Acceptance:** transactional schema state and non-atomic idempotent engine conversion are separate migrations; the engine step checks table existence/current engine before each DDL.
  - **Tests:** migration structure assertion, idempotent conversion fixture, partial-state rerun, then production migration table/engine verification.
  - **Production proof:** migrations `0092`-`0094` are applied; schema state and non-atomic idempotent engine conversion are separate, and the production engine audit reports 14/14 InnoDB.

- [x] **P1.B4h Remove server AppleDouble files from Python compile scope.**
  - **Symptom:** production `compileall management orders` fails with `source code string cannot contain null bytes` for `orders/._nova_poshta_documents.py` and `orders/._telegram_notifications.py`.
  - **Root cause:** ignored 163-byte AppleDouble resource-fork metadata was copied to production beside real `.py` modules; `compileall` treats the `._*.py` names as Python source.
  - **Risk:** a required deploy gate stays red and can hide a genuine syntax error among known noise; future recursive tooling may attempt to parse metadata as code.
  - **Affected branches:** production compile gate, recursive scanners, manual file uploads from macOS.
  - **Acceptance:** verify the files are untracked AppleDouble metadata, move only the proven artifacts to a recoverable quarantine, keep `._*` ignored, and make production compileall pass without excluding real tracked Python files.
  - **Tests:** `file`, `git check-ignore`, `git ls-files`, clean production `compileall`, and no tracked-tree change from cleanup.
  - **Production proof:** exactly two ignored AppleDouble files were moved to recoverable `tmp/appledouble_quarantine_20260723/`; production `compileall management orders` passes at SHA `0225caf2` and the tracked tree is clean.

- [x] **P0.B4i Add an explicit bounded daemon maintenance lease.**
  - **Symptom:** after `restart.txt` stopped the daemon for a production outbox fixture, the minute cron immediately ran `--ensure`, restarted the daemon, claimed the fixture, and sent one synthetic administrator Telegram notification.
  - **Root cause:** the restart sentinel only asks the current daemon to reload; it is not a durable maintenance contract and `--ensure` has no state that distinguishes an intentional pause from a crash.
  - **Risk:** a migration, rollback fixture, or pending operational alert can be processed during a maintenance window; deploy verification is no longer no-network by construction.
  - **Affected branches:** cron watchdog, manual `--ensure`, deploy restart, daemon outbox drain, production rollback fixtures.
  - **Acceptance:** an explicit atomic maintenance lease prevents both watchdog spawn and daemon work, is visible in runtime status, has a bounded expiry so a forgotten lease recovers, and the production deploy process enters/exits it deliberately before `restart/ensure`.
  - **Tests:** ensure during active maintenance, running daemon observes maintenance and exits before work, stale lease recovery, malformed lease fail-safe, concurrent maintenance activation, and one production no-send fixture while cron continues.
  - **Production proof:** SHA `0225caf2` used the one-time old-daemon bootstrap under spawn/daemon OS locks. Active maintenance blocked watchdog, `--forever`, manual drain, a second owner and a wrong release token. A production MariaDB rollback fixture remained pending with zero claims/HTTP calls; after exact-token release, one daemon returned with both heartbeat ages 4.3 seconds, queue/outbox zero and no lease/fixture residue.

- [x] **P0.B4j Make every production-DB verification command fail closed.**
  - **Symptom:** production lacked `rg`; an empty discovery pipeline still invoked Django and launched the complete 2,803-test SQLite suite. An earlier ambiguous inline settings invocation also attempted to create a MySQL test database before failing permissions.
  - **Root cause:** the shell pipeline did not require a non-empty explicit module list, and the acceptance path still allowed `test_settings`/SQLite to be mistaken for production evidence.
  - **Risk:** runaway processes, checks against the wrong database contract, misleading green evidence, and accidental test-schema creation attempts on production infrastructure.
  - **Affected branches:** focused/full suite discovery, migration verification, concurrency fixtures, deploy handoff and P0.10 closure.
  - **Acceptance:** the production-contract verifier asserts MySQL/MariaDB, the exact configured production database identity, no `test_*` schema and explicit no-network fixtures; missing tools, empty discovery and wrong settings stop before Django tests or database mutation. SQLite may remain a fast developer aid but can never satisfy a checklist acceptance gate.
  - **Tests:** missing `rg`, empty module list, SQLite/wrong settings, `test_*` DB name, expected production identity, rollback-only MariaDB fixture, and orphan-process cleanup.
  - **Production proof:** SHA `b58cd2a6`; the dedicated verifier rejected a wrong DB name and rollback fixtures without maintenance, while the six DB-free guard tests rejected SQLite, `test_*`, configured/selected DB mismatches and visible test schemas. Pre-migrate identity and post-migrate production MariaDB rollback fixtures passed with `mocked_no_network`, forced mid-fixture exception cleanup, unchanged `AUTO_INCREMENT`, zero test schemas and zero leaked rows. No Django test database was created.

- [ ] **P0.B5 Separate observation/analysis from global and per-client reply enablement.**
  - **Symptom:** when global `is_enabled=False`, customer webhook events return 200 but are discarded before message/client storage; paused manager-led chats also finish without scheduled high-reasoning analysis.
  - **Root cause:** one enable flag gates ingress, analysis, reply, and follow-up instead of reply automation only.
  - **Risk:** permanent CRM blind spots and inability to recover history after access is granted or the bot is resumed.
  - **Affected branches:** webhook ingress, polling, queue, deterministic extraction, manager echo, snapshots, follow-ups, resume behavior.
  - **Acceptance:** global stop, client pause, and takeover store every eligible customer/manager event exactly once, run deterministic extraction, and coalesce analysis; they generate zero typing/seen/customer Gemini chat/Send API/follow-up; resume does not reply to old backlog.
  - **Tests:** global off, paused client, takeover, stop/resume race, webhook+poll duplicate, manager message provenance, hidden/opt-out skip policy.
  - [x] **P0.B5a — observation/reply boundary:** webhook and fallback polling persist eligible inbound while global/per-client reply is paused, run deterministic evidence extraction, mark the row observed without a reply backlog, suppress typing/Gemini/Meta/follow-up, and atomically drain pre-stop pending rows to observed state.
    - **Production proof:** SHA `3d71a6f2`; production MariaDB rollback fixtures covered global stop, per-client pause, delayed-provider cutoff, stop/resume, manager takeover, and a due follow-up with every network/classifier/log transport mocked. Observed rows ended `done`, no stopped backlog became replyable after resume, the eligible follow-up sent exactly once through the mock, fixture rows were removed, and `AUTO_INCREMENT` values stayed unchanged. The production contract selected only `qlknpodo_MySQL_DB` with zero visible `test_*` schemas. After maintenance release exactly one daemon PID was running; DB/cache heartbeat ages were about 0.1 seconds, queue/outbox were zero, maintenance was absent, and the effective model was `gemini-3.6-flash`. The six DB-free lock/heartbeat tests explicitly skipped both configured databases; no local SQLite result is used as production evidence.
  - [ ] **P0.B5b — coalesced high-reasoning analysis:** add a durable per-client watermark/debounce job that analyzes the whole changed conversation after the manager/customer burst, stores model/version/reasoning/confidence/evidence/analyzed_at, retries safely across six-key project-aware pools, and never grants reply permission.
  - [ ] **P0.B5d — include every analysis table in the production engine contract.**
    - **Priority:** P0 — durable leases and coalescing rely on transactional row ownership.
    - **Symptom:** the green `audit_ig_table_engines` result covers 14 runtime tables but omits the existing `management_igconversationanalysissnapshot` table; a new analysis-job table would be omitted as well unless the contract changes.
    - **Root cause:** `IG_RUNTIME_TABLES` was assembled before analysis persistence became a daemon-owned critical path and was not extended with migration `0086`.
    - **Risk:** MyISAM/default-engine drift can silently invalidate snapshot/job transaction assumptions while deploy health still reports every required table healthy.
    - **Affected branches:** deterministic snapshots, high-reasoning job claim/retry, concurrent webhook/poll scheduling, deploy verification, and production recovery.
    - **Acceptance:** both snapshot and job tables are explicitly InnoDB, the authoritative engine audit requires them, and missing/wrong-engine state fails closed.
    - **Tests:** required-table contract, missing table, wrong engine, idempotent conversion, and a production MariaDB two-claimer rollback fixture.
  - [ ] **P0.B5e Make opt-out a durable communication hard stop.**
    - **Priority:** P0 — consent and customer safety override conversion automation.
    - **Symptom:** an explicit `не пишіть/стоп` marks only that message as no-reply and cancels current follow-ups; a later neutral message can become reply-eligible because `_client_blocked()` has no durable opt-out truth.
    - **Root cause:** opt-out exists only as one analysis-snapshot interaction type, not as an independently persisted client communication state.
    - **Risk:** unwanted automated replies after explicit withdrawal, complaints, platform-policy exposure, and invalid funnel/follow-up behavior.
    - **Affected branches:** webhook/poll ingress, resume, follow-ups, high-reasoning analysis, manual client controls, and CRM lifecycle display.
    - **Acceptance:** explicit opt-out stores timestamp/source evidence and makes every automatic customer send/follow-up/high-analysis ineligible until an authorized, audited manual opt-in; payment/order history is preserved and opt-out remains distinct from lost.
    - **Tests:** opt-out then neutral message, paid customer opt-out, global stop/resume, manager message, duplicate webhook/poll, follow-up due, hidden client, and authorized opt-in audit.
  - [ ] **P0.B5f Configure and verify Gemini key aliases by Google Cloud project.**
    - **Priority:** P0 — analysis retry correctness and quota pressure depend on the provider's project boundary.
    - **Symptom:** six configured key aliases are cooled independently even though Gemini quotas are project-scoped; the repository has no non-secret evidence showing which aliases share a project.
    - **Root cause:** key rotation stores alias-level state but has no authoritative `alias -> project group` configuration.
    - **Risk:** repeated 429 calls against the same exhausted project, misleading key-health UI, delayed analysis, and unnecessary quota pressure.
    - **Affected branches:** chat, management analysis, checker, health probes, retry ordering, and effective capacity reporting.
    - **Acceptance:** configure the non-secret project group for all six aliases, propagate project-scoped 429 cooldown to every alias in that group, show unknown/incomplete mapping explicitly, and verify behavior with mocked provider responses; never store API keys in the mapping or logs.
    - **Tests:** two aliases in one project, aliases in separate projects, missing/invalid mapping, project 429/recovery, pool status, and six-key retry order.
    - **External input required:** an owner must confirm only the project grouping of `GEMINI_API` through `GEMINI_API6`; the implementation accepts `GEMINI_KEY_PROJECT_GROUPS` without exposing key values.
  - [ ] **P0.B5g Reject analysis finalization by an expired or superseded lease owner.**
    - **Priority:** P0 — a stale worker must never publish CRM truth after another worker has reclaimed the durable job.
    - **Symptom:** the post-Gemini transaction locks the client and payment rows, but creates the snapshot before proving that the same processing job still has the claimed lease token, unexpired lease, and claimed revision.
    - **Root cause:** ownership is checked only by a conditional telemetry update after snapshot creation; a zero-row update cannot undo the already-created snapshot.
    - **Risk:** two workers can publish conflicting analysis for one revision, stale payment/policy evidence can become operator-visible, and a previous worker can interfere with a reclaimed job.
    - **Affected branches:** stale-lease reclaim, slow Gemini completion, payment/opt-out races, snapshot dedupe, retry accounting, and daemon concurrency.
    - **Acceptance:** finalization follows the established client-to-job lock order, verifies processing status/token/lease/claimed revision before locking projections or writing a snapshot, returns an explicit lease-lost outcome, and never mutates the replacement claim.
    - **Tests:** worker A claim, lease expiry/reclaim, worker B claim, worker A completion without snapshot/job mutation, and worker B-only successful completion.
  - [ ] **P0.B5h Reconcile missed conversation-analysis scheduling automatically in the daemon.**
    - **Priority:** P0 — payment and order truth commits are authoritative even when their best-effort analysis scheduler crashes.
    - **Symptom:** `reconcile_ig_analysis_jobs` can repair stale/missing jobs manually, but the long-running daemon only drains already-scheduled jobs and never invokes reconciliation.
    - **Root cause:** reconciliation was implemented as an operator command without a bounded periodic daemon schedule.
    - **Risk:** a crash between payment commit and scheduling leaves the CRM snapshot stale indefinitely; paused/manager-led chats can also remain unanalyzed until manual intervention.
    - **Affected branches:** payment truth, order truth, manager-led conversations, daemon restart, retry/backoff, reconciliation cursor, and queue health.
    - **Acceptance:** the analysis thread performs a small reconciliation batch soon after startup and every bounded interval while not in maintenance, independently from reply enablement; existing covered pending/processing work keeps its revision and backoff.
    - **Tests:** scheduler failure after payment commit, automatic mismatch recovery, no revision churn for covered pending/processing jobs, maintenance suppression, startup run, interval gating, and cursor traversal beyond one batch.
  - [ ] **P0.B5i Make the payment-during-analysis regression executable.**
    - **Priority:** P0 — payment authority is a release gate for this slice.
    - **Symptom:** `test_payment_confirmed_during_gemini_overrides_model_intent` references `Decimal` without importing it.
    - **Root cause:** the concurrency regression was added after the module imports and was only compile-checked; Python compilation does not resolve runtime names.
    - **Risk:** the suite fails before exercising payment override behavior and can provide no evidence that verified ledger truth wins over stale model output.
    - **Affected branches:** high-reasoning finalization, payment truth race verification, and the production MariaDB rollback fixture gate.
    - **Acceptance:** the test module imports the standard decimal type and the regression reaches its assertions with all network transports mocked.
    - **Tests:** targeted payment-during-Gemini regression plus the complete analysis-job verification set.
  - [ ] **P0.B5j Keep the analysis retry budget terminal for unchanged input.**
    - **Priority:** P0 — periodic repair must not turn a bounded provider retry policy into an infinite quota loop.
    - **Symptom:** reconciliation treats a terminal `FAILED` job as stale and calls `schedule_analysis`; scheduling resets attempts and backoff for every non-processing job, so the same conversation receives another five attempts every reconciliation interval.
    - **Root cause:** the durable job stores watermark/revision but no fingerprint of the payment/prompt/message state whose retry budget was exhausted.
    - **Risk:** unbounded Gemini quota/cost pressure, repeated failures hidden as fresh retries, misleading queue health, and starvation of other conversations.
    - **Affected branches:** max-attempt handling, periodic reconciliation, payment-truth repair, prompt upgrades, six-key cooldown, and daemon queue fairness.
    - **Acceptance:** store a non-secret required-state fingerprint covering message watermark, prompt version, and verified-payment truth; unchanged terminal failures remain failed, while a changed fingerprint schedules exactly one new revision and retry budget.
    - **Tests:** repeated reconciliation after terminal failure, payment change at the same watermark, new message, prompt-version change, and pending/processing coverage without backoff reset.
  - [ ] **P0.B5k Do not publish an obsolete transcript when its claimed revision changes.**
    - **Priority:** P0 — a valid lease token does not authorize a worker to publish analysis for a revision it did not claim.
    - **Symptom:** `_claim_is_current()` accepts `watermark/revision >= claimed`; a new message or truth revision arriving during Gemini therefore lets the old worker publish its older transcript before requeueing.
    - **Root cause:** lease ownership and revision identity are treated as one monotonic condition instead of two independent invariants.
    - **Risk:** append-only history gains knowingly stale snapshots, operators see transiently wrong evidence, and a later failure can leave the stale snapshot as the latest result.
    - **Affected branches:** burst coalescing, payment/order changes during Gemini, snapshot dedupe, stale reclaim, and retry accounting.
    - **Acceptance:** finalization requires exact claimed watermark/revision; a live owner with newer input atomically releases to pending without snapshot, while an expired/reclaimed owner changes nothing.
    - **Tests:** new message during Gemini, payment/order revision during Gemini, exact current claim, expired token, and replacement-token preservation.
  - [ ] **P0.B5l Persist analysis telemetry in each append-only snapshot.**
    - **Priority:** P0 — one mutable job row cannot provide historical evidence for model policy, cost, or calibration.
    - **Symptom:** key alias, reasoning level/policy, thought tokens, and candidate tokens are overwritten on `IgConversationAnalysisJob` but absent from `IgConversationAnalysisSnapshot`.
    - **Root cause:** telemetry fields were added only to the queue-control model, while the approved design requires every material snapshot to retain its own provenance.
    - **Risk:** past decisions cannot be audited or compared after the next run, key/project incidents cannot be reconstructed, and calibration by model/policy is impossible.
    - **Affected branches:** high-reasoning analysis, six-key routing, usage statistics, incident review, and future calibration.
    - **Acceptance:** each AI snapshot stores non-secret key alias, effective reasoning level/policy, thought/candidate token counts, model, prompt version, latency, trigger, and analyzed time.
    - **Tests:** snapshot telemetry persistence, repeated revisions with distinct telemetry, safe empty metadata, and no API-key value storage.
  - [ ] **P0.B5m Include durable order truth in analysis scheduling and reconciliation.**
    - **Priority:** P0 — paid and fulfillment/order state are independent CRM facts.
    - **Symptom:** the current fingerprint/reconciliation checks only whether verified payment exists; order creation, cancellation, shipment, or reversal can leave the same-message analysis snapshot current indefinitely.
    - **Root cause:** payment projection hooks schedule analysis, but authoritative order materialization and later order-state transitions have no equivalent versioned trigger.
    - **Risk:** manager-led paid/order conversations show stale next actions, shipment reminders conflict with real fulfillment, and statistics mix payment intent with order truth.
    - **Affected branches:** order materialization/linking, payment reconciliation, shipment/TTN updates, periodic reconciliation, and CRM lifecycle display.
    - **Acceptance:** the non-secret required-state fingerprint includes authoritative linked deal/order state; order truth transitions schedule or are automatically reconciled into exactly one new revision without granting reply permission.
    - **Tests:** order created/cancelled/shipped at the same message watermark, scheduler failure recovery, no change/no churn, and payment/order axes remaining independent.
  - [ ] **P0.B5n Bound analysis provider time below its durable lease and use fresh clocks.**
    - **Priority:** P0 — a lease shorter than a worst-case provider attempt creates routine false reclaim.
    - **Symptom:** management JSON can spend two 90-second attempts without an overall deadline while the job lease is 150 seconds; one `now` is also reused for later claims and failure backoff in the same drain batch.
    - **Root cause:** conversation reanalysis reused the audio-oriented management timeout and the batch captured time only once.
    - **Risk:** valid workers are reclaimed mid-call, subsequent jobs start with shortened leases, retries become immediately due, and snapshots churn under provider degradation.
    - **Affected branches:** Gemini timeout/backoff, multi-job drain, stale reclaim, failure retry, and daemon throughput.
    - **Acceptance:** management JSON has a bounded deadline/read timeout with margin below the analysis lease; every claim, reclaim and failure uses a fresh clock.
    - **Tests:** provider timeout, two-attempt transient, later claim in one batch, fresh failure backoff, and lease-lost recovery.
  - [ ] **P0.B5o Preserve substantive messages when a burst ends with a reaction.**
    - **Priority:** P0 — reaction suppression must not discard sales evidence from the same debounce window.
    - **Symptom:** `_skip_reason()` inspects only the latest rules snapshot, so a product question followed by an emoji makes the whole coalesced job `reaction_only` and permanently skipped.
    - **Root cause:** skip policy has no analyzed-watermark window and treats the final event as representative of the full changed range.
    - **Risk:** product/size/payment intent disappears from CRM analysis and follow-up decisions while the job is marked current.
    - **Affected branches:** deterministic taxonomy, debounce, paused observation, reconciliation, and high-reasoning scheduling.
    - **Acceptance:** skip Gemini only when every newly changed message since the analyzed watermark is reaction-only; any substantive changed message keeps the coalesced analysis eligible.
    - **Tests:** reaction only, substantive then reaction, reaction then substantive, already-analyzed substantive plus new reaction, and duplicate delivery.
  - [ ] **P0.B5p Make Gemini 3.6 Flash primary for management conversation analysis.**
    - **Priority:** P0 — the configured product model must be the model actually attempted for high-reasoning CRM work.
    - **Symptom:** `role="management"` uses a chain beginning at Gemini 3.5 and excludes Gemini 3.6, although the effective bot model and requirements specify 3.6.
    - **Root cause:** only the chat role chain was upgraded.
    - **Risk:** CRM decisions silently use an older model and UI telemetry misleads operators about effective capability.
    - **Affected branches:** conversation reanalysis, order/product decisions, manager summaries, model fallback, and health reporting.
    - **Acceptance:** management attempts Gemini 3.6 Flash first across its key pool, retains bounded free-model fallbacks, and persists the actually used model.
    - **Tests:** management chain order, configured override validation, fallback, telemetry, and no unsupported model.
  - [ ] **P0.B5q Make project-scoped Gemini cooldown atomic and monotonic.**
    - **Priority:** P0 — provider quota is project-scoped, not alias-scoped.
    - **Symptom:** project 429 updates aliases in separate autocommit writes, and an in-flight success can clear one alias after a sibling recorded the project cooldown.
    - **Root cause:** project identity was added to selection/status, but durable health remains six independent mutable rows without a transactional group invariant.
    - **Risk:** concurrent workers continue calling the exhausted project, quota pressure grows, and UI reports contradictory alias availability.
    - **Affected branches:** chat, management, checker, probes, six-key fallback, and pool status.
    - **Acceptance:** known-group 429 locks and updates all aliases atomically; success cannot shorten an active sibling project cooldown; unknown aliases remain independent.
    - **Tests:** concurrent 429/success ordering, atomic group visibility, expiry recovery, separate projects, unknown mapping, and rollback.
  - [ ] **P0.B5r Use one evidence source-role vocabulary for rules and AI snapshots.**
    - **Priority:** P0 — provenance must be machine-comparable across analysis engines.
    - **Symptom:** deterministic snapshots store `user`, while the high-analysis transcript normalizer emits `customer`; the focused regression expects the established `user` value.
    - **Root cause:** presentation labels were reused as persisted role identifiers.
    - **Risk:** evidence filters, audits, manager-vs-customer safeguards, and tests disagree about who made a claim.
    - **Affected branches:** evidence normalization, historical snapshots, UX drill-down, and calibration exports.
    - **Acceptance:** persisted roles use the message enum vocabulary `user|model|manager|system`; prompt presentation may use localized labels without changing stored provenance.
    - **Tests:** user/model/manager evidence, invalid message ID, false quote, and mixed transcript.
  - [ ] **P0.B5s Preserve historical snapshot timestamps during schema upgrade.**
    - **Priority:** P0 — migration time is not analysis time.
    - **Symptom:** adding non-null `analyzed_at=timezone.now` rewrites every old snapshot to the deploy timestamp, and the UI then presents that value as analysis recency.
    - **Root cause:** the migration default did not backfill from immutable `created_at` before enforcing the final field contract.
    - **Risk:** historical records appear freshly analyzed and operators trust stale CRM conclusions.
    - **Affected branches:** migration 0095, cockpit recency, reconciliation freshness, and audit history.
    - **Acceptance:** existing rows copy `created_at`; new rows receive real analysis time; migration remains deterministic and idempotent.
    - **Tests:** old snapshot preservation, new snapshot default, null-free final schema, and migration replay on MariaDB.
  - [ ] **P0.B5t Reconcile skipped analysis against the current required-state fingerprint.**
    - **Priority:** P0 — automatic repair must recover a missed payment/order hook even when the previous conversation revision was intentionally skipped.
    - **Symptom:** reconciliation treats every watermark-current `SKIPPED` job as current without comparing its stored fingerprint to current payment/order/prompt truth.
    - **Root cause:** the skipped fast path checks only analyzed watermark/revision, while completed AI snapshots also check `required_state_fingerprint`.
    - **Risk:** a reaction-only conversation can remain permanently stale after verified payment or order creation if the best-effort scheduler crashes between the authoritative commit and job scheduling.
    - **Affected branches:** reaction-only skip, payment/order commit recovery, daemon reconciliation, terminal job coverage, and CRM payment display.
    - **Acceptance:** a skipped job is current only when its required-state fingerprint still matches; a truth change queues exactly one revision, while unchanged skipped work creates no churn.
    - **Tests:** reaction-only skip followed by a missed verified-payment hook, unchanged repeated reconciliation, order change at the same watermark, and preserved pending/processing backoff.
  - [ ] **P0.B5u Require an unexpired lease on every analysis completion path.**
    - **Priority:** P0 — skip and provider-failure paths are durable job finalization just like snapshot publication.
    - **Symptom:** `_finish_skip()` and `_finish_failure()` select by processing status/token but do not reject an expired lease; before reclaim, an old owner can still mark the job skipped/failed or release a newer revision.
    - **Root cause:** exact lease validation was added only to the post-Gemini snapshot transaction.
    - **Risk:** an expired worker can mutate retry accounting or analyzed watermarks, hide due work, and interfere with the next owner even though stale ownership is no longer valid.
    - **Affected branches:** hidden/opt-out/reaction skip, empty transcript, provider error, stale reclaim, newer revision during processing, and queue telemetry.
    - **Acceptance:** every skip/failure completion locks the job, validates processing status, exact token and an unexpired lease with a fresh clock; expired/reclaimed owners change nothing, while a still-owned superseded revision is released to pending without publishing analyzed state.
    - **Tests:** expired skip owner, expired failure owner, replacement token, newer revision under the same live token, and exact current skip/failure.
  - [ ] **P0.B5v Make the historical-timestamp migration test field-specific.**
    - **Priority:** P0 — this test is the release evidence that deploy does not rewrite historical analysis recency.
    - **Symptom:** the test finds the first `AddField` after a numeric list offset, which can be `IgClient.opted_out_at` instead of snapshot `analyzed_at`, yet still passes the ordering assertion.
    - **Root cause:** migration operations are matched only by class name and position, not by model/field identity or the backfill callable.
    - **Risk:** a later migration edit can regress historical timestamps while the focused contract test remains falsely green.
    - **Affected branches:** migration 0095, cockpit analysis recency, migration review, and production rollback verification.
    - **Acceptance:** the test locates the nullable add and final alter specifically for `IgConversationAnalysisSnapshot.analyzed_at`, verifies the intervening backfill callable, and rejects reordered or unrelated operations.
    - **Tests:** exact operation identity/order plus a MariaDB fixture proving old `created_at` is preserved.
  - [ ] **P0.B5w Give each reclaimed revision its own retry budget.**
    - **Priority:** P0 — a newer customer/payment/order revision must not inherit exhausted provider attempts from a crashed older claim.
    - **Symptom:** scheduling increments `revision` while a job is processing but leaves the active lease and attempts intact; if that worker dies, bulk stale reclaim cannot tell which revision those attempts belonged to and carries them into the new work.
    - **Root cause:** the durable job stores only mutable current watermark/revision and no claimed watermark/revision for the active lease.
    - **Risk:** a newly changed conversation can receive fewer than the configured five attempts, become terminal after its first failure, or expose misleading retry telemetry after a process crash.
    - **Affected branches:** new message/payment/order during Gemini, daemon crash/restart, stale reclaim, max attempts, and periodic reconciliation.
    - **Acceptance:** every claim persists its exact claimed watermark/revision; conditional claim rejects a candidate changed before ownership; stale reclaim preserves attempts only for the same revision and resets them for newer input; every completion clears claimed ownership fields.
    - **Tests:** stale reclaim of unchanged fifth attempt, new revision during the fifth attempt followed by crash, conditional-claim race, successful completion, skip, failure, and replacement claim telemetry.
  - [ ] **P0.B5x Give Gemini current authoritative order truth and revalidate it before publish.**
    - **Priority:** P0 — an order-triggered analysis cannot reason about facts that never enter its input, and an old order snapshot cannot be published after those facts change.
    - **Symptom:** the fingerprint hashes deal/order/payment/tracking/shipment fields, but the Gemini payload contains only `verified_payment`, watermark and transcript; finalization also reuses the pre-provider fingerprint without recomputing current order truth.
    - **Root cause:** scheduling identity and provider context were implemented as separate payloads, and only payment boolean received a post-provider deterministic override.
    - **Risk:** shipment/order reanalysis spends quota without seeing the triggering state, publishes stale next-action evidence, and immediately churns another reconciliation revision.
    - **Affected branches:** order creation/cancellation/shipment/TTN, payment during Gemini, append-only snapshot fingerprint, reconciliation, and CRM next action.
    - **Acceptance:** one canonical non-secret truth payload feeds both fingerprint and Gemini; finalization recomputes it under locks; changed order truth supersedes/requeues without snapshot, while a missed payment-only transition is deterministically normalized and stored with the current fingerprint.
    - **Tests:** order truth present in provider payload, order change during Gemini with missed hook, payment during Gemini, no stale snapshot, current fingerprint persistence, and no follow-up revision churn.
  - [ ] **P0.B5y Use one deterministic projection-to-deal lock order.**
    - **Priority:** P0 — MariaDB deadlocks can abort paid-order materialization or analysis finalization.
    - **Symptom:** analysis finalization locks deals then payment projections, while order creation locks the projection then deal.
    - **Root cause:** the two services independently chose opposite row-lock order for the same client/deal graph.
    - **Risk:** concurrent payment/order creation and slow-analysis completion form a wait cycle, producing deadlock victims and stale operational state.
    - **Affected branches:** paid order materialization, payment reversal, analysis finalization, linked order reads, and retry/reconciliation.
    - **Acceptance:** all involved paths use deterministic projection -> deal -> linked order ordering after client/job ownership; analysis locks each set by primary key before recomputing truth or writing a snapshot.
    - **Tests:** concurrent order materialization/finalization MariaDB fixture, deterministic query order, reversal overlap, and deadlock-free rollback cleanup.
  - [ ] **P0.B5z Keep project cooldown monotonic under repeated 429 events.**
    - **Priority:** P0 — a stale short rate-limit response must not reopen a project already held by a longer day/top-up cooldown.
    - **Symptom:** `_apply_429_state()` unconditionally overwrites `cooldown_until`, so a later short `minute` 429 shortens an active longer cooldown even though group writes are atomic.
    - **Root cause:** atomic alias locking was added without a max-deadline merge policy.
    - **Risk:** workers resume calls against an exhausted project early, amplify quota pressure, and show contradictory recovery times.
    - **Affected branches:** all Gemini roles, project alias groups, retry ordering, health UI, and concurrent 429/success handling.
    - **Acceptance:** each 429 computes a proposed deadline and preserves the later active deadline/scope; no event can shorten cooldown, while expiry and later longer cooldown work normally.
    - **Tests:** long then short 429, short then long, grouped aliases, unknown alias, success ordering, expiry, and transaction rollback.
  - [ ] **P0.B5aa Fail migration when a required analysis table is missing.**
    - **Priority:** P0 — recording the engine migration as applied must prove every transactional table exists.
    - **Symptom:** migration 0096 silently skips `row is None` and succeeds even when a required snapshot/job/key-state table is absent.
    - **Root cause:** the conversion loop treats missing and already-correct tables as the same no-op path.
    - **Risk:** deploy reports applied migrations and healthy code while leases/cooldowns run on an incomplete schema.
    - **Affected branches:** production migrate, engine audit, daemon startup, key project cooldown, rollback, and disaster recovery.
    - **Acceptance:** 0096 raises before completion for any missing required table, converts wrong engines idempotently, and the post-migrate runtime audit requires all 17 tables.
    - **Tests:** missing first/middle/last table, MyISAM conversion, all-InnoDB no-op, non-MySQL skip, and production 17/17 proof.
  - [ ] **P0.B5ab Make analysis scheduling idempotent for an already covered state.**
    - **Priority:** P0 — overlapping message/payment/order/reconcile hooks must not duplicate Gemini cost or supersede useful work.
    - **Symptom:** `schedule_analysis()` increments revision for every call even when watermark and required-state fingerprint are identical and already pending, processing, terminal, skipped, or done.
    - **Root cause:** coalescing only guarantees one job row, not one revision per required state.
    - **Risk:** overlapping hooks cancel an in-flight owner, reset terminal budgets, postpone debounce, create needless revisions, and consume provider quota twice for one truth state.
    - **Affected branches:** classifier, payment hook, order hook, periodic reconciliation, retry/backoff, and daemon throughput.
    - **Acceptance:** exact covered watermark/fingerprint returns the existing job unchanged in every lifecycle state; only a new message/prompt/payment/order fingerprint creates one revision, and changed input starts with a fresh retry budget without stealing the active token.
    - **Tests:** duplicate pending/processing/done/skipped/failed schedules, overlapping hooks, changed watermark, changed truth, unchanged due/backoff/token, and concurrent conditional claim.
  - [ ] **P0.B5ac Terminalize an unchanged stale claim at the retry cap.**
    - **Priority:** P0 — a crashed provider worker must not bypass the bounded retry policy.
    - **Symptom:** stale reclaim always returns an expired processing job to `pending`, and the claim query accepts it even when the unchanged revision has already reached `MAX_ATTEMPTS`.
    - **Root cause:** terminal retry handling exists in the ordinary provider-failure path but is missing from lease-expiry recovery; claim eligibility also has no retry-cap guard.
    - **Risk:** repeated worker crashes or lease expirations can produce attempts 6, 7, and beyond, consume Gemini quota indefinitely, and starve other conversations.
    - **Affected branches:** daemon crash/restart, stale lease reclaim, retry telemetry, queue health, reconciliation coverage, and provider degradation.
    - **Acceptance:** stale reclaim marks an unchanged revision `failed` when its attempt count is at the cap, clears lease ownership, and leaves it terminal; a newer revision still returns to `pending` with attempts reset, and no pending job at the cap can be claimed.
    - **Tests:** unchanged fifth-attempt stale reclaim, defensive pending-at-cap claim rejection, newer revision during fifth attempt, and unchanged sub-cap reclaim.
  - [ ] **P0.B5ad Keep communication opt-out independent from commercial loss.**
    - **Priority:** P0 — consent truth must not corrupt conversion, objection, or drop-off analytics.
    - **Symptom:** classifier defines `no_buy = opt_out or NO_BUY_RE`, so pure `STOP`, `unsubscribe`, or `не пишіть` also writes `lost_reason=no_buy`, `NO_BUY`, a LOST signal, and may move a non-paid client to cold.
    - **Root cause:** communication withdrawal phrases were embedded in the commercial-refusal regex and both axes shared one boolean branch.
    - **Risk:** loss statistics overcount opt-outs, prior customer intent/stage is destroyed, retargeting cohorts become invalid, and operators cannot distinguish consent from purchase refusal.
    - **Affected branches:** deterministic classification, snapshots, funnel/lost reasons, objections, follow-ups, manual opt-in, and hidden/paid statistics.
    - **Acceptance:** pure opt-out changes only communication state and yields `interaction_type=opt_out`; explicit no-buy changes commercial truth; a message containing both records both independently; paid/order truth remains untouched.
    - **Tests:** pure STOP/unsubscribe/no-write phrases with prior high intent, pure explicit no-buy, combined no-buy plus opt-out, paid opt-out, and absence/presence of LOST signals and NO_BUY objection as appropriate.
  - [ ] **P0.B5ae Evaluate stale-reclaim decisions before clearing their source fields.**
    - **Priority:** P0 — production MariaDB evaluates single-table `UPDATE` assignments from left to right by default.
    - **Symptom:** stale reclaim clears `claimed_revision` before the later `last_error=Case(...)` reads it, so an unchanged exhausted claim can receive the non-terminal `stale_lease_recovered` reason even while its status becomes `failed`.
    - **Root cause:** the bulk update was reviewed with simultaneous-assignment semantics, but production MariaDB exposes earlier assignments to later expressions.
    - **Risk:** queue diagnostics and incident automation misclassify terminal quota exhaustion, while SQLite or mocked checks can remain green.
    - **Affected branches:** stale lease recovery, terminal retry telemetry, production health UI, incident review, and MariaDB-only verification.
    - **Acceptance:** every conditional expression reads the original claimed revision/attempt count before those source fields are cleared; unchanged capped work records `stale_lease_retry_exhausted`, and newer work records `stale_lease_recovered` with reset attempts.
    - **Tests:** production MariaDB rollback fixture for unchanged fifth attempt and newer fifth-attempt revision, exact status/reason/attempts, and no fixture residue.
  - [ ] **P0.B5af Make refusal and communication-consent phrase matching conservative.**
    - **Priority:** P0 — consent withdrawal must be recognized, while vague negative wording must not falsify commercial loss.
    - **Symptom:** `Мне не нужно больше писать`, `Мені не потрібно більше писати`, and `Меня не интересует рассылка` match the generic `NO_BUY_RE` branch but not `OPT_OUT_RE`; generic `не нужно/не интересует` can also classify a product or preference phrase as a final purchase refusal.
    - **Root cause:** object-free negative fragments were placed in the commercial regex, while the consent regex covered imperative forms but omitted common infinitive and message-receipt forms.
    - **Risk:** the bot may continue messaging after consent withdrawal, corrupt lost/objection analytics, cancel valid sales work, and place clients into wrong retargeting cohorts.
    - **Affected branches:** deterministic classification, durable opt-out, follow-up cancellation, no-reply routing, rules snapshots, funnel statistics, and manual opt-in.
    - **Acceptance:** consent phrases are recognized independently of grammar form; a commercial refusal requires an explicit purchase/order/product object or refusal verb; ambiguous negative preferences do not become final loss; combined messages set both axes.
    - **Tests:** RU/UA imperative and infinitive opt-out, unsubscribe/STOP, newsletter/message-receipt variants, explicit purchase refusal, product-specific rejection, ambiguous `не нужно/не интересует`, and combined phrases.
  - [ ] **P0.B5ag Fail closed for opt-out when deterministic enrichment raises.**
    - **Priority:** P0 — a classifier failure cannot authorize a customer reply after an explicit stop request.
    - **Symptom:** ingress creates an eligible opt-out message as `pending`, then swallows every classifier/follow-up exception; if deterministic classification raises, that row remains eligible for Gemini and Send API.
    - **Root cause:** the consent/no-reply barrier lives inside a best-effort enrichment block after the pending state has already been persisted.
    - **Risk:** a customer can receive an automated reply or follow-up after saying `STOP`, creating consent, reputation, and platform-policy exposure.
    - **Affected branches:** webhook and polling ingress, classifier failures, follow-up cancellation, pending queue drain, Gemini generation, Send API, and duplicate delivery.
    - **Acceptance:** a small deterministic opt-out guard runs inside the ingress transaction before fallible enrichment; it persists the communication hard stop, marks the message observed/done, and cancels pending follow-ups even when classification fails; no customer transport is invoked.
    - **Tests:** mocked classifier exception for RU/UA/EN opt-out, message status/processed time, durable client fields, pending follow-up cancellation, `process_pending=0`, and zero Gemini/Meta calls.
  - [x] **P0.B5ah Version changed deterministic classification semantics.**
    - **Priority:** P0 — append-only evidence must identify which rules produced it.
    - **Symptom:** opt-out/no-buy and score-band semantics changed while `ANALYSIS_RULES_VERSION` and the rules-snapshot dedupe key remained `2026-07-23.v1`.
    - **Root cause:** behavioral edits were made without advancing the persisted policy version.
    - **Risk:** old collapsed snapshots cannot be distinguished or safely recomputed, calibration mixes incompatible policies, and per-message `get_or_create` can retain stale v1 truth.
    - **Affected branches:** rules snapshot dedupe, reconciliation, analytics calibration, UI evidence, backfill, and audit history.
    - **Acceptance:** advance the rules version for the semantic change; new snapshots use the new version/dedupe key; historical rows stay append-only and explicitly identifiable.
    - **Tests:** version contract, new dedupe key, old/new coexistence, and idempotence within the new version.
    - **Implementation/evidence:** scoring and communication semantics were first versioned as `2026-07-24.v3`; the current taxonomy correction advances `ANALYSIS_RULES_VERSION` to `2026-07-24.v4`. Production MariaDB temporary-table proof asserted the prior snapshot version/dedupe behavior on SHA `c564a9e0`; historical rows remain append-only and distinguishable.
  - [x] **P1.A16a Keep communication opt-out separate from reversible commercial scoring.**
    - **Priority:** P1 — withdrawing consent must stop automation without erasing a still-valid commercial opportunity.
    - **Symptom:** the first reversible-scoring implementation treated every opt-out as a hard commercial zero, so a pure `STOP` could lower an existing checkout/high-intent readiness score.
    - **Root cause:** communication opt-out and explicit purchase refusal were passed through the same `hard_zero` scoring branch.
    - **Risk:** paid/order-pending or otherwise qualified conversations could be misreported as cold/lost and become ineligible for accurate conversion analysis after manual opt-in.
    - **Affected branches:** classifier readiness, rules snapshots, opt-out/manual opt-in, funnel/drop-off analytics, and operator UX.
    - **Acceptance:** pure opt-out preserves the prior commercial readiness; explicit `no_buy` resolves to zero; verified payment dominates both and remains `100`; communication remains independently paused.
    - **Tests:** pure opt-out with prior high readiness, explicit no-buy, combined no-buy plus opt-out, verified-paid opt-out, and DB-free readiness contract.
    - **Implementation/evidence:** `_resolve_readiness()` now preserves pure opt-out while keeping `no_buy` hard-zero and verified-ledger payment authoritative. Focused production-style DB-free suite passes 8/8 with the unused database skipped; production SHA `c564a9e0` passed the MariaDB temporary-table proof with zero real-table residue and zero external transports.
  - [x] **P0.B5ai Backfill durable opt-out truth for existing conversations.**
    - **Priority:** P0 — newly safe ingress does not protect clients whose opt-out was recorded before durable fields existed.
    - **Symptom:** migration 0095 adds nullable opt-out fields but does not derive them from existing deterministic opt-out evidence/messages.
    - **Root cause:** schema rollout and legacy-state reconciliation were separated without a bounded data migration or explicit production command.
    - **Risk:** an existing opted-out client can remain reply/follow-up eligible after deploy even though append-only evidence already records the stop request.
    - **Affected branches:** legacy rules snapshots, inbound messages, client pause state, pending follow-ups, statistics, manual opt-in audit, and deployment rollback.
    - **Acceptance:** a bounded, idempotent, no-network reconciliation derives only high-confidence historical opt-outs, preserves payment/order/commercial state, records source message/time, cancels pending follow-ups, and reports ambiguous rows for manager review; production proof is rollback-only before any committed backfill is authorized.
    - **Tests:** deterministic old snapshot/message, ambiguous phrase, paid client, already opted-in-after-opt-out, duplicate run, bounded cursor, no Gemini/Meta/Telegram, and no fixture residue.
    - **Implementation/evidence:** added `backfill_ig_opt_out --dry-run`/bounded write command with a dedicated durable cursor, explicit deterministic consent-withdrawal evidence, ambiguous snapshot reporting, opt-in protection, idempotence, follow-up cancellation, and maintenance-gated writes. Production migration `0099` applied on MariaDB `qlknpodo_MySQL_DB`; dry-run inventory scanned five clients with `updated=0`, `ambiguous=0`, and no cursor mutation. A rollback-only MariaDB fixture proved explicit STOP, opt-in-after-source, ambiguous snapshot, duplicate run, pending follow-up cancellation, cursor persistence, and zero residue; no Gemini/Meta/Telegram transport ran. Existing production inventory remains zero affected historical opt-outs, so no live backfill was authorized.
  - [x] **P0.B5aj Separate missed-event reconciliation from historical AI backfill.**
    - **Priority:** P0 — rollout must not spend an unverified project-scoped Gemini quota across the historical inbox.
    - **Symptom:** the first daemon start after 0095 scanned every historical client because `analysis_reconcile_cursor=0`; with no confirmed `GEMINI_KEY_PROJECT_GROUPS`, production created 56 historical jobs and completed 8 AI snapshots before maintenance stopped the worker.
    - **Root cause:** periodic recovery and one-time historical backfill share one unbounded eligibility policy; the cursor paginates the archive but does not define a rollout cutoff or require explicit backfill authorization.
    - **Risk:** uncontrolled token/quota spend, project-wide 429 amplification across aliases, delayed live analysis, and historical processing that operators did not authorize.
    - **Affected branches:** daemon startup, periodic reconciliation, payment/order missed-hook recovery, cursor wrap, project cooldown, deployment, and queue health.
    - **Acceptance:** persist an analysis rollout cutoff; automatically reconcile only messages, jobs, payment truth, or order truth changed at/after that cutoff; historical backfill is disabled by default and cannot run unless explicitly enabled and the alias-to-project mapping is complete; migration quarantines pre-cutoff pending reconcile-only jobs without deleting completed audit rows.
    - **Tests:** old message/no job, new message, old message/new job, post-cutoff payment/order update, cursor wrap, explicit backfill with complete/incomplete mapping, migration quarantine, daemon restart, and production queue counts with zero further historical Gemini calls.
    - **Implementation/evidence:** migration `0097` persists `analysis_reconcile_after`, quarantines unfinished pre-cutoff reconcile jobs, and gates historical work behind both `analysis_backfill_enabled` and complete alias-to-project mapping. Production SHA `f7d63915` has migrations `0097`, `0098`, and `0099` applied; `analysis_backfill_enabled=false`, `analysis_backfill_allowed=false`, project mapping incomplete, analysis pending/failed `0/0`, and zero pre-cutoff pending/processing reconcile jobs. The retained historical reconcile audit count is 56 with no new historical Gemini work authorized. Cursor/eligibility/migration tests and prior rollback-only evidence cover wrap, missed-event recovery, explicit mapping gate, and quarantine; no live backfill or external transport was run.
  - [x] **P0.B5ak Use a field-specific durable clock for linked order truth.**
    - **Priority:** P0 — a broad or missing order timestamp can bypass the rollout cutoff or lose a real missed event.
    - **Symptom:** generic `Order.updated` changes after unrelated full saves, while relevant `save(update_fields=...)` transitions can leave it unchanged.
    - **Root cause:** reconciliation used one general-purpose model timestamp for both operational edits and the bounded order fields included in the CRM fingerprint.
    - **Risk:** address/comment/UTM edits can restart unauthorized historical Gemini work, while payment, status, tracking, or shipment changes can leave a stale snapshot after scheduler failure.
    - **Affected branches:** order creation/linking/unlinking, direct staff deletion, payment reversal, Nova Poshta status updates, tracking, shipment notification, periodic reconciliation, and rollout cutoff.
    - **Acceptance:** maintain a dedicated `IgDeal.order_truth_updated_at` only for order creation/linking/unlinking and changes to `status`, `payment_status`, `tracking_number`, `shipment_status`, or `shipped_notified_at`; unrelated order saves do not move it; reconciliation uses this clock and never generic `Order.updated`.
    - **Tests:** unrelated full save, relevant `update_fields` save, initial order link, transaction-safe `SET_NULL` after direct order deletion, shipment notification, missed scheduler recovery, pre-cutoff rejection, no external transports, and rollback-only MariaDB residue/AUTO_INCREMENT proof.
    - **Implementation/evidence:** `IgDeal` link and `shipped_notified_at` changes now advance `order_truth_updated_at` through a post-save signal that preserves narrow `update_fields`; Order signals continue to cover status/payment/tracking/shipment changes and direct deletion/unlink. Production SHA `d7df803f` passed the DB-free policy suite (10/10, both databases skipped), `manage.py check`, migration drift, InnoDB production contract, and a rollback-only MariaDB fixture covering shipped update, link, relevant order update, unrelated edit, unlink, and zero residue. No network transport ran; daemon returned healthy after restart with a single process.
  - [x] **P0.B5al Make schema-aware deploys restart Passenger before declaring the bot page healthy.**
    - **Priority:** P0 — a stale web process can keep serving pre-migration Python after the database and checkout have moved forward.
    - **Symptom:** `/bot/api/status/` and `/bot/` returned HTTP 500 after SHA `142e27a2` was checked out and migration `0097` was applied; traceback showed `InstagramBotSettings` loaded without `analysis_reconcile_after`.
    - **Root cause:** the previous deploy pulled and migrated the new code but did not complete `touch tmp/restart.txt`/Passenger reload, so already-loaded Django workers retained the old model class.
    - **Risk:** staff loses the management bot console and status visibility while the daemon/database may appear healthy; repeated polling amplifies traceback noise.
    - **Affected branches:** every schema-aware management deploy, bot dashboard, `/bot/api/status/`, status polling UI, rollout/migration verification, and incident recovery.
    - **Acceptance:** deploy procedure must run `check`, migration/drift checks, `touch tmp/restart.txt`, `run_instagram_bot --ensure`, and an authenticated or in-process staff HTTP proof of `/bot/api/status/` returning 200 JSON; public unauthenticated boundary must return 302 rather than 500; production SHA, migration state, daemon heartbeat, and Passenger reload evidence are recorded together.
    - **Tests:** DB-free view smoke with staff request, production `RequestFactory`/view proof, public HTTP auth-boundary smoke, stale-process regression review, no customer/Meta/Telegram sends, and post-restart traceback scan.
    - **Production evidence:** SHA `142e27a2`; migration `0097` applied; new process view proof returned `200 application/json` with `success=true`; public `/bot/api/status/` returned `302`; one daemon PID and sub-second DB/cache heartbeats; 17/17 InnoDB and payment truth audit findings `0`.

  - [x] **P0.B5am Reconcile paused conversations into an evidence-bound, readable CRM projection.**
    - **Priority:** P0 — paused replies must not disable stage truth, and operators need one clear category instead of raw duplicate events.
    - **Symptom:** a manager-led customer who selected a size and discussed payment can remain at `Написав`; the card shows repeated raw signal codes such as `size_concern ×4` and `checkout_started ×2`, while the latest manager snapshot can hide the customer's category.
    - **Root cause:** deterministic stage projection was coupled to the reply path; historical Gemini backfill is intentionally gated, and the UI rendered the append-only signal event log directly without grouping or source-role precedence.
    - **Risk:** staff misread a checkout conversation as a cold/new lead, treat a signal as payment proof, miss complaints, and spend analysis quota on hidden/spam clients that should be excluded entirely.
    - **Affected branches:** paused/takeover ingress, reconciliation cursor, rules snapshots, stage funnel, payment-truth display, client detail API, category filters, statistics, and English/Ukrainian technical labels.
    - **Acceptance:** visible paused/manager-led clients receive deterministic no-network stage projection and rules snapshots before any historical-AI cutoff; hidden/blocked/spam clients are excluded from reconciliation and due claims; only verified payment ledger truth produces `paid`; detail API groups each signal type with Ukrainian label/count/latest evidence; manager observations never hide the latest customer category; exact terms remain English (`live`, `ENV`, `API Key`, `Conversions API`, `Meta Test Event Code`, `Checkout started`) while explanatory UX remains Ukrainian.
    - **Tests:** DB-free stage monotonicity and manager-signal exclusion; DB-free signal grouping; production MariaDB rollback fixture for paused payment/size conversation, hidden exclusion, rules snapshot/stage projection, grouped API response, payment `unverified`, and zero external transports; desktop/mobile browser proof.
    - **Implementation/evidence:** deterministic projection is now invoked from classifier and cursor reconciliation independently of reply enablement; hidden/blocked/spam clients are excluded before scheduling/claiming; manager observations are kept as context while customer snapshots drive category filters/cards; detail API groups durable signals by type with latest value/time and the UI explains that signals are not payment proof. DB-free contract suite passed 19/19 with `Skipping setup of unused database(s)`; production rollback fixture passed under the bounded maintenance lease with `mocked_no_network`. On SHA `e7109828`, client `59` returned `stage=checkout`, `bot_paused=true`, `hidden=false`, one rules snapshot, `payment_truth=unverified`, and grouped API signals `Взяв менеджер ×8`, `Розмір ×4`, `Checkout started ×2`, `Кастомний принт ×2` (`signal_event_count=16`). Production runtime had one daemon, fresh DB/cache heartbeats, zero reply/analysis/outbox backlog, 17/17 InnoDB tables, and payment-truth audit findings `0`; no customer, Gemini, Meta, or Telegram transport was invoked by the proof.

- [x] **P1.B5c Replace the global long-held reply lock with a bounded two-level permission barrier.**
  - **Priority:** P1 — correctness is currently fail-closed, but latency and operator availability degrade under slow AI/provider calls.
  - **Symptom:** unrelated clients are serialized, while global stop, client pause, or manager takeover can wait for the full Gemini/Meta timeout before returning.
  - **Root cause:** one process-wide exclusive `flock` is held across conversation generation and external provider I/O in order to guarantee that no reply crosses a stop/pause boundary.
  - **Risk:** head-of-line blocking, delayed replies in unrelated chats, an admin request timing out while the eventual state transition remains unclear, and growing queue lag during a slow provider incident.
  - **Affected branches:** inbound reply generation/send, due follow-ups, global stop, per-client pause/resume, and manager takeover.
  - **Acceptance:** use per-client send ownership plus a short global generation/permission epoch; revalidate the global/client epoch immediately before every external customer send; stop/pause/takeover completes within a documented bounded time and guarantees zero post-boundary sends without waiting for another client's whole generation; expose lock-wait/abort telemetry in Ukrainian.
  - **Tests:** two clients generate concurrently, stop during Gemini, pause one client while another is slow, takeover at the pre-send boundary, provider timeout, stale owner recovery, and exactly zero customer sends after a committed stop/pause epoch.
  - **Production evidence:** SHA `6932d822`; migration `0098` applied on MariaDB `qlknpodo_MySQL_DB`; rollback-only fixture proved global/client epoch invalidation with zero persisted residue; 7/7 DB-free reply-boundary tests passed while explicitly skipping both production databases; staff status API returned HTTP 200 with `reply_barrier` telemetry; public auth boundaries returned 302; exactly one daemon was running with sub-second DB/cache heartbeats, queue `0`, analysis pending/failed `0/0`, and effective model `gemini-3.6-flash`; all 17 runtime tables remained InnoDB and payment truth audit findings remained `0`.

- [x] **P1.B5d Make daemon ensure startup verification truthful and startup-budget aware.**
  - **Priority:** P1 — deploy recovery succeeded, but the operator command emitted a false failure and could cause repeated/manual spawn attempts.
  - **Symptom:** immediately after maintenance release on SHA `6932d822`, `run_instagram_bot --ensure` returned `daemon child exited before acquiring singleton lock`; a retry seconds later returned `daemon alive — ok` and production had exactly one healthy daemon.
  - **Root cause:** `_ensure()` waits a hard-coded 3 seconds for the daemon lock, discards the `Popen` handle, and labels every timeout as a child exit without checking `poll()`, current lock ownership, or heartbeat truth.
  - **Risk:** false deploy failure, redundant recovery attempts, confusing incident evidence, and unsafe operator pressure to spawn another worker even though the singleton daemon is still starting.
  - **Affected branches:** maintenance release, schema deploy restart, cron watchdog, slow Django import/startup, concurrent ensure calls, daemon singleton acquisition, and production verification.
  - **Acceptance:** define a documented startup budget; retain and inspect the child process handle; distinguish exited child, still-starting timeout, and another healthy winner; perform one final lock/heartbeat reconciliation before failing; never report success without singleton lock plus fresh heartbeat.
  - **Tests:** slow child acquires within budget, exited child with return code, live child timeout, concurrent winner, stale old daemon release, exact one-spawn boundary, and production maintenance release/ensure proof.
  - **Production evidence:** SHA `84718f60`; `DAEMON_START_WAIT_SECONDS=15`; 11/11 daemon-path tests passed with DB setup skipped; maintenance release returned `daemon spawned` without a false timeout, then one daemon PID held the singleton lock with fresh DB/cache heartbeats; staff status API returned 200 and public auth boundaries returned 302.

- [x] **P0.B6 Fail closed for Meta data-deletion signed requests.**
  - **Symptom:** the public data-deletion callback accepts a syntactically valid signed request when the app secret is absent.
  - **Root cause:** HMAC validation runs only inside `if app_secret`.
  - **Risk:** forged audit/receipt rows and false compliance signals; current callback is not destructive, but fail-open verification is still incorrect.
  - **Affected branches:** public deletion callback, audit receipts, privacy incident response.
  - **Acceptance:** missing secret rejects the signed callback with a safe status; explicit local-test override is isolated; manual authenticated deletion remains available; no secret/raw signed payload in logs.
  - **Tests:** missing secret, valid/invalid HMAC, malformed payload, replay/idempotency, manual deletion unaffected.
  - **Implementation/evidence:** `_parse_meta_signed_request` now requires a configured `IG_APP_SECRET`/`FACEBOOK_APP_SECRET`, validates the HMAC before JSON decoding, rejects malformed/non-object payloads without raising, and never logs signed material. Production SHA `e2fa7426` passed the DB-free parser suite (3/3, with both production databases explicitly skipped) and a mocked no-network view proof for missing-secret rejection, valid acceptance, and invalid-signature rejection. The production MariaDB contract remained `qlknpodo_MySQL_DB` with zero test schemas; no Meta callback or customer transport was sent. Passenger restart and daemon ensure completed with one live daemon and no new status errors.

- [x] **P0.B7 Do not treat an acquiring `hold` as confirmed payment.**
  - **Symptom:** the payment service grouped Monobank `hold` with `success`, set `paid_at`, moved the client to paid, created an order, and counted conversion before funds were captured.
  - **Root cause:** `MONO_SUCCESS` contained both statuses even though the provider contract distinguishes an authorization hold from a successful debit.
  - **Risk:** unfunded orders, false paid conversion/revenue, premature Purchase attribution, and shipment before confirmed capture.
  - **Affected branches:** provider webhook/poll, deal truth, order materialization, follow-up cancellation, CRM paid view, statistics, CAPI eligibility.
  - **Acceptance:** `hold` is append-only pending evidence only; it cannot set `paid_at`, positive payment truth, paid stage, order, conversion, or Purchase. A later provider `success` promotes exactly once; reversal/cancel remains independently auditable.
  - **Tests:** hold-only, hold→success, duplicate hold, out-of-order success→older hold, prepayment/full payment, every paid aggregate and order boundary.
  - **Production proof:** SHA `fc727a35`; `hold` is pending-only, `success` requires exact expected amount, terminal truth cannot be resurrected, provider event/projection tables are InnoDB, and paid-truth audit returned zero findings.

- [x] **P0.B8 Make payment-ledger trigger migration safe on MariaDB.**
  - **Symptom:** production `0090_payment_truth_projection` created its table/columns, then failed before trigger creation and migration recording with `TransactionManagementError`.
  - **Root cause:** trigger DDL ran inside Django's default atomic migration although MariaDB cannot roll that DDL back.
  - **Risk:** partially applied schema, deploy interruption, absent append-only database guards, and unsafe repeated migration attempts against already-created objects.
  - **Affected branches:** fresh install, production upgrade, rollback/retry, append-only enforcement, deploy completion evidence.
  - **Acceptance:** migration declares the correct non-atomic boundary; fresh SQLite/MariaDB migration creates both triggers; the observed partial production state is reconciled without deleting payment evidence; migration history, schema, triggers, engines and runtime all agree.
  - **Tests:** fresh migration, interrupted/partial recovery inspection, idempotent trigger recreation, production `showmigrations`, information-schema engine/column/trigger checks.
  - **Production proof:** the observed empty partial schema was inspected before recovery; no payment evidence was deleted. `0090` now declares `atomic = False`, fresh SQLite migration passed, production migration history is applied, and `ig_payevt_no_update`/`ig_payevt_no_delete` exist on the InnoDB event table at SHA `fc727a35`.

#### P1.B — CRM truth, intelligence, orders, and conversion

- [ ] **P1.B1 Implement the four-axis CRM state model.** Store and display interaction stage, payment truth, fulfillment truth, and automation/capability state independently. Derived lifecycle summaries may show `paid` or `waiting_shipment`, but Gemini never writes them. Add append-only transition history, reason, evidence, actor/service, source event, version, and timestamp for every axis. Tests cover conflicting axes, legal transition matrices, late/refunded payments, shipment updates, manager takeover, opt-out after purchase, and legacy migration.

- [ ] **P1.B2 Add idempotent event-triggered and delayed conversation analysis.** Deterministic extraction runs once per new watermark; high-reasoning jobs coalesce by client/message watermark and support hourly/nightly reconciliation. Persist due time, lease, attempts, remaining-time deadline, model/key/task/level, tokens, latency, outcome, skip reason, and analyzed watermark. A restart, pause, or repeated hook cannot duplicate analysis cost. Prioritize payment ambiguity/high intent and skip hidden/opt-out/spam according to explicit policy.

- [ ] **P1.B3 Store structured commercial memory with provenance and conflicts.** Track product stable ID/SKU/variant, size/fit, color, quantity, custom-print brief, language, desired time, delivery need, objections, explicit price-sensitivity evidence, likes/dislikes, last promise, category, and next action. Every fact stores source role/message/time, deterministic/model/operator origin, confidence, model/rules version, superseded/conflict state, and expiry for changing catalog facts. Manager facts never become customer intent/payment proof.

- [ ] **P1.B4 Replace cumulative readiness with reversible evidence scoring.** Deduplicate repeated semantic signals; allow later refusal, silence, unavailability, changed product, and verified outcome to move state in either direction; separate deterministic facts from model inference; expose probability and confidence independently. Prohibit language, nationality, ethnicity, grammar, writing manner, or inferred wealth as features. Add at least 100 Ukrainian/Russian fixtures spanning terse speech, sarcasm, mixed language, reactions, abuse, custom print, collaboration, wholesale, stock/size ambiguity, promise-to-pay, verified payment, refund, and repeat purchase.

- [ ] **P1.B5 Complete role-preserving memory and analysis transcripts.**
  - **Symptom/root cause:** rolling memory and order/product extraction label every non-customer message as bot text because they use a binary user/non-user renderer.
  - **Risk/branches:** manager promises can be presented to Gemini as bot statements, corrupting intent, objection, product, order, and next-action provenance in memory, snapshots, and follow-ups.
  - **Acceptance:** preserve `customer`, `bot`, `manager`, `system`, and `followup` roles end-to-end; manager facts remain searchable but never become customer intent/payment proof.
  - **Tests:** identical sentence from customer/bot/manager, manager payment promise, manager order acknowledgement, follow-up text, rolling summary, product/order extraction, snapshot evidence.

- [ ] **P1.B6 Add evidence-bound multi-order identity linking.** Link one IG client to zero/many site/manual orders through an append-only match record. Exact normalized phone is the primary candidate key; corroborate with name, city, Nova Poshta branch, time window, products, amount, checkout/payment/deal IDs, and manager confirmation. Ambiguous/shared-phone/fuzzy-only candidates stay review-only. Store confidence, evidence, matcher version, rejected candidates, link/unlink actor/time/reason. UI shows each order/items/order value/paid value/payment/order/TTN state. Tests cover one client-many orders, shared phone, typo, manual bank payment, site checkout, wrong-order rejection, duplicate payment, link correction, and TTN update.

- [ ] **P1.B7 Reconcile refunds, reversals, cancellations, returns, and multiple payments.** The current `already_paid` fast path ignores later reversal/refund semantics. Preserve an immutable payment-event ledger, derive current truth, keep full/prepayment as one Purchase with full discounted order value plus actual paid value, and never erase history. Tests cover partial/full refund, disputed payment, cancelled order, returned shipment, repeated webhook/poll, and a later second order for the same client.

- [ ] **P1.B8 Expand taxonomy and policy routing.** Add collaboration/creator/advertising, wholesale/B2B, support/complaint, community/casual/meme, real reaction events, and unknown-safe handling. Keep opt-out distinct from lost. Reaction-only and casual acknowledgement rules must not create automatic sales replies. Add abuse warning/escalation rules without interpreting profanity alone as non-buying intent.

- [ ] **P1.B9 Make discounts a versioned eligibility policy, not a prompt suggestion.** Record previous objection handling, elapsed conversation time, explicit affordability evidence, margin eligibility, product exclusions, manager approval, offer version, and expiry. A 10% rescue is rare, capped, independently visible, and impossible after refusal/opt-out/paid/manager takeover. Measure verified outcome and complaints, not reply rate. Tests cover repeated requests for discounts, prompt injection, low-margin/custom products, 5% then exceptional 10%, quiet hours, Meta window, and no-buy stop.

- [ ] **P1.B10 Govern retention without deleting CRM/audit truth.**
  - **Symptom/root cause:** `purge_stale_clients()` physically cascades stale CRM cards without dry-run, and `_trim_messages()` keeps only the last 2000 messages globally rather than by evidence/retention policy.
  - **Risk/branches:** hidden/paid/order history, score evidence, reanalysis sources, correction audit, and legal/payment evidence can disappear during routine maintenance.
  - **Acceptance:** dry-run-first archive/anonymization rules preserve payment/order/stage/score/correction truth; explicit privacy erasure remains separate and authenticated; redacted evidence receives tombstones.
  - **Tests:** hidden stale, paid stale, old active chat, snapshot references, global-volume pressure, dry run, idempotency, verified privacy deletion.

#### P1.C — Meta, Gemini, webhook, and CAPI contracts

- [ ] **P1.C1 Finish P0.A9 with a central Meta request contract.** Build all Graph URLs through a versioned request builder/transport that rejects unversioned, wrong-host, downgrade, fragment, and credential-leaking URLs. Status/UI separately expose local allowlist mode, token permission probe/time, account access mode (`standard_test`, `advanced_public`, `unknown`), and per-recipient delivery result. App Role/Standard Access is labelled test-only and never treated as public Advanced Access. Contract tests enumerate every bot Meta endpoint and pagination URL.

- [ ] **P1.C2 Complete webhook coverage and observability.**
  - **Symptom/root cause:** the parser handles only message-shaped events; referral/postback without `message` is discarded, `is_self` and reaction events are not classified, unknown/deleted fields have no counters, and each nonempty webhook may spawn an unbounded thread.
  - **Risk/branches:** lost attribution/actions, accidental response to self events, missed reaction taxonomy, invisible API drift, and worker/thread exhaustion.
  - **Acceptance:** handle messages, quick replies, postbacks, referrals without text, echoes/`is_self`, reactions, attachments, deletes/unsends, unsupported/unknown-valid fields, duplicate `mid`, and out-of-order delivery; bounded queue/backpressure owns async work.
  - **Tests:** one official-style fixture per field variant, unknown counters, no-response assertions, duplicate/out-of-order events, referral-only postback, thread/backpressure limit.

- [ ] **P1.C3 Add endpoint-class Meta rate limiting and headers.**
  - **Symptom/root cause:** conversation-list pages are paced, but per-conversation message pages can burst; `_http()` drops response headers, so `Retry-After` and proven usage state cannot be observed.
  - **Risk/branches:** avoidable 429s, incomplete polling, misleading health, and starvation across older conversations.
  - **Acceptance:** one shared-account pacer covers list and message pagination; Send endpoint classes remain separate; bounded headers, retry time, error class/time, counters, latency, and degraded state are recorded without guessing remaining quota.
  - **Tests:** multipage list+message pacing, concurrent pollers, 429 with/without headers, server errors, deadline exhaustion, stale-cache fallback, fair round robin.

- [ ] **P1.C4 Make Meta feedback test/live modes safe and truthful.**
  - **Symptom/root cause:** the UI stores a `TEST...` code, but the IG event sender never passes it to the CAPI service; an operator can believe an event is test-only while the code path is live.
  - **Risk/branches:** unintended production ad events, misleading operator controls, polluted attribution, and inability to verify test mode safely.
  - **Acceptance:** disabled/preview/test/live are separate; preview is no-network; test validates and forwards the code; live requires explicit server capability/authorization and cannot be selected accidentally; Ukrainian help explains consent, match data, dedupe, attribution-not-causality, and status meanings.
  - **Tests:** code forwarded in test mode, disabled/preview zero SDK calls, invalid/blank code rejected, live permission guard, no silent test-to-live fallback, actor/config audit.

- [ ] **P1.C5 Emit one deferred IG Purchase only after verified payment and linked/materialized order.**
  - **Symptom/root cause:** verified payment calls `log_or_send(Purchase)` before `_on_deal_paid()` materializes the order; with `order=None` the event is skipped, and order creation has no later CAPI hook.
  - **Risk/branches:** confirmed IG purchases silently never reach the configured feedback pipeline, while the cockpit may imply that enabling the checkbox works.
  - **Acceptance:** provider payment verifies first, order is created/linked second, one stable Purchase event is prepared third, retail-flow dedupe is honored, and full order value/paid value/refund semantics are explicit.
  - **Tests:** full/prepay, webhook+poll race, delayed order creation, duplicate callback, SDK timeout/retry, retail dedupe, refund/cancel, disabled/test/live modes. Do not run live events during implementation.

- [ ] **P1.C6 Group Gemini keys by provider project/quota domain.** Six aliases do not necessarily represent six independent quotas. Store/configure project grouping, apply 429 cooldown at the proven project scope, retain key-specific auth/model errors, and show capacity without claiming unsupported remaining quota. Tests cover two aliases in one project, independent projects, borrow pools, project 429, key 403/404, and recovery.

- [ ] **P1.C7 Enforce a real end-to-end Gemini deadline.**
  - **Symptom/root cause:** the 75-second deadline is checked before an attempt, but the final HTTP call and backoff may still receive their full timeout and overrun the advertised hard ceiling.
  - **Risk/branches:** stuck client lease, delayed reply/analysis, daemon lag, and overlapping work after retries.
  - **Acceptance:** compute remaining time before HTTP/backoff, cap connect/read/sleep to that budget, stop when another attempt cannot fit, and persist deadline/attempt outcome; keep chat `medium`, complex decisions/intelligence `high`, probes `low`.
  - **Tests:** fake-clock connect/read timeout, backoff, last-attempt overrun, fallback model, cancellation, lease release, telemetry.

- [ ] **P1.C8 Add truthful provider/runtime health.** Expose daemon PID/SHA, cache backend/health, DB engine contract, webhook last seen/signature state, queue depth/oldest age/lag, outbox due/failed oldest age, configured/effective/last Gemini model and key, key-pool probe health, generation capability, Meta token capability, and concrete delivery blockers. “Daemon alive” never means “can answer this recipient.”

#### P1.D — statistics and Ukrainian cockpit

- [ ] **P1.D1 Define separate measures and explicit denominators.**
  - **Symptom/root cause:** current “conversations” equals client count, paid equals mutable stage, revenue sums deal order value, and objection output mixes distinct clients with raw signal events.
  - **Risk/branches:** false conversion/revenue, incomparable percentages, repeated-signal inflation, and business decisions made from undefined denominators.
  - **Acceptance:** count users, conversations, messages by role, invoices/attempts, verified payment transactions, paying users, orders, order value, paid value, refunds, shipments, and deliveries separately; every metric declares numerator, denominator, exclusions, unknowns, sample size, timezone, range, and date semantics.
  - **Tests:** one user-many conversations/orders/payments, repeated signals, hidden/archive, prepayment, refund, unknown attribution, boundary timestamps in `Europe/Kiev`.

- [ ] **P1.D2 Implement honest time/cohort semantics.** Support custom from/to plus first-contact cohort, interaction-event, analysis, payment, order, and shipment dates. Current “last message in range then current stage” is not a funnel and must be labelled/replaced. Add cumulative funnel/drop-off with cohort retention, current-state distribution as a separate view, and drill-down to the exact included users/events.

- [ ] **P1.D3 Centralize hidden/archive and eligibility scopes.** Implement one tested analytics scope used by overview, funnel, products, objections, language, campaign, drop-off, follow-up, revenue, calibration, and export. Hidden records remain stored and visible only through an explicit archive scope; excluded counts and reasons are shown. Define paused/blocked/test/staff/friend semantics explicitly.

- [ ] **P1.D4 Add complete filters and stable identifiers.** Filters: custom dates, interaction stage, payment truth, fulfillment truth, stable product ID/SKU/variant, source/ad/campaign, assigned manager/owner, objection, category, language, follow-up outcome, and archive scope. Add an auditable assigned-manager model before offering that filter. Product analytics keeps stable IDs and treats titles as labels only.

- [ ] **P1.D5 Separate user objections from signal-event volume.** Distinct-client objection rates and raw/repeated signal counts are separate measures. “Дорого” requires explicit evidence/confidence or operator confirmation; it cannot be inferred from language/nationality/writing style. Show threshold/version/evidence and allow drill-down/correction. Tests cover repeated price messages, conflict, later acceptance, multiple objections, and small denominators.

- [ ] **P1.D6 Build prediction calibration and audit correction.** Compare saved predictions with later verified paid/lost outcomes; report Brier/calibration bands, false-high/false-low, sample size, version drift, and manual corrections. Authorized operators may correct product/category/objection/band/next action with required reason and before/after audit; payment/order facts remain read-only to this workflow.

- [ ] **P1.D7 Rebuild client list/detail around evidence and next action.** Each row/card shows Ukrainian interaction stage, payment truth, fulfillment truth, owner, product, next action and reason, full due date and live countdown, delivery capability, analysis model/version/time, probability/confidence, evidence/uncertainty, and score/stage history. Do not truncate the only evidence line with nowrap/ellipsis; render all evidence accessibly.

- [ ] **P1.D8 Complete Ukrainian localization and help text.** Replace raw `Paid`, `legacy`, `conf`, `obj`, `FU`, `discount`, `ad`, `Reasoning`, `Worker`, `Heartbeat`, `Rescue offer`, raw signal/deal/follow-up enums, and unexplained acronyms with backend labels and Ukrainian copy. Every setting explains what it controls, what it does not prove, risk, default, and operator action—especially Meta feedback/test code, polling, allowlist, analysis, reply automation, and model choice.

- [x] **P1.D8a Make the bot overview model explanation runtime-truthful.**
  - **Priority:** P1 — operators currently see contradictory model names on the same production screen.
  - **Symptom:** the overview status reports effective model `gemini-3.6-flash`, while the adjacent “Як працює” explanation still claims replies use `gemini-3-flash-preview`.
  - **Root cause:** the explanatory paragraph renders the raw legacy `settings.gemini_model` value once on the server and never synchronizes it with the normalized runtime model status.
  - **Risk:** an administrator can misdiagnose model rollout or key compatibility and cannot trust the cockpit as an operational source of truth.
  - **Affected branches:** overview template, status rendering, Ukrainian help copy, configured/effective model distinction, browser QA.
  - **Acceptance:** remove the hardcoded model name; render a localized explanation from the same bounded status field used by the model tile, with an honest fallback before status loads; never expose a key value.
  - **Tests:** template contract rejects the retired identifier, runtime rendering uses the effective model, missing-status fallback remains readable, production browser shows one consistent model identifier.
  - **Implementation/evidence:** `023022c6` replaces the stale server-rendered model with a bounded `textContent` update shared by the model tile and explanation, with a readable pre-status fallback. The DB-free production-settings contract passed `3/3` with `Skipping setup of unused database(s): default`, plus compile/diff checks. Production was fast-forwarded to `023022c6361747e8a0cd11eafafc6dd00709345d`; after `--ensure`, authenticated browser QA showed `Працює`, `Агент онлайн і відповідає`, and exact equality `gemini-3.6-flash` in both DOM nodes. No key values or external transports were used.

- [ ] **P1.D8b Keep the model settings selector aligned with normalized runtime truth.**
  - **Priority:** P1 — a legacy alias can leave the settings form with no selected option even while generation correctly uses Gemini 3.6.
  - **Symptom:** production stores `gemini-3-flash-preview`; status normalizes it to `gemini-3.6-flash`, but the selector compares options with the raw `settings.gemini_model` value.
  - **Root cause:** the option `selected` conditions read the persisted alias rather than the bounded `status.gemini_effective_model` used by the provider path.
  - **Risk:** an administrator may save the wrong fallback, believe no model is selected, or misread which model the six-key pool uses.
  - **Affected branches:** settings form, model allowlist, effective/configured model display, Gemini key pooling, operator QA.
  - **Acceptance:** exactly one allowed option is selected from normalized status for legacy and current aliases; save validation remains allowlist-bound; no credential values are exposed.
  - **Tests:** template contract for normalized selection, legacy/current option rendering, invalid model rejection, production browser settings proof.

- [ ] **P1.D9 Apply semantic colors without using color as the sole carrier.** Green only for verified paid/delivered truth, yellow for high intent/payment pending, blue for shipped/in transit, neutral for information/exploration, red for opt-out/lost/abuse/blockers. Waiting shipment is not “complete.” Every chip has text/icon/state semantics; funnel completion is not inferred from mutable stage.

- [ ] **P1.D8c Localize client-card operational labels and keyboard access.**
  - **Priority:** P1 — an administrator must understand category, objection, next action, attribution, delivery blocker, and legacy score without decoding English abbreviations or relying on pointer-only interaction.
  - **Symptom:** the client list rendered `obj`, `FU`, `discount`, `ad`, and `legacy` fragments and used clickable `div` rows with no keyboard semantics.
  - **Root cause:** the first category UI slice localized badges/detail headings but left the compact list summary on an older diagnostic vocabulary.
  - **Risk:** complaints and next actions are missed during rapid triage; keyboard and assistive-technology operators cannot open a client card reliably.
  - **Affected branches:** client list, complaint/support filters, delivery-blocked queue, follow-up triage, category UX, accessibility.
  - **Acceptance:** list summaries use Ukrainian labels and backend-provided objection labels, category badges remain explicit (including `Підтримка / скарга`), and each row supports Enter/Space with an accessible name; no raw legacy abbreviations remain.
  - **Tests:** DB-free template contract for localized labels, absence of raw abbreviations, role/tabindex/keyboard handler; production desktop/mobile browser proof remains required for the broader D11 item.

- [ ] **P1.D8d Remove mixed-language operator vocabulary from the bot cockpit.**
  - **Priority:** P1 — visible Ukrainian UX must not require an administrator to decode English infrastructure terms or raw enum values.
  - **Symptom:** overview/settings/statistics still showed `Heartbeat`, `Reasoning`, `Worker`, `live`, `AI`, `Ad ID`, `SKU`, `Legacy`, and raw follow-up/deal/signal statuses.
  - **Root cause:** internal enum and runtime terminology leaked directly into labels and compact detail HTML.
  - **Risk:** inconsistent Ukrainian interface, slower complaint/payment triage, and misinterpretation of analysis state or payment truth.
  - **Affected branches:** overview health, settings, knowledge-base links, client detail, statistics, follow-up/payment visibility.
  - **Acceptance:** all visible operator labels are Ukrainian; technical identifiers remain only where exact API/model names are necessary; internal status codes are mapped to localized labels with an honest unknown fallback.
  - **Tests:** DB-free contract asserts localized labels and rejects the retired visible terms; production desktop/mobile browser proof remains required before marking the UX slice complete.

- [ ] **P1.D10 Remove stored DOM-XSS paths from the cockpit.**
  - **Symptom/root cause:** the JavaScript escape helper omits quotes while untrusted avatar/name/invoice values are concatenated into HTML attributes and an inline `onerror` handler.
  - **Risk/branches:** stored DOM XSS in authenticated CRM through profile/catalog/payment-controlled strings; session/action compromise.
  - **Acceptance:** build nodes with `textContent`, validated `setAttribute`, safe URL scheme/host policy, and event listeners; remove inline handler construction and unsafe `innerHTML` data paths.
  - **Tests:** double/single quotes, markup, `javascript:`, data URLs, broken avatars, long names, malicious invoice URLs, CSP-compatible rendering.
  - **Implementation checkpoint:** client-list/detail avatar and invoice paths now use `safeHttpUrl` (`http/https` only), quote-aware escaping, `data-avatar` fallback listeners, and `rel="noopener noreferrer"`; full D10 closure still requires the remaining dynamic HTML paths and authenticated browser/CSP proof.

- [ ] **P1.D11 Add full responsive/accessibility browser acceptance.** Verify 1440/1280/768/390/320 widths, long Ukrainian text, long product/ad IDs, 200+ clients, responsive/scrolled tables, no overlap/clipping, keyboard-only client rows/tabs/actions, focus visibility, screen-reader state, empty/loading/error modes, and live countdown/status updates. Save desktop/mobile evidence for overview, offline/dead worker, client detail, filters, stats, key health, and Meta capability.

- [ ] **P1.D12 Add catalog/size/custom-print operator guidance.** Instructions and structured context must expose current product/SKU/price/availability and both oversize and future classic size-guide resources without scanning the entire site on every reply. Store guide type/product applicability/stable media ID/version, send concise links/media, and keep custom-print pricing/feasibility manager-authoritative when structured data is insufficient. Test stale catalog, classic/oversize choice, ambiguous fit, missing image fallback, custom garment constraints, Telegram/contact copy convenience, and no invented facts.

- [ ] **P1.D13 Fix proven global management-header horizontal overflow.**
  - **Symptom:** a real Chromium run at 1440 px reports document/header/workspace width about 1552 px; right-side status chips and page content extend beyond the viewport.
  - **Root cause:** the global management header keeps the full navigation and action group on one non-shrinking row without a bounded responsive fallback.
  - **Risk:** CRM controls are clipped or require horizontal page scrolling on common desktop widths; mobile verification cannot be trusted while the shared shell overflows.
  - **Affected branches:** management global header, workspace/main content, every CRM tab including notification review.
  - **Acceptance:** no document-level horizontal overflow at 1440/1280/768/390/320; navigation remains reachable, status is conveyed by text as well as colour, and page-local cards do not overlap.
  - **Tests:** real Chromium bounding-box/scroll-width assertions and screenshots with long Ukrainian outbox content at desktop/mobile widths.

#### P2.B — validation, governance, and handoff

- [ ] **P2.B1 Add a production-contract test profile.** Run MySQL/MariaDB engine-aware concurrency, FileBasedCache multiprocess singleton, Meta/Gemini mocked contracts, CAPI no-network fixtures, and migration engine assertions against the explicitly asserted production database contract. Local SQLite may be used only as developer feedback and must never close an acceptance checkbox. Document what each profile proves and fail closed before mutation when database identity differs.

- [ ] **P2.B2 Add read-only health and data-quality commands.** Report impossible axis combinations, paid-without-ledger, ledger-paid-without lifecycle update, stale analysis watermarks, conflicting facts, orphan snapshots/order links, hidden leakage, overdue queue/outbox/follow-ups, wrong table engines, cache singleton state, missing keys/secrets, and attribution without event/order evidence. Repair commands are separate, idempotent, and dry-run first.

- [ ] **P2.B3 Add privacy/retention/export governance.** Define access, retention, redaction, anonymization, export, and deletion for raw messages, evidence excerpts, media metadata, model outputs, order links, attribution touches, corrections, and audit history. Never expose secrets or unnecessary PII in list views/logs/exports.

- [ ] **P2.B4 Add outcome-driven experimentation controls.** Version scoring, prompt, follow-up, and discount policies; assign eligible cohorts with holdouts; measure verified conversion, refunds, complaints, and opt-outs; stop harmful variants. Do not optimize on reply rate or model score alone.

- [ ] **P2.B5 Reconcile this master plan after every slice.** A checkbox closes only when its exact code/tests/production evidence exist. Narrow `P1.A*` work must not falsely close broader `P1.*` acceptance. Keep SHA, migration, engine/cache, runtime, and browser evidence next to the checkbox. Remove stale handoff statements rather than leaving contradictory history.

### P1 — CRM truth and conversion behavior

- [ ] **P1.1 Versioned conversation analysis.** Add analysis snapshot/evidence/confidence model and deterministic fact extraction contract. Backfill only where evidence is available; label historical data as legacy. Commit/push/deploy.
- [ ] **P1.2 Replace misleading readiness percentage.** Introduce score bands and evidence display; keep compatibility field during migration; add calibration fixtures for 100 representative scenarios. Commit/push/deploy.
- [ ] **P1.3 Manager observation mode.** Ensure every manager echo is stored/classified, follow-ups cancel, no bot answer is generated, and resume is explicit/audited. Commit/push/deploy.
- [ ] **P1.4 Follow-up policy engine.** Implement the approved ladder, quiet hours, Meta window, max contacts, opt-out/no-buy hard stops, and atomic rescheduling. Commit/push/deploy.
- [ ] **P1.5 Follow-up UX countdown.** Show next action, due time, policy reason, deadline, cancellation/skip reason, and live countdown on the client card and queue. Commit/push/deploy.
- [ ] **P1.6 Payment truth and reconciliation.** Reconcile invoice, Monobank webhook/poll, deal, order, and purchase event idempotently. Test prepayment/full payment semantics and payment-link recovery. Commit/push/deploy.
- [ ] **P1.7 Current catalog and SKU evidence.** Version catalog context, invalidate price/availability cache, record product-match source/confidence, and prevent low-confidence paylinks. Commit/push/deploy.
- [ ] **P1.8 Media/vision safety.** Add URL/MIME/size/timeout allowlists, bounded downloads, ambiguous-match handling, and tests for images/links/stories/reels. Commit/push/deploy.
- [ ] **P1.9 Routed playbooks and prompt security.** Add versioned instruction validation, trusted/untrusted delimiters, jailbreak controls, and scenario fixtures in Ukrainian/Russian. Commit/push/deploy.
- [ ] **P1.10 Ad attribution and CAPI ledger.** Add first/last/assisted touch, stable event IDs, consent/match-data guards, and distinct verified-revenue stats. Commit/push/deploy.
- [ ] **P1.11 Conversion/admin cockpit.** Add evidence-driven client cards, funnel bands, follow-up queue, key health, console filters, and honest Meta delivery status. Commit/push/deploy.

### P2 — optimization and resilience

- [ ] **P2.1 Database/index/retention pass.** Add indexes, query plans, bounded retention commands, orphan/stuck-state repair, and MySQL production measurements. Commit/push/deploy.
- [ ] **P2.2 Provider and queue load controls.** Add global/per-client backpressure, deadlines, circuit breakers, and graceful manager escalation. Commit/push/deploy.
- [ ] **P2.3 Reporting/calibration loop.** Add weekly predicted-vs-paid calibration, follow-up lift, ad lag, opt-out/complaint monitoring, and manual correction audit. Commit/push/deploy.
- [ ] **P2.4 Browser UX polish and accessibility.** Finish responsive console, keyboard/focus states, empty/error/loading states, and screenshots at desktop/mobile. Commit/push/deploy.
- [ ] **P2.5 Operational documentation and handoff.** Update deploy, rollback, key rotation, Meta permission diagnosis, webhook verification, cron, privacy, and incident runbooks. Commit/push/deploy.
- [ ] **P2.6 Final chaos/regression pass.** Exercise provider outage, all keys exhausted, daemon kill/restart, duplicate webhook, Telegram failure, payment race, manager takeover, opt-out, media abuse, and rollback. Commit/push/deploy.

---

## 15. Test Matrix and Commands

### Local developer feedback per slice (never acceptance evidence)

```bash
cd /Users/zainllw0w/TwoComms/site
.venv/bin/python twocomms/manage.py check
.venv/bin/python twocomms/manage.py makemigrations --check --dry-run
.venv/bin/python twocomms/manage.py test \
  management.tests_ig_audit_fixes \
  management.tests_ig_bot_resilience \
  management.tests_ig_sales_automation \
  management.tests_ig_clients_ui \
  management.tests_gemini_keys
.venv/bin/python -m compileall -q twocomms/management twocomms/orders
git diff --check
```

Add the slice-specific test module to that command rather than relying on the full suite alone. Use mocked HTTP for Gemini/Meta/Telegram. Do not send live customer messages in tests.

These commands may use local SQLite only as quick developer feedback. Per the
owner's production-truth rule, their result cannot close any checkbox and is not
reported as production compatibility evidence.

### Complete related developer regression feedback

After the focused red/green cycle, run every `tests_ig_*`, `tests_gemini_*`, and
`tests_bot_*` module. This is mandatory even when the focused test passes:

```bash
cd /Users/zainllw0w/TwoComms/site
DJANGO_SETTINGS_MODULE=test_settings .venv/bin/python twocomms/manage.py test \
  $(rg --files twocomms/management \
    | rg '/tests_(ig|gemini|bot).*\.py$' \
    | sort \
    | sed -E 's#^twocomms/##; s#/#.#g; s#\.py$##')
```

The pre-change 2026-07-23 baseline is **375 tests passing**. This SQLite command
is optional developer feedback, not a deployment or acceptance gate. A passing legacy
suite does not replace new MySQL/FileBasedCache/browser/provider contract tests.
No command in this gate may send a real Meta message, Gemini customer response,
Telegram alert, payment request, order, or CAPI event.

### Required integration scenarios

- Same Meta webhook delivered three times: one queue row, one model reply, one CRM analysis.
- Bot sends a multi-chunk response: all own echoes ignored; manager's identical text to another client still pauses that client.
- Manager sends five messages: five stored/classified messages, one takeover alert, zero AI replies.
- Bot resumed, manager returns later: one new takeover alert and a new state epoch.
- Gemini 3.6 returns `STOP`, `MAX_TOKENS`, 429, 503, 404, safety block, and empty content across six keys.
- All keys unavailable: one customer-facing safe fallback/manager task per inbound message, one Telegram alert per failure epoch.
- Payment link generated, unpaid, paid by webhook, paid by poll, duplicate payment webhook, and order creation race.
- Follow-up due in quiet hours, outside Meta window, after opt-out, after manager takeover, and after payment.
- Catalog price changes after memory summary creation; response uses the current structured price.
- Image URL redirect/private host/oversize/non-image/ambiguous product.
- Prompt injection asking for secrets/tags/system prompt; no disclosure or unsafe control action.

### Browser checks

- Management bot page at desktop and mobile widths.
- Status transitions: disabled, starting, running, enabled-but-worker-missing, Meta delivery blocked.
- Client detail live updates, score evidence, manager state, follow-up countdown, payment truth, and console filtering.
- Secret inputs remain blank/masked after save and reload.

---

## 16. Deploy and Rollback Runbook

### Standard server sequence after each approved slice

```bash
IG_BOT_MAINTENANCE_ID=$(python manage.py run_instagram_bot --maintenance-on 1800 \
  | sed -n 's/^maintenance active lease_id=\([^ ]*\).*/\1/p')
test -n "$IG_BOT_MAINTENANCE_ID"
git pull --ff-only origin main
python manage.py verify_ig_production_contract \
  --expected-database qlknpodo_MySQL_DB
python manage.py migrate
python manage.py verify_ig_production_contract \
  --expected-database qlknpodo_MySQL_DB --rollback-fixtures
python manage.py check
python manage.py collectstatic --noinput
python manage.py compress --force
python manage.py seed_ig_bot_sales_playbooks
touch tmp/restart.txt
python manage.py run_instagram_bot --maintenance-off "$IG_BOT_MAINTENANCE_ID"
test "$(python manage.py shell -c 'from management.services.ig_maintenance import maintenance_status; print(maintenance_status()["active"])' | tail -n 1)" = "False"
python manage.py run_instagram_bot --ensure
python manage.py poll_ig_deal_payments --limit 5
```

For the **first rollout that introduces maintenance support**, the old in-memory
daemon cannot observe the new lease. Bootstrap once by taking the existing
`ig_bot_spawn.lock`, touching `restart.txt`, waiting for and holding
`ig_bot_daemon.lock`, then pull the new code and create the lease directly through
`management.services.ig_maintenance.activate_maintenance()` before releasing both
OS locks. Record the returned `lease_id`. Do not use the future standard sequence
until that bootstrap has completed. This closes the old-daemon/cron window without
customer, Telegram, Gemini, payment, order, or Meta sends.

Then verify:

- `git rev-parse --short HEAD` equals the pushed SHA.
- migrations are applied; no pending migration/check errors.
- Passenger responds, daemon process exists, cache heartbeat is fresh, DB heartbeat is fresh.
- `InstagramBotSettings` enabled/model/AI state is expected.
- key health has no accidental secret output; the intended probe/result is present.
- queue, follow-up, notification outbox, and error counts are sane.
- live endpoints return expected management/storefront status codes.

The deployment is incomplete until the slice also proves:

- exact production SHA equals the pushed SHA and the worktree/main diff is understood;
- required table engines match the concurrency contract;
- exactly one daemon PID owns the current SHA after concurrent `--ensure` checks;
- cache backend/health, DB heartbeat, daemon heartbeat, queue depth/oldest age,
  outbox due/failed oldest age, and follow-up backlog are sane;
- maintenance is explicitly inactive after release; a failed exact-token release
  stops the deploy instead of allowing `--ensure` to report a false success;
- configured, effective, and last-success Gemini model/key/task are distinguished;
- webhook signature health, local allowlist, token permission probe, account
  access level, and concrete recipient delivery history are separate facts;
- no live Meta/CAPI test event or customer message was sent without a new explicit
  authorization for that exact smoke;
- desktop and mobile browser evidence exists for every UX-affecting slice.

### Rollback

- Stop the daemon only if required to prevent duplicate sends.
- Revert the single slice commit or deploy the last known-good SHA; run migrations only forward unless a tested reversible migration exists.
- `touch tmp/restart.txt`, run `--ensure`, verify heartbeat and queue state, and record the reason in the incident log.
- Never delete production messages, deals, payment attempts, attribution events, or notification audit rows during rollback.

---

## 17. Open Decisions for Approval Before Implementation

The current prompt is sufficient to start P0 with the recommendations above. These choices should be confirmed before P1 policy work:

1. Keep the default automated send window at `10:00–19:00 Europe/Kyiv`, or extend it to a different business window?
2. Confirm the default follow-up ladder: payment `+45m` then next day; unanswered qualification `+2h` then next day; maximum two customer reminders; 5% then one capped 10% rescue only when eligible.
3. Confirm whether Meta CAPI feedback remains opt-in until match-data/consent is proven, with no live test events during implementation.
4. Confirm that `gemini-3.6-flash` is the only primary chat model and older models are fallback-only, not an equal round-robin.
5. Confirm the operator roles allowed to resume automation after a human takeover and to approve the final 10% rescue offer.

Until these are answered, use the recommended defaults and keep policy values configurable through validated admin settings rather than hardcoding them into prompts.

---

## 18. Handoff Summary (reconciled 2026-07-23)

Production is currently on the same SHA as local `main`; the daemon/watchdog are
running and both heartbeats are fresh. That does **not** mean the bot is ready to
reply publicly or that the CRM is trustworthy. The webhook app secret is absent,
Advanced Access/public-recipient delivery is unproven, configured model state is
stale, and production has no analysis snapshots/follow-up tasks/Meta event ledger.

The highest-priority implementation order is now:

1. P0.B1 ledger-only payment/fulfillment truth;
2. P0.B2/P0.B3 real FileBasedCache/MyISAM concurrency correctness;
3. P0.B4 autonomous alert outbox;
4. P0.B5 observation and analysis while reply automation is paused/stopped;
5. P1.C1 explicit Graph/permission/delivery capability;
6. structured memory, reversible score, multi-order/TTN linking, honest analytics,
   and the Ukrainian cockpit described by the remaining P1/P2 items.

The previously shipped takeover logic is correct sequentially and the prior
duplicate-alert symptom is covered by unit tests, but it must be reverified after
the production-engine concurrency fix. The first production chat requiring
reanalysis is already evidenced by message watermarks; do not manually promote it
to paid or auto-link an order without provider/order evidence.

This file remains the source of truth for future agents. A checkbox is never closed
from code inspection or a passing SQLite suite alone: it needs its focused tests,
complete related suite, checks/compile/diff gates, pushed SHA, production deploy,
runtime evidence, and browser proof where applicable.
