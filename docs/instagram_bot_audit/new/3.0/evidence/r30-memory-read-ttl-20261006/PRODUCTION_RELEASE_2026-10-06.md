# Production · P5-1 read-time TTL · 06.10.2026

Release `0e4570b9bb0fdced6dece80c9f8448d99b86ff4f` on main; supervisor/child SHA match, PID2866904; main+workers healthy, maintenanceOFF. Management0226 applied; `makemigrations management --check --dry-run` reports no changes. Saved-default migration emitted no DDL/data SQL; customer prompts/publication unchanged.

Bounded READ ONLY: 2 real clients, both reader results empty/no_matching_heads; no synthetic memory facts or expiration evidence fabricated. Automatic provider calls0; both memory generation/admission flagsFALSE. Native22/22 exercises real signed facts/exact expiry/immutable guards.

Consumer activation/materiality/chain-depth recovery/full P5-1 and natural72h20 remain OPEN. [Code/tests](README.md).
