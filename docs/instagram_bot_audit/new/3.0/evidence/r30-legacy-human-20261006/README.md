# P1-6 legacy human receipt compatibility · 06.10.2026

Existing whole-command SENT receipts are recognized only through the exact command/transcript/operation/hash/namespace/recipient/MID graph, including all four bounded receipt positions. No synthetic HumanReplyPart, transcript, takeover, memory event or provider call is created. UNKNOWN/provider-started defers attribution; conflicting or erased evidence does not become a manager action. Exact receipt wins over unrelated historical in-flight work; late reconciliation keeps the original send.

Database-read failure retains retryable status across observation, manager projection, reconciliation, final acknowledgment and real webhook inbox retry. The materialization check accepts the exact original legacy command for later chunks despite the historical blank transcript namespace. Permission/privacy denial remains finite and non-retryable.

Shared CPython3.14.6/Django6.1, disposable MariaDB11.4 full migrations: 88/88 PASS, 11.196s. Suites: legacy receipt16, echo integration10, real inbox retry6, existing Human echo45 and transport11. Actual mocked four-part send, empty webhook/poll replay, early/late receipts, cross-lane identity, namespace/recipient collision, temporary DB failure/retry, privacy and existing transport boundaries. No production test fixture or real provider request.

The first gate invocation supplied two nonexistent regression module names: actual 32 legacy tests passed; two loader errors were corrected without changing production code. Final exact 88-test gate is retained. No schema change. Deploy pending; natural72h20 and full P1-6 remain open. Rollback: revert the scoped code commit, retain already-applied Human0222–0224 schemas and actual receipts.
