# R30: manager-approved noncatalog apparel in scoped hosted checkout

Design only, 2026-10-13. Baseline audited in `/Users/zainllw0w/.codex/worktrees/r30-media-integration/site`; integration waits for foundation 38 to land. No repository implementation or acceptance is claimed by this document. Proposed names below are interfaces to reconcile with foundation 38, not a second schema to install beside compatible foundation entities.

## Accepted behavior

An Instagram customer can request a purple hoodie, size L, absent from the public catalog. The bot records a source-bound draft and requests a manager decision. An authenticated authorized manager edits the exact item, size, color, quantity, unit price in UAH, optional photo and actual sourcing/availability terms. Only a current READY approval may become a commercial quote. The client uses the existing hosted Instagram checkout, enters Nova Poshta/contact data, and pays through the existing provider and invoice generation. Verified settlement materializes one ordinary canonical Order with correct custom OrderItems and an Instagram client assignment.

Multiple custom apparel items and catalog/custom mixed baskets are supported by the same ordered basket. No fake Product, catalog stock, invented manufacture/raw-material allocation, new provider, alternate checkout, prepayment Order or fake customer account. A manager-approved commercial intention is distinct from the execution scope paid now. A WAIT item is not charged or reserved; it needs fresh physical READY approval before later execution.

The execution scope is either the entire intention or an exact customer-accepted partial partition. Deferred lines stay visible under the same intention, without being silently lost or marked bought. Together means wait for all lines; separate means the accepted READY selection is one ordinary order now, and any later selection gets a new episode and ordinary order linked to the same intention. This slice splits whole source lines. Quantity splitting requires separate reviewed source sublines with distinct identities; do not invent it inside an invoice serializer.

## Existing components and exact blockers

| Component | Integration seam |
| --- | --- |
| `management/ig_bot_models.py:2858` IgCheckoutProposalItem | Product and variant are already nullable. Extend typed custom item transport and authority identity; `price_source='manager_custom'` alone is not authority. |
| `orders/models.py:678` OrderItem | Already supports nullable Product, `is_custom`, textual custom color, exact title/size/quantity/money. Add an owned custom-photo reference if foundation provides none. `product_image` presently returns None for custom lines. |
| `storefront/views/manual_orders.py:817`, `:1029` | Existing custom line builder/editor and mixed-line UX provide normalization/UI primitives. Do not call `manual_order_create` for an unpaid hosted draft: that would create a parallel real Order. Its quantity clamp/price rounding must not silently normalize approved terms. |
| `management/services/ig_revision_checkout.py:347` | Current custom intent is fail-closed. Add an assisted authority-reference path before catalog-only rejection, with the same customer send/current-source admission. Raw provider selectors cannot enter that path. |
| `management/services/ig_checkout.py:221`, `:729` | Quote validator and item serializer currently require catalog Product objects. Add an internally constructed validated custom item branch; retain every catalog resolver and money guard for catalog rows. |
| `management/services/ig_revision_cart_binding.py:269` | `_configuration` and quote binding currently require positive product ID. Add explicit assisted v2 structural contracts using original identity/index and dense selected quote index; no global null-product relaxation. |
| `management/services/ig_checkout_cart_provenance.py:125`, `:137`, `:262` | Carry the same assisted binding through proposal, expanded attempt rows and actual OrderItem IDs. Preserve owner, exact configuration and totals. |
| `management/services/ig_checkout_payment.py:322`, `:413` | Snapshot rejects null product and revalidation rebuilds catalog-only quotes. Revalidate the frozen assisted authority and serialize typed custom rows in the same frozen attempt. |
| `management/services/ig_inventory.py:79`, `:114` | Strict reserve requires catalog inventory policy for every row. Catalog rows retain stock policy; custom rows use finite reviewed supply approval, never catalog stock or an UNTRACKED fake policy. |
| `management/services/ig_checkout_generation.py:686`, `:1466`, `:2047` | Reuse proposal/generation leases, provider ambiguity/retries, exact settlement and winner retirement. Add assisted execution authority checks at prepare/dispatch/paid boundaries without another payment state machine. |
| `orders/payment_attempts.py:48`, `:211` | Current per-row materializer always indexes Product. Branch only for verified typed custom rows; preserve catalog validation, source provenance, winner key, payment status and side effects. |
| `management/services/ig_checkout_payment.py:1596`, `ig_order_links.py:198` | Keep existing verified-payment binding/immutable attribution/client assignment; execution binding records the exact ordinary order returned here. |
| `storefront/views/ig_checkout.py:705`, `:995`, `templates/pages/ig_checkout.html:125` | Reuse current item display, NP form and branded fallback. Derive a grant-scoped image URL from owned asset reference; never public customer storage URL. |
| `management/services/ig_revision_intents.py:324` | Reuse existing manager case/notification receipt, but generic manager-task approval does not approve the basket. Extend that case with a link to the commercial request/decision panel. |

## Minimal model responsibilities

Use compatible foundation 38 entities where they already fulfill these contracts. Do not create synonyms or duplicate the intention registry. The minimum distinct responsibilities are:

1. **AssistedCommerceRequest: immutable source-bound intention version.** Stable intention UUID, version, client/owner, source episode/deal and source revision, reset floor/permission generation, bounded full capture and digest. Full capture contains every original line's stable identity and original index, source choice evidence and source purchase evidence. It contains safe item/configuration facts, not raw customer messages or private URLs. Unique `(intention_uuid, version)`. The current request version is published through the existing case/owner head using CAS; history remains append-only.
2. **AssistedCommerceDecision: immutable manager decision version.** Request/version, monotonically assigned decision version, authenticated actor, timestamp, current source/head digest, full reviewed row snapshot, per-row READY/WAIT/REJECTED state, exact title/size/color/quantity/UAH price, safe asset reference/digest, finite physical-approved quantity and truthful manager sourcing note. Quote price approval and physical readiness are explicit independent facts. Unique `(request, version)` and immutable protected payload. No manager decision is inferred from bot text or generic task status. A revision is a new decision, never an in-place edit of an approval.
3. **AssistedCommerceExecutionBinding: one proposal's chosen execution.** Request, exact decision, proposal OneToOne, selected/deferred identity partition, scope mode, explicit customer choice/purchase evidence, dense quote index to original index mapping, exact execution digest, immutable sealed execution payload. Mutable lifecycle/current-slot bookkeeping only: prepared/current/expired/revoked/settled/review plus actual canonical order OneToOne and terminal reason. Decision-to-execution OneToOne is the simplest safe rule: one physical approval can authorize one proposal/checkout series. Invoice retries reuse that proposal, not a second execution. Expiry or revocation requires a fresh manager physical decision to create another proposal. A winning settlement freezes the execution permanently.
4. **Optional AssistedCommerceImageAsset:** only if no suitable owned asset exists. Owner request/client, uploading authenticated actor, decoded raster metadata/content digest, private storage identity, lifecycle/retention/erasure state. Immutable identity/content; explicit replacement creates another asset and invalidates approval. It has no public URL. It is not another product/image catalog. The canonical custom OrderItem references the approved asset; this is durable product evidence with its own retention contract, not an implicit indefinite copy of ephemeral IG media.

Keep selected/deferred lines in bounded JSON inside the existing/foundation source owner unless real query/physical guard requirements justify separate line tables. Do not add a new invoice, payment, raw-material or shipment model for this slice.

## Versioned binding interface

Schemas: `ig-assisted-commerce-request.v1`, `ig-assisted-commerce-decision.v1`, `ig-assisted-checkout-binding.v2`, and a version-discriminated provenance extension. Preserve the existing v1 catalog behavior and `legacy_unknown` compatibility. New manager-assisted quotes never use `legacy_unknown` as proof.

`ig-assisted-checkout-binding.v2` contains:

```json
{
  "schema": "ig-assisted-checkout-binding.v2",
  "request": {"id": 1, "intention_id": "uuid", "version": 1, "digest": "sha256"},
  "decision": {"id": 1, "version": 1, "digest": "sha256", "actor_id": 1},
  "owner": {"client_id": 1, "episode_id": 1, "deal_id": 1, "source_revision_id": 1},
  "full_capture_digest": "sha256",
  "execution": {
    "id": 1,
    "scope": "full",
    "selected_line_ids": ["line-a"],
    "deferred_line_ids": [],
    "choice_source": {"kind": "customer_choice", "reference_id": 1, "digest": "sha256"},
    "purchase_source": {"kind": "customer_purchase", "reference_id": 1, "digest": "sha256"}
  },
  "lines": [{
    "line_id": "line-a", "original_index": 0, "quote_index": 0,
    "kind": "custom_apparel", "approval_line_id": "line-a",
    "approval_digest": "sha256",
    "configuration": {
      "product_id": null, "color_variant_id": null, "title": "Purple hoodie",
      "size": "L", "color_name_custom": "Purple", "qty": 1,
      "unit_price": "1500.00", "currency": "UAH", "is_custom": true,
      "photo_asset_id": null, "photo_digest": null
    }
  }],
  "execution_digest": "sha256"
}
```

This is a structural transport sketch, not a self-authenticating payload. IDs, JSON hashes, `price_source`, actor IDs and option_values supplied by an LLM/client are untrusted. The effect owner reloads request/decision/execution/current head and constructs this binding from authoritative rows. A DTO cannot be forged by serializing a string reference in option_values.

Authority verifier returns a bounded typed `VerifiedAssistedExecution` object containing canonical owner, current sealed request/decision, full capture proof, selected/deferred partition, normalized ready rows, deadline and decision capacity. Structural validators consume this internally validated object or its server-built binding; public callers never grant authority by setting `verified=true`.

The parent full-capture proof must show both sets are unique, disjoint and exactly cover the original intention; every original index/line ID matches; selected quote indices are dense `0..n-1`; each selected row is READY, exact approved quantity/configuration and has accepted purchase scope. Catalog rows still carry their original catalog source choices and live readiness. Custom rows carry their own typed approval. Deferred WAIT rows carry neither price charge nor capacity claim.

`_configuration` should dispatch on binding schema and line kind, with shared finite serialization. Its custom return includes exact title/color/size/quantity/unit price/currency/photo identity and approved line digest. V1 still requires positive Product ID. A missing/unknown schema, null catalog product or non-null custom product/variant rejects. Do not trust a custom string inside generic option_values to choose the branch.

## Quote and payment DTOs

Normalized quote rows retain the existing catalog DTO for catalog lines. An internal custom DTO has kind, stable line ID/index, approved decision/line digest, title, size, color label, quantity, unit price/line total, currency, asset reference/digest, no product/variant, and no catalog bundle pricing. It is only constructed by authority verification.

Proposal item may carry typed approval identity in dedicated fields or strictly validated server-owned option_values, but proof also lives in the immutable revision/execution binding and cannot be reconstructed from raw mutable proposal fields. Proposal item `price_source='manager_custom'` is descriptive. Quote digest binds line kind/identity, title, size, color, qty, price, currency, photo identity, approval decision and scope partition in addition to existing catalog fields. Current catalog DTO digest omits title/color/photo, so simply reusing it would fail the custom exact-approval contract.

The frozen attempt custom row carries canonical nullable Product/variant, exact approved fields, `is_custom=true`, typed approval reference/digest, source line identity/index, proposal item ID, photo reference/digest and execution proof. The provider basket uses the existing `_proposal_basket` minor-unit serializer. Payment materialization recognizes custom rows only after authenticated persisted assisted execution/provenance validation, not because `is_custom` is true. It creates the normal OrderItem and binds exact bulk-created IDs through the same provenance machinery.

Do not reinterpret `catalog_total` as an assertion that custom apparel was in the catalog. Retain the historical database field if needed for compatibility, but expose generic merchandise total and record catalog/custom subtotals in the typed snapshot. Aggregate approved goods total is exact; negotiated discount and promo eligibility are line-based, with custom eligibility explicit. Preserve the accepted payment policy; do not infer 200+COD or full-payment-only from the word custom. Zero-total fulfillment remains a separate explicitly covered existing contract; a normal positive-price custom order does not fabricate a payment.

## Currentness, CAS and finite capacity

Use the established client/commercial barrier and deterministic order for locks: permission/current source/head, request, current decision, execution/proposal, generation/attempt, catalog inventory targets. Reconcile exact order with foundation 38 lock ordering to avoid inversion. Provider network calls stay outside database transactions under existing generation dispatch leases.

- Draft submit/edit includes expected request version, source/current-head digest and permission epoch. Manager submit includes expected decision version and full draft digest. Stale CAS returns conflict with changed fields, not last-write-wins.
- All selected rows require current approved source revision/reset scope, valid client, authorized actor, READY availability and finite positive integral quantity. Each custom approved line is bound once to the execution's exact same approved quantity. No stock model or warehouse reservation is created for custom supply.
- Decision-to-execution OneToOne prevents the same approved finite quantity authorizing two proposals. One active execution per intention uses nullable `active_slot=1` and unique `(intention_uuid, active_slot)` on the mutable execution binding, with checks permitting only NULL or 1. A partial execution releases the slot only under a terminal disposition; settled coverage remains recorded and excluded from subsequent selections.
- A new manager decision checks settled/prior in-flight execution coverage under the request/intention lock. It cannot approve already consumed lines again by changing decision version. Late provider-paid older executions stay visible and prevent silently charging the same selected need again; use existing paid ambiguity/review guards.
- READY is time-bounded physical approval, not a permanent availability claim. Proposal expiry cannot extend approval freshness. WAIT-to-READY requires a new manager decision, preserving original intention/source lines. Reusing a quote after expiry requires fresh decision and proposal; retrying the same safe invoice generation stays inside the existing series.
- Link preparation and invoice prepare/dispatch revalidate the exact decision/execution/current source. Once valid settlement arrives, never discard payment truth because approval later changed. Preserve frozen original terms and route resource/late/obsolete conflicts through existing reconciliation; only a proven current winner creates/binds one actual Order.
- Cancel/revoke updates the execution lifecycle under the same barrier, releases the current slot, revokes outstanding access tokens as policy requires, and terminalizes existing generation through existing APIs. No blind invoice resend or provider cancellation assumption.

## Database guards

All new/foundation ownership/decision/execution tables must be InnoDB with physical owner FKs and unique constraints where available. Native MariaDB acceptance must verify engines and installed guards; SQLite success does not establish concurrency safety.

Physical constraints: request identity/version uniqueness; decision request/version uniqueness; proposal/execution OneToOne; decision/execution OneToOne; actual order/execution OneToOne; active-slot shape/uniqueness; nonnegative version fields; protected actor/request/proposal/order foreign keys. Child owner must match parent client/request/episode/deal. Reject raw INSERT/UPDATE ownership mismatches and mutations to immutable request/decision/sealed execution columns using repository-standard physical guards. Direct JSON payload updates must not bypass immutable digests. Lifecycle transitions and order attachment require owner-compatible canonical winner evidence.

If MariaDB guards cannot express JSON line partition/decimal equality reliably, enforce those at the sole guarded service write boundary and protect stored payload immutability physically; do not claim an SQL CHECK proves the entire JSON contract. Test raw SQL ownership/mutation bypasses separately from semantic application validation. Reuse foundation partition/capacity guard tables if supplied rather than adding conflicting triggers.

## Media and privacy

Optional manager-uploaded photo uses bounded decoded JPEG/PNG/WebP (actual MIME/decoder verification, dimensions/pixel/byte limits, no SVG/HTML, no arbitrary URL fetching), orientation/metadata normalization and private owned storage. Manager must preview the same asset identity they approve. Client/customer uploaded IG source media is private ephemeral evidence, not automatically publishable product photography. Any approved copy/reference requires explicit ownership/publishability and retention scope; erasure/expiry is enforced by the same privacy barrier/lease primitives.

Proposed GET `/ig-checkout/<proposal_uuid>/items/<proposal_item_id>/photo/` validates existing signed session grant, exact proposal/item/decision/asset ownership, current usable asset lifecycle and allowed frozen revision. It returns bounded safe image bytes with no-store/private/noindex/nosniff/no-referrer headers, never storage paths/customer URLs. Missing/foreign/erased asset yields unavailable and placeholder. Operator previews retain management PII permission and audit/lease semantics. Grant token itself must not appear in image URLs or logs.

Order admin photo access uses authenticated order access, not the customer's expired checkout grant. Do not put a dynamic grant URL into durable OrderItem.product_image; expose a dedicated custom-photo accessor in UI context. Product/variant image precedence for catalog rows remains unchanged.

## Routes and manager/client UI sketch

- Existing client manager case opens a new section in the existing workspace: source intention, catalog/custom rows, exact current facts, pending fields and source references. Bot-proposed values are labeled draft; pending quote has no payable link.
- POST request draft: bounded typed DTO and expected source/request version. POST manager decision: expected request/draft/decision version, exact full rows, readiness/sourcing/physical qty, prices, optional owned asset IDs, authenticated actor. POST execution preparation: exact decision reference, full/accepted-partial scope and source choice/purchase proof. These are existing management authenticated/CSRF routes; permissions exclude Meta reviewer/read-only roles.
- Manager controls edit custom title, size, color, quantity, price, optional photo and READY/WAIT. Show total and selected/deferred needs separately; no stock count, manufacture-started badge or paid toggle in quote approval UI. Approval button seals the reviewed snapshot and prepares the existing hosted proposal through one service; it does not send customer text or create Order.
- Existing receipt/outbox sends approved link only after preparation plus current source/send authority succeeds. Show draft/approval/prepared/link delivered/invoice/payment/order as distinct persisted truths; generic manager task completion cannot claim delivered link.
- Existing hosted page shows the exact selected basket and neutral custom sourcing terms, chosen NP details and accepted payment policy. For partial execution, clearly lists deferred identities as unpaid/awaiting fresh confirmation. No checkout field accepts price, approval IDs or line replacements from the customer.
- Existing normal order/admin/client card lists exact custom fields/photo and ordinary payment/delivery status. Later separate order is a new episode with same intention reference; it does not overwrite the first paid order.

## Acceptance gates for implementation 39

1. Full purple hoodie L flow with manager correction and with/without safe photo; one actual paid order, ordinary admin and intended client linkage.
2. Two custom + mixed catalog/custom baskets, including identical descriptions with distinct source IDs/recipient/size; exact line and money provenance.
3. Full READY versus READY+WAIT: together waits; customer-accepted partial creates only selected order; WAIT never charged/reserved; fresh later READY/new episode creates another ordinary order with preserved intention and prior coverage.
4. Draft/incomplete/rejected/foreign/raw model approval cannot invoice. All current catalog availability/pricing/option/promo guards continue to reject invalid catalog rows.
5. Real two-connection manager edit/approve/CAS and duplicate execution preparation; one decision/proposal capacity, physical raw SQL ownership/immutable guards.
6. Races around link/invoice dispatch, decision replacement, source correction/reset/erasure/takeover; no stale action, provider truth retained if paid late.
7. Duplicate invoice POST/callback/poll/late winner/reordered terminal statuses; one canonical series Order, exact original frozen rows and reconciliation of competing paid executions.
8. Exact finite money/integer qty rejection: NaN, infinity, negative, excessive decimals/caps, zero/fractional qty, omitted size/color/title, oversized safe text, no silent clamp/rounding.
9. Mixed promo/discount/prepayment/UAH minor-unit basket sums and delivery goods/payer distinction; no manager quote mistaken for payment verification.
10. Grant/payer/recipient identities: authenticated sharer cannot become intended IG customer; invalid NP selection rejected through existing resolver.
11. Foreign/expired/erased/replaced photo at approval and retrieval, corrupt/nonraster/oversized upload, image endpoint ownership, no public/private storage URL leak.
12. Authenticated actual manager/customer/order UI at 320/390/desktop, loaded hashed assets, exact unchanged source labels, accessible keyboard/focus and placeholder without overflow.

Focused code tests, native disposable MariaDB/physical/concurrency gates and authenticated local UI prove engineering behavior only. Production schema/SHA/runtime/UI readbacks after authorized deployment and suitable natural cohorts remain separate evidence. No test provider probes, customer sends or production fixtures are justified by this design artifact.

### Concrete preparation boundary

Private models/migration reuse existing `_ReviewEvidence`, not a new cryptographic lifecycle framework. Execution stores only immutable selected/deferred authority and a single terminal projection: canonical paid Order attachment, or known unpaid proposal release. Actual invoice/provider/review states stay in existing proposal/generation. The custom aggregate capacity cap is the existing checkout cap 50; mixed executions must also enforce total quantity 50 and the existing 1,000,000 UAH quote limit.

Canonical execution attachment must occur after `bind_verified_payment` has persisted proposal PAID, generation PAID_WINNER and canonical client assignment. A provider-paid RESOURCE_REVIEW without Order preserves the active intention hold. Verified refund/manual resource resolution has not been traced to an accepted release proof in this preparation: subsequent same-intention execution remains blocked at this boundary. Do not clear the historical winner or add a generic manual release. This is a deliberately open reconciliation contract, not completion of the full passport.
