# Website review + UGC · контракт владельца · 09.10.2026

CODE/TEST реализованы; PROD PENDING до фактической квитанции. Этот файл фиксирует контракт и границы приёмки.

- По прямому уточнению владельца **5% — только добавка к подтверждённой UGC-награде10%**. Отдельный бонус5% не выдаётся.
- Повышается **тот же неиспользованный активный код10% до15%**, один раз. Изначальные code/issued_at/valid_until и90дней сохраняются; lifetime grant не повторяется.
- Потраченный, просроченный, отозванный либо зарезервированный в checkout код не повышается. Инвойс/заказ с уже зафиксированной скидкой не пересчитывается. Неизвестная/processing/ambiguous доставка требует сверки, не повторного сообщения.
- Нужен настоящий опубликованный после модерации **отзыв с оценкой1–5** к конкретной купленной каталожной позиции. Любая честная оценка допустима; положительный текст или5звёзд не являются условием. Комментарий без оценки не даёт бонус.
- Одна подтверждённая подходящая товарная позиция даёт компонент5% один раз, а не5% за каждый товар/комментарий/повторную модерацию.
- Product должен иметь действующую опубликованную страницу. Custom-print/DTF/null-product и неподтверждённые соответствия не получают ссылки/обещания. Смешанный заказ может участвовать через его настоящую каталожную позицию.
- Источник — exactclient/order/assignmentversion/orderitem/product + confirmed paid/carrier receipt/privacy/reset fences. Email/ник/скриншот/произвольный ReviewID и старый is_verified_purchase не доказывают эту привязку.
- Частная invitation capability разрешает только отзыв о выбранной купленной позиции; не авторизует аккаунт и не показывает контактные/платёжные данные. После обмена на ограниченный session context URL очищается; private state/token не попадает в public cache.
- В карточке отзыва сохраняется incentivized disclosure независимо от старой campaign. До moderation+grant показывается pending/ожидание, без ложного утверждения начисления.
- Историческое сообщение10% и provider receipts остаются неизменными. Новое подтверждение15% имеет отдельную generation того же outbox; повторное выполнение не выдаёт купон/бонус/сообщение заново.
- Review перед UGC и UGC перед Review проходят один источник/once-only writer. Обычный manager input15 не даёт полномочие на награду.

Production baseline86e4baba8, защищённое read-only чтение: Reviews0/approvedstars0/UGCrewards0/activeassignments9. Review table **MyISAM**; PromoCode/guestusage/UGC/lifetime/Order/OrderItem/PaymentAttempt/assignment **InnoDB**. Нужна миграция Review→InnoDB и новых authority/outbox constraints; local MariaDB и production schema checks обязательны. Production не тестовая fixture.

Оркестрация: reviews_contract — website provenance/models/services; aftercare_path — coupon uplift/guest capability/versioned reward outbox; memory_card_ui — composer и source-bound bot invitation; client_ui_test_maintenance — независимые native regressions; aftercare_review — independent acceptance. Root владеет DB/Git/deploy/browser/docs. В параллельном отдельном пакете P1-4 исправляется legacy lifetime-paid suppression при dispatch нового source-bound intent; отдельный commit/deploy после этого блока.


## Дополнительный контракт marketing opt-in

После подтверждённой оплаты создаётся приглашение именно для **post_purchase_marketing** этого client/order/assignment/version/reset. Отправка — в подтверждённом стандартном окне и при действующих pause/takeover/privacy/global-opt-out/provider-permission guards. Текст и кнопки UK/RU/EN выбираются по текущему собственному сообщению клиента, не по случайной старой карточке. Пользователь может согласиться, отказаться и отозвать согласие. Простое «да», вручную введённый payload, чужой sender/namespace/MID, неподтверждённая доставка карточки, истёкший или старый reset не дают подписку.

В журнале раздельно сохраняются signed immutable invitation, actual provider MID/receipt HMAC и immutable source-confirmed answer. Воронка показывает business_accepted/declined/revoked/expired и отдельно **native future grant unverified / standard window only**. Подтверждение business consent **не продлевает24h**. Вне окна автоматическая реклама остаётся заблокирована до отдельной доказанной native account capability. Согласия payment reminder/restock не реализованы этим срезом и не заимствуют marketing answer.

Обещание UGC/ссылок на бонус требует актуального business consent; обычный сервисный запрос честного отзыва и выдача уже заработанного кода сохраняют своего владельца. Выданный10% содержит зафиксированное приглашение с настоящими купленными каталожными ссылками только при подтверждённом согласии. Нет ссылок/обещания+5 для custom/DTF/null-product и неизвестной привязки. После настоящей staff-модерации любой оценки1–5 тот же код становится15%, доставка подтверждения — отдельная immutable generation. При отзыве согласия до socket рекламная часть блокируется, при уже доставленной части — UNKNOWN/AMBIGUOUS, без повторной отправки.

## Долговечность, блокировки и контекст

Модерация и review_uplift lifecycle job фиксируются в одной transaction. Потерянный on_commit callback не теряет бонус: existing bounded worker выполняет persisted job. Callback/Telegram/IndexNow ошибки не отменяют успешно зафиксированную модерацию; ошибка enqueue откатывает approval. Reject удерживает15% для новых оплат, повторное approval не создаёт второй компонент/код и не меняет срок.

Единый порядок Client→Order→Promo→Reward→Review применяется в checkout, uplift, delivery и lifecycle. Реальные двухсоединительные MariaDB сценарии проверяют checkout против reject, reservation против uplift и final delivery guards. Provider receipt checkpoints и uncertain delivery не подменяются retries. Review legacy MyISAM переводится в InnoDB только на пустой таблице под canonical write freeze; новые authority tables/SQL triggers проверяются на native MariaDB и после deployment.

Gemini получает один captured finite benefit slot, общий с admin/state/manifest: policy10+5, effective/available15, исходный90-day expiry, used/reserved/held/expired, source-order match, бизнес-согласие и unverified native grant. В контексте нет coupon/private URL/секретных payload; LLM не может выдавать, повышать или применять код.

## Подтверждение платформы

Context7 прочитан по официальной Instagram Platform документации: [overview](https://developers.facebook.com/docs/instagram-api/overview), [quick replies](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/quick-replies), [button template](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/button-template). Быстрый ответ приходит отдельным webhook payload; кнопки≤20символов. У подтверждения business purpose нет документально принятой в этом account native future grant. Messenger capabilities не перенесены автоматически в Instagram; HumanAgent не используется для автоматической рекламы.

Приёмка: fresh-schema native78 PASS/16.210s. Финальные affected/provider dispatch/producer budget gates и production evidence записываются в RELEASE после фактического завершения. Browser composer1816/390/320 проверен: overflow0, обязательные звёзды, optional email, очищенный URL, disclosure; screenshots доступны в private local QA, production assets проверяются отдельно.


Финальный native набор:482сценария,481PASS/49.970s; единственная ошибка — oversized order_number новой fixture, не ослабленный guard. Fixture приведена к реальному max_length, весь consent набор повторно проходит отдельно (квитанция в RELEASE). Посторонние legacy проверки14 предварительно воспроизводились теми же14ошибками на исходном HEAD bot_followups; относятся к отдельному P1-4 пакету и не входят в этот релиз.

Producer приглашений отдельно ограничен min(limit,100), учитывает current provider-dated USER/namespace/reset и исключает уже существующие exact scopes в SQL. Advisory cursor продвигается также после отказа owner, поэтому непроверенный manual paid заказ не блокирует более поздний проверенный. Cache не создаёт business authority. В default daemon batch10 зарезервирован consent slot и отдельный slot для остальных due queues даже при постоянном lifecycle backlog. Generic opt-in/resume не отменяет ANY prior global opt-out для business purpose.

Rollback: сначала отключить именно IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED; scoped revert/main push/канонический SSH pull/check/restart. Не удалять receipt/answer/review/component и не разворачивать engine назад;15% нельзя подтвердить старым кодом без component owner. No native future-token backfill, mass sends, forced real-customer UGC/review или production fixtures.
