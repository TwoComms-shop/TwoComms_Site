# Чек, оплата, карточка и будущая память · контракт совместимости 06.10.2026

## Область и уровень доказательств

Read-only сверка действующего плана 3.0 v1.6, его review/crosswalk, сценариев владельца и существующих интерфейсов clean worktree. Этот документ не объявляет реализацию, тесты или release принятыми. Основной `90_ПЛАН_РЕАЛИЗАЦИИ_3_0.md` не изменялся. DB, provider, SSH, customer sends и тестовые команды в этой сверке не использовались.

Нормативные источники: [план 3.0](90_ПЛАН_РЕАЛИЗАЦИИ_3_0.md), [review 21](21_REVIEW_2026-09-30.md), [crosswalk 22](22_TASK_CROSSWALK_2026-09-30.md), [final review 23](23_FINAL_REVIEW_2026-09-30.md), [спецификация карты 20](20_РЕДИЗАЙН_КАРТИ_СПЕЦИФИКАЦИЯ.md), [план 2.0: C01/C02/C03/C05/C06/C13/C16](../2.0/03_IMPLEMENTATION_PLAN_FINAL.md). `05_SCENARIO_MATRIX.md` прочитан из primary checkout: в clean tracked baseline файл отсутствует; ссылки на его номера ниже требуют включения нужных fixtures, а не копирования частных переписок.

## Что уже принято планом и что остаётся

| ID | Документированная основа на 06.10 | Обязательство затронутого среза |
|---|---|---|
| P0-1/2/3 | Captured context/card/actual manifest core выпущен (`fe98c174c`); полные паспорта `[~]` | Добавить наблюдение/договорённость в существующий captured путь с одинаковой truth в actual prompt, карточке и map; не второй mutable client state |
| P0-4 | Source/queue/reader/schema core выпущен (`850baedfb`); generation flags FALSE | Новые данные не зависят от narrative generation/SENT. Не включать summary provider consumer побочным эффектом receipt rollout |
| P1-5 | Hosted settlement/reversal/canonical mirror core выпущен (`e8d657f33`); natural paid/full passport OPEN | Screenshot/manual review не provider proof; сохранить canonical settlement/reconciliation, callbacks и current scope |
| P1-6 | Takeover core выпущен; human delivery core в плане ожидает deploy | Permitted observation/derived CAS может работать на паузе; customer/business sends остаются у действующего actor-aware owner |
| P1-7 | Final nonlive admission core выпущен | Любой новый OCR/background provider caller отдельно использует actual admission, роль, deadline, budget и final denial → 0HTTP |
| P2-1.A/B/C | Existing source/topology adapters, отдельные паспорта `[~]` | Registry source→producer→projector→UI; observation/review/settlement/effect различимы, replay не производит новый бизнес-факт |
| P4-1→P4-2 | Полные exact offer/invoice/tender/amendment контракты `[~]` | Договорённая сумма со своим source не равна canonical catalog price, внесённой сумме или full settlement |
| P5-1 | Typed allowlist language/objection/deferred_intent; consumer contracts `[~]` | Receipt/amount/order keys не добавлять в typed память без producer/schema/reader/materiality/activation gates |
| P7-1.B/C | Affected deterministic/MariaDB/UI/release gates; 72h+20 natural turns OPEN | Новый release можно проверить как узкий срез; не закрывать весь план или natural gate по локальным tests |

Прямые зависимости остаются существующими: P0-1 ← P0-4/P1-7, P0-2 ← P0-1, P0-3 ← P0-2; P4-2 ← P4-1/P1-5; P5-1 ← P0-4/P0-3. Связи crosswalk обозначают интеграционные обязанности, не новые рёбра DAG. Future consent/typed/diagnostic UI не блокируют независимый безопасный canonical observation срез (§0.3/§2).

## Контракт источников и полномочий

1. **Принятый вход.** Persisted owned USER message + namespace/provider event-time + immutable source identity, reset/erasure/scope. Model/manager echo и старый import с новым DB ID не становятся новым customer receipt. Incoming fact admission отделён от send authority (P2-1.B, §14.C.2/8).
2. **Медиа.** Частное owned part/index/hash, capture state, inspection state и actual request/model. Успешный request не доказывает чтение всех parts (C03; I-7; P2-6). Нельзя сохранять публичную копию чека или signed URL в audit/prompt-memory.
3. **OCR-наблюдение.** Распознанные amount/date/type/confidence привязаны к конкретной прочитанной части и являются customer evidence/derived observation; unreadable/unknown/conflict имеют явный статус. OCR/chat/instruction внутри чека не money/send authority (I-2/I-4; C03/C06).
4. **Договорённость.** Order total, unit price, quantity, currency, payer/recipient/line и внесённая сумма — отдельные значения с per-field source. Нельзя назначить последний receipt watermark источником ранее согласованной цены. Exact catalogue/configuration и current offer/approval проверяются своими owners; изменение line инвалидирует старую offer authority (P4-1/2; §14.C.3).
   Normalization `L/l/л`, цветовых aliases/склонений UA/RU/EN меняет canonical значение, сохраняя исходные raw text/source/digest. Negation, alternatives и questions не выбирают первый token. Цена не меняется от языковой нормализации. Пример synthetic инварианта: товары750 + согласованная доставка100 = total850; это отдельные components/sources. OCR «внесено750» не доказывает погашение850 и не делает100 shipping оплаченной. Произвольные альтернативные числа/валюты остаются conflict, не LLM arithmetic authority.
5. **Manager review.** Receipt/claim может создать existing scoped review. Его очередь/notification — не фактическая доставка менеджеру. Решение требует authenticated actor/permissions, current review/source/amount contract, CAS и отдельный audit. Free-text note или OCR не равны этой операции (C05; P0-2; P1-6).
6. **Оплата.** Provider-verified settlement, authenticated manager-reviewed purchase и исторический факт покупки не сливаются. Provider attempt/invoice identity + authoritative version/receipt дедуплицируют деньги; `occurred_at` не idempotency. Order existence/CRM stage не paid. Deposit/partial/full, refund/reversal, zero/gift coverage сохраняют собственную семантику (P1-5/P4-2; B08.6/8/9/10; B10.4).
7. **Ответ клиенту.** «Получили чек», «проверяется», «подтверждено менеджером» и «подтверждено провайдером» разрешаются разными evidence. Любой paylink/order/handoff claim требует своего persisted action. Durable outbox/SENT/UNKNOWN не заменяет semantic coverage всей просьбы (§14.C.7; I-11).
8. **Память/карта.** Narrative может описать ограниченный observation, но не повысить его authority повторением. Canonical facts доступны до summary/typed generation и после выхода сообщения за последние60. Reset очищает scope; erasure отменяет связанное derived содержание, но clear-memory не стирает money ledger (P0-4/P5-1; B15.7).

## Существующие интерфейсы и интеграционные ловушки

- `ig_turn_intelligence.build_turn_context` принимает sealed boundary/source digests/event-time watermark, publication/permission/reset/erasure, episode/line/recipient и scoped snapshots. Новая receipt snapshot должна быть captured до generation и оставаться исторически неизменной после следующего сообщения. Любое late publication должно повторно проверить текущую owner/source/CAS границу.
- `ig_client_state_card.assemble_client_state` — pure adapter поверх captured components; `components['slots']` позволяет отдельный envelope value/status/authority/source_refs/scope/observed_at/watermark/validity/conflict/superseded_by. Он выполняет structural/scope checks, **не re-verification** источника: factory обязан валидировать persisted source/media/result.
- Existing `payment.current` строится из `payment_truth`: wrapper `status=confirmed` означает валидную capture, а вложенный financial status может иметь другое значение. Wrapper сам по себе не paid. Receipt/OCR pending нельзя подавать сюда как `payment_ledger` или читать wrapper как settlement. Для observation пригоден отдельный finite slot с truthful customer-source/derived authority либо точное nested review состояние с явным consumer contract.
- `render_client_state_prompt` допускает только целые slots, оценивает UTF-8 bytes/4, возвращает included/omitted/oversized/digest. Required overflow должен сохранять safety facts/readiness → safe bounded outcome; не молчаливое усечение OCR или lost pending-payment requirement. Actual manifest отражает включение и omissions (P0-2/3/P1-3).
- `bot_payment_truth.client_has_verified_payment` — provider-money predicate; `client_has_confirmed_purchase` — lifetime CRM relationship. `current_payment_confirmation`/`current_manager_confirmation_review_q` — current presentation with provenance, исторический review не платит за новый episode. Не заменять current episode guard lifetime aggregate.
- `ig_payment_review.create_payment_review` уже сохраняет claim до costly enrichment; `extract_payment_review_evidence`, `payment_review_duplicate_identity`, `payment_confirmation_candidate`, `record_review_decision` — existing owners. Новый observation worker должен переиспользовать их identity/outcome, а не второй payment/debt ledger; stable receipt replay не повторяет OCR, review, order или notification.
- Independent queue отделена от reply eligibility, но не от historical/reset/privacy/freshness/provider admission. Claim token/lease, один coalesced successor и bounded retry нужны только для реально нового material source. Paused/AI-disabled/no-reply вход может сохранять наблюдение без customer sends; provider denial/failure имеет конечный reason и durable retry/disposition.
- Новый `IgPaymentObservationSource`/migration0225 — OneToOne source identity и lease/outcome для existing observation pipeline, не financial state/второй ledger. Its outcome хранит source/review refs и finite booleans/reasons; amount/OCR/private bytes остаются у existing private source/review owners. Чтение queue state не proof чтения чека/оплаты.
- Private raw OCR/receipt не кладётся в ordinary narrative/manifest/export. Оставлять минимальные разрешённые normalized values и private refs; no-store/no-referrer/VIEW_PII/owner/hash/lease/two-phase deletion сохраняются. Erased media не может заявляться как вновь осмотренное (P2-6/P0-3).

### Narrow privacy integration review нового queue

`IgPaymentObservationSource.message/client` используют `on_delete=CASCADE`, `db_constraint=False`. Каноническая erasure процедура `bot_views._delete_direct_bot_records` вызывает ORM `message_scope.delete()` и затем `IgClient...delete()`: Django collector должен удалять queue rows даже без SQL FK. Это не доказательство SQL/raw-delete cascade. Native gate должен проверить конкретный canonical path, отсутствие orphan queue/PII и missing-source worker outcome.

При сверке `_observe_claim` первоначально читал queue row через `.get()` до `try`; erasure после claim могла удалить её и прервать drain `DoesNotExist`. Root получил finding: отсутствующая row после claim должна дать конечный `observation_claim_lost/source_erased`, без provider/review writes; final patch и regression требуют отдельного evidence. Аналогично disappearance исходного message/client во время I/O, replacement lease и erased media должны быть безопасными конечными outcomes. Проверка этого документа read-only; исправление или pass этим текстом не утверждаются.

## Готовность: affected acceptance matrix

| Сценарий | Обязательная проверка интерфейса/эффекта |
|---|---|
| USER receipt без текста; text claim→receipt; два разных чека | По одному owned part/result/claim identity; enrichment одного review без склейки независимых платежей; catalog picture отдельно от receipt |
| OCR даёт200, договорённый total1090 | Payment amount200 не переписывает order total1090; prepayment/COD balance отдельно; не full-paid по совпадению receipt |
| Товары750 + доставка100 = total850; receipt750/850/conflicting currency | Components/source provenance сходятся; receipt750 partial, receipt850 только evidence pending review; shipping оплачена лишь по соответствующему settlement/decision contract |
| Точная согласованная скидочная сумма; ambiguous/multiple totals | Source negotiation retained; resolver/final current amount guard не подменён; conflict даёт явный missing selector/review outcome |
| `L/l/л`, aliases/склонения цвета; «не L, а XL», «M або L», цитата/вопрос | Same canonical value при однозначном смысле; source raw/digest неизменны; correction scoped, ambiguity сохранена; price/payment authority не растут |
| Старый paid order + новый episode; два order/recipient/line | Current truth не заимствует old paid/price/receipt/адрес; linked historical context виден отдельно |
| AI disabled, manager pause, no-reply, failed generation | Observation persisted/queued независимо; никаких новых bot sends, order/settlement mutations из observations |
| Reset/erasure/source edit во время OCR; inverse completion | Старый result не публикуется/не возвращает scope; свежий head не перезаписан; privacy check перед I/O и final CAS |
| Imported historical receipt с новым DB ID; stable replay/restart | Provider event-time/historical gate сохранён;0повторных provider calls/claims/notification/effects без нового admissible source |
| Chat/OCR/manager-note injection | Не меняет price/money/permission/order; prompt данные нейтрализованы, final server guards fail closed |
| Provider DENY/UNKNOWN/DB error/cooldown/deadline; OCR role burst | Actual0HTTP до denial; при разрешённом useful OCR обычный admission/budget; chat reserves не съедены,6project scopes независимы |
| Manager confirm double-click/two tabs; late callback/reversal | One canonical decision/money projection, authenticated actor + expected candidate/CAS; wrong amount/currency/foreign review rejected |
| Screenshot pending; manager verified; provider paid; refund | Card/prompt/map одинаково различают states/authority; provider predicate не расширен manager/OCR; paid побеждает stale expiry |
| Ack SENT/partial/UNKNOWN; queue manager notification | Ack не закрывает неснятые order/payment obligations; notification queue не manager receipt; UNKNOWN без blind resend |
| >60history и correction; summary flagsOFF; future typed consumer | Canonical agreement/observation не исчезает без summary;0новых summary calls; future typed отдельно проходит P5-1 allowlist/source/TTL/materiality gates |
| Budget overflow; source erased после reply | Mandatory money/scope запреты не усечены; actual manifest named omission; historical replay `not_reconstructable`, без raw PII prompt storage |
| Authenticated card/full/inline/short/current/history320/390/desktop | Реальные sources, pending/conflict/receipt vs paid, выбранный order; GET/poll не пишет/не генерирует; actual loaded assets + console QA |

Соответствующие существующие scenarios:4.4/4.5/5.12 и R30-S01/02/03/05/10/12/17/18/19/23/24/26/28/29/31/32/35. Crosswalk: B03.19/21, B05.2/9, B08.1/3/6/8/9/10/11/12, B10.1/2/4/9, B15.7. Это affected subset; будущие consent/reward capabilities не объявляются готовыми этим release.

**Условие активации:** root подтверждает source/observation→review→captured consumer→allowed action/receipt contract, focused mocked regressions и disposable MariaDB CAS/unique/locking/lease/rollback для затронутых writes; затем scoped release/runtime/schema/UI. Обратная совместимость drain сохраняет SENT/UNKNOWN/records. Post-release72h+20подходящих natural turns остаётся отдельным OPEN gate; не получать его synthetic production событиями или quota probes.

## Реализованный candidate · owner interfaces · root gates ожидаются

Этот раздел добавлен после первоначального read-only аудита. Он описывает текущий scoped candidate, а не отменяет исторические пределы сверки выше. Final combined/native/browser/release evidence, точные test counts и release SHA добавляет root после завершения проверок; здесь их приёмка не объявлена. Полные паспорта плана и natural72h+20turns остаются OPEN.

### Обезличенная причинная цепочка инцидента

Root read-only восстановил внутренний case351/source3285: клиент прислал receipt с amount970; ранее согласованы merchandise850 + delivery120 = payable970, white/L/oversize и TWOCOMMS/product1654 с screenshot reference3242. Это audit IDs и предметные facts; имена, контакты, IBAN и исходный receipt здесь не сохраняются.

До исправления карточка оставалась `new` с пустыми compatibility selection полями, диалог был paused, а receipt был privately owned, но uninspected. Owned capture не доказывал OCR, а отправочный путь не мог исправить эти source facts на паузе. Отдельные проблемы: отсутствие durable независимой observation работы, потерянная source-bound договорённость и нечестное сближение receipt/paid/order UI. Наличие970 на изображении не доказывает банковскую оплату;850 — стоимость товара,120 — доставка,970 — согласованный payable, а не unit price модели.

Из этого следует проверяемый результат candidate: accepted source получает observation owner без снятия pause; договорённость сохраняет собственные per-field sources; card/prompt/map показывают pending/observed evidence; менеджер может рассмотреть текущие facts и оформить полный заказ разрешённым действием. Автоматическое paid/order/customer send по одному OCR или receipt запрещено. Historical case correction и production outcome не объявляются выполненными этим описанием.

### Producer → captured consumer → permitted action

| Owner/interface | Текущий scoped candidate | Обязательная граница |
|---|---|---|
| `ig_conversation_agreement.extract/persist/read_conversation_agreement` | Reverified typed состав, normalization size/color/fit/quantity, negotiated goods/shipping/payable и source vector в existing client context | Correction/alternatives/quotes/scopes сохранены; digest/raw source не меняется от normalization; negotiated price не catalog authority |
| `IgPaymentObservationSource`, management0225; `enqueue_payment_observation`, `observe_payment_source`, `drain_payment_observations` | Durable OneToOne source job, independent ingress/worker repair in existing analysis lane (one source/iteration), lease/CAS/retry/media progression | No send/invoice/paid rights; exact current source/namespace/reset/episode, historical cutover/event-time; erased/missing source и stale lease дают конечный outcome |
| `ig_receipt_inspection.inspect_receipt_media` / `bound_receipt_inspection` | Owned part/hash receipt/product/other recognition, bounded normalized receipt facts, uncertain/deferred/retryable distinctions и cached actual observation | Private bytes/admission/use lease; результат не переносится на чужую part/hash; low confidence не inspected-receipt truth; OCR не paid |
| `ig_payment_observation.read_receipt_observation` | SELECT-only scoped observation/source refs/source rows, pending vs observed/absent | No provider/capture/queue write на GET; current event-time watermark, private lifecycle и reset/erasure проверяются |
| `ig_admin_state_capture.capture_payment_context`; `ig_response_plan.capture_response_plan`; `ig_turn_capture` | Один source-verifying read model для receipt/agreement/current payment; plan capture сохраняет snapshot/boundary/fence, turn capture сверяет ту же границу и fresh fence | Full client/episode/order/line/recipient/reset/namespace/watermark parity; changed/unknown capture даёт named omission, historical context не заменяется latest |
| `ig_response_plan.build_response_plan` / `coverage`; `ig_response_debt.record_response_coverage` | Source-bound `payment:receipt`, `payment:claim`, payment support obligations; receipt ACK+pending закрывает только customer response; digest-covered `payment_verification=unresolved` сохраняется для payment IDs собственных sealed sources | Size ACK/manager control не покрывает payment; text-only claim не proof чтения receipt; verified/paid marker не вводится, managerial review сохраняется canonical pending |
| `ig_payment_review.create_payment_review`, `payment_confirmation_candidate`, `record_review_decision` | Existing review/dedupe, OCR evidence и source-bound amount contract; goods/shipping/payable/payment amounts разделены | Authenticated actor/permissions/current candidate/CAS/idempotency; notification queue не notification SENT; provider predicate не расширяется |
| `orders.services.ig_review_order_builder.payment_review_order_completion_requirements` / `create_order_from_payment_review` | Exact current reverified agreement, draft/source proof и current transcript repro; полный допустимый offsite/custom договор получает Order после manager approval, неполный — typed completion requirements | Read-only preview не создаёт заказ; expired/stale/missing source не обходится; audited merchandise/shipping/payable сохраняются отдельно; `provider_confirmed=False` для manual evidence |

### Менеджер: external echo и authenticated action

Plain external manager echo остаётся источником разговора/наблюдением. Namespace и role label не доказывают authenticated principal, его payment permissions или money decision. Такой echo не должен автоматически изменять paid/order truth.

Отдельный local adapter `ig_payment_review.apply_authenticated_manager_payment_confirmation` принимает только proven `human_reply` source: source role/status/SENT, durable HumanReplyCommand/parts, exact actor/context/recipient/source ownership, актуальная граница и явная completed full-payment statement. Actor должен иметь `MANAGE_IG_PAYMENTS_PERMISSION` и `VIEW_IG_CONVERSATION_PII_PERMISSION`; решение проходит existing audited manager payment path. Это не provider confirmation, новый customer send или право по произвольной manager note. Explicit paid из manual-order формы — отдельное authenticated staff действие со своим audit; OCR и просмотр preview его не запускают.

### Receipt links, PDF и приватность

Known PDF/receipt links сохраняют message/source provenance и доступный manual review path. Поддерживаемые privately owned изображения анализируются автоматически; ссылочный/PDF evidence не выдаётся за уже прочитанную картинку. Candidate не делает arbitrary external fetch по ссылке из чата, не расширяет сетевую authority по OCR и не копирует receipt в публичное хранилище. Не сохранять IBAN/имя получателя/transaction reference/raw OCR в ordinary memory/manifest/export; source refs и разрешённые normalized facts остаются у private owner с existing retention/erasure.

### Production configuration evidence без provider probes

Root выполнил ограниченную read-only сверку configured keys/project scopes на production:6configured keys относятся к6независимым project scopes. Секреты и project labels здесь не приводятся. В текущем role allocation четыре scopes доступны management работе, два сохраняют chat reserves; receipt observation не должна отнимать защищённый chat budget.

Receipt caller предпочитает configured image-capable `gemini-3.5-flash`; остаётся полный допустимый fallback chain `gemini-3.8-flash → gemini-3.7-flash → gemini-3.6-flash → gemini-3.5-flash-lite`, subject to actual role/capability/availability/admission/deadline. Explicit operator model override сохраняется. Наличие chain или configured key не доказывает успешную generation/capacity; отказ одного project не закрывает остальные, а live reply/recovery сохраняют собственный root8/2/1 budget. Receipt consumer имеет отдельный консервативный предел2actual HTTP boundaries на claim; quota/UNKNOWN denials выполняют0HTTP. Пять bounded claims дают максимум10boundaries в неизменном цикле observation; новый material source/media revision начинает отдельный цикл. Успешный exact-source OCR cache не повторяется при чтении/replay.

Read-only owner-observed configured profiles: Flash5RPM/20RPD; Lite15RPM/500RPD. Это зафиксированные локальные profiles на06.10, а не probe текущего остатка или безусловная гарантия внешнего Google quota. Актуальный useful request использует existing accounting/admission; deny/unknown/error даёт0HTTP. Automatic startup/GET/poll/cron/rollout/preflight по-прежнему выполняют0provider health/metadata/generation probes. Provider availability/quality подтверждаются разрешённым необходимым трафиком, а не этим configuration snapshot.

### Pending root acceptance

- Consolidated affected mocked suite: receipt recognition/cache/multipart/low-confidence/retry; source/lease/reset/erasure races; language/negation/alternatives; agreement→review→manual-order; payment response coverage; captured request/card/journey. Фактический final count, overlaps и failures фиксирует root; synthetic quota tests выполняют0external provider I/O.
- Disposable MariaDB/InnoDB: management0225/schema/OneToOne/ORM CASCADE, competing lease/claim, source change/erasure during provider I/O, source transfer pre-episode→review episode, double approval/order idempotency, parent/child source-current fences. Production не fixture.
- Authenticated browser: pending vs observed vs manager/provider confirmation, amount components/normalization/source links, manual completion/preview, manager pause, desktop390/320, actual loaded assets и console. HTTP200/hash сами не gate.
- Scoped commit/push/SSH pull по AGENTS.md, productionmain/SHA/schema/supervisor/child/runtime и relevant read-only checks; затем root release evidence и rollback/drain contract.
- Минимум72часа+20подходящих естественных новых turns после контролируемого включения. Этот gate остаётся OPEN независимо от green mocked/native suite; customer sends/provider probes ради числа запрещены.

### Дополнительные интеграционные исправления

- Retained agreement сохраняет reverified material sources за пределами recent80messages. Пустой input не стирает head; новая quote может заменить истёкшую после проверки источника; withdrawal/counteroffer/reset не оживляют старый offer. Order builder использует тот же retained head и проверяет новые источники.
- Audited manager size set/clear из текущего main имеет приоритет над историческим chat offer. Card показывает конфликт, legacy mirror не переписывает canonical choice, auto order требует совместимую текущую configuration.
- Current-review receipt anchor сохраняется при длинной переписке и bounded capture. Unreadable PDF/link остаётся manual evidence; unknown OCR currency не получает UAH автоматически.
- Receipt OCR убран из automatic customer reply drain: существующий fenced analysis worker обрабатывает один source даже при disabled replies/AI, с maintenance/owner/stop guards. Полезный explicit receipt observation до capture конкретного reply остаётся отдельным bounded действием.
- Approval/order/manual reconciliation используют общий порядок client advisory barrier → transaction → client → review → остальные строки; manager native adapter и observer используют ту же границу.
- Manager alert разделяет goods/delivery/payable и provisional reported amount. Номер970 не подменяет merchandise850 или verified settlement.

### Root verification before upstream integration

Consolidated no-network SQLite suite: **796 tests, OK, 9 vendor/fixture skips**. Django system check: 0 issues; scoped Python AST:57 files; existing selection-editor JS:9/9. Native MariaDB suite additionally executes vendor-dependent claim/locking/source races with actual migration0225. Its final result and post-integration release verification remain separate gates.

The combined gate exposed and corrected two consumer defects: strict request-manifest schema rejected the new agreement/receipt version keys, and list/card capture repeated source reads beyond their existing query budgets. The schema now accepts exactly those two version tokens; budget guards remain unchanged. Accepted manager product photos retain immutable media/source proof, while a later customer receipt cannot replace the agreed product reference.

Concurrent upstream commits57f54d151/29f4a5255 add exact legacy human receipt compatibility and passive bounded console/overview. Root integrates these before release; they do not grant a legacy echo financial authority or introduce OCR/provider probes on GET. Plan section15 keeps future multiline/recipient/repeat-purchase passports OPEN; ambiguous or incomplete custom configurations require explicit manager completion rather than an invented order.

### Final integrated release gate

After rebasing onto upstream29f4a5255: **1000 no-network SQLite tests, OK, 9 skips**; **358 disposable native MariaDB tests, OK, 3 skips**. Native affected tables are InnoDB, source-message identity is unique, actual0225 reverse/reapply both succeed and the owned disposable namespace/user are removed. Django check:0 issues; scoped AST:58 Python files; selection-editor JS:9/9; inline template JS syntax passes. These checks use mocked provider transports and do not claim external model capacity or live settlement.

The additional generic notification gate exposed five pre-existing failures, reproduced on untouched upstream29f4a5255. Its fixtures now respect current permissions and private-media handling: authorized review uses an authorized operator, plain staff remains denied, private receipts stay at the authenticated preview, and public product-media retries retain their transport idempotency. No production permission or privacy guard was relaxed.

Material receipt/cart/amount changes renew a previously delivered review summary on the same canonical notification row. Prior finite Telegram receipt/candidate/version are audited, old buttons cannot approve the new candidate, retry budgets/backoff remain bounded, and the producer queues without network IO under financial locks. A changed UNKNOWN/SENDING/DEAD_LETTER report retains its original delivery identity and a durable deferred-material marker; the workspace explicitly says that the latest report is not delivered. Only a known successful completion can queue that deferred revision, using exact review/notification IDs. Transcript noise and unchanged OCR replay do not generate a new report.

Production pull/schema/process checks, useful source3285 reconciliation and authenticated workspace QA follow this gate and must be recorded separately. Natural72h+20turn acceptance and future section15 passports remain OPEN.
