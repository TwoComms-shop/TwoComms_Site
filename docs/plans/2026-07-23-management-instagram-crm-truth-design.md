# Management Instagram CRM Truth Redesign

**Status:** approved by owner direction on 2026-07-23; implementation must follow the master checklist in `2026-07-22-management-instagram-bot-super-upgrade-plan.md`.

**Goal:** turn the existing Instagram bot cockpit into a production-safe CRM and analysis system that observes every eligible conversation, never invents payment or fulfillment truth, explains every inference, and gives operators clear Ukrainian-language actions and statistics.

## 1. Why the current single-stage model is insufficient

The current `IgClient.stage` mixes four unrelated facts:

1. what the customer appears to want;
2. whether payment is actually verified;
3. whether an order exists or has shipped;
4. whether the bot may reply.

That creates unsafe transitions. A model control tag can currently set `paid`, statistics trust that stage, and follow-ups stop even when no provider payment exists. Conversely, a customer may have paid or placed an order in a manager-led conversation while the CRM remains `new` because paused conversations are not reanalysed.

The redesign separates these facts and preserves append-only evidence for every material transition.

## 2. Production evidence behind this design

Read-only SSH evidence collected on 2026-07-23:

- production and local SHA both equal `fa0b6a9b42aa64211f5cc6f7301a0c7d0fbb6443`;
- management migrations through `0087` are applied and `manage.py check` passes;
- daemon is running with fresh DB and cache heartbeats; queue and notification outbox are empty;
- webhook verification is fail-closed but unhealthy because `IG_APP_SECRET` is absent;
- direct token and page token exist; `/v25.0/me/permissions` reports `instagram_manage_messages=granted`, but this does not prove Advanced Access or public-recipient delivery;
- configured cache backend is `FileBasedCache`, not Redis;
- `IgClient`, `InstagramBotMessage`, `IgFollowUpTask`, and `IgDeal` tables are MyISAM; snapshot and notification tables are InnoDB;
- production contains 58 CRM clients, of which 37 are hidden, but zero analysis snapshots, zero Meta feedback events, and zero follow-up tasks;
- the configured model field still contains `gemini-3-flash-preview`, while normalization selects `gemini-3.6-flash`; no successful analysis model/key/reasoning telemetry exists;
- one active paused manager-led conversation contains order intent, delivery details, full-prepayment wording, and a manager order acknowledgement, but remains `stage=new`, has no snapshot, and has no exact phone-linked `Order` candidate.

Local baseline before documentation changes: all 375 `tests_ig_*`, `tests_gemini_*`, and `tests_bot_*` tests pass. The new checklist therefore requires production-contract and negative tests not represented by the old suite.

## 3. Canonical CRM axes

### 3.1 Interaction and commercial stage

This is inference/routing state, not payment truth:

- `information` — asks for facts without evidenced purchase interest;
- `interest` — evidenced product, size, price, custom-print, collaboration, or service interest;
- `high_intent` — explicit order-ready actions, concrete variant/quantity/delivery choice, or a bounded score supported by evidence;
- `payment_pending` — verified invoice/payment step exists, but payment is not confirmed;
- `lost` — explicit refusal, terminal unavailability, or operator-confirmed loss;
- `opt_out` — explicit request not to contact; independent from lost and preserved after purchase;
- `unknown` — insufficient evidence.

`paid` and `waiting_shipment` may be displayed as lifecycle summaries, but they must be derived from the payment and fulfillment axes rather than written by Gemini.

### 3.2 Payment truth

Only payment/provider/order reconciliation may change this axis:

- `none`;
- `invoice_created`;
- `pending`;
- `verified_prepayment`;
- `verified_full_payment`;
- `refunded`;
- `reversed_or_disputed`;
- `unknown_requires_review`.

A promise, screenshot, manager statement, generated URL, or model inference is evidence of intent only. It never becomes verified payment.

One purchase remains one purchase for prepayment and full payment. Store both full discounted order value and actual paid value separately.

### 3.3 Fulfillment truth

Only linked order and shipment records may change this axis:

- `no_order`;
- `order_linked`;
- `preparing`;
- `waiting_shipment`;
- `shipped`;
- `delivered`;
- `cancelled`;
- `returned`;
- `ambiguous_requires_review`.

A TTN is attached only through an explicitly linked order. A client may have zero, one, or many linked orders.

### 3.4 Automation and capability state

These facts remain independent:

- observation/analysis enabled;
- automatic reply enabled globally;
- automatic reply enabled for this client;
- manager takeover/owner;
- hidden/archive state;
- daemon alive;
- webhook healthy;
- local allowlist allows the sender;
- token permission probe result;
- account access level: `standard_test`, `advanced_public`, or `unknown`;
- last delivery result for the concrete recipient;
- Gemini generation capability.

The UI must never replace this matrix with one green “online and replies” label.

## 4. Event and analysis pipeline

1. Webhook or polling stores a durable normalized event exactly once.
2. Deterministic extraction runs once per message and records role-aware facts.
3. A conversation watermark schedules/coalesces analysis work.
4. Reply automation checks policy independently and may stop without cancelling analysis.
5. High-reasoning analysis reads the changed conversation window, structured catalog/order facts, prior facts, and manager/customer role provenance.
6. The system stores an append-only snapshot with model, key alias, reasoning task/level, prompt/rules version, analyzed watermark, token use, latency, confidence, evidence, conflicts, and uncertainties.
7. Material stage/score/next-action changes write append-only history.

Global Stop, client pause, and manager takeover block typing, generation of customer replies, Send API, and follow-ups. They do not discard inbound messages or disable scheduled analysis. Resume must not replay old messages as customer replies.

## 5. Structured commercial memory

Every memory fact is an entity with provenance, not an overwritten JSON value. Required fact types include:

- language and communication preference;
- requested product stable ID/SKU and variant;
- size, fit, color, quantity;
- custom-print garment, artwork state, placement, dimensions, technique, deadline, quantity, and handoff need;
- collaboration, creator/advertising, wholesale/B2B, support/complaint, community/casual categories;
- desired purchase time and deadline;
- delivery city/branch need without exposing unnecessary PII in list views;
- objections and price-sensitivity evidence;
- likes/dislikes;
- last customer promise;
- next best action and due time.

Each fact stores source message, source role, observed time, confidence, deterministic/model/operator origin, model/rules version, conflict state, superseded link, and expiry where catalog facts can become stale.

Manager statements can establish an operator observation or structured order fact, but never customer intent or provider payment truth by themselves.

## 6. Reversible scoring

The conversion estimate is a versioned prediction, not “solvency” and not a psychological diagnosis.

- Deduplicate repeated evidence by semantic key and message watermark.
- Positive and negative evidence can move the estimate in both directions.
- Hard facts and model inference remain separate.
- Probability and confidence are independent.
- Verified payment closes the prediction outcome but is not produced by the prediction.
- Language, nationality, ethnicity, writing style, grammar, and perceived wealth are prohibited features.
- Price sensitivity is recorded only from explicit price/affordability evidence, not inferred from identity or manner of writing.

The UI shows band, bounded probability, confidence, top positive/negative evidence, uncertainty, version, and analyzed time. Calibration uses later verified outcomes and reports sample size, Brier score, false-high/false-low, and drift by version.

## 7. Taxonomy and reply policy

Required interaction classes include:

- reaction-only;
- casual/community/meme;
- information-only;
- product interest;
- size/fit;
- custom print;
- collaboration/creator/advertising;
- wholesale/B2B;
- price objection;
- trust/prepayment/delivery objection;
- high intent;
- payment pending;
- support/complaint;
- explicit no-buy;
- opt-out;
- spam/abuse;
- manager observation;
- unknown.

Reaction-only events do not receive a generated response. Abuse uses a versioned warning/escalation policy; it does not infer intent from profanity alone. Discounts are policy-controlled actions, never prompt-only suggestions. A 10% rescue offer is rare, operator-visible, capped, auditable, and available only after explicit eligibility evidence and earlier non-discount handling.

## 8. Order and TTN linking

Create an append-only `IgClientOrderLink`-style record. Candidate generation may use exact normalized phone as the primary key and corroborate with name, city, Nova Poshta branch, time window, products, amount, deal, checkout/payment IDs, and manager confirmation.

- Never auto-bind on fuzzy name/address alone.
- Shared phones, conflicting candidates, or weak evidence remain review-only.
- Store matcher version, confidence, evidence, rejected candidates, actor, link/unlink time, and reason.
- One client can link to multiple orders; one order has at most one active IG client link unless an explicit reviewed exception exists.
- UI shows each order, items, order value, paid value, payment truth, order state, TTN, and shipment notification state independently.
- Telegram order alerts carry stable correlation IDs for client, deal/payment attempt, and order.

## 9. Meta attribution and CAPI

Referral/ad data is an attribution touch, not causal proof and not payment proof. Preserve immutable first, last, and assisted touches with stable ad/product identifiers.

CAPI remains disabled by default until consent/match-data/event-dedupe requirements are verified. No live test event may be sent without a separate explicit authorization.

The existing IG Purchase sequence must be redesigned so that:

1. provider payment is verified;
2. the order is materialized or linked;
3. one stable Purchase event is prepared;
4. full order value and paid value have explicit semantics;
5. retry is idempotent;
6. refund/cancellation is reconciled;
7. test mode actually forwards a validated test-event code and cannot silently become live mode.

Statistics may segment outcomes by attribution touch and chosen model. It must not claim that an ad caused a purchase without experimental evidence and spend data.

## 10. Statistics contract

Count separately:

- unique users;
- conversations/threads;
- inbound, manager, bot, and follow-up messages;
- qualified/high-intent users;
- invoices/payment attempts;
- verified payment transactions;
- paying users;
- orders;
- order value;
- actual paid value;
- refunds/returns;
- shipments/deliveries.

Every metric declares numerator, denominator, exclusions, timezone (`Europe/Kiev`), date semantics, attribution mode, unknown count, and sample size. Date modes include first-contact cohort, interaction-event date, payment date, order date, and shipment date.

Filters: custom from/to date, stage, payment truth, fulfillment truth, product stable ID/SKU, source/ad/campaign, manager/owner, objection, category, language, follow-up outcome, and explicit archive scope. Hidden users remain stored but are excluded from all operational aggregates unless the operator explicitly selects archive analysis.

Product interest is not sales performance. Verified orders/revenue are not inferred popularity. Objection rates use distinct eligible users plus an event drill-down; repeated signals cannot silently inflate the user denominator.

## 11. Ukrainian operator UX

The cockpit uses calm, work-oriented Ukrainian language. Raw enums such as `Paid`, `legacy`, `conf`, `FU`, `Rescue offer`, and `worker` are not user-facing.

The list row shows:

- interaction stage;
- payment truth;
- fulfillment truth;
- analysis/reply owner;
- product;
- next action with reason;
- full date and live countdown;
- capability blocker when applicable.

Color supplements text and icons:

- green only for verified paid/delivered facts;
- yellow for high intent or payment pending;
- blue for shipped/in transit;
- neutral for information/exploration;
- red for opt-out, lost, abuse, or blocking errors.

Waiting shipment is not presented as complete. All settings include plain-language help, especially Meta feedback/test mode, polling, allowlist, AI analysis, and reply automation.

DOM rendering must use safe node APIs, validated URLs, keyboard semantics, and no inline event handler construction. Browser acceptance covers 1440, 1280, 768, 390, and 320 px, long Ukrainian strings, long product/ad identifiers, 200+ clients, responsive tables, focus/keyboard use, and no clipping/overlap.

## 12. Runtime and storage architecture

Correctness must not depend on Redis being installed.

- Daemon singleton uses OS `flock` or a database-backed lease that is atomic on production storage.
- Critical state tables that require transactions/row locks migrate to InnoDB, or algorithms use explicit atomic conditional updates/unique constraints proven on MySQL.
- Notification outbox has an autonomous drain with bounded backoff, stale-send recovery, and dead-letter visibility.
- Queue health exposes oldest pending age, processing age, throughput, last item, and failures.
- Routine retention never deletes hidden/paid/order/audit truth. Archive/anonymization and privacy erasure are separate governed operations.
- Raw message/content retention is per-client/time based, dry-run capable, and preserves referenced evidence or records an explicit redaction tombstone.

## 13. Gemini reasoning policy

Use `gemini-3.6-flash` through the existing six-key role pools, with project grouping added so multiple aliases from one quota project do not create false fallback capacity.

- `health_probe`: low;
- `customer_chat`: medium;
- deterministic extraction: no model where rules suffice;
- product/size/catalog/media/payment/order decisions: high;
- customer intelligence/conversion/reanalysis/conflict resolution: high;
- routine memory/report summary: medium, escalating to high only by a recorded complexity rule.

Gemini 3 uses `thinkingLevel`; Gemini 2.5 uses the versioned `thinkingBudget` mapping; never send both. Deadlines use remaining-time-aware HTTP timeout and backoff. Persist requested/effective task, level/budget, policy version, model, key alias, latency, finish reason, token counts, attempt result, and skip reason. Never store or render raw chain-of-thought.

## 14. Delivery strategy

Implementation proceeds in independently deployable slices:

1. stop false paid truth and repair status/statistics authority;
2. make daemon/outbox/concurrency correct on the real cache and MySQL engines;
3. preserve and analyse paused/global-stop conversations;
4. add structured memory, reversible score, and expanded taxonomy;
5. add payment/order/TTN reconciliation and multi-order linking;
6. rebuild statistics and Ukrainian cockpit;
7. harden Meta/CAPI/webhook/rate contracts;
8. finish retention, calibration, browser accessibility, and chaos verification.

Every slice follows TDD, focused tests, the complete related Instagram/Gemini/chat suite, Django checks, migration drift, compile, diff checks, commit/push, production migrate/check/static/compress/seed/restart/ensure, and fresh SHA/runtime verification. No deploy is complete without production SHA, daemon PID uniqueness, DB/cache heartbeat, queue/outbox, webhook/capability, and effective model evidence.
