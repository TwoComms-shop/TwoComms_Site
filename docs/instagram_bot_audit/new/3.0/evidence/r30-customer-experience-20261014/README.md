# Клиентские карточки, доставка и самостоятельная корзина · 14.10.2026

Позднее уточнение владельца добавляет обязательную проверку всей цепочки к инженерным блокам39–40. Нативные кнопки, Gemini, пожелания, склад, менеджер, checkout и платёж должны читать совместимые подтверждённые данные.

## 40A · Коммуникация о доставке — ✅ выпущена

Код `c91c290725958f5bfe24bc1e8dd4c5d1b15e3c5c`. **88 уникальных Python/MariaDB проверок PASS,0 skips** выполнены на отдельной копии baseline621ed070d +точно13 файлов релиза; байты сверены перед staging. Новые сообщения обещают отправку согласованных позиций, объясняют отдельную оплату перевозчику и согласованный максимум. Точная дата прибытия зависит от перевозчика. Нулевой merchant cap означает только явные согласованные условия, не автоматическую акцию. Созданная ТТН не называется фактической отправкой; неподтверждённые1–3дня перевозки удалены.

Доставка и цена товара имеют отдельную authority: Gemini может назвать точный согласованный предел только с текущей фазой/плательщиком/валютой. Предел не разрешает назвать ту же сумму ценой товара; фактический тариф, предоплата/включённая доставка, акции и страховые гарантии не выдумываются. Несколько позиций, смешанные денежные фразы, UK/RU/EN, склад/характеристики/даты и платёжные ограничения проверяются независимо.

Исторически отправленные предложения сохраняют свой подписанный текст и кнопки. Новые split/fulfillment сообщения фиксируют собственную версию и текст перед первой отправкой. UNKNOWN не повторяется; native MID записывается до истории. Это изменение новых сообщений, не переписывание клиентской переписки.

Canonical SSH main/pull/check/static/compress/restart/supervisor выполнены. Production main/supervisor/child на том же SHA, PID1840399 healthy, maintenanceOFF;15READONLYSELECT,0provider probes,0customer test sends,0production fixtures.81catalogue rows/stock digest и15существующих native guards не изменены. Миграций в40A нет; checkout UI и новые карточки не входят в этот выпуск. См. машинные manifests.

## 40B · Карточки и варианты — ✅ реализовано и DEPLOYED

Код `1eaa1a30f2bff2fbd2d8cabe54d61b00ec6c2f04`. Native generic carousel: фото конкретного товара, Select/Size chart, страницы до3карточек, дальнейший просмотр. Таблица размеров отправляется изображением. Настроенные цвета/размеры видны до отдельного stock/manager gate, включая нулевой учётный остаток. Кнопка действует только после собственных подтверждённых SENT частей/реального MID, текущего owner/namespace/session/reset/asset proof. Повреждённый или чужой payload не получает выбора. Native configured variant/цвет сохраняются в reducer/source→Gemini.

При известном отказе native template применяется полный source-bound photo+quick-reply fallback. Все требуемые части должны быть подтверждены. UNKNOWN не повторяется и не закрывает долг. Effective fallback согласован с историей, завершением turn и мониторингом; ограниченный монитор сообщает неполное покрытие. Свободное текстовое «первую» без действительного presentation witness не объявляется полностью поддержанным.

## 40C · Правки по той же ссылке — ✅ реализовано и DEPLOYED

До банковского dispatch: ±/удаление, точная актуальная revision/digest/CAS, UUID replay, серверные суммы. SHARE оплачивает, но не изменяет корзину. Обычный checkout сохраняет native link-holder receipt без фиктивного Instagram message/личности/24h. При custom/mixed/partial пожелании старые цены/решения неизменны: свежая проверка менеджера, настоящий alert/outbox, новый successor quote и новый hosted ACK; WAIT остаётся неоплаченным пожеланием. Проверены собственный выбор, цепь старой ссылки, просроченные/чужие grants, реальные owner/head races, UNKNOWN bank и privacy fences.

При оплате legacy корзины учитываются только текущие revision fingerprints складских резервов. Старые RELEASED ревизии остаются историей; текущий released/missing hold по-прежнему требует review. Исходная ссылка после собственных native правок читает реальный invoice/paid состав через bounded verified bridge; stale invoice POST по-прежнему409 до provider. Native selection связывается с реальным выигравшим Order только по проверенному P/Deal/Episode/Attempt/rows/provenance, не по подписи статуса.

## 40D · Фактическая покупка и доставка — ✅ реализовано и DEPLOYED

Общий порог **≥3000грн утверждённых товаров после скидок**. Полная стоимость товаров отделена от суммы предоплаты и тарифа перевозчика. Нативный progress/остаток обновляются по серверным данным; уменьшение корзины/промокод/изменение порога может убрать автоматическую льготу — новый economics ACK обязателен до банка. Исторически замороженный invoice/reissue сохраняет свою политику. Явные manager/manual/split условия не заменяются автоматическим порогом. Основная mini-cart metadata использует ту же policy; исторические Orders не backfill.

Actual winner/Attempt→материализованные Order rows→подписанный CheckoutOrderAuthority→Gemini/response plan/shared state/admin. Видны реальные товары/размеры/количество/цены, discount/net/payable, подтверждённая оплата/остаток, dated native изменения. Новый producer фиксирует именованные quote rows до/после правки: **4→3**, без подмены wishlist snapshot. Стандартный link-holder source не требует fakeIGID, не расширяет Meta window. Нативные facts читаются через реальный facade; повторные immutable proof reads исключены только внутри того же atomic admin capture, с сохранением обоих owner/source/payment fences и лимита96.

Winning payment choice синхронизирует фактические Deal.requested amount/pay_type; 200грн не сравниваются с исходным full request3000 как ложный конфликт. Квалифицированная последующая manager payment берётся из текущего source-qualified ledger; несовместимые bank/manager суммы, refund/reversal/future evidence не дают full-payment authority. Legacy manager-created Orders сохраняют реальные цены/квалифицированную оплату без фиктивной bank Attempt. Paid UI показывает стоимость заказа и отдельно «сплачено за цим рахунком», без нового amount-due/апсейла заблокированной корзины.

## Проверки и точная граница приёмки

**387 уникальных Python/MariaDB scenarios PASS,0skips** на frozen source; actual native0235/0236 InnoDB/guards, реальные races и legacy/v2 payment paths. **40Node PASS,0skips**, PythonAST75, Django check/drift/templates clean. Идентификаторы случаев не суммируются повторно между диагностическими runs. Whitespace-only EOF cleanup после gate не меняет логику.

Chrome local UK/RU/EN,320/390/desktop: qty4→3→4→3,3800→2850,150до льготы, fresh delivery ACK; реальная изолированная provider adapter materialization и readonly original link; authenticated manager/shared state показывает покупку2850,0balance и именованный4→3. Скриншот — **синтетический локальный пример**, не production заказ.

Canonical main/pull/migrate0236/check/static/compress/restart/supervisor выполнены. Production main/supervisor/child `1eaa1a30f2bff2fbd2d8cabe54d61b00ec6c2f04`, PID2378739, main/workers healthy, maintenanceOFF. **22READONLYSELECT;0provider probes;0testcustomer sends;0production fixtures.**2новые InnoDBtables/9новых или уточнённых source/privacy guards, nullable native source columns;15assisted guards unchanged,81stock rows/digest unchanged. Loaded production inline renderer подтверждён в авторизованном браузере;320/390 overflow0. Public hashed JS exact source; CSS отличается только3 collectstatic font URL rewrites. См. [production receipt](PRODUCTION_RECEIPT.json), [tests](TEST_MANIFEST.json), [UI receipt](UI_RECEIPT.json).

**OPEN:** natural native gallery/edit/successor/partial paid cohorts (native rows пока0); actual provider acceptance не подменяется fixtures. Next Engineering41: одна положительная подписка на точный список ожидания, свежая физическая проверка менеджера, независимая channel eligibility. Automatic multi-waybill, quantity-in-line split, paid amendment, deadline-before-selection/route, native future capability/P5 checkpoint/P7 и27полных паспортов `[~]` не закрыты этим выпуском. UGC90дней сохранён.

Rollback: не удалять native receipts/Orders/holds/guards и не возвращать несовместимый старый reader для новых платежей. Исправление выполняется scoped forward commit через main; production MariaDB не является тестовой fixture.
