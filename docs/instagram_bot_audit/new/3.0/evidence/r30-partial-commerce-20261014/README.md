# Engineering40 · частичное оформление и продолжение · 14.10.2026

**✅ DEPLOYED.** Код `656732f72233b18ebc0636b45edcf0e410dc5faf`.160 уникальных Python/MariaDB сценариев и105Node PASS,0skips. Canonical migration0234, runtime и авторизованный browser проверены. [Production receipt](PRODUCTION_RELEASE_2026-10-14.md). [Согласованное поведение](CONTRACT.md).

## Реализованный путь

R1 сохраняет весь исходный список и отдельные идентичности получателей/строк. Менеджер утверждает готовность, актуальную цену выбранной готовой части и условия доставки. Только собственная кнопка действительно доставленной карточки создаёт проверяемый выбранный scope. «Ждать всё» оставляет пожелание без счёта, резерва, заказа или подписки.

E1 проходит существующие proposal → invoice generation → PaymentAttempt → canonical Order. O1 содержит только выбранные целые строки. После подтверждённой оплаты собственная кнопка продолжения принимает точный неоплаченный остаток в новую episode/session: R2 сохраняет исходное intention и ancestry, но требует новой физической проверки менеджером, цены, карточки и покупки. Для трёх исходных строк тот же путь поддерживает R3/O3. Исходные IDs сохраняются отдельно от новых current IDs и плотных quote positions; settled/inflight/deferred coverage не даёт продать строку повторно. Предыдущие оплаченные товары, получатель, платёж и ТТН остаются историческими данными своего заказа.

Основные causal-сценарии: [native sources/API](../../../../../../twocomms/management/tests_ig_assisted_partial_sources.py), [approval/scopes/R2](../../../../../../twocomms/management/tests_ig_assisted_partial_core.py), [checkout и неизменность O1](../../../../../../twocomms/management/tests_ig_assisted_partial_checkout.py), [оплата и удержания](../../../../../../twocomms/management/tests_ig_assisted_partial_payment.py), [двух- и трёхзаказная история](../../../../../../twocomms/management/tests_ig_assisted_partial_history.py). Они используют реальные локальные producers; внешние transport/provider responses заменяются тестовыми ответами.

## Доставка и неопределённый результат

Согласуются payer и максимальная сумма UAH для now/later/together. Остаток R2/R3 использует свой scope **later**, включая случай, когда часть остатка снова WAIT. Это предел расходов клиента, а не банковский счёт за доставку или обещание тарифа перевозчика. Реальный документ с неизвестной/превышающей предел стоимостью удерживается на проверке: повторное создание, shipping notification, tracking и aftercare блокируются до проверки того же документа. [Shipping guards](../../../../../../twocomms/management/tests_ig_assisted_partial_shipping.py), [tracking/notification](../../../../../../twocomms/management/tests_ig_assisted_partial_tracking.py).

UNKNOWN карточки нельзя обходить новой operation/approval. Неопределённый банковский эффект сохраняет защиту каталожного и размерного остатка после обычного срока резерва; paid RESOURCE_REVIEW сохраняет hold. Автоматическое освобождение по позднему результату за существующим временем доказательства остаётся OPEN. Автоматическое создание production ТТН здесь не заявлено.

## Схема и пределы чтения

0234 добавляет CHECK для partial + nonnull owned choice и переустанавливает15 native guards. FULL39 сохраняет точные сравнения native totals, подписанные columns, OneToOne decision/proposal/order, уникальное active intention и terminal/privacy fences. Новых signed columns нет; rollback возвращает full39 guards, не переписывая историю. [Native migration/guard tests](../../../../../../twocomms/management/tests_ig_assisted_partial_models.py). Финансовые/source/stock parents требуют InnoDB; legacy Product/auth metadata допускает MyISAM без конвертации и без обещания transactional metadata locks.

**96SELECT для обычной истории/client_context не расширен.** Native R2/R3 history тестируется в прежнем пределе с повторным чтением источников, подписей и финальных fences. Отдельный fresh assisted-offer reader имеет другой объём работы: сравнительный профиль двух custom-строк зафиксировал full39=382SELECT и partial40=307SELECT, prompt descriptor=0SQL. Это не перенос96 на свежую финансовую проверку. [Раздельное измерение](../../../../../../twocomms/management/tests_ig_assisted_partial_read_budget.py); per-call verified read unit не заменяет новую проверку перед каждым эффектом.

Execution envelope ограничен **256KiB**. Новые E1/E2/E3 содержат подписанные ссылки/digests на точную immutable native checkout revision вместо повторных вложенных quote/cart snapshots. Native quote проверяется в собственном прежнем **1MiB** пределе; лимиты Request/Decision/Execution не увеличены. Старый inline FULL39 читается совместимо.

## Приёмка и открытые границы

Финальная приёмка:160 уникальных Python/MariaDB сценариев (прогоны157/5/51 пересекаются) и105Node PASS,0skips; Django check/migration drift и5 rendered inline scripts чистые. [Точный manifest](native-accepted.json), [validation](VALIDATION.json), [production](PRODUCTION_RELEASE_2026-10-14.md). Прежние промежуточные ошибки исправлены и перепроверены; они не суммируются как PASS.

Дополнительно: освобождение dispatched резерва требует точного подписанного отказа native bank writer; bare FAILED и очищенный локальный ambiguity flag не заменяют evidence. Исходный durable UNKNOWN journal запрещает позднему writer создать ложное доказательство отказа.

Engineering41 — самостоятельный положительный opt-in на точный список ожидания — **OPEN**. Partial/Resume не дают маркетингового согласия. Деление количества одной строки, изменение оплаченных условий, новое согласование превышенного тарифа и естественный оплаченный production cohort не объявлены закрытыми этой локальной приёмкой. Production fixtures, реальные тестовые bank invoices и автоматическая production ТТН не создавались.


Локальный UI: [desktop](ui-paid-desktop.png), [390px](ui-paid-390.png), [320px](ui-paid-320.png); клиентское оформление [desktop](ui-checkout-desktop.png), [390px](ui-checkout-390.png), [320px](ui-checkout-320.png). Изображения используют только синтетический локальный пример.
