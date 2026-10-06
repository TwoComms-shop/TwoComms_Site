# P1-4: follow-up по текущему эпизоду · 06.10.2026

CODE/TEST accepted; production pull pending at this commit. Existing scheduler/outbox remain owners; no duplicate follow-up FSM.

Task binds exact revision/source digest/reset/current commercial episode/selection line/recipient/deal. Missing or ambiguous scope fails with a finite reason; historical paid deal cannot suppress a genuine new episode. Created Order alone does not prove paid. Original accepted intake proves episode event boundary, including first inquiry with an empty selection. Planning and dispatch revalidate scope; client cooldown, one ordinary unanswered reminder, price3h/explicit selection90m, permission/takeover/window remain.

Tests retain original assertions and add canonical ingress/episode fixtures where direct DB fixtures lacked real intake. New episode tests cover historical/current paid, ambiguous/missing/foreign scope, gift recipient, stale source/selection, new statement, permission change, first intake and native concurrent episode switch. All external/provider/customer transport is mocked.

- Combined SQLite:281 total,280 PASS,1 native skip,23.434s; [log](combined-candidate.log).
- Native MariaDB11.4/InnoDB with real management migrations:286/286 PASS,0 skip,67.925s; includes the concurrent episode-switch test and admission/native regressions. [log](native-combined-candidate.log).
- Legacy consumer compatibility:118 total,117 PASS,1 native skip,8.372s. [log](p1-legacy-candidate.log).
- HEAD follow-up relevance baseline35/35 PASS,2.879s; historical baseline kept. [log](followup-relevance-head.log).

These are overlapping aggregate suites, not unique test counts for this block. Combined suites also exercise unpublished unified-context/publication candidates; legacy compatibility specifically verifies this block with the previous context consumer. No production fixtures or synthetic provider probes. P7-1.C natural72h/20-turn evidence and whole P1-4 passport remain open until observed; deployed status is added only after SSH verification.
