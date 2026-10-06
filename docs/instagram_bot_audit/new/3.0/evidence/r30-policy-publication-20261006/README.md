# P1-1/P1-2: reviewed brand source и publication parity · 06.10.2026

CODE/TEST accepted; production pull pending at this commit. Existing instruction draft/publication/head/audit/rollback remain owners.

Bounded deterministic Markdown parser admits allowlisted public sections and exact already-approved core-tone/size instruction bodies. Private, malformed, duplicate or conflicting source material cannot become policy. Dry-run only reads source/draft and emits hashes/reasons/diff. Explicit reviewed import uses frozen source hash + draft revision/hash CAS, creates/updates draft only, never publishes. Source/parser/section provenance survives immutable publication. Legacy empty provenance keeps historical hashes unchanged. User's dirty brand.md is preserved and not published.

Parity validates actual immutable head snapshot, exposes raw/effective canonical core, separate instruction/public-facts/knowledge identities, missing/corrupt/stale states. Rollback verifies target snapshot hash before any mutation. Stale source review cannot overwrite a changed draft. UI labels draft review and provenance explicitly and uses escaped textContent.

Repeated API reads perform no DML; /bot/api/ is excluded from storefront analytics that previously wrote session/PageView state. Direct and fetch API regression preserves strict zero-write assertion; normal storefront tracking remains active. [Focused3/3 log](readonly-focused.log).

Shared overlapping gate evidence: [SQLite281/280PASS/1native skip](../r30-followup-episode-20261006/combined-candidate.log), [native MariaDB286/286 with real0220/0221 migrations](../r30-followup-episode-20261006/native-combined-candidate.log), [legacy118/117PASS/1native skip](../r30-followup-episode-20261006/p1-legacy-candidate.log). No provider/customer HTTP. Whole passports/natural runtime acceptance remain open; production/UI status follows actual SSH/browser checks.
