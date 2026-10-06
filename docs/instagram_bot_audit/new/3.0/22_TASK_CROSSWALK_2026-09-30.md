# 22 · Task crosswalk 2.0 → 3.0 · 30.09.2026

Компактная миграция **196 действующих паспортов B01–B17** из [плана 2.0 v2.7](../2.0/03_IMPLEMENTATION_PLAN_FINAL.md) в [план 3.0](90_ПЛАН_РЕАЛИЗАЦИИ_3_0.md). Выбраны только заголовки задач раздела 3; исторические D-блоки и упоминания ID не создают строки. C01–C19 старого плана сохраняются нормативными; поздние прямые owner decisions имеют приоритет в своей области.

`Ист.` — буквальный исторический чекбокс действующего паспорта 2.0: **31 `[x]`, 66 `[~]`, 94 `[ ]`, 5 `[!]`**. Это происхождение записи, **не текущая приёмка 3.0**. `[x]` сохраняет лишь документированную узкую область; новые изменения требуют своей проверки. `retained` означает сохранить эту основу и подтвердить совместимость при затрагивании, не повторять реализацию. `deferred` — видимый отложенный контракт с gate, не отмена требования. Без нового evidence статус 3.0 не повышается.

Строка связывает требование с новым паспортом; несколько ID — интеграционные обязанности, **не автоматически рёбра DAG**. Прямые зависимости остаются в единственных паспортах плана. Конкретный gate ниже дополняется соответствующими C-контрактами и P7-1; таблица не заменяет старые source/evidence/acceptance детали. P2-8 — только UI-расширение единого P2-4, отдельное decision-log хранилище не создаётся. Низкоприоритетные points/rewards явно отложены внутри P4-4; paid gift tender сохраняется отдельно в P4-2. Косметический coordinator refactor и эксперименты отложены без снятия надёжности и release gates.

**Граница проверки:** исходный crosswalk составлен по локальным документам; число строк, уникальность и буквальные исторические статусы проверены разбором Markdown. Позднее подтверждение владельцем seen/typing30.09 явно отражено в B03.18; предметные CODE/TEST/PROD уточнения — в21/23 и основном плане. Это не blanket acceptance остальных строк. Разрешение спорной disposition и финальные задачи остаются в основном плане.

## B01 · 10 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B01.1 | `[x]` | retained → P2-6 | Сохранить закрытые байты; совместимость private save после изменения storage/runtime. |
| B01.2 | `[x]` | retained → P2-5/P2-6 | Все actual callers сохраняют HTTPS/DNS/redirect/MIME/bytes/fence guards. |
| B01.3 | `[x]` | retained + P2-5/P7-1 | Сохранить per-part capture/inspection; закрыть audio wrong-modality fallback. |
| B01.4 | `[x]` | retained → P7-1 | Crash/retry не теряет caption, source ownership и не дублирует ответ. |
| B01.5 | `[x]` | retained + P2-5 | Автоанализ поддерживаемых изображений; новые story/video сценарии отдельно. |
| B01.6 | `[x]` | retained → P2-5/P2-7 | Source hash/part/actual request binding; сертификат остаётся candidate без authority. |
| B01.7 | `[x]` | retained + P2-7 | Сохранить programme/case; человеческое решение награды/условий остаётся отдельным gate. |
| B01.8 | `[x]` | retained → P2-6/P7-1 | No-store/URL redaction; проверить failure wording без утечки и ложной отправки. |
| B01.9 | `[x]` | retained → P2-1/P2-5 | Per-part coverage/reason из actual evidence остаётся видимым после UI интеграции. |
| B01.10 | `[x]` | retained → P7-1 | Историческая media acceptance не заменяет corpus новых типов/маршрутов. |

## B02 · 13 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B02.1 | `[x]` | retained → P1-1/P1-2 | Сохранить узкие локальные тексты; актуальная публикация проверяется отдельно. |
| B02.2 | `[x]` | retained → P1-6 | Ручной resume обязателен; новый inbound/elapsed time не снимает takeover. |
| B02.3 | `[x]` | retained + P1-3 | Не обходить compiler; mandatory policy и budget conflict принять в actual payload. |
| B02.4 | `[x]` | retained → P1-6/P2-6 | Server actor/role/scope и permission epoch сохраняются для новых действий. |
| B02.5 | `[x]` | retained + P1-2 | DB/default/core version parity и rollback; локальный текст не равен опубликованному. |
| B02.6 | `[x]` | retained → P0-1/P4-2 | Typed proposal/финальный guard сохраняет authority, сроки и grounded fallback. |
| B02.7 | `[x]` | retained → P1-1/P1-2/P1-3 | Publication CAS/invalidation/rollback; новая версия реально читается следующим ходом. |
| B02.8 | `[x]` | retained + P1-1 | Approved UA/RU/EN facts, точные сроки/условия; brand archive/publication определён явно. |
| B02.9 | `[ ]` | P1-1/P1-2/P1-3/P7-1 | Цельный rules slice: actual prompt version, negative corpus и post-release evidence. |
| B02.10 | `[~]` | P1-6/P7-1 | Resume создаёт свежий successor; не отправляет старый backlog без текущего intent/permission. |
| B02.11 | `[x]` | retained → P0-1/P7-1 | Первый понятный запрос получает grounded ответ; internal selector error не становится переспросом. |
| B02.12 | `[~]` | P0-1/P1-4 | Current intent допускает sales acts; благодарность/цитата/отрицание не запускает продажу. |
| B02.13 | `[ ]` | P1-1/P1-2/P1-3/P0-3 | Неопубликованный core и policy mismatch; actual manifest подтверждает согласованную версию. |

## B03 · 21 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B03.1 | `[x]` | retained → P7-1 | Durable signed ingress/dedupe; acknowledge не опережает сохранение обязательства. |
| B03.2 | `[x]` | retained → P0-1/P7-1 | CustomerTurn/source ownership сохраняется при объединении и successor. |
| B03.3 | `[x]` | retained → P0-1/P2-5 | Immutable bundle/media manifest; context builder не теряет исходные части. |
| B03.4 | `[x]` | retained → P7-1 | Latest inbound/takeover/erasure CAS повторно проверяется перед effect/send. |
| B03.5 | `[x]` | retained → P7-1 | Per-part outbox и SENT immutability; UNKNOWN никогда не blind resend. |
| B03.6 | `[~]` | P7-1 | Lost HTTP/late echo/multipart pause: один effect и durable UNKNOWN reconciliation. |
| B03.7 | `[x]` | retained + P1-7/P7-1 | Circuit/read probe/hygiene сохранить; общий background DB-active cap не объявлять принятым. |
| B03.8 | `[x]` | retained → P7-1 | Actual collation selector на MariaDB; FK/schema integrity проверять отдельным gate. |
| B03.9 | `[x]` | retained + P2-6 | Fence до blob deletion; poll/backfill/restore cutoff остаётся отдельным lifecycle gate. |
| B03.10 | `[~]` | P2-3/P3-9/P7-1 | Progress каждой lane, ownership/restart; живой heartbeat не скрывает stalled обязательство. |
| B03.11 | `[~]` | P3-9/P7-1 | Recovery хвостов с конечным outcome; alert только actionable и без второго send. |
| B03.12 | `[~]` | implementation remainder → P1-6/P3-10/P7-1 | Рабочие takeover/command guards сохранить; actual per-part human outbox/echo provenance и private draft/note lifecycle ещё нужны, draft_hash не draft store. |
| B03.13 | `[ ]` | P2-3/P3-10 | Один primary attention frame; selection/mode/paid отдельны от overdue response debt. |
| B03.14 | `[ ]` | P7-1 | Ingress→turn→send→recovery/human: crash/race/UNKNOWN на изолированных данных и MariaDB. |
| B03.15 | `[x]` | retained + P7-1/P2-3 | Response debt/manual transfer сохранить; UI показывает owner/reason, не только процесс. |
| B03.16 | `[x]` | retained → P0-1/P4-1/P7-1 | Preference без exact product; fallback не добавляет выдуманную цену/товар. |
| B03.17 | `[~]` | P7-1 | Burst successor переносит uncovered sources/clock/cap и исходный root budget. |
| B03.18 | `[~]` | retained / owner-accepted30.09 | Seen/typing подтверждены владельцем целиком; исторический статус сохранён только как история. Отдельная повторная работа/ручная приёмка снята; регрессия только при изменении затронутого пути. |
| B03.19 | `[~]` | P7-1/P1-4/P2-1 | Multipart receipt→count/time/original episode/follow-up cursor, crash и late correction без resend. |
| B03.20 | `[~]` | P0-4/P5-1/P2-3/P7-1 | Runnable/progress admission не блокируется raw old pending; manager/media analysis freshness принять. |
| B03.21 | `[!]` | P7-1 | Exact-source historical debt inventory→owner review→evidenced disposition; никаких blanket closure/resends. |

## B04 · 12 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B04.1 | `[!]` | P1-7 | RISK-Q1: nonlive enforce DENY закрывает actual HTTP; live binding/advisory отдельно решается. |
| B04.2 | `[~]` | P1-7 | Verified project/model/feature buckets; aliases не множат quota; metadata не generation proof. |
| B04.3 | `[~]` | P1-7 | Capability/free/effort registry и actual supported payload; unknown профиль fail-closed. |
| B04.4 | `[~]` | P1-7/P7-1 | Capture/quota/generation/repair/send общий horizon, lineage 8 HTTP/2 scarce/1 repair. |
| B04.5 | `[~]` | P1-7 | Schema/auth/quota/transient/UNKNOWN различаются по scope; bounded cooldown/recovery. |
| B04.6 | `[ ]` | P1-7/P7-1 | Две live workers/fair queue только после общего DB-active cap; три диалога без starvation. |
| B04.7 | `[~]` | P2-4/P0-3 | Per-attempt requested/effective model/effort, task/source/usage/latency без hidden reasoning. |
| B04.8 | `[~]` | P2-3/P1-7 | Оператор видит quota/queue/deny reason; mapping/profile mutations versioned и audited. |
| B04.9 | `[ ]` | P1-7/P7-1 | 2–3 диалога, contention/429/timeout/pause и p50/p95; metadata smoke не закрывает acceptance. |
| B04.10 | `[ ]` | P1-7/P7-1 | Все background SQL секции одного account проходят общий DB-only admission до двух workers. |
| B04.11 | `[x]` | retained → P1-7/P7-1 | Durable fallback chain без новой квоты/бюджета на successor, один допустимый ответ. |
| B04.12 | `[~]` | P1-7/P7-1 | Typed failure/same-tier salvage, actual HTTP vs skipped, deadline/compatibility и no lost reply. |

## B05 · 11 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B05.1 | `[~]` | P0-4/P5-1 | Meaningful-event analysis scheduler/debounce/watermark; takeover допускает analysis без send. |
| B05.2 | `[ ]` | P5-1 | Typed recipient/line facts и evidence; payment/consent/shipment остаются своим authority. |
| B05.3 | `[~]` | P0-4 | Writer CAS по watermark/head/reset/erasure; late summary не перезаписывает новый факт. |
| B05.4 | `[ ]` | P0-1/P0-4/P5-1 | Actual live typed reader; current episode и stale/claimed/confirmed различаются. |
| B05.5 | `[ ]` | P0-1/P0-3/P5-1 | Общий compiler/manifest с выбранными source refs и bounded authoritative context. |
| B05.6 | `[ ]` | P0-2/P4-4 | Commitment с owner/due/condition/permission; просроченное обещание не остаётся в summary. |
| B05.7 | `[~]` | P5-1/P2-1 | Source-bound objection case/attempt/outcome; ordinary вопрос не становится barrier. |
| B05.8 | `[~]` | P5-1 | Analyst proposals с evidence/conflict, human approval для policy; нет autonomous authority. |
| B05.9 | `[ ]` | P0-2/P0-3/P5-1 | UI memory freshness/coverage совпадает с actual prompt; unknown не выглядит complete. |
| B05.10 | `[ ]` | P0-4/P5-1/P7-1 | Один активный writer/reader, flags/rollback; legacy удалить после consumer acceptance. |
| B05.11 | `[~]` | P5-1/P2-1 | Native/trace общий source/materiality contract, negative corpus и resolved lifecycle. |

## B06 · 10 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B06.1 | `[~]` | P4-1 | Один exact existing variant resolver; unsupported selection не создаёт SKU/price. |
| B06.2 | `[~]` | P4-1 | Semantic catalogue profiles/ranking с evidence; broad selection сохраняет полноту кандидатов. |
| B06.3 | `[ ]` | P4-1/P4-2 | Отдельные print family/line/recipient, multi-line scope и independent variants. |
| B06.4 | `[~]` | P0-1/P4-1/P2-1 | Referral/image→catalog provenance, ambiguous unknown; реальный Meta entry отдельно проверяется. |
| B06.5 | `[ ]` | P4-1 | Live catalogue revisions/invalidation/freshness; prompt snapshot не подменяет источник текущей цены. |
| B06.6 | `[ ]` | P4-1 | Редактируемая C04 availability policy; replenish vs confirm-before-commit, scope/version. |
| B06.7 | `[ ]` | P4-1/P4-2/P4-3 | Feasibility case: auth POST/CAS/approval terms/customer acceptance; не снимать takeover. |
| B06.8 | `[!]` | P4-1/P4-2 | PurchaseDemand вместо новых holds; legacy paid/unpaid/manual allocation сверить без guessed release. |
| B06.9 | `[ ]` | P4-1 | Stock overview/manual physical usage и DTF production tasks; demand не списывает stock. |
| B06.10 | `[ ]` | P4-1/P4-2/P7-1 | Все C04 ветки: initial unavailable vs valid late zero, multi-line approval/decline. |

## B07 · 8 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B07.1 | `[ ]` | P4-1/P0-1 | VisualPlan проходит actual coordinator/permission/outbox, не только planner unit tests. |
| B07.2 | `[ ]` | P4-1 | Сетка точного изделия/fit/version; медиа действительно отправлена и связана с вариантом. |
| B07.3 | `[ ]` | P4-1/P4-2 | Signed product/fit/size postbacks с current revision и guarded state update. |
| B07.4 | `[ ]` | P4-1 | Pagination 201→201 reachable; truncated не выдаёт no_more_candidates, no repeats. |
| B07.5 | `[ ]` | P4-1/P0-2 | Size advice по конкретным числам; recipient scope, optional measurements, без гарантии/навязывания. |
| B07.6 | `[ ]` | P4-1/P2-5 | Visual pacing/fallback по capabilities; все части проходят outbox, полезный текст не теряется. |
| B07.7 | `[ ]` | P4-1/P2-1/P2-4 | Card sent/tap/selection разные evidence, current variant/fit видны оператору. |
| B07.8 | `[ ]` | P4-1/P7-1 | Image→card→tap→state actual integration; stale postback/media failure/permission race. |

## B08 · 12 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B08.1 | `[~]` | P4-2/P4-1 | Multi-line proposal→offer; checkout собирает delivery, feasibility gate и current composition. |
| B08.2 | `[~]` | P4-2/P2-6 | Actor/customer/offer revision access; shared link не подтверждает ownership. |
| B08.3 | `[~]` | P4-2 | Immutable offer price/valid_until vs bank invoice expiry; repricing/renewal с явным consent. |
| B08.4 | `[ ]` | P4-2 | Единый calculator/site parity, stable allocation/rounding; суммы строк сходятся с итогом. |
| B08.5 | `[ ]` | P4-2/P2-6 | Code scopes/expiry/dedupe, personal+promo incompatibility; military discount human decision. |
| B08.6 | `[ ]` | P4-2 | Full/deposit/zero/custom, goods shipping basis и 0/150/200/2999/3000 boundaries. |
| B08.7 | `[!]` | P4-1/P4-2 | No physical reserve в generation/payment; три оплаты/одна основа без нарушения initial approval. |
| B08.8 | `[~]` | P4-2/P1-5 | Idempotent invoice create/reconcile/reissue; uncertain provider create не создаёт второй invoice. |
| B08.9 | `[~]` | P1-5/P4-2 | Verified webhook/ledger settlement, duplicate/late/amount mismatch; OCR/words не paid. |
| B08.10 | `[ ]` | P4-2 | Tender/redeem/amendment ledger, audited compensation; gift и discount не смешиваются. |
| B08.11 | `[~]` | P4-2/P2-1 | Offer/invoice/accepted pay action/paid distinct sources; current source-bound amounts/actions. |
| B08.12 | `[ ]` | P4-2/P7-1 | Calculator→offer→invoice→settlement end-to-end; zero/gift/multi-line/COD и races. |

## B09 · 20 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B09.1 | `[~]` | P2-1 | Registry/persisted node enum/read-model state/fresh evidence; action DAG отдельно от route history. |
| B09.2 | `[ ]` | P2-1/P4-2 | NextAction/focus смена не отменяет paid; amendment/new episode с независимыми blockers. |
| B09.3 | `[~]` | P2-1 | Visits/loops/route change event-time, dedupe и source bounds; unknown path не дорисовывать. |
| B09.4 | `[ ]` | P2-1/P2-3 | Icon/count/scope/coverage registry, unknown vs zero; progressive disclosure без raw wall. |
| B09.5 | `[~]` | P2-1/P3-10 | Versioned keyed read-model, stale/last-good; refresh сохраняет focus/draft/details. |
| B09.6 | `[~]` | P2-1/P3-10 | Container widths/mobile/zoom, compact/full coherent; no horizontal-scroll gate. |
| B09.7 | `[~]` | P2-1 | Одна anchored detail panel, source navigation и distinct fact/interpretation/possible. |
| B09.8 | `[~]` | P2-1 | Episode/order selector/route versions/returns; historical view не получает текущие facts. |
| B09.9 | `[~]` | P2-1/P2-3/P3-10 | Один attention frame, minimal icons/counts; mode/selection/paid независимо. |
| B09.10 | `[~]` | P2-1/P3-10 | Motion по actual events, stable IDs/reduced-motion; декоративная анимация не P0 gate. |
| B09.11 | `[ ]` | P1-6/P3-10/P7-1 | Human composer/draft/pending/UNKNOWN/echo с server capability, no duplicate/wrong recipient. |
| B09.12 | `[ ]` | P3-10/P2-1 | Live keyed list/panels, scroll anchor/focus, expanded controls и draft при reconnect. |
| B09.13 | `[ ]` | P3-10/P7-1 | Conflicting UI states, 320/390px/desktop/200%/UA-RU/keyboard/touch; 5-секундное понимание. |
| B09.14 | `[ ]` | P2-1/P7-1 | Каждый domain producer→registry→read-model→action→evidence; unavailable capability не active button. |
| B09.15 | `[x]` | retained → P2-1 | Проверенное episode/event ядро сохранить; новое UI не расширяет evidence authority. |
| B09.16 | `[~]` | P2-1/P3-10/P7-1 | Вся многовариантная карта/подворонки с factual/possible separation; поздние visual overrides приоритетны. |
| B09.17 | `[~]` | P5-1/P2-1 | Native/trace source guards, один anchored material barrier и честный unknown outcome. |
| B09.18 | `[~]` | P2-1/P2-3/P1-4 | API/graph/list current focus согласован; old paid order не закрывает new purchase/response debt. |
| B09.19 | `[~]` | P2-1/P3-10 | Topology/geometry, linked order context без fabricated causality; no overlap/hover-only meaning. |
| B09.20 | `[~]` | P2-1/P5-1/P7-1 | Retain enabled bounded refresh; selected corpus/freshness/coverage, no model call on ordinary open. |

## B10 · 10 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B10.1 | `[~]` | P4-4/P2-6 | Verified order ownership/multiple-order choice; чужой номер/email не раскрывает PII. |
| B10.2 | `[ ]` | P4-4/P0-2 | Payer/buyer/recipient scopes; gift не заменяет постоянные size/address facts клиента. |
| B10.3 | `[~]` | P4-4/P2-1 | Order/shipment source/time/freshness; TTN created не handed-over/received. |
| B10.4 | `[ ]` | P4-2/P4-4 | Paid amendment revision/feasibility/delta/audited top-up; immutable payment history. |
| B10.5 | `[~]` | P4-4 | Preparation vs carrier срок, конкретный deadline human case; controlled Commitment/delay. |
| B10.6 | `[ ]` | P4-4/P4-1 | Mixed readiness/combined or split delivery; дополнительные расходы/international quote согласованы человеком. |
| B10.7 | `[~]` | P4-4 | Service brief с line/issue/evidence; no blanket denial по custom/points/received. |
| B10.8 | `[~]` | P4-4/P4-2 | Human service decision и financial/line correction; no blanket refund/coupon revival. |
| B10.9 | `[ ]` | P4-4/P1-6 | Case/Commitment/map обновляются; active takeover готовит draft, resume читает новые факты. |
| B10.10 | `[ ]` | P4-4/P7-1 | Order→service→corrected state, gifts/mixed lines/paid delta и permission boundaries. |

## B11 · 17 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B11.1 | `[ ]` | P4-3 | BusinessConsent/ProviderGrant/Handoff и три purpose независимы; capability/window fail-closed. |
| B11.2 | `[ ]` | P4-3 | Native invitation→receipt→answer→grant/expiry/revocation; actual entitlement/results отдельно от UI. |
| B11.3 | `[ ]` | P4-3/P1-4 | Единый durable scheduler, purpose/intent/line/due/lease; claim и pre-send revalidation. |
| B11.4 | `[ ]` | P1-4/P4-3 | One unanswered intent/quiet hours/explicit due; refusal/pause не запускают payment nudge. |
| B11.5 | `[ ]` | P1-4/P4-3/P4-2 | Reminder consent/current invoice/paid/UNKNOWN/expiry; paid cancels send, expired link не повторять. |
| B11.6 | `[ ]` | P4-1/P4-3 | Exact restock interest→stock revision→manager approval; send лишь actual permission, alert независим. |
| B11.7 | `[ ]` | P4-3/P2-7 | Opaque one-time Telegram/WhatsApp handoff token; click остаётся initiated без inbound verification. |
| B11.8 | `[ ]` | P4-3/P2-6 | Verified linking переносит scoped redacted brief; duplicate не mint identity/marketing grant. |
| B11.9 | `[ ]` | P4-4/P4-3 | Delivered-order following/received и external_ugc без заказа; human review→one lifetime grant/one use/90d; promised10%/reject, unsolicited5%/10%/reject; proactive offer после positive feedback без service case. |
| B11.10 | `[ ]` | P2-1/P4-3/P4-4 | Purpose consent/condition/due/cancel/evidence; browser timer лишь server projection. |
| B11.11 | `[ ]` | P4-3/P7-1 | Grant durability и revocation/expiry/purchase/interest/pause/merge race; no unauthorized send. |
| B11.12 | `[ ]` | P4-3/P7-1 | Enabled independent channels приняты отдельно; native external blocker явно сохраняется. |
| B11.13 | `[~]` | P1-4/P7-1 | Manager case блокирует только свой scope; технический debt не подавляет посторонний current reply. |
| B11.14 | `[~]` | P1-4 | New episode не suppressed old paid/done; один ordinary unanswered follow-up на intent. |
| B11.15 | `[~]` | P1-4/P7-1 | Timer от полезного полного SENT; отдельное channel window не обновляется внутренним revision time. |
| B11.16 | `[~]` | P1-4/P0-1 | Current intent/source prose+controls проходят финальный guard; без invented offer/photo/discount. |
| B11.17 | `[ ]` | P1-4/P7-1 | PRICE/RADIO full receipt/UI/outcomes, latest inbound/payment/takeover/UNKNOWN; no old test nudges. |

## B12 · 10 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B12.1 | `[ ]` | deferred → P4-4 | Сохранить D051 non-expiring programme/version; новые donation terms только explicit decision, без expiry/автодоната. |
| B12.2 | `[ ]` | deferred → P4-4 | Shadow line entitlement при verified eligible settlement один раз; existing UGC не points ledger. |
| B12.3 | `[ ]` | deferred → P4-4/P2-6 | Transfer по просьбе с verified binding; тот же grant, no double mint. |
| B12.4 | `[ ]` | deferred → P4-4 | Atomic spend+реальный reward issue, concurrent balance/code dedupe; no stub POINTS10. |
| B12.5 | `[ ]` | deferred → P4-4 | Voluntary donation obligation отдельно от подтверждённого перевода; no inactivity auto-donation. |
| B12.6 | `[ ]` | P4-2 | Paid gift tender независимо от points shop; verified payment, one-time redeem/remaining scope. |
| B12.7 | `[ ]` | deferred → P4-4/P2-7 | Custom gift packaging и configurable QR terms; no donor PII/вечный hardcode. |
| B12.8 | `[ ]` | deferred → P4-4/P4-2 | Human service line correction earned/spent/donated, compensating entries без повторного права. |
| B12.9 | `[ ]` | deferred → P4-4/P2-1 | Entitlement/transfer/spend/donation/source/programme UI; суммы объясняются ledger. |
| B12.10 | `[ ]` | deferred → P4-4/P7-1 | Zero/custom/merge/spend/donation/partial service parity; paid gift отдельно от discount. |

## B13 · 9 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B13.1 | `[~]` | P2-7/P4-1 | Retain creator branch; custom/prize/wholesale/dropship/parallel purchase scope принять отдельно. |
| B13.2 | `[ ]` | P2-7 | Typed adaptive custom brief; known fields не спрашивать повторно, next-action completeness. |
| B13.3 | `[ ]` | P2-7/P4-3/P2-6 | Original vs reference private hash/revision/channel; screenshot не production original. |
| B13.4 | `[ ]` | P2-7/P4-2 | Prize candidate→human scope/actual amount due; covered prize не fabricated paid/gift tender. |
| B13.5 | `[ ]` | P2-7/P4-2 | Human feasibility/quote per service; DTF film отдельно от garment, no invented amount/quantity. |
| B13.6 | `[ ]` | P2-7/P4-2 | Current brief/file/quote/mockup acceptance и actual settled/scoped prize production gate. |
| B13.7 | `[~]` | P2-7/P2-4 | Retain creator brief; wholesale/dropship/ROI evidence, no invented compensation/profit guarantees. |
| B13.8 | `[ ]` | P2-7/P2-1 | Brief→file→quote→mockup→payment→production blockers/owner; новая картинка не обходит reapproval. |
| B13.9 | `[ ]` | P2-7/P7-1 | Image-only prize/custom/dropship/wholesale/interrupted handoff; помогают без обхода money/mockup gates. |

## B14 · 8 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B14.1 | `[ ]` | P5-1 | Knowledge-gap registry из durable candidates, scoped fingerprint/dedupe, no raw transcript copies. |
| B14.2 | `[ ]` | P5-1/P2-4 | Actual validator/media/catalog/case outcomes с reason/evidence; lawful boundary не defect автоматически. |
| B14.3 | `[ ]` | P5-1/P0-1 | Grounded useful partial answer/один вопрос; human authority для денег/приза/feasibility сохраняется. |
| B14.4 | `[ ]` | P5-1/P2-4 | Period/unique episode/commitment/gap significance report; attempts не равны клиентам. |
| B14.5 | `[ ]` | P5-1/P1-3 | Evidence-based analyst draft/conflicts; human editor approval, no autonomous policy rewrite. |
| B14.6 | `[ ]` | P5-1/P1-3/P0-3 | Published version реально вошла в новый ответ; scenario verification и rollback перед gap closure. |
| B14.7 | `[ ]` | P5-1/P2-1 | UI gap/evidence/draft/actions рядом с причиной; publication отдельно от observed improvement. |
| B14.8 | `[ ]` | P5-1/P7-1 | Gap→proposal→approval→publish→new response→outcome, stale evidence/CAS/rollback. |

## B15 · 8 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B15.1 | `[~]` | P2-6 | Purpose/class TTL с owner/review; ongoing service extension, financial/grant/rights separate policy. |
| B15.2 | `[~]` | P2-6/P7-1 | Namespace cutoff для poll/backfill/reimport/restore; erasure epoch/FK integrity без reward remint. |
| B15.3 | `[~]` | P2-6/P4-3 | Verified scope/link/export/RBAC; is_staff/shared link/username не полный PII access. |
| B15.4 | `[ ]` | P2-6/P4-2 | Military proof minimal private ref + human decision; no LLM статус/diagnosis/скидка по фото. |
| B15.5 | `[~]` | P2-6/P0-3/P2-4 | Actual payload/log allowlist, no signed URL/PII/hidden reasoning; audio/vision distinct. |
| B15.6 | `[~]` | P2-6/P7-1 | Bounded cleanup lease/retry, blobs вне transaction; restore применяет tombstones до writers. |
| B15.7 | `[~]` | P2-6/P2-1 | Reset/unlink/revoke/erasure различимы; scoped authorized audit, money ledger не clear-memory. |
| B15.8 | `[ ]` | P2-6/P7-1 | Изолированный upload→export→retention→erasure→replay/restore, failed media и ongoing case. |

## B16 · 8 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B16.1 | `[~]` | P2-4/P4-4 | Versioned outcome events/dedupe client/episode/order/line/attempt; no doubled purchase. |
| B16.2 | `[~]` | P2-4/P4-2/P4-4 | Referral→episode→offer→order/return provenance; manual assignment и unknown attribution отдельно. |
| B16.3 | `[ ]` | P2-4/P4-2 | Eligible denominator/revenue/net/margin/gift/zero/COD definitions; deposit не full revenue. |
| B16.4 | `[~]` | P2-3/P3-9/P2-1 | Primary attention по actionable obligation/aging; selection/mode/paid/customer scoring не priority. |
| B16.5 | `[~]` | P2-4/P5-1 | Coverage/helpfulness/handoff/objection outcomes с evidence; silence unknown, loss reason не guessed. |
| B16.6 | `[~]` | P2-4/P1-7 | Task/model/effective effort/queue-send latency/quota/outcome/sample; no free-tier infinity. |
| B16.7 | `[ ]` | deferred → P7-1 | Малые обратимые experiments только после defect/evidence baseline, human scope и stop gate. |
| B16.8 | `[ ]` | P2-3/P2-4/P7-1 | Report/panel сходятся с event/financial definitions; unknown coverage видна, приёмка сценарная. |

## B17 · 9 задач

| Старый ID | Ист. | Назначение 3.0 | Оставшийся gate / сохраняемая граница |
|---|---|---|---|
| B17.1 | `[ ]` | deferred refactor → P7-1 | Сохранить предметные boundaries; структурный cleanup после consumers, без нового async проекта. |
| B17.2 | `[ ]` | P0-4/P5-1/P4-2/P7-1 | Удалять duplicate writers лишь после activation/acceptance; intentional legacy action exclusions сохранить. |
| B17.3 | `[ ]` | P1-1/P1-3/P4-1/P4-2 | Единый domain owner/version/invalidation; stale FAQ claims и dependency alerts предметно triage. |
| B17.4 | `[ ]` | P7-1/P2-3 | Producer/state/consumer/UI/gate/effective mode registry; off не бросает reconciliation/withdrawal. |
| B17.5 | `[ ]` | P3-10/P7-1 | Поздние domain UI integrations не возвращают overlap/scroll-x/contrast/input regressions. |
| B17.6 | `[ ]` | P7-1 | Runbook по реализованным recovery/UNKNOWN/resume/rollback/deletion; readiness read-only. |
| B17.7 | `[ ]` | P7-1 | Unique IDs/direct DAG/required consumers/owner rules/crosswalk; no hidden unresolved forward blocker. |
| B17.8 | `[ ]` | P7-1 | Actual release/SHA/schema/flags/local-prod evidence/externally blocked capabilities + remaining gates. |
| B17.9 | `[!]` | P7-1 | Broad baseline+incident corpus+MariaDB gates; after release минимум 72h И 20 подходящих новых ходов. |

**Контроль полноты:** 196/196 действующих ID, 0 повторов, 0 отсутствующих. Покрытие записи не означает выполнения контракта; при переносе/закрытии основного паспорта обновлять оставшийся gate и evidence, сохраняя этот исторический статус.
