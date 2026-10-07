# P5-1 · Same-value materiality core · 07.10.2026

Repeated authenticated observations of the same currently valid `observed_language`, `objection_observed` or `deferred_intent` preserve the existing signed fact, evidence and head. The fresh immutable analysis result is retained; publication reports `unchanged_heads` rather than appending another equivalent assertion.

No authority field is refreshed: original source, observed time, confidence, validity/TTL, signing identity and head revision remain unchanged. A changed semantic value/date, expired or invalidated fact, reset boundary or invalid retained source/evidence follows the existing strict append/rejection path. New foreign evidence, stale results, signature failure and revoked retained keys keep their existing rejection behavior.

Classification occurs only after the existing result/current-job/chain/evidence admission under client and head locks. All candidate plans precede any publication writes; a later-slot failure rolls back earlier writes. Equivalent Django TextChoices/plain-string coordinates normalize only in the pure classifier adapter; signed V1 payloads and exact integer identities stay unchanged.

Acceptance: shared CPython3.14.6/Django6.1, current disposable MariaDB11.4.12. Root final integrated gate: **70 tests, 67 PASS and 3 existing SQLite schema-profile skips**; includes a real two-publisher race, changed slot, expiry, reset, evidence ownership, key rotation/revocation, atomic rollback, golden V1 MACs and public outcome compatibility. The native unchanged race produces zero fact/evidence/head writes. Both worker and independent read-only review completed; root repeated the native gate after repair. Initial red fixtures exposed a `snapshot` naming collision, TextChoices adapter mismatch and a fault injection firing during old-head verification; final gate fixes the causes, not the authority guards.

Modules: `tests_ig_memory_materiality`, `tests_ig_typed_memory`, `tests_ig_typed_memory_reader`, `tests_ig_typed_memory_read_ttl`, `tests_ig_typed_memory_schema`, `tests_ig_typed_memory_mariadb`.

No migration, model, consumer or activation flag changes. The existing **512 append-depth guard remains enforced** for real changes. This is not an immutable-checkpoint implementation: additive certificates/epoch/CAS, key retirement and bounded reader-performance contracts remain next. Memory generation/admission acceptance flags stay FALSE; full P5-1 and natural72h/20-turn acceptance remain open. Production evidence is recorded separately after SSH verification.
