# Live Instagram UX and dialogue QA — 22 October 2026

Status: first release `34e6d16ed` deployed and runtime/browser verified; owner-directed sizing and standard-funnel follow-ups locally verified; integrated release and conversion-audit extension are in progress. This is an evidence journal for the current release, not a completion checklist for Plan 90. Verified initial local/production baseline: `78861a018cd258b99b2c0bcdfbafad610e5c8013`.

The dated sections below preserve discovery and intermediate failures. Their pending wording is historical; the latest release-verification section at the end governs completion.

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


## First production release — verified 22 October, 20:29 UTC

Code `34e6d16ed32e9481a408897829bd856a6ffcc4fc` was committed/pushed to main and pulled over the documented SSH procedure after exact-code CI passed. [Django gate](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39845472800):741 Instagram regressions,11 backend skips,36 Node tests and static gate passed. [Native MariaDB gate](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39845472859):all five suites passed, disposable cleanup verified. Conversion-context suite took23m16s, motivating a separate additive native suite for the new diagnostics tests rather than removing coverage.

Production proof at20:29:36UTC: corev6, publication6/version6 (`03b42fab4c16b921105449c2baa07d05b89a8617bacfc89fbf8067ee1aa1993f`), permissionepoch10, migration0246, supervised child917362/parent917136 with exact loaded SHA, all10 workers healthy, maintenanceoff. Runtime verification rejected HTTP and non-read SQL. Exact hashes of all three changed management assets matched.

Authenticated Chrome loaded `bot_client_list.639596bfb3f2.js?v=live-list-v2`, corresponding CSS and `bot_chat_reading.9ca453a98de3.js?v=chat-reading-v3`. Client2, whose latest real manager message was20October, now appears fourth; it was absent from the previous top10. Rapid2→363→361 reselection ended with selected361, matching conversation header and URL; three manager voice players were present. The open card remained stable through background reconciliation. Desktop screenshot is retained privately. Production narrow-viewport override did not take effect (measured1816px), so do not claim a production mobile screenshot; the earlier exact-module narrow fixture and automated mobile reading-anchor tests are separate evidence. Temporary overrides were reset and the fixture server stopped.

Real fresh-inbound QA remains unavailable: Instagram showed a login form. The owner was asked to sign into confirmed test account@zainllw0w; no historical customer message was replayed and no takeover bypassed.

## Actual Gemini probes and resulting follow-up work

Two14-case managed synthetic prompt runs made28 successful provider requests, all durable analysis-lane graphs terminal with matching winner `gemini-3.5-flash-lite`, null customer/source/reply IDs.420 candidate attempts were cancelled before dispatch;28 succeeded. This is not customer delivery or a production latency SLA.

The first probe omitted the native source-language instruction; after using the real helper in production order, the second probe had no language drift. Existing native winner/repair/boundary guards already reject the observed wrong-language candidates. A malformed regex tuple in the first private evaluator caused one PatternError; the provider request itself succeeded. The evaluator was fixed and cached results retained. Do not classify that exception as a production/provider fault.

Raw proposals were not universally valid: conservative truth/context checks, prohibited controls in the no-actions probe, an unnecessary follow-up question and real specification confusion were observed. Every answer was manually inspected; none was sent to a customer. These probes expose model limitations rather than proving sealed execution failure. Native standard-funnel tests then confirmed two real English failures: `regular fit for…` parsed the preposition as a fit, and `regular` failed to acknowledge canonical `classic`. Both are repaired and verified through physical fake-HTTP receipts and completed semantic obligations. New10-case standard module plus adjacent tests:204/204 passed. Negative, quoted, wrong-fit and nonactive cart-line guards remain intact.

## Owner clarification and sizing authority — 23 October Kyiv

The owner clarified that finished garments are not separately weighed. Routine questions receive that reviewed policy directly, without an unnecessary manager case. Garment mass, fabric GSM and customer body measurements remain separate. The owner explicitly confirmed GSM depends on the specific base; blanket hoodie320 GSM authority is removed, while confirmed composition/cut facts remain. No350–380 mapping or numerical garment mass is invented.

Source/parser tests also found polite-comma requests being treated as garment choices. The repaired source parser preserves cart selection, distinguishes body measurements and fabric density, and keeps independent price answers. A new policy capability initially exposed a missing-schema guard in material validation; fresh review caught it before release, and only actual reviewed base/category capabilities now grant material authority. Weight/source/parser batch91/91 passed; schema/grammar focused36/36 passed. These follow-ups still require the next integrated release.

The site's height/weight formula is a T-shirt estimate. Explicit hoodie/non-T-shirt category must outrank fuzzy T-shirt catalog text. Incompatible availability could previously recommendS for an estimatedXXXL; the corrected advisor fails honestly and clears stale output, retaining only immediate neighbouring candidates with an approximate-fit explanation.26 Node advisor tests passed.

Read-only production inspection found26 hoodie products using catalog2/grid2 with exactly the historical generated six-row preset, no image or review provenance. The default seed command generated those values; persisted JSON is therefore not sufficient proof of measured owner data. New shared legacy-seed detection quarantines those numerical cells on public/PDP/bot paths, while preserving sellable sizes and genuinely configured custom measurements. No production DB rewrite or invented replacement chart is authorized/performed. The promised actual hoodie chart remains an owner input.

## Conversion analysis extension — implementation in progress

Priority is the ordinary retail path and demonstrable payment/conversion truth. Existing episodes, order-deduped paid cohorts, source-bound analysis and protected five-revision technical observations remain authoritative in their distinct scopes.

The existing analyzer uses one bounded Gemini call, but its input watermark excludes the final bot reply sent after that input. Prompt-only diagnostics cannot evaluate that response. Assigned implementation adds narrowly receipt-bound reply-review context, a bounded diagnostic object on the existing snapshot and operator/statistics projection. Customer-reported reasons must have an owned causal refusal quote; price concern or silence alone remains a hypothesis/unknown. Findings must not mutate payment, consent, funnel/lost_reason, follow-up authority or durable preference memory. Model readiness is uncalibrated; it is not an observed purchase probability. No added provider loop, automatic historical replay or time-aging fingerprint is permitted.

No completion/deployment claim for this extension is made until code, native tests, exact release, actual analyzer probes and authenticated UI verification are recorded below.


## Conversion audit implementation and integrated follow-up — 23 October

The existing conversation-analysis request now includes a bounded receipt-backed review of up to four revisions/eight SENT effects. Customer intent still uses admitted customer sources; accepted sending, readable text, and response time use separate diagnostics. Pre-SENT and media-only content remain explicitly unobservable where appropriate. This is the same provider call, not a second analyzer, an automatic prompt rewrite, or authority to change memory, consent, payment, cart, outreach, or manager cases.

The additive `0247` migration stores bounded normalized `conversion_audit` on the existing immutable analysis snapshots. It stores exact customer evidence, source scope, motivations, readiness evidence and reported/possible nonconversion reasons, without duplicating full outgoing texts or provider MIDs. Existing empty snapshots retain their fingerprint: introducing the field, opening a page, or advancing the clock does not initiate historical provider work. New audited state has a receipt-context digest; a concurrent receipt change discards only obsolete diagnostics while preserving still-valid baseline analysis.

CRM detail shows facts, customer-reported explanations, hypotheses and counterevidence separately. AI readiness is explicitly uncalibrated; unknown/historical/noncommercial states do not become zero-percent conversion. Selecting another episode cannot reuse the current purchase estimate. A valid source-bound current baseline readiness assessment remains visible when the optional new reason audit is missing; missing reason coverage does not erase independently valid prior analysis. Lightweight list/poll paths return metadata without expensive audit serialization. Aggregate statistics are DB-only, at most 31 days and 50 qualifying episodes per page, with explicit coverage/selection limits; paid outcomes use existing canonical payment authority. Silence is an observed event, never a proven loss cause. This report does not replace the existing five-revision technical sample or mature paid-cohort conversion metrics.

Independent review found and repaired three attribution defects before release: the Ukrainian word «мене» accidentally matched a negation suffix; «не купую через розмір, ціна ні до чого» could falsely become a customer-reported price cause; and negated gift intent could be classified as explicit gift motivation. Positive and negative UA/RU/EN cases now have exact-quote regressions. A real source-edit reproduction also showed statistics retaining an obsolete receipt sample; statistics now reuse canonical current-source and whole-send proof with bounded prefetched rows. Default dispatch/finalization proof behavior remains unchanged and independently passed source/effect negative-parity checks.

Root local checks: 90 runner/workflow/Stage-0 contracts passed; 72 Node tests passed, including all ten analytics UI tests; static pipeline and system/migration checks passed. The first 816-test integration run found two failures: an obsolete 320-GSM journey fixture and loss of exact product identity when the selected base lacked approved composition data. The fixture was updated to the owner policy; the identity fix preserves proven SKU identity without granting fabric authority. The corrected product-identity/weight/knowledge boundary batch passed 74/74, including the unchanged native different-SKU isolation test. The exact final CI revision remains the release gate. Focused final audit/analysis/freshness/payment-metric/revision-delivery integration: 72/72 passed. Size-authority integration: 37/37 passed.

Remaining owner data: a reviewed numeric hoodie measurement image and per-base density mapping. No replacement chart, numeric garment mass or universal hoodie GSM was invented. Private prompt probes are synthetic model evaluations; they do not establish real image recognition, Instagram delivery, or a conversion uplift.


## Live restart gap found during final production QA — 23 October

A new read-only runtime check found seven child restarts under the same stdlib supervisor after the first release. Two child exits were observed as SIGKILL (`-9`); the initial process ran 2,621 seconds and another 145 seconds. Several successors exited cleanly within 2–7 seconds. The daemon log contains the exact message `analysis lane owner busy — daemon exit`; the configured watchdog is the supported stdlib supervisor, not a duplicate Django fallback. On one measured recovery, sticky supervisor backoff plus the busy-owner exit left 127.017 seconds before the next successful child start, excluding its startup time. A currently healthy pulse had concealed this intervening availability gap.

Confirmed application coupling: `_forever_locked` acquired the 90-second conversation-analysis lease before publishing its pulse or starting any customer worker. An orphaned or legitimate manual analysis owner therefore blocked customer service. Five renewal-failure branches also set the shared daemon stop event. Analysis is optional background work and must not control reply-process availability.

The corrective slice moves ownership acquisition, renewal, context binding and exact-generation release into the analysis worker. Busy/frozen/lost ownership pauses that worker with bounded five-second coordination waits; it does not stop customer lanes, steal a lease or unfreeze a manual owner. `waiting_owner` proves a live coordination attempt while explicitly denying readiness and claim admission; actual stale progress and DB errors remain unhealthy. A renewal is added between the potentially slow analysis and event consumers, and shutdown starts no further event consumer after analysis returns. Independent native-model/outbox tests prove one accepted `SENT` reply while analysis is waiting; source-based startup tests prove the customer pool starts without acquiring the analysis lease.

The initial SIGKILL source is not established. Current observed child RSS was about 175 MiB, but that does not prove historical peak memory or exclude hosting limits. Kernel/LVE diagnostic interfaces are unavailable in this hosting account. No host limits were changed and no OOM or Meta attribution was invented. The application coupling and consequent recovery amplification are independently reproducible and are being fixed; external process termination remains a separate operational uncertainty.


### Final integrated and real-provider checks, 23 October Kyiv

The `9a61efdef` local Instagram gate passed **1036 tests, 12 backend-specific skips**,254.659s. This includes the previous delivery/funnel/context/identity work plus analysis-owner isolation. The earlier `d25835cd4` exact-code Django CI passed826 tests. Its native matrix passed five suites, but the new dialogue-quality suite failed with one MariaDB1062 duplicate-key error; it is being reproduced in a disposable local database rather than attributed to customer/provider failure or hidden by a SQLite result.

Six frozen final analyzer prompts were submitted through the existing production managed gateway before deploying the new analyzer modules. All six durable request/attempt graphs completed with null customer/source/reply lineage. They are fictional prompt probes, not deployed analyzer-job execution or customer delivery. Strict CURRENT local normalization/evaluation found four cases needing work: a price objection was overstated as a customer-reported lost cause; a receipt-backed irrelevant reply was not diagnosed; generic purchase interest was incorrectly asserted to mean a personal recipient; and a valid own-customer quote after reported speech was rejected because punctuation was stripped during source projection. The latter is a normalizer defect; the other cases require clearer instructions. Existing defensive normalization already demoted the unsupported price/self claims. Original raw results remain private and immutable; a corrected, separately frozen probe is required before closure.

The corrected six-case managed probe passed **6/6 strict raw-output and CURRENT normalizer checks**. All six fictional user inputs and strict assessment logic were unchanged; only the production prompt contract and the independently verified punctuation-preserving source fix changed. Actual winners were Gemini3.6Flash and3.5FlashLite through existing routing. Price concern remains a hypothesis; an explicit causal refusal remains customer-reported; the wrong/partial SENT reply produces a relevance hypothesis with its exact admitted revision-effect reference; unavailable SENT and prior paid support produce no invented loss cause; the independent own-self statement survives quoted foreign speech. Twelve analyzer provider requests total cover the original and corrected batches; these are not customer deliveries or a calibrated business-effect measurement. No duplicate post-deployment batch is required.

Fresh independent review also reproduced two opposite aggregate freshness failures: a newer admitted manager echo left an old audit current, while a higher-PK delayed older customer event incorrectly invalidated a still-current audit. Statistics now reuse canonical analyzer source admission and provider-event ordering with PK tie-breaking, within the existing bounded query budget. Twenty statistics regressions pass, including foreign/excluded-source controls.
