# Engineering41B — catalog manager-followup restock consent

Status: ✅ DEPLOYED21.10.2026 (Europe/Kyiv). Code `98da94cc4c559e342df7a9e81a9512f0daee2c21`.

## Customer behavior

A fresh genuine CART manager decision with catalog WAIT lines can carry one
positive RS1 quick reply in the existing manager-update message. The invitation
lists every WAIT item with its exact configuration and quantity. AVAILABLE and
CLARIFY lines never become a waiting-list subscription. Positive accounting
stock does not overrule the manager's WAIT decision.

Only the customer's own native click after confirmed delivery creates consent.
The original full basket/configuration must still match before invitation and
acceptance. After acceptance, later basket edits do not cancel or replace the
agreed list. Actual availability notification still requires a separate physical
ALL manager review. Accounting stock, anticipated supply and the initial WAIT
answer do not grant that authority.

## Delivery and evidence

- Reuse the original fulfillment FollowUpTask and its claim/client lease. No new
  outbound queue, fake inbound source or fabricated RevisionDeliveryEffect.
- Seal recipient, exact body, one button/payload, genuine request/decision,
  original source, full basket and configuration. Recheck current manager rights
  and the frozen wire immediately before provider HTTP.
- The real SENT/MID/sent_at checkpoint owns the alternative restock receipt.
  Unknown outcomes are never replayed. Missing history/receipt can be repaired
  from a valid historical checkpoint without another provider call.
- Failed old repairs defer and sort behind untouched checkpoints, including when
  the next worker run is already beyond the retry interval.
- Authenticate bounded active interests before deduplicating overlapping catalog
  variants. An unrelated product interest does not suppress the new invitation.
- Preserve historical revision receipt signatures by omitting only the newly
  added NULL task column from their signed columns.
- Migration0242 adds exactly one producer per receipt and a native insert guard.
  Existing append-only update/privacy-delete guards remain. SQLite table remakes
  preserve exact local and referencing triggers in both directions and roll back
  a failed remake. Downgrade refuses to discard native receipts.

## UI and limits

The producer-versioned accepted ACK attributes each item to the customer’s
notification choice. This prevents a bare `fit: classic` label from being
misread as stock authority by exact-line validation. UK/RU/EN regressions still
reject an added current-stock assertion; historical41A ACK/revoke bytes remain
unchanged.

UK/RU/EN follows the genuine customer source. One affirmative button; no negative
button. The existing readable admin waiting-list/context view consumes the same
receipt/answer/subscription facts; no additional admin widgets or static assets.

The existing950-byte single-message bound remains. If the full offer/ACK/notice
cannot fit, send the complete ordinary manager update, with no truncated or
partial subscription. Previously frozen plain updates keep their original copy.
No reservation, arrival-date guarantee or automatic warehouse write is implied.

Business validity remains90days. A quick-reply consent does not extend Instagram's
native messaging window or prove future marketing capability. No historical
customer replay, synthetic production fixture or forced-open window is allowed.

## Verification and deployment

✅170 tests PASS,0skips,1092.874s on the final source snapshot. The native
MariaDB11.4.12 gate passed all real migrations, reverse/reapply, database/schema
checks and verified cleanup. The five SQLite/copy checks are included in170,
not added to inflate the total. AST, model migration drift, Django checks,
CLI registration and staged diff checks passed.

The real revision RS1→ACK scenario caught an exact-line fit attribution issue;
the new producer's versioned copy resolves it without weakening truth guards.
The legacy fulfillment test fixture now reuses the seeded settings singleton.
The ALL lifecycle test invokes the genuine reconciliation producer before
physical review and respects the15-minute subsequent worker check.

Reproducible gate:

```sh
.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite restock-followup
```

Use local MariaDB binaries from PATH, or MARIADB_SERVER_BIN and
MARIADB_INSTALL_DB_BIN. The gate owns a new temporary server/schema/user, uses
all real migrations and forbids external provider traffic.

Production baseline: main `d74157be9cc1582a7230bafd6af08f31a9d1c256`,
manager/restock gates enabled, daemon main/workers healthy, maintenanceOFF,
81catalog rows; readonly stock digest
`c84facc0c868919af3437b71db996ba21a3d615c1bd7cfd8be49684f9df6a30f`.

Production deployment used scoped commit→GitHub main→canonical SSH git pull,
then management0242, Django check, WSGI refresh and supervisor reload. Production
uses the supported3.14 venv: CPython3.14.8/Django6.1; the mandated local test
runtime is3.14.6/Django6.1. No package/environment replacement was performed.
Runtime main/supervisor/child SHA matched the code commit, both workers and main
were healthy, maintenanceOFF. Both nullable origin columns, all3receipt guards
and the XOR constraint were verified.7readonlySELECTs,0provider attempts,
81catalog rows and stock digest unchanged. No manual customer sends or production
fixtures. Static assets were unchanged; no new browser/UI acceptance is claimed.

[Machine-readable verification](VERIFICATION.json), [tested source hashes](SOURCE_SHA256.json).

## Remaining work

First audit the existing41A explicit-request path end to end with zero-fit
catalog authority: genuine request → real RevisionDeliveryEffect → own RS1 click
→ execute_claimed_revision. Code review identifies the same conditional bare-fit
ACK exposure; no legacy end-to-end failure or universal fix is claimed. If
confirmed, version new invitations’ ACK copy while preserving historical revoke
proofs. This gate precedes new feature expansion.

Assisted/custom ALL-WAIT requests need their own genuine typed manager-update
producer before proactive consent can be attached. Full multipart support must
cover invitation, accepted ACK/revocation and final notification together.
Cross-purpose ALL delegation and a fresh visible customer-account Meta journey
remain separate acceptance gates. Full Engineering41 and27passports remain
partial. Preserve the Oct18–20 cooperation/ingress/commerce QA releases.
