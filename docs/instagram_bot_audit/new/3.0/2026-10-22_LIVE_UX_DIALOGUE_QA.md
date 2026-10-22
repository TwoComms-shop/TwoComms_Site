# Live Instagram UX and dialogue QA — 22 October 2026

Status: implementation complete; integrated local verification passed; commit, production release and actual Gemini probe pending. This is an evidence journal for the current release, not a completion checklist for Plan 90. Verified initial local/production baseline: `78861a018cd258b99b2c0bcdfbafad610e5c8013`.

## Customer-visible contracts

- Sort conversations by the newest actual incoming or successfully sent/observed outgoing message, including a manager's native Instagram voice message. Use provider event time; importing old history must not promote a conversation. Keep CRM inbound timestamps and Meta permission-window anchors independent.
- Reconcile rows in place and animate their movement. Preserve selection, focus, text selection, avatars, and reading position; settle animations under reduced motion or hidden-page conditions.
- Refresh an open conversation and its card without reloading the page. A late response for A must never mutate B. Persisted card changes must refresh even when there is no new text. Preserve unfinished operator drafts.
- Profile fetching must not block customer replies. Persist proven name/username before a potentially slow avatar download. Honor provider cooldowns.
- Unknown questions receive an honest acknowledgement and one exact, updated manager brief. Independent answerable questions continue. A manager notification alone is not a delivered customer reply.
- Product, fit, size and language remain source-bound. Never invent hiring acceptance, payment, availability, production timing, discounts, or sensitive customer characteristics.

## Confirmed causes and changes under verification

| Finding | Evidence | Remediation / remaining verification |
|---|---|---|
| List order depended on `last_message_at`, whose poll update was inbound-only. Native manager replies did not necessarily advance it. | `bot_clients_api`; poll persistence; incoming/outgoing transcript model. | New read-only activity projection uses actual message event time and successful outgoing state. Verify against MariaDB and the reported live voice message. |
| Zero-new-message polling did not invalidate the full client card; cursor zero requested a heavy full detail. | Full/incremental API and inline polling code. | Opaque persisted `detail_revision`, explicit `poll=1`, guarded/coalesced detail refresh, visible-only 120-second reconciliation fallback. |
| List replacement and stale requests could disrupt focus, reading position or selected client. | Existing render/open/poll flows. | Keyed reconciliation, interruptible 340 ms FLIP movement, selection/generation/connected-element guards. Browser verification pending. |
| Profile provider work ran in a 15-minute main-service lane; name saving followed avatar I/O. | `run_instagram_bot`, `ensure_profile`. | Existing background refresher handles bounded urgent profiles; proven identity commits before avatar I/O; no profile HTTP on the customer response path. Discovery failure retains its 120-second cadence. |
| Textless native audio/video used an image label, and the projection verifier expected that same incorrect label. | Revision echo projector and typed projection matcher. | Both sides share typed attachment labels; native accept/drain/dedup tests. No historical source promotion. |
| Spoken double-XL aliases selected XL or nothing; a negated alias could select XL. | Native `parse_turn` tests: `два XL`, `дві іксель`, glued variants, `Не хочу два XL`. | Dedicated parser repair and affirmative/quoted/question/negative/quantity regressions in progress. |
| A second unknown product question opened another manager task instead of updating the first exact brief. | Native revision fake-HTTP test; both acknowledgements had receipts, but two tasks remained. | Source/epoch/episode/product-safe merge in progress. Closed cases and distinct products must remain isolated. |
| Composition acknowledgement left semantic debt; “I will also check…” falsely failed material truth validation. | Native revision tests, not merely a prompt hypothesis. | Align topic aliases and narrowly recognize honest future checking; retain fabric-fact authority. |
| Invented “You are hired” / “Вас прийнято на роботу” passed the send guard. | Native revision pipeline physically fake-sent the candidate despite unknown hiring status. | Affirmative employment-decision guard and native no-dispatch regression in progress. |
| A fresh answer could retain an unsupported short wait apology. | Native fresh source + candidate `Перепрошую за очікування.`. | Precise same-source delay-apology normalization; preserve useful answer and unrelated empathy. |

## Existing safeguards verified by source review

The selected-product fit consultation already distinguishes product-specific photographs from illustrative examples of another design. Communication preferences already have bounded source-backed verbosity/emoji controls. Narrative memory excludes contact details and keeps dated observations. The editorial `brand.md` archive is not runtime authority. These are existing contracts to preserve, not newly discovered missing features.

## Prompt and sales criteria

Use concrete, verified information tied to the present question. Answer known questions before asking at most one necessary clarification; a complete answer may need no question. Keep tone and emojis responsive to explicit preferences and current content. Do not infer military status, income, personality or private life from a print, photograph, language, or writing style.

A Direct-work offer can receive thanks, a truthful brief explanation of the virtual assistant and human escalation, current leadership review, and conditional follow-up here after that review. It must not invent hiring, conditions, autonomous learning, or a future talent pool. The existing chat is a contact channel; another contact method is optional when useful.

External evidence informs evaluation, not a promised Ukrainian conversion uplift:

- [Packard & Berger, concrete language in customer service](https://doi.org/10.1093/jcr/ucaa038): use specific language that demonstrates understanding; do not transfer an effect size to this brand.
- [NN/g original chat UX study](https://www.nngroup.com/articles/chat-ux/) and [chatbot design guidelines](https://www.nngroup.com/articles/ai-chatbots-design-guidelines/): avoid repetitive questions and canned replies; study scope limits generalization.
- [Google conversation questions guidance](https://developers.google.com/assistant/conversation-design/questions): focused clarification; voice-design guidance is an interaction principle, not Instagram platform documentation.
- [USF 2026 research summary](https://www.usf.edu/business/news/2026/04-20-chatbot-empathy-can-worsen-customer-reactions-usf-study.aspx): excessive emotional mirroring may backfire; institutional summary is not full-paper evidence for a universal rule.
- [NN/g transparency and handoff dimensions](https://www.nngroup.com/articles/dimensions-of-ai-chatbots/): natural conversation must remain truthful about assistant identity.
- [NBU financial-literacy material](https://harazd.bank.gov.ua/uploads/0/643-finansova_mudrist_vipusk_3.pdf): clear, inspectable official payment paths when relevant; no unsolicited fear-based script.

## Verification ledger

Worker results are preliminary until integrated source freeze and root review:

- Motion module: 8 Node tests; independent jsdom checks of retained row/image/text identities, focus and selection. Actual browser proof pending.
- Live UI: 25 combined Node tests for list, chat reading, conversation scroll, stale request guards and refresh coalescing. Actual browser proof pending.
- Identity/echo/runner: 185 focused tests, one existing skip; no real provider I/O. Background failure cadence regression included.
- Backend: activity/revision tests and 41 existing client/cursor tests passing; expanded final batch pending. Fixed query count alone is not MariaDB latency proof.
- Native simultaneous-client gate: disposable MariaDB migration/test run pending. Production is never a test database.
- Dialogue baseline: 11 test methods, four passed and seven failed before repairs; fake model candidates and fake HTTP test server behavior, not Gemini's actual wording.

## Release acceptance still required

1. Finish and independently review scoped fixes; run meaningful integrated regressions on frozen sources.
2. Actual managed Gemini evaluations with synthetic cases and no customer delivery; record hard factual/intent/product/privacy failures separately from tone.
3. Browser motion/live card/reselection QA and authenticated production checks with loaded asset URLs.
4. Scoped commit and push to `main`, documented SSH `git pull`, required migration/publication/static refresh, supervised-child loaded SHA and runtime identity proof.
5. Document remaining limits honestly, including unavailable fresh inbound test or closed messaging windows. No synthetic production messages, historical replay, or unverified end-to-end delivery claim.

## Root integration evidence (pre-release)

- Read-only production diagnosis at 19:28 UTC: checkout and supervised daemon still on baseline `78861a018`; ten worker lanes healthy. Published core v5/head5 match the previous release. Client361 now has real subsequent incoming messages and native manager voice messages; the voice metadata is `audio` while old persisted echo text said “image”. No historical record was rewritten. Human takeover remains respected.
- Candidate service executed in memory against live MariaDB under a strict SELECT/SHOW/EXPLAIN-only wrapper; HTTP blocked. Current cardinality: 334 clients, 288 visible, 3,338 messages. Legacy ordering median6.726ms; candidate ordering median8.545ms (three repetitions each, limit20 with current related joins). Indexed per-client transcript reads are used. Detail-revision median48.354ms/51.356ms/52.581ms for clients361/359/5; each is one annotation query, tokens stable across these reads. These are current-size ordering/revision timings, not total endpoint latency or future-traffic load proof.
- Real Chrome module fixture: manager5→1 retained all keyed rows/name text nodes, focus and selected text on client3; requestAnimationFrame positions moved continuously during the actual WAAPI animation. Rapid updates6→5→4→2 settled in correct order with no remaining animations and preserved selection. Scrolled reading-anchor delta was0px. This fixture uses exact production module hashes and synthetic rows; it is not a production-message event.
- Native disposable MariaDB11.4.12: five tests passed, zero skips,13.533s. New three-client/burst/capacity/drain tests plus two existing controls. Two healthy receipt times1.0534s/1.9389s while a third fake-provider request remained held until2.1001s; third receipt2.4063s. Three real DB connections, one unique SENT receipt per client, all five incoming fixture rows processed, leases released. Initial diagnostic failure was a canned reply that failed to answer the fixture's extra size question; semantic debt correctly remained. The repaired fixture keeps one intent and verifies all burst texts reach the sealed payload. Test timings are not provider SLA or OS-crash proof. Both disposable runs verified schema/user cleanup, absent process, closed port and removed datadir.
- Root Node gate:32 tests passed before the final older-history anchor regression; worker final batch33 passed. Static pipeline passed (1,974 asset files,571 manifest entries, compressor rendered URLs).
- Initial broad integration:703 tests,11 backend-specific skips, one expected obsolete test assertion (`33 !=17`) after the newly required brief merge. The assertion was corrected to exact source union/current-source subset rather than weakening native delivery proof. Final frozen integration run remains required.
- Independent reviews found and fixed stale A→B action effects, mobile background-refresh reading jumps, missing-motion-asset workspace failure, older-page reading jumps after event-time sorting, additional hiring-acceptance wording, and malformed case JSON throwing during receipt validation. Each received a focused regression; final integrated verification pending.
- Further native reproduction confirmed the same silence mechanism in B2B and generic manager questions: Q1 acknowledgement completed, but appending Q2 replaced the task's source anchor and caused `pending_question_context_changed` before physical delivery (one generation, zero HTTP, zero effects; revision still claimed). A creator→business follow-up also overwrote the earlier creator brief. Shared repair is implemented: stable case anchor, immutable per-revision scopes, subtype isolation, independently proven recorded-question projection and separate notification history. Product, generic-manager and B2B Q1→Q2→Q3 now send/finalize; creator→business keeps separate cases. A Q2 with a SENT acknowledgement and still-owed independent price answer remains visible as pending, without falsely settling response debt. A mutable JSON legacy flag cannot waive snapshot proof. Focused210 tests and two final corruption tests passed; fresh independent review found no actionable issue.
- Actual authorized full→poll→full→poll integration with a real fixture transcript, after one warmup: stable revision token and zero semantic DB writes with unmocked card/journey/selection/follow/debt helpers. Bulk name/takeover update invalidates exactly once; subsequent sequences remain stable. All18 activity/revision tests pass. No background refresh-loop defect was found.
- Full profile/avatar legacy fixtures initially failed: isolated original-baseline harness reproduced all seven failures (missing synthetic provider identity and an incomplete URLconf), before correcting fixtures. Both complete modules plus new identity suite now pass24/24, zero skips, network denied. Source authority guards were preserved; retired main-service profile constants/budget/import were removed.

Private evidence stays under `/private/tmp/twc-live-*` and `/private/tmp/twocomms-burst-fairness-native-20261022/recheck/`; no customer text, credentials, SQL parameters, or full provider payloads are copied into this journal.

### Integrated local gate

Root run of the exact55 Instagram modules now selected by CI: **739 tests, zero failures/errors,11 backend-specific skips**,228.827s. The skipped native concurrency checks have a separate actual MariaDB proof above; GitHub's exact-commit native matrix is still required. Final UI runtime tests: **36/36**. Django system check, migration-drift check, static/compressor gate and MariaDB runner/workflow contracts passed. Profile fixtures now participate in the broad gate. Only45 explicitly listed release paths are staged; unrelated untracked workspace files remain untouched.

The final root code review additionally identified per-scope notification reads in the product-merge validation path. They are now batched once, client-filtered, with exact notification objects passed to unchanged receipt validation. Native two-versus-three prior-scope tests observe one notification read and no query growth; a missing notification denies the merge without fallback reads. Latest focused56 tests passed after this refinement. No other source changes are planned.
