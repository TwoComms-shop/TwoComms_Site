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
- [x] **P0.2 Restore daemon liveness.** Harden `run_instagram_bot --ensure`, install guarded cron, fix absolute paths/locks, and expose honest offline/dead-worker states. Test concurrent ensure and heartbeat ages. Commit/push/deploy.
- [x] **P0.3 Deduplicate takeover Telegram alerts.** Add transition/epoch logic and regression tests for repeated manager echoes, resume, and concurrent events. Commit/push/deploy.
- [x] **P0.4 Add idempotent notification outbox.** Route permanent delivery, AI failure, spam, payment-link failure, takeover, and recovery alerts through one deduplicated mechanism. Verify Telegram call count and persisted status. Commit/push/deploy.
- [x] **P0.5 Make Gemini 3.6 authoritative.** Add `gemini-3.6-flash` to model/key policy, make the settings model effective, and add model-aware generation config/finish-reason handling. Run mocked tests plus the six-key read-only production probe. Commit/push/deploy.
- [x] **P0.6 Make key rotation health-aware.** All six configured keys now participate in role-prioritized fallback (own keys first, then borrowed keys); per-key cooldown remains isolated; Gemini 3.6 model-major ordering and telemetry are covered by 56 focused tests. Production SHA `857ac233` verified all six chat candidates with `state=running`; probe output exposed names/status only, never key values. Commit/push/deploy.
- [x] **P0.7 Fail closed on webhook signature in production.** Missing `IG_APP_SECRET` now rejects POSTs; only explicit `IG_BOT_ALLOW_UNSIGNED_WEBHOOKS=true` enables a development bypass. Status exposes `configured`, `unsigned_override`, `healthy`, and `state`; the missing-secret warning is bounded instead of emitted for every event. Production SHA `11b4f9cf` reports `missing_secret` with override disabled, so unsigned traffic is intentionally blocked until the real Meta secret is configured. Commit/push/deploy.
- [x] **P0.8 Guarantee no duplicate customer send.** Added `send_state`/timestamps and a conditional send boundary: `sending` is persisted before Meta I/O, success becomes `sent`, and timeout/5xx/partial delivery becomes `unknown` with automatic retry disabled. Stale processing rows that crossed the boundary are failed instead of requeued; post-send claim loss cannot replay the request. Migration `0081` is applied on production SHA `3853088a`; focused resilience/audit/e2e tests pass. Commit/push/deploy.
- [ ] **P0.9 Fix secret presentation and access boundaries.** Mask/write-only custom tokens, audit reviewer/admin permissions, and test that secrets never appear in HTML, JSON, logs, or errors. Commit/push/deploy.
- [ ] **P0.10 Production recovery verification.** Confirm daemon heartbeat, webhook health, queue drain, no new duplicate alerts, and a clean rollback point. Mark only after server evidence. Commit/push/deploy.

### Additional findings from the second audit (2026-07-22)

These findings were discovered while tracing the full queue/worker/provider path after the initial plan was written. They are now explicit delivery items rather than informal follow-up notes:

- [x] **P0.A0 Watchdog deploy-reload race.** A fresh heartbeat from a pre-deploy daemon could make an immediate `--ensure` skip the replacement process, and the old process could delete the new heartbeat during exit. Heartbeat now carries a timestamp/sentinel, watchdog compares PID and restart mtimes, and cleanup is owner-guarded. Production SHA `7159ae63` passed `touch restart.txt -> ensure -> sleep 8s` with `state=running` and both heartbeats fresh.
- [x] **P0.A1 Follow-up retry backoff.** Persisted `attempt_count`, `next_attempt_at`, and `last_error` now gate eligibility; transient failures use 5m/10m/20m/40m bounded backoff and then terminal skip, while unknown provider delivery is never retried. Recovery and no-hot-loop tests pass; migration `0082` is applied on production SHA `d0aeb6c2` (runtime reverified on `7159ae63`).
- [x] **P0.A2 Polling cursor/batch correctness.** Added durable `IgPollCursor` (migration `0083`), chronological batch processing, bounded paging, per-conversation cursor gating, webhook-mid dedup through the existing unique constraint, and attachment propagation. Production migration is applied on SHA `9b7610c3`; daemon status remains `running` after deploy.
- [x] **P0.A3 Model allowlist and authority.** Settings accept arbitrary model strings while the UI omits `gemini-3.6-flash`; enforce a provider allowlist, make the selected model effective, and expose configured versus actually used model.
- [x] **P0.A4 Fail-closed webhook verification.** Covered by P0.7: HMAC success/failure, missing-secret rejection, explicit development override, endpoint status, and configuration health tests are in `tests_ig_webhook_security.py`.
- [ ] **P0.A5 Durable Telegram notification delivery.** Manager/AI/delivery alerts have no persisted idempotency record or Telegram success check. Add an outbox keyed by client/event epoch and make delivery/retry state observable.
- [ ] **P1.A6 Gemini probe generation budget.** Tiny probes can return `MAX_TOKENS` with empty content because thinking consumes the output budget. Use model-aware thinking/output settings and classify finish reasons correctly in health checks.

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

### Local fast checks per slice

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
git diff --check
```

Add the slice-specific test module to that command rather than relying on the full suite alone. Use mocked HTTP for Gemini/Meta/Telegram. Do not send live customer messages in tests.

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
git pull --ff-only origin main
python manage.py migrate
python manage.py check
python manage.py collectstatic --noinput
python manage.py compress --force
python manage.py seed_ig_bot_sales_playbooks
touch tmp/restart.txt
python manage.py run_instagram_bot --ensure
python manage.py poll_ig_deal_payments --limit 5
```

Then verify:

- `git rev-parse --short HEAD` equals the pushed SHA.
- migrations are applied; no pending migration/check errors.
- Passenger responds, daemon process exists, cache heartbeat is fresh, DB heartbeat is fresh.
- `InstagramBotSettings` enabled/model/AI state is expected.
- key health has no accidental secret output; the intended probe/result is present.
- queue, follow-up, notification outbox, and error counts are sane.
- live endpoints return expected management/storefront status codes.

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

## 18. Handoff Summary

The immediate production blockers are known: the daemon is dead despite `is_enabled=True`, watchdog cron is missing, `IG_APP_SECRET` is absent, the configured model is not authoritative, and manager echo alerts are not transition-idempotent. The first implementation slices must restore liveness and eliminate duplicate alerts before any conversion optimization is trusted. Gemini 3.6 capability is already verified on all six production keys; the remaining work is safe routing, telemetry, model configuration, and regression coverage.

This file is deliberately the source of truth for future agents: it records current evidence, root causes, invariants, policy defaults, exact code surfaces, test scenarios, deploy commands, rollback constraints, and the checkbox-by-checkbox delivery contract.
