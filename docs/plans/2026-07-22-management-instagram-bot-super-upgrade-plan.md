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
- [ ] **P0.4 REOPENED — complete the notification outbox.** Unique dedupe rows and Telegram message IDs exist, but no autonomous worker drains failed rows. Add due time, bounded backoff/jitter, stale-`sending` recovery, terminal/dead-letter state, daemon/command drain, and operator visibility. Verify one-shot Telegram failure recovers without another business transition and never duplicates a confirmed message. Commit/push/deploy.
- [x] **P0.5 Make Gemini 3.6 authoritative.** Add `gemini-3.6-flash` to model/key policy, make the settings model effective, and add model-aware generation config/finish-reason handling. Run mocked tests plus the six-key read-only production probe. Commit/push/deploy.
- [x] **P0.6 Make key rotation health-aware.** All six configured keys now participate in role-prioritized fallback (own keys first, then borrowed keys); per-key cooldown remains isolated; Gemini 3.6 model-major ordering and telemetry are covered by 56 focused tests. Production SHA `857ac233` verified all six chat candidates with `state=running`; probe output exposed names/status only, never key values. Commit/push/deploy.
- [x] **P0.7 Fail closed on webhook signature in production.** Missing `IG_APP_SECRET` now rejects POSTs; only explicit `IG_BOT_ALLOW_UNSIGNED_WEBHOOKS=true` enables a development bypass. Status exposes `configured`, `unsigned_override`, `healthy`, and `state`; the missing-secret warning is bounded instead of emitted for every event. Production SHA `11b4f9cf` reports `missing_secret` with override disabled, so unsigned traffic is intentionally blocked until the real Meta secret is configured. Commit/push/deploy.
- [x] **P0.8 Guarantee no duplicate customer send.** Added `send_state`/timestamps and a conditional send boundary: `sending` is persisted before Meta I/O, success becomes `sent`, and timeout/5xx/partial delivery becomes `unknown` with automatic retry disabled. Stale processing rows that crossed the boundary are failed instead of requeued; post-send claim loss cannot replay the request. Migration `0081` is applied on production SHA `3853088a`; focused resilience/audit/e2e tests pass. Commit/push/deploy.
- [x] **P0.9 Fix secret presentation and access boundaries.** Custom Direct/Gemini credentials are write-only password fields with explicit presence indicators; blank saves preserve existing values and explicit clear flags are admin-only. Status JSON exposes no custom values, token-like query parameters are redacted in diagnostics, and 17 privacy/secret tests pass. Production SHA `2974501d` reports no `custom_*` fields in status and daemon `running`. Commit/push/deploy.
- [ ] **P0.10 Production recovery verification.** Confirm daemon heartbeat, webhook health, queue drain, no new duplicate alerts, and a clean rollback point. Mark only after server evidence. Commit/push/deploy.

### Additional findings from the second audit (2026-07-22)

These findings were discovered while tracing the full queue/worker/provider path after the initial plan was written. They are now explicit delivery items rather than informal follow-up notes:

- [x] **P0.A0 Watchdog deploy-reload race.** A fresh heartbeat from a pre-deploy daemon could make an immediate `--ensure` skip the replacement process, and the old process could delete the new heartbeat during exit. Heartbeat now carries a timestamp/sentinel, watchdog compares PID and restart mtimes, and cleanup is owner-guarded. Production SHA `7159ae63` passed `touch restart.txt -> ensure -> sleep 8s` with `state=running` and both heartbeats fresh.
- [x] **P0.A1 Follow-up retry backoff.** Persisted `attempt_count`, `next_attempt_at`, and `last_error` now gate eligibility; transient failures use 5m/10m/20m/40m bounded backoff and then terminal skip, while unknown provider delivery is never retried. Recovery and no-hot-loop tests pass; migration `0082` is applied on production SHA `d0aeb6c2` (runtime reverified on `7159ae63`).
- [x] **P0.A2 Polling cursor/batch correctness.** Added durable `IgPollCursor` (migration `0083`), chronological batch processing, bounded paging, per-conversation cursor gating, webhook-mid dedup through the existing unique constraint, and attachment propagation. Production migration is applied on SHA `9b7610c3`; daemon status remains `running` after deploy.
- [x] **P0.A3 Model allowlist and authority.** Settings accept arbitrary model strings while the UI omits `gemini-3.6-flash`; enforce a provider allowlist, make the selected model effective, and expose configured versus actually used model.
- [x] **P0.A4 Fail-closed webhook verification.** Covered by P0.7: HMAC success/failure, missing-secret rejection, explicit development override, endpoint status, and configuration health tests are in `tests_ig_webhook_security.py`.
- [ ] **P0.A5 REOPENED — durable Telegram notification delivery.** Persistence/dedupe is implemented, but autonomous recovery is not. Close only together with reopened P0.4 after restart, concurrent-drain, timeout-ambiguity, missing-credential, and dead-letter tests plus production outbox evidence.
- [x] **P1.A6 Gemini 3.6 model-aware generation and health probe.** Context7 confirms `gemini-3.6-flash` as an official model and documents `thinkingConfig.thinkingLevel` for 3.6 plus separate thought/output usage. The current chat payload always sends legacy `thinkingBudget=0`, and the planned six-key probe command is absent. Added model-specific generation normalization (`thinkingLevel=low` for 3.6, compatible settings for older fallbacks), redacted `probe_ig_gemini_pool --role chat --model gemini-3.6-flash --parallel 2`, persisted bounded probe telemetry, and correct `STOP`/`MAX_TOKENS`/`SAFETY`/empty-content classification. Production `10586cd6`: all six keys returned HTTP 200/STOP for `gemini-3.6-flash`; 69 targeted tests, `check`, and migration check passed. A `200 + MAX_TOKENS` probe proves reachability but not a usable answer and does not quarantine the model/key. Commit/push/deploy.

### Additional findings from the Context7/API contract audit (2026-07-23)

Context7 sources used for this pass: official Meta Graph API reference and official Gemini API documentation. Documentation evidence is treated as a contract input, while production behavior and mocked contract tests remain the acceptance authority.

- [x] **P0.A6 Complete conversation discovery instead of monitoring only ten threads.** Meta `/conversations` is paginated, but `refresh_conv_ids()` requests `limit=10` and stores only the first page. This can silently exclude older conversations from polling/analysis when more than ten active threads exist. Implemented bounded pagination (10 pages/500 validated IDs), deduplication, page/ID/Graph-host validation, page-cycle protection, page-scoped cache keys, and cold-cache nonblocking behavior. Production `37b01440` is running; 84 IG regression tests pass.
- [x] **P0.A7 Respect Meta conversation rate limits and partial-page failure semantics.** Context7 documents a 2 requests/second limit per Instagram professional account for Conversations API. Implemented 0.5s page pacing, distributed refresh lock, no partial snapshot publication, stale-cache recovery on 429/5xx/malformed page, and a `refresh_pending` result instead of blocking the daemon. Production `37b01440` is running with fresh heartbeat and zero pending queue/outbox; 84 IG regression tests pass.
- [x] **P0.A8 Verify tagged-send policy against current Instagram Messaging eligibility.** Context7's official Instagram Platform source describes `HUMAN_AGENT` as a human-support response for complex issues up to seven days, while normal API responses remain inside 24 hours; it did not establish automated sales or shipment reminders as eligible. `send_text_tagged()` therefore fails with `policy` before token/provider I/O unless the caller explicitly supplies `human_authored=True`, and only the `HUMAN_AGENT` tag is accepted. Shipment automation now uses ordinary `RESPONSE` only inside a conservative 23-hour window; outside it, or after ambiguous/permanent delivery, it creates one visible skipped manager-task with the prepared TTN text and a retryable deduplicated Telegram alert. It never marks `shipped_notified_at` without confirmed delivery and never auto-retries an unknown result. Commit `1a3d48d1` passed 7 shipment policy tests, 376 IG/Gemini/chat tests, `check`, migration drift, compile, and diff gates. Production SHA `1a3d48d1` rejected an automated tagged smoke with zero token/HTTP calls; no eligible shipment required an action, queue/outbox stayed empty, heartbeats were fresh, and effective model remained `gemini-3.6-flash`. No live customer message was sent.
- [ ] **P0.A9 Make Graph version/permission capability explicit.** All Meta requests must remain centrally versioned through `v25.0`; add contract tests that reject unversioned Graph calls in the bot path. Keep three separate facts in status/UI: local sender allowlist, token/permission capability, and actual recipient delivery result. App roles are a test-only capability and must never be shown as equivalent to Advanced Access for public users.
- [ ] **P1.A7 Audit webhook field coverage against v25.** Build fixtures for `messages`, `messaging_postbacks`, echoes, referrals, attachments, deletes/unsupported events, duplicate `mid`, and out-of-order delivery. Unknown-but-valid fields must be ignored and counted, not crash the worker or create a customer response.
- [ ] **P1.A8 Add provider-level rate and quota observability.** Context7 documents separate limits for Conversations (2 rps), text/link Send API (100 rps), and audio/video Send API (10 rps). Add endpoint-class counters, bounded 429 backoff, last rate-limit class/time, and an operator-visible degraded state. Do not infer remaining Meta quota when response headers do not prove it.
- [ ] **P1.A9 Keep Meta attribution and CAPI claims evidence-bound.** Referral/ad metadata may establish an attribution touch, not a verified purchase. A `Purchase` remains gated by verified payment/order truth, uses a stable dedupe event ID, and live CAPI test events remain disabled without explicit authorization. Add fixtures separating referral, checkout intent, payment promise, payment pending, and provider-verified payment.
- [ ] **P2.A1 Add an API-contract review gate.** Before a Graph/Gemini version bump, run mocked request/response fixtures for endpoints, fields, error codes, finish reasons, rate limits, and permission wording; record the checked documentation date and deployed API/model identifiers. This prevents a constant-only version bump from being mistaken for compatibility.

### Additional findings from the Context7 reasoning/CRM analytics audit (2026-07-23)

These items preserve the complete customer-intelligence, paused-chat analysis, statistics, and operator-UX scope requested on 2026-07-23. They supplement, rather than replace, P1.1-P1.11. Evidence must stay separated from inference: language/tone can be observed, but ethnicity, nationality, personality, or ability to pay must not be asserted without explicit conversation/order evidence.

- [x] **P0.A10 Add task-based Gemini reasoning routing.** Replace the global `low`/legacy `thinkingBudget=0` behavior with the versioned task matrix in §3.4: customer chat is at least `medium`; product/size/media/payment/order/conversion decisions use `high`; probes remain `low`. Convert tasks to a tested Gemini 2.5 fallback budget without ever sending both controls. For Gemini 3.x remove explicit `temperature`/`topP`/`topK` so the provider's reasoning-optimized defaults remain effective; preserve compatible sampling on 2.5 fallbacks. Return and persist bounded task/level/policy/token telemetry, never thought text. Acceptance met: 297 focused IG/Gemini tests, `check`, migration check, compile, and production SHA `bcc0431e`; migration `0085` applied, all six keys returned HTTP 200/STOP for `gemini-3.6-flash`, production chat payload is `medium`, payment payload is `high`, and daemon/queue/outbox are healthy.
- [x] **P0.A11 Make the selected chat model authoritative for pooled keys.** `_run_with_pool()` now passes one validated model chain into both manual and pooled-key paths; non-default allowed primary selection and fallback ordering are covered by regression tests. Production SHA `27c389ac` confirmed `gemini-2.5-flash` becomes the first pooled candidate when selected, with 12 expected key/model candidates, while daemon/heartbeats/queue/outbox remained healthy. The normal configured primary remains `gemini-3.6-flash`.
- [x] **P0.A12 Harden per-conversation message pagination.** `poll_ingest()` now validates cached conversation IDs and every nested message/time/sender/text/attachment/paging field, rejects non-string bounded IDs, allows only centrally versioned `graph.facebook.com/v25.0` page URLs, detects cycles, and publishes messages/cursor movement only after a complete usable page chain. Each provider call is capped at 5 seconds; one poll is capped at 40 requests/20 seconds and persists a round-robin cache offset so older chats cannot starve. Commit `a96302ed` passed 19 polling tests, 374 IG/Gemini/chat tests, `check`, migration drift, compile, and diff gates. Production polling is enabled and running on SHA `a96302ed`; a mocked hostile-host smoke made one v25 request and rejected the next URL without Meta I/O, then live daemon observation showed two validated cached conversations/two cursors, no recent polling safety warnings, fresh heartbeats, empty queue/outbox, and no `last_error`. No live customer test message was sent.
- [x] **P1.A11 Persist versioned customer-intelligence snapshots.** Added idempotent append-only `IgConversationAnalysisSnapshot` records keyed by message/rules watermark with band, bounded heuristic purchase estimate, independent confidence, structured role/message evidence, uncertainties, last analyzed message, model/rules version, latency, and trigger. Manager messages are labeled as manager evidence and no longer inflate customer readiness; payment intent remains unverified rather than paid; legacy `buying_readiness` is visibly labeled as fallback. Production migration `0086` required `db_constraint=False` because both referenced legacy tables are MyISAM; the empty partial table from the first failed DDL was verified and removed, then production SHA `ad4d02fb` passed an InnoDB snapshot insert/read smoke (`exploring`, `0.2800`) with daemon/queue/outbox healthy. Historical backfill and high-reasoning reanalysis remain open under P1.1/P1.A14.
- [ ] **P1.A12 PARTIAL — build the complete interaction taxonomy.** The first version added reaction/info/product/size/custom/price/high-intent/payment/no-buy/opt-out/spam/manager classes and correctly avoids replying to emoji-only text. It still lacks collaboration/creator/advertising, wholesale/B2B, support/complaint, community/casual/meme, and real reaction-webhook coverage; `opt_out` is also collapsed into the `lost` band. Preserve the shipped behavior and add the missing classes, independent opt-out state, Ukrainian labels, fixtures, and production-safe backfill.
- [ ] **P1.A13 Analyze paused and manager-led conversations without auto-reply.** Customer and manager messages must update deterministic facts and queue analysis even while automation is paused. Manager text is labeled as manager evidence, cancels unsafe follow-ups, and can never prove customer intent or trigger a generated customer reply. Add paused/resumed/takeover race fixtures.
- [ ] **P1.A14 Add idempotent event-triggered and delayed reanalysis.** Run cheap deterministic extraction once per new message, coalesce high-reasoning work by conversation/message watermark, and support configurable hourly/nightly reconciliation for changed chats. Record trigger, due time, lease, attempts, token usage, last analyzed watermark, and skip reason so pause/restarts cannot duplicate cost.
- [ ] **P1.A15 Store structured commercial memory with provenance.** Track requested product/variant, size, color, quantity, custom-print brief, language, desired purchase time, delivery need, objections, price sensitivity evidence, likes/dislikes, last promise, and next action. Every value needs source role/message/time, confidence, conflict state, and expiration where catalog facts can change.
- [ ] **P1.A16 Replace cumulative readiness inflation with reversible scoring.** Deduplicate repeated signals; allow later refusal, silence, unavailability, or verified payment to move state in either direction; separate deterministic hard facts from model inference; show probability and confidence independently. Add at least 100 representative Ukrainian/Russian fixtures including terse speech, sarcasm, mixed language, reactions, abuse, custom print, stock/size ambiguity, promise-to-pay, and verified payment.
- [ ] **P1.A17 Make payment and revenue truth dominate prediction.** Generated link, promise, screenshot, and `payment_pending` remain intent evidence only. Green/paid state, revenue, conversion, and product attribution require the verified provider/deal/order ledger. Reconcile refunds/cancellations and keep full-payment/prepayment as one purchase with separate paid value.
- [ ] **P1.A18 Exclude hidden clients from every operational aggregate.** Centralize the active-client scope and contract-test all overview, funnel, product, objection, language, campaign, drop-off, follow-up, revenue, and export queries. Hidden records remain available only in an explicitly selected archive view and never silently affect denominators.
- [ ] **P1.A19 Add funnel/drop-off and product-demand analytics.** Date-range reports must show unique conversations, qualified/high-intent/checkout/verified-paid counts, product/variant interest, objection and loss reason, unanswered stage, language, ad/referral touch, follow-up recovery, verified revenue, and sample size/denominator. Popularity means evidenced interest; sales performance means verified orders/revenue.
- [ ] **P1.A20 Add prediction calibration and honest precision.** Compare each saved prediction with later verified paid/lost outcomes; report false-high, false-low, Brier/calibration bands, sample size, drift by prompt/rules version, and manual corrections. Display one decimal place only when the denominator supports it; never imply `0.1%` accuracy from a tiny sample.
- [ ] **P1.A21 Redesign client-list and detail UX around evidence.** Use green only for verified paid/order state, yellow for high intent/payment pending, red for explicit no-buy/opt-out/spam/lost, and neutral styling for exploration/information/reaction-only. Cards show band, probability, confidence, payment truth, top evidence/uncertainty, product, next action, follow-up countdown, last analysis, and whether bot/manager/paused observation owns the chat. Color is supplemented by icons/text and never presented as a psychological certainty.
- [ ] **P1.A22 Expand statistics UX and drill-down.** Add accessible date controls and filters for stage, product, ad/campaign, objection, language, category, payment truth, and hidden/archive scope; every chart drills into the exact client cohort and states its numerator/denominator. Add empty/loading/error states, responsive tables, keyboard/focus support, and desktop/mobile browser screenshots.
- [ ] **P1.A23 Add operator correction and audit workflow.** Let authorized staff correct product, category, objection, band, next action, and false model inference with a required reason. Store before/after/actor/time, feed corrections into calibration, and never overwrite verified payment/order facts through this UI.
- [ ] **P1.A24 Link attribution without overstating causality.** Preserve referral/ad/campaign first/last/assisted touches and conversation entry context. Reporting may segment outcomes by touch but must label attribution model and cannot claim an ad caused a purchase solely because its referral was present.
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

- [ ] **P0.B4 Add autonomous notification outbox drain and recovery.**
  - **Symptom:** one Telegram timeout/500 leaves a critical alert failed forever unless the same business transition calls `notify_manager()` again.
  - **Root cause:** the request path creates and sends outbox rows synchronously; daemon/status never claims due failed rows.
  - **Risk:** silent loss of takeover, payment, AI outage, shipment review, delivery block, and spam escalation alerts.
  - **Affected branches:** every `IgBotNotification` event producer, daemon loop, deploy/restart, cockpit health.
  - **Acceptance:** autonomous due-row drain, `next_attempt_at`, bounded exponential backoff+jitter, stale-sending recovery, timeout ambiguity policy, confirmed-message idempotency, dead-letter state and Ukrainian operator action.
  - **Tests:** one-shot failure then recovery without new event, restart, concurrent drains, missing credentials, stale sending, ambiguous timeout, confirmed Telegram message ID.

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

- [ ] **P1.B4c Do not render unavailable outbox telemetry as zero.**
  - **Symptom:** backend `None` values become `0` through JavaScript fallback and the cockpit claims an empty healthy outbox.
  - **Root cause:** UI conflates unavailable data with a measured zero.
  - **Risk:** operators miss migration failures and database outages.
  - **Affected branches:** status API error fallback and overview rendering.
  - **Acceptance:** unavailable counts render as `Дані недоступні` with a textual warning independent of colour; measured zero remains distinct.
  - **Tests:** null telemetry render contract and normal zero/non-zero payloads.

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
  - [ ] **P0.B5a — observation/reply boundary:** webhook and fallback polling persist eligible inbound while global/per-client reply is paused, run deterministic evidence extraction, mark the row observed without a reply backlog, suppress typing/Gemini/Meta/follow-up, and atomically drain pre-stop pending rows to observed state.
  - [ ] **P0.B5b — coalesced high-reasoning analysis:** add a durable per-client watermark/debounce job that analyzes the whole changed conversation after the manager/customer burst, stores model/version/reasoning/confidence/evidence/analyzed_at, retries safely across six-key project-aware pools, and never grants reply permission.

- [ ] **P0.B6 Fail closed for Meta data-deletion signed requests.**
  - **Symptom:** the public data-deletion callback accepts a syntactically valid signed request when the app secret is absent.
  - **Root cause:** HMAC validation runs only inside `if app_secret`.
  - **Risk:** forged audit/receipt rows and false compliance signals; current callback is not destructive, but fail-open verification is still incorrect.
  - **Affected branches:** public deletion callback, audit receipts, privacy incident response.
  - **Acceptance:** missing secret rejects the signed callback with a safe status; explicit local-test override is isolated; manual authenticated deletion remains available; no secret/raw signed payload in logs.
  - **Tests:** missing secret, valid/invalid HMAC, malformed payload, replay/idempotency, manual deletion unaffected.

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

- [ ] **P1.D9 Apply semantic colors without using color as the sole carrier.** Green only for verified paid/delivered truth, yellow for high intent/payment pending, blue for shipped/in transit, neutral for information/exploration, red for opt-out/lost/abuse/blockers. Waiting shipment is not “complete.” Every chip has text/icon/state semantics; funnel completion is not inferred from mutable stage.

- [ ] **P1.D10 Remove stored DOM-XSS paths from the cockpit.**
  - **Symptom/root cause:** the JavaScript escape helper omits quotes while untrusted avatar/name/invoice values are concatenated into HTML attributes and an inline `onerror` handler.
  - **Risk/branches:** stored DOM XSS in authenticated CRM through profile/catalog/payment-controlled strings; session/action compromise.
  - **Acceptance:** build nodes with `textContent`, validated `setAttribute`, safe URL scheme/host policy, and event listeners; remove inline handler construction and unsafe `innerHTML` data paths.
  - **Tests:** double/single quotes, markup, `javascript:`, data URLs, broken avatars, long names, malicious invoice URLs, CSP-compatible rendering.

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
