# P4-3: restock interest is not subscription

Baseline: `27acbf6d42a375fd556bf5642d8696e2144ebe18`. Production read-only observation at 2026-10-09T11:34:24.735426Z: Instagram Login, Graph v25.0, healthy workers, one SENT post-purchase business-consent invitation; zero provider HTTP calls. No verified native future-grant evidence. This release does not assert account support for future notifications.

## Confirmed gap

The inventory callback previously matched mutable `sales_context._stock_gap` and current product, materialized a customer-facing F2 task, and cleared the gap. Neither the gap nor the current product proves a source-bound restock subscription. The task event key included callback time, so retries were not reliably the same event. Clearing the gap from a stale client could overwrite a newer selection. The automatic sender checked stock and selection but did not require the separate restock purpose.

## Release contract

- No automatic restock customer message without an implemented and validated purpose-specific consent contract. Post-purchase business consent, general bot opt-in, a selected product, and a stock gap grant no restock permission. Existing payment and service paths retain their own policies.
- A genuine committed, currently valid exact inventory event may create visible durable internal manager work. It must say that subscription is unconfirmed and must not claim a customer notification was sent or that future messaging is authorized.
- Exact product, variant, fit, size, options and inventory revision are checked against actual catalog state; malformed, stale, cross-product, unavailable and future events fail closed.
- The stock-edit fingerprint is captured inside its editing transaction. Production inspection found Product and ProductColorVariant are MyISAM, while inventory rules, clients and tasks are InnoDB; parent `FOR UPDATE` alone is insufficient. A database-scoped per-product MariaDB named lock surrounds the editor transaction and commit callbacks, with finite two-second contention/409 response and release on all exits. No storage-engine conversion is included.
- Retries and inventory flapping do not multiply work for the same recorded interest. The owner must preserve unrelated tasks, fresh client context and newer stock gaps. No gap is cleared merely because internal review was queued.
- Legacy automatic restock tasks also meet the final provider-boundary fence; no customer HTTP request may bypass it.
- Privacy, hidden/blocked, opt-out, pause/takeover and service precedence remain respected. No production fixtures, customer test sends or provider health probes.

## Open gates

The signed one-button UK/RU/EN restock invitation, source-bound answer/revoke, exact subscription producer, payment-reminder agreed time/invoice contract, native account capability and natural delivery cohort remain separate unfinished work. This safety slice must not be described as completion of P4-3.

Root owns tests, disposable MariaDB, Git, production and documentation. One worker owns the follow-up implementation and new regression module; independent review reads frozen output. No schema changes or expansion of the post-purchase consent ledger.
