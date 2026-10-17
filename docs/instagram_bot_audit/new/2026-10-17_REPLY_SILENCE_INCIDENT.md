# Instagram reply silence, 2026-10-17

## Verified production incident

Baseline release: `e01f7915a`. Evidence was read from production MariaDB and the
authenticated CRM; no production fixtures, provider probes or message replay
were used to diagnose the incident.

- Customer sources `3384` and `3385` arrived at 11:51:11 and 11:51:16 UTC
  (14:51 Kyiv). The first source asks about a specific T-shirt; the second is
  its photograph. Both remained pending, with zero legacy attempts.
- Revision `224` transferred the original text to revision `225` when the
  photograph arrived. The resulting sealed bundle contains both sources.
- The JPEG was captured and admitted: 368857 bytes. This was not missing media.
- Live generation began at 11:51:18. Gemini 3.8 timed out, then returned 503;
  the fallback model's first result failed the price-authority check. Its
  repaired result succeeded at 11:51:49. The validated proposal was saved at
  11:51:50, before the 11:51:56 revision deadline.
- The accepted catalog candidate was product `112`, confidence 1.0. This is
  recognition evidence, not stock, price, order or payment authorization.
- A read-only replay of `build_turn_intent` over the stored sources returned
  `purpose=unknown`, no commerce evidence, and `current_purpose_disallows_sales`
  for the saved reply. No delivery effect or Meta send request was created.

## Causes and corrective contract

1. **Product interest was missing from current-purpose admission.** The
   customer's ordinary Ukrainian interest phrase was not recognized. Explicit
   garment inquiries now permit product consultation in Ukrainian, Russian and
   English. Quoted/reported text, retractions, service holds, checkout consent
   and payment authority retain independent gates.
2. **Historical collecting shadows starved expired execution recovery.**
   Source-producer ownership admitted answered/unowed unsealed rows into the
   first 15 candidates, while the debt writer rejected them without changing
   state. The same rows were selected forever. Exact source eligibility now
   applies before the limit, and real execution claims/proposals take priority.
   A read-only MariaDB comparison selected `[210, 223, 225]`; the old selector
   instead returned 15 historical collecting rows and missed the incident.
3. **Legacy understood media was described as unreadable.** Unknown new media
   taxonomy must not negate an exact bound legacy `understood` image outcome.
   The response normalizer no longer adds that false limitation. Unknown or
   unavailable sibling parts still require clarification; no new taxonomy,
   social, reward or financial authority is inferred.
4. **Useful catalog-photo answers could remain semantic reply debt.** The
   delivery regression also checks that exact recognized reference media and
   the answered garment inquiry do not create a false unresolved question.
   Unreadable, omitted, mismatched or independently captioned media remain owed.
5. **Process health concealed overdue reply execution.** Health now reports
   an active claimed revision whose deadline and worker lease have expired
   without a recovery disposition. Its public health result degrades even
   when daemon/process heartbeat is healthy. Structured warning events retain
   proven client/revision IDs and finite reason codes, without customer text.

## Recovery boundaries

The incident's original 30-minute provider horizon had already expired before
this investigation. Installing the correction must classify that existing
proposal as operator work, preserve its sources/proposal, and avoid a blind
late generation or duplicate customer message. Customer delivery is a separate
receipt-backed action; a healthy daemon, successful Gemini result, deployed
commit or manager task alone does not prove the customer received an answer.

Automatic health/status checks make no provider requests. The tests use local
in-memory SQLite with mocked provider boundaries; MariaDB was used only for
read-only forensic and query validation. Production remains authoritative for
the final release and recovery-state verification.

## Validation evidence

- The exact text/photo regression traverses generation, proposal persistence,
  product selection, exact photo inspection, transport receipt and completed
  revision. It observes one generation and one physical mocked send; replay
  performs neither.
- Six recovery regressions include more than a full batch of historical shadows
  before an unsent proposal, repeat execution, unchanged source statuses and
  attempts, source-count mismatches, cutoff and rollback ownership.
- Media coverage remains unresolved for multiple product photographs, document
  siblings, omitted/unreadable parts, changed hashes and independent captions.
- The broad 384-test run had three failures from two existing test methods.
  Both methods reproduce the exact same three failures on a pristine
  `e01f7915a` checkout: RU/EN fallback-language expectations in
  `AuditedSizeResponsePlanTests.test_real_fallback_is_neutral_in_each_language_and_does_zero_provider_work`,
  and a historical `catalog_media` group expectation in
  `ResponsePlanSourceTests.test_photo_question_with_choice_closes_only_after_real_presentation_sent`.
  These baseline issues are not claimed fixed by this incident release.
- Final scoped release run: **383 tests passed** in 46.527 seconds. The two
  independently reproduced baseline methods above were explicitly excluded;
  they were not modified, marked successful or silently skipped. Django system
  check and scoped diff checks passed.
