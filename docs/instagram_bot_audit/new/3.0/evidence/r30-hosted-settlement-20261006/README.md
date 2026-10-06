# P1-5 hosted settlement core · 06.10.2026

CODE/TEST accepted; production pull pending at this commit. Reuses canonical Deal/Proposal/Generation/PaymentAttempt/IgPaymentEvent/Projection/order/lifecycle owners; no new money ledger or customer sender.

Versioned identity/currency/minor-unit/net settlement normalization uses exact hosted winner proof. Every supplied alias must agree; malformed/non-ASCII money, missing/unordered modification version, foreign/old/losing invoices and inconsistent totals enter finite review without inventing paid truth. Positive conversion chooses initial versus paid settlement under canonical locks. Refund/reversal cannot be lost behind a stale preview. Projection, legacy mirror writes and dirty acknowledgement remain protected by the same graph/projection locks; older repair cannot overwrite newer committed truth. Existing amount-only internal initial-conversion API and processing preview contracts are preserved; complete provider proof remains mandatory for pulled/settlement observations.

Applied settlement uses existing reconciliation flag/append-only event/commit recovery and effect owners. Full reversal changes canonical payment state; partial settlement requests review. Closed customer window preserves the financial fact without sending. Repeated or reordered events do not create a second order/winner/effect.

Provider schema was checked against [official Monobank documentation](https://monobank.ua/api-docs/acquiring/integrations/marketplace-and-agents/post--api--merchant--invoice--create): amount is in minor units, finalAmount is the amount after refunds, ccy is numeric ISO currency, modifiedDate is optional last-change version. No provider probes were used.

- First broad SQLite210 had21 compatibility failures and6native skips; [log retained](hosted-final-sqlite.log). Code compatibility was corrected, legacy assertions unchanged.
- [Final compatibility SQLite](hosted-compatibility-sqlite.log):210 total/204PASS/6native skips,12.189s; actual money/lifecycle/order/legacy APIs included.
- [Native broad gate](hosted-final-native.log):210 total/208PASS/2fixture assertion failures,30.520s. Two forced conversion races wrongly assumed unpaid PaymentAttempt.paid_amount=0; canonical field is nullable and untouched invoice holdsNULL. Fixture changed to assertIsNone, preserving all downstream settlement/order/version/count assertions.
- [Final native race gate](hosted-native-race-fixture-final.log):6/6,3.800s. Newer completed repair, mirror-lock contention, initial conversion before refund/reversal, webhook/reconcile and duplicate event races covered. Suites overlap and are not summed as unique tests.

Independent review's four blockers repaired before acceptance; no remaining actionable finding in scoped review. No synthetic provider/customer I/O or production fixtures. Natural paid event/latency and complete passport/72h20 remain open. No schema change; rollback restores scoped service commit while canonical events/projections remain authoritative.
