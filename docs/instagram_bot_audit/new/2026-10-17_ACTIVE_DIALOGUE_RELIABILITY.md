# Active Instagram dialogue reliability — 2026-10-17

Continuation of `2026-10-17_REPLY_SILENCE_INCIDENT.md`; baseline release
`b35177076`. This audit distinguishes an unanswered request, a confirmed
physical send, a question awaiting the customer, and a case awaiting a manager.

## Confirmed defects and changes

1. Expired-work selection could repeatedly return a delivered selector question
   awaiting the customer. Operator review previously closed the alert without
   disposing of the execution owner. Source-bound waiting/handled/replied
   dispositions now clear execution leases and are excluded before queue LIMIT.
   Stale recovery transitions cannot revive those owners. New inbound retains
   its own successor and can proceed.
2. A confirmed human reply did not settle even a matching one-source execution
   obligation. Settlement now checks the immutable complete SENT command and
   exact context source. UNKNOWN, failed, partial, unrelated, and multi-source
   obligations are not guessed complete. Explicit operator review handles
   reviewed multi-source cases without inventing transport receipts.
3. Fast media failures renewed the ten-second preparation deadline at every
   lease reclamation. The original deadline is retained; pending work resumes
   at that deadline, and stale preparation ownership stops additional fetches.
4. Neutral ingress observations and won proposals with no planned delivery
   could prevent fragmented-message transfer. Validated inert observations and
   exact successful unsent proposal sources can now follow the successor.
   Original evidence remains, semantic coverage is recalculated, and the same
   provider economic budget remains spent. Any planned/SENT/UNKNOWN delivery
   keeps the existing no-transfer boundary.
5. The parked collaboration lane bounded its queue before rejecting unchanged
   impossible work. Exact inexpensive exclusions now precede LIMIT; remaining
   content rejections receive one durable disposition and manager obligation.
6. Positive requests with a conclusively failed generation could silently lose
   the existing acknowledgement/handoff because a nonexistent optional flag
   disabled the entire fallback. Positive failed-request handling now runs;
   neutral acknowledgements retain their optional gate. Existing source,
   permission, failed-graph, handoff, and once-per-wait proofs remain required.
7. A changed message under the same database ID was not independently checked
   at every physical-send boundary. Current source identity/content digests are
   now recomputed. Legitimate image inspection projections do not invalidate
   the original source. Audited pretransport scope failures remain known
   non-dispatches rather than ambiguous sends.
8. A changed technical-debt fingerprint after a terminal review could not be
   explicitly triaged again. A new observation can now be acknowledged/claimed
   with history preserved; unchanged reviewed observations remain idempotent.
9. Repeated read timeouts on the same primary Gemini model consumed both scarce
   dispatches. Rotation after the first scarce read timeout preserves candidate
   ordering, the deadline, and the 8 HTTP / 2 scarce / 1 repair limits. Timeout
   evidence remains distinct from an empty response. Repair guidance now covers
   the strict media-observation shape.
10. Confirmed human replies with NULL `provider_created_at` were absent from
    unified prompt history. Live revision 228 omitted message 3386, which named
    the customer's design; the next response consequently asked the design
    again. History now uses proven human-send time before the sealed watermark,
    preserving strict receipt and recipient matching. This grants no product,
    payment, availability, or checkout authority.
11. Ordinary follow-up queues could synchronously process 20–50 sends each
    before the next active-dialogue tick. They now rotate one-item slices,
    check queued customer input between callbacks, and yield after a small
    cooperative time slice. Continuous inbound retains finite fairness for
    background tasks. Parked historical holding work is capped at one attempt
    per revision pass. Notification/profile batches are also limited to one,
    with an input check between them; DB/privacy maintenance retains progress.
    Permanent blocked inbox history and ownerless legacy rows without an active
    consumer do not masquerade as ready customer work. An already running HTTP
    call is not interrupted.
12. Paid-shipping discovery selected unbound customer choices before finding a
    paid order. An older unpaid choice could hide a newer paid winner. Discovery
    now filters actual paid proposal/order/attempt owners before LIMIT, while
    the existing binder still verifies exact signed terms and allocations.

## Customer 360 evidence

- Revision 226's authorized fresh Gemini attempt failed its deadline: two 3.8
  read timeouts, a Lite schema rejection, and a Lite repair timeout. It sent
  nothing. The evidence does not establish that API credentials were invalid.
- Human command 1 sent a safe excerpt of revision 225's actual prepared Gemini
  text, naming «Правил немає» and asking fit/size. Instagram returned a provider
  message ID; message 3386 is the confirmed transcript. No stock/price promise
  was added.
- The customer replied with message 3387 asking about fits. After an audited
  return to the bot, revision 228 answered automatically using Gemini Lite.
  The subsequent request for examples was also processed. This established
  working ingress/generation/delivery, while exposing the history defect above.
- The second automatic request (3389) was received at 13:36:46.211 UTC;
  generation finished at 13:36:53.122, the first physical receipt at
  13:36:55.825, and final text receipt at 13:36:57.104: about 10.9 seconds
  from persisted input to final send. Its provider timestamp was 13:36:42.286.
  This measures one actual request, not a universal latency guarantee.
- The stale product control in superseded revision 225 is not promoted into a
  current basket. A prepared proposal is not committed customer selection.

## Verification and operational boundaries

Focused regressions exercise real enqueue, preparation, sealed capture,
generation/outbox receipts and mocked physical delivery, including forged
proofs, exhausted shared budgets, UNKNOWN outcomes, new inbound and operator
actions. Production MariaDB is used only for live runtime and scoped audited
operational review, never as a test fixture.

Historical partial sends and blocked unproven webhook identities remain audit
evidence. Reviewing them must not rewrite them as full delivery or replay
unproven input. Provider outages can still consume the bounded reply deadline;
the bot must acknowledge/escalate once and preserve the customer's obligation.

Release validation and production verification are recorded after the final
scoped run and deployment.

- Broad integrated run: **1,173 tests OK (13 database-feature skips)** in
  150.864 seconds, with no excluded test methods. Previously stale fixture
  contracts were updated to real policy publication, source intelligence,
  product facts, exact checkout revision/digest, and repeat-purchase evidence;
  production authority checks were retained.
- Later adversarial review added the reset/late-human-projection boundary and
  service-lane priority refinements. Their affected capture and scheduler
  suites passed (71 tests; 146 tests with one database-feature skip).
- A subsequent expanded run was stopped when an older split-execution fixture
  attempted external HTTP with its synthetic token. The shared fixture now
  blocks ancillary transport before setup; exact card/payment stubs retain
  their existing assertions. The final release run also blocks socket connects.
- Final frozen core run: **1,344 tests OK (15 database-feature skips)** in
  201.216 seconds. The process-wide socket guard blocked 29 ancillary connection
  attempts before transport; none was allowed to reach the network. The
  wrapper's additional zero-attempt assertion returned nonzero despite the
  entire Django suite passing; this is recorded separately from test outcomes.
  These callbacks were denied, not replaced with successful delivery receipts.
