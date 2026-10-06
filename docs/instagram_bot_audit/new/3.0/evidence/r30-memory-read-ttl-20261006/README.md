# P5-1 · Read-time typed-memory TTL · 06.10.2026

Accepted release gate, production deployment pending.

Reader captures one aware instant, rejects malformed clocks before database access, omits current facts when valid_until <= read time, and does not resurrect predecessors. Integrity/keyring/scopes/reset/source fences remain mandatory before expiry omission. SELECT-only reading performs no sweep, tombstone, generation or mutation. Missing consumers and memory activation remain open; both generation/admission flags must remain FALSE.

Native MariaDB: **22/22 PASS, 1.670s**, real migrated immutable guards. Tests retain actual physical tamper rejection and separately validate malformed detached evidence. [Log](native-memory-22.log).

Management0226 synchronizes the saved system_prompt field default with the already shipped source after parallel receipt integration. It is an AlterField migration whose SQL is **no-op**, changes no table/data/customer prompt/publication. Management migration state reports no changes. [SQL](management0226-sql-noop.log), [state](management-migration-check.log).

Rollback: previous commit restores reader behavior; memory flags stay disabled. Full P5-1 consumer/materiality/depth-recovery contracts and natural72h20 gate remain open.
