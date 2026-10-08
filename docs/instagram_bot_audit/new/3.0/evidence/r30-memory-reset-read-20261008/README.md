# P5-1 reset-safe invalidation and bounded reader batch · 08.10.2026

Baseline main/production5dce77dbd; root +3scoped Sol/high workers, separate service/test owners and independent READ_ONLY review. No checkpoint/schema/feature activation/provider changes in this block.

## Correctness

Both real reset selectors previously selected head IDs before locking. A newer post-reset assertion could win before the tombstone acquired client/head locks, and stale reset work could invalidate that new fact. Selectors now capture exact client/fact/revision/audit and boundary. The existing deterministic projector rechecks these and the latest reset, eligible scope and source watermark under client→head locks. Mismatch returns stale without DML; the unchanged sibling still gets its required tombstone. No deletion of audit/facts, all-candidate publisher planning and previous model/native guards remain.

The generic internal deterministic append helper preserves its existing optional-argument compatibility. A reason string alone is not proof of reset authority; both production reset callers pass full snapshots and audit/boundary. No external/customer API exposes this helper. TTL still rechecks the current expiry and never resurrects a predecessor; privacy fence remains.

Read-only chain materialization uses RowNumber partitioned by slot_key, bounded512+1 separately for every selected slot, source-result join and evidence prefetch. A request-local frozen bundle binds exact signed head identity, slot, keyring fingerprint and chain limit. All signatures, semantic record keys, sources, evidence ordinals and complete predecessor traversal still validate. No global cache, consumer activation or changed fact-key authority; budget overflow fails closed for the whole read.

## Executed acceptance

Required shared CPython3.14.6/Django6.1, real migrated disposable MariaDB11.4 with immutable INSERT/UPDATE/DELETE guards. Final gate89 tests:86PASS/3SQLite-specific skips,11.294s. Suites: tests_ig_typed_memory_read_budget, tests_ig_typed_memory_reset_cas, tests_ig_typed_memory, tests_ig_typed_memory_reader, tests_ig_typed_memory_read_ttl, tests_ig_typed_memory_mariadb, tests_ig_typed_memory_schema, tests_ig_memory_materiality. System check/AST/diff clean.

New regressions: both real selector interleavings +two-connection MariaDB barrier race, exact fact/revision CAS, latest identical-boundary/new-audit identity, source watermark without pair, client ownership, erasure, no-audit rejection, successful unchanged reset, sibling survival/invalidation, immutable history and idempotency. Batch regressions: real512 signed assertion/source/evidence chain under native guards, old evidence failure, legal overflow with lowered reader budget leaves siblings complete, changed signed head/slot/limit/keyring bundle rejection, separate-read retired-key rejection, zero DML/provider. Native fixtures constructed source→fact→evidence→head; no guard disabled or mocked for construction.

Earlier55 and45 test runs overlap; focused18 also included imported fixture tests, then imports changed to module aliases to avoid duplicates from the new files. Final count above is the executed final gate, not the sum of previous runs. Independent review's overflow/fingerprint coverage requests were implemented and re-reviewed; no remaining actionable defect reported.

## Local cost measurement

[Raw bounded benchmark](LOCAL_READ_BENCHMARK.json):10reads per implementation/depth, exact returned snapshot parity against baseline5dce77dbd, all SELECT-only. Three heads; depths1/32/128/512; facts/evidence3/34/130/514. Queries11→7 at every depth, independent of chain length. A separate actual native benchmark test passed in4.811s including fixture construction.

At512: baseline wall p50=52.100ms/p95=112.078ms, batch55.793/128.620ms; CPU48.446/108.233 vs46.119/119.133ms. This small ordered local sample does not prove a latency improvement or production p95 SLO. Full cryptographic validation stays linear; reducing DB round trips is the accepted result. H1/H4, concurrent production latency and large-history checkpoint performance remain OPEN.

## Next contract and rollback

No revision reset, guard relaxation, archived-prefix trust, key retirement or revision513 recovery. Checkpoint remains OPEN: separate monotonic revision from bounded segment; signed immutable exact endpoint/all-scope/prefix commitment +previous checkpoint/key-policy; decide archived-prefix corruption and source/key retirement semantics; migrate ORM/CHECK/fact/head guards together; preserve client/head CAS, atomic multiple slots, current source/evidence/TTL/reset and privacy deletion. Narrative memory activation from b454 remains unchanged and independently operational.

Rollback is a scoped code revert on main, documented push/SSH pull/restart; no schema rollback or data rewrite. Production acceptance is recorded separately after release.
