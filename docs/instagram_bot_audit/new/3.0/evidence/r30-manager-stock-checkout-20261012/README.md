# Block35 · Manager stock reconciliation and ordinary checkout

An enabled exact stock position can now be reconciled after a fresh manager AVAILABLE/accessible-unrecorded decision, then prepared as an ordinary full-basket offer and finite reservation when actual customer purchase evidence exists. The action confirms an absolute physical count; it never adds stock per client or turns a manager dialogue answer into money/order authority. Availability-only enquiries finish stock reconciliation without inventing purchase consent.

The shared catalogue stock and every requested exact size count remain separate. NULL size tracking becomes an explicit confirmed numeric cap; missing/disabled rules and ambiguous warehouse mapping need the responsible editor. Ordinary reservation and payment use that cap through complete grouped item identities; payment consumes both shared and size counts atomically. Current source-bound negotiated prices and payment policy are preserved. Full initial holds transfer into the first invoice generation without double counting or releasing a gap.

A signed preview, immutable owner-bound ledger, operation UUID and row locks cover changed selection, counts, reservations and privacy. Busy edits deny temporarily without ignoring protected quantity or terminalizing a valid payment allocation. Failed checkout rolls back stock, movements, quote, reservations and audit together. UNKNOWN UI outcomes retain the same request/decision/operation; a different case cannot settle them. Historic receipts separately report whether the current basket binding remains valid.

The interface provides blank, explicit count fields, protected/minimum facts, readable explanations and direct navigation to the existing proposal workspace. Inputs are 46px and mobile buttons 44px or higher. At 320/390px the section is 269/339px, with no horizontal overflow; desktop 1816px also passed. A discovered lower-row funnel caption/legend overlap is corrected by reserving the actual visible caption height. Final loaded assets: stock JS 1cf2cd950754, CSS c1fb1d036cc4, journey JS bbda591ea1da.

## Verification

- [316/316 native MariaDB scenarios](NATIVE_RELEASE_GATE.txt), 132.844s, 0 skips. 68 new cases plus 248 affected compatibility cases. Real independent connections cover one-unit contention, busy item/reservation edits, paid contention retaining protection, physical count CAS, payment/rollback, full two-variant invoice transfer and numeric-order warehouse write-off.
- [10/10 controller scenarios](NODE_UI_GATE.txt), 0 skips: uncertain results, receipt binding, idempotent retry, fresh-preview reconfirmation, catalogue counts and readonly staff.
- [Schema/check evidence](SCHEMA_CHECKS.txt): Django check; no management migration drift;17 changed Python files parsed; actual 8 InnoDB evidence owners plus catalogue parent; 24 physical guards. SQLite only verifies 24 trigger DDL install/remove, never substitutes for native transactions.
- Actual authenticated local browser POST→ready 790 UAH offer→existing proposal workspace→reload. [UI receipt](LOCAL_UI_RECEIPT.json) and [actual database readback](LOCAL_UI_DATABASE_RECEIPT.json): stock 1/size 1, immutable receipt 1, active finite hold 1, no public token or payment attempt. Local external HTTP is forbidden.

Initial negative runs exposed incomplete fixtures and real lock/size risks. The final accepted run above contains all fixes. Busy tests prove the real guard boundary and unchanged capacity, with subsequent same-offer retry; they do not require one particular SQL optimizer path. Suites overlap with prior runs; no cumulative test total is claimed.

Root owned integration, native DB, browser and releases. Scoped subagents supplied stock/evidence, checkout/price, invoice transfer, UI tests and independent read-only review. Review blockers were corrected and accepted before release.

## Production boundary

Production delivery and runtime acceptance are recorded separately after canonical commit/push/SSH pull, migration 0231, static rebuild and owned daemon reload. [Pre-migration catalogue evidence](PRODUCTION_BEFORE.json) records 81 rows and its logical inventory digest; the engine change must preserve that digest. No production fixture, forced customer notification, provider health probe, backfill or fake order is permitted.

Natural manager-action cohorts and complete programme passports remain open. Fresh CART-review reuse for standing restock, split execution/cost/consent, pre-selection deadline routing, native future marketing grants, immutable memory checkpoint recovery and P7 remain separate next work. The existing 90-day UGC reward policy stays unchanged.
