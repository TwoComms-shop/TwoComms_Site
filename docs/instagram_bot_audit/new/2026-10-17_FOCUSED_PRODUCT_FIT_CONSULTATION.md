# Preserve the chosen print through fit consultation

## Actual failure

Customer 360 sent a product photograph and said she wanted that shirt. The
original sealed identification resolved product 112, «Правил немає», with a
single confident product candidate and exact admitted-image evidence. Its
financial/control execution failed; that does not erase what product the
customer was discussing and does not authorize executing the old control.

The bot subsequently answered a cut question by asking which design, then
answered “show examples” with three unrelated product choices. The customer
had to correct it: she wanted photographs of the cuts. A manager supplied two
examples; the customer then asked whether there was a women's version.

Native catalogue evidence for 112:

- Published T-shirt, active classic and oversize options.
- Explicit unisex/women audience metadata; unisex expands to both audiences.
- Native colour image 85 explicitly describes an oversize T-shirt. Images
  86–88 have no fit description; no image proves classic.
- Product description, active options, photographed cut, and physical stock
  are separate facts. None substitutes for the others.

## Desired customer experience

The customer has already done the work of choosing a print. Reopening the
whole catalogue adds decisions and makes the assistant appear not to listen.
Continue with the same print, answer the question she actually asked, and ask
only one unresolved selection question. Do not add pressure, invented urgency,
or an unsupported assurance that every cut fits every person perfectly.

1. Acknowledge the identified shirt; explain classic/regular versus oversize.
2. Show a verified photo of that print and cut when one exists. State clearly
   when the requested cut has no verified photo for this print.
3. If useful, show a separately labelled cut illustration using another print:
   “приклад посадки, інший принт”. It illustrates the silhouette, not a new
   product suggestion. Size-chart measurements can support the comparison.
4. Answer a women's/men's question about this same shirt using its unisex facts.
   Do not redirect to a different assortment just because gender was mentioned.
5. Continue with the next missing fit/size choice. A fit correction changes the
   cut, while an explicit new-print request releases the old discussion focus.

An appropriate answer to the actual audience question is: “Ця футболка
«Правил немає» — унісекс. І класичну (regular), і oversize носять і дівчата,
і хлопці. Різниця саме в посадці: класика — звична, oversize — вільніша.”
This is a scoped product/base fact, not a guarantee about stock or every future
catalogue item.

## Technical contracts

- Discussion focus is a source-proved, bounded read projection. It can survive
  failed delivery or financial-control execution while granting no checkout,
  stock or payment authority. Revalidate original source, image identity,
  successful identification graph, product publication and reset boundary.
- Keep original print focus through cut questions, cut-photo requests and
  audience questions. A new customer photo, explicit different-print request,
  contradictory source, reset or damaged evidence releases it.
- A fit consultation is a distinct response-plan capability. Generic
  `show_products`/catalogue fallback cannot satisfy it using unrelated prints.
  Prompt guidance and server-side response validation consume the same DTO.
- Photo-fit evidence comes from explicit, unambiguous merchant-owned native
  asset descriptions, bound to their current asset owner and description
  digest. A filename or active fit option does not prove what a photograph
  depicts. Negated, mixed and ambiguous descriptions abstain.
- A dedicated fit-illustration manifest labels every asset and carries no
  product-choice action or “shown product” projection. Recheck asset/focus
  bindings immediately before physical sends, including template fallbacks.
- Approved fit/audience answers can reuse the existing deterministic-input,
  authority, semantic-coverage and canonical-outbox contracts. Pure factual
  questions need not consume a provider deadline. Mixed price, order, payment
  and selection obligations still require their ordinary authorization.
- Manager takeover, opt-out, erasure, new inbound and unknown delivery remain
  independent fences. A useful factual answer cannot falsely settle another
  commercial or delivery obligation.

## Continued-silence root cause

Revisions 230/231 never reached model admission. Their original execution
records say `pending_inbound`; a later expiry path mislabeled missing provider
manifest as the cause. Their incoming receipts 806/813 were already processed.
The only matching earlier receipt still unprocessed at both executions was
803: the bot's own native-carousel echo from revision 229/effect 68.

Release `86df94f2b` added exact owned-template handling. The explicit repair of
803 matched its existing SENT effect, projected existing history, and completed
the inbox event without another send. Current client ingress fence then read
false. Unknown historical echoes were not replayed.

## Live response and operational evidence

- Controlled human command 2 answered source 3395 with the exact-print unisex
  explanation; Instagram confirmed SENT. The source-bound manager obligation
  (task 122/revision 231) completed automatically with the physical receipt.
- Task 121/revision 230 was explicitly reviewed after the manager's two fit
  photographs and the customer's acknowledgement of those examples.
- The client was returned to automation. This does not replay either answered
  question. Existing historical technical cases were reviewed through audited
  lifecycle services; receipts and blocked unproven identities were retained.

Validation and release results are appended after the integrated checks.

## Repeated stall after choosing classic

Source 3397 is the customer's exact answer “Звична” to the confirmed question
3396, which explicitly contrasted “класика — звична” and “oversize — вільніша”.
Both revision 232 and its recovery child 233 reached Gemini and received HTTP
200, but their graph terminated with `local_semantic_rejection` /
`catalog_selector_missing`. The response plan still required a product choice;
a sensible next-size question therefore failed validation. Unchanged outage
recovery repeated a content failure instead of fixing the lost selection.

The repair binds a terse answer to its actual preceding delivered fit question,
persists that customer preference in the source decision, and carries the
identified print into the current selection candidate. A fresh current winner
still uses the existing authorized product/fit/size actions. No old proposal
or failed checkout control is executed. A next size-only answer can recover
the immediately preceding customer fit preference by revalidating the same
question/source evidence; manager prose alone is not selection authority.

After the graph's bounded semantic repair is exhausted, a proved content
rejection cannot spawn another unchanged outage child. Safe source-bound
clarification runs first; otherwise the existing manager obligation is created
once immediately. Provider outages retain their bounded recovery policy.
Permission, answered-source, new-inbound and uncertain-delivery fences apply
before creating any new obligation.

Controlled human command 3 answered source 3397 with “Домовились: «Правил
немає», класичний крій (regular) 🤍 Який розмір вам потрібен?” Instagram
confirmed SENT with a physical message ID. This operational response is
recorded as a human command, not falsely attributed to a new Gemini winner.

## Verification boundaries

The initial integrated run executed 1,431 tests and found one actual selector
precedence regression. The unchanged audited-size-clear test required `size`,
while advisory fit context returned `fit`. Canonical readiness now wins over
advisory discussion context. A focused selection candidate also abstains when
an audited set/clear supersedes its sealed source. The subsequent 73-test run
passed, including the original failing test. A separate actual-manager-clear
regression proves the old sealed `M` remains in history but cannot become a
current selection candidate.

Recovery checks include a confirmed manager answer cancelling the failed
lineage without opening another model request or creating new reply debt.
The completed task's JSON label also uses its final status, even if the legacy
approval field remains pending. An authenticated API regression covers this.

The full local suite uses the repository's deterministic SQLite test settings.
A process-wide socket blocker prevents real external connections; blocked
ancillary connection attempts are counted rather than misreported as zero
attempts. SQLite skips do not prove MariaDB concurrency behavior. Production
acceptance separately reads the actual source/question proof, current product
focus, native fit-photo metadata, physical human receipt, queue ownership and
deployed commit. It does not replay the customer's answered source or send a
synthetic customer message.

Final integrated result: **1,433 tests, OK, 15 existing SQLite feature skips**
in 237.401 seconds. The blocker counted 29 prevented socket attempts; no real
external connection was permitted. The additional real manager-clear candidate
test also passed separately. Django system checks and staged diff checks pass.

## Live next step: size chart

After release `e720711e7`, the customer sent source 3399, “Скиньте розмірну
сітку”. Manual resume 235 reached Gemini; both HTTP 200 answers were rejected
with `unauthorized_url,configuration_mismatch`. The now-bounded recovery
correctly created one manager obligation instead of repeating the same graph.

Two independent gaps required correction. The chart producer only accepted a
canonical selected product, although the discussed print and the customer's
classic preference were already proved. Also, the prose truth parser read
`size_grids` inside a chart URL as a size assertion. URLs retain their separate
exact allowlist check and are masked before prose claim parsing; neighboring
stock, price and configuration statements still need their own authority.

The chart uses a presentation-only certificate proving product 112 and classic
from source 3397 and its delivered question 3396. It neither selects a SKU nor
authorizes stock or checkout. The existing signed native chart manifest and
outbox deliver the exact chart image. Only that bound image covers the new
size-chart obligation; prose, product cards and unrelated obligations do not.
Pure chart requests can use this path without opening a Gemini graph, while a
previous failed graph remains failed. UNKNOWN and changed-source/asset guards
still prevent replay. A bounded preference reader preserves the classic choice
through chart questions into a subsequent M reply, stopping at ambiguous or
conflicting customer changes.

The actual native source is grid 6, “Класична футболка — CRC FS-101”, assigned
to product 112/classic/variant 83, file `size_grids/classic-tshirt.webp`.
Controlled human command 4 sent its verified public image URL to source 3399;
Meta confirmed SENT and task 123 completed via the exact source receipt. This
was an operator text/link response, not a fabricated automatic image receipt.

The health observer now recognizes this exact completed human-reply
disposition: task 123 was completed, but revision 235 still appeared as one
unowned attention item. No historical source or delivery receipt is deleted to
remove that observation. Its SQL predicate requires the exact settlement audit,
single source, physical SENT command and matching recipient/receipt. Unknown
parts, unrelated context and forged closure remain attention. The same predicate
runs before the sampling limit and in the per-revision reader.

Follow-up validation: 198 affected presentation/live/outbox tests passed; the
final native-chart suite passed 8/8. All 30 lane-health tests passed, including
8 new native human-settlement regressions. The URL truth/guard suite passed
37/37. Health queries captured in tests are SELECT-only. The native MariaDB
comparison of command 4's physical first MID and its receipt array also passed.

Final chart-integrated rerun: **1,473 tests, OK, 15 existing SQLite feature
skips**, 252.471 seconds; 29 ancillary socket attempts were blocked. The final
canonical selected-line chart test and all 30 health tests also passed in their
dedicated runs. Staged diff checks are clean.

MariaDB follow-up exposed a serialization-only mismatch in the new observer:
the command MID array and the nested task MID array had different whitespace.
The comparison now canonicalizes both arrays (`JSON_COMPACT` on MariaDB,
`JSON` on SQLite) while preserving exact order and values. All 31 health tests
pass, including raw formatting variation and an unconfirmed extra MID. The
exact new read-only SQL against production recognizes revision 235 and returns
customer revisions healthy, exact attention count zero, without changing data.
The client is back in automatic mode at epoch 15, has no unresolved reply debt,
and returning it did not create a duplicate request for answered source 3399.
