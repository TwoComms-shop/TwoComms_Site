# Аудит безопасности платежей (Monobank)

Дата: 2026-07-06. Находки проверены по коду. Эксплойт против боевых данных НЕ выполнялся (только статический анализ), чтобы не менять реальные заказы.

## Находка 9 (CRITICAL, безопасность): вебхук Monobank НЕ проверяет подпись для заказов магазина → подделка оплаты

### Факты
- Маршрут: `storefront/urls.py:444` — `path('payments/monobank/webhook/', csrf_exempt(views.monobank_webhook), ...)`. CSRF отключён (это нормально для вебхука), но взамен обязана быть криптопроверка подписи.
- Функция проверки подписи СУЩЕСТВУЕТ: `storefront/views/monobank.py:196` `_verify_monobank_signature(request, ...)` — читает заголовок `X-Sign`, тянет публичный ключ мерчанта (`/api/merchant/pubkey`), проверяет ECDSA/SHA256.
- **НО она нигде не вызывается.** `grep` по всему `monobank.py`: единственное вхождение — сама строка `def`. Это мёртвый код.
- В `monobank_webhook` (monobank.py, ~строка 1330+) для заказа магазина выполняется:
  ```python
  order = _get_order_by_payment_refs(invoice_id=..., order_ref=..., order_id=...)
  status_value = result.get('status') or payload.get('status')
  _apply_monobank_status(order, status_value, payload=payload, source='webhook')
  ```
  `_apply_monobank_status` (monobank.py:~строка) выставляет `order.payment_status = 'paid'/'prepaid'`, если `status` ∈ `MONOBANK_SUCCESS_STATUSES` — **напрямую из тела запроса, без верификации**.

### Вектор атаки
Любой в интернете может отправить:
```
POST /payments/monobank/webhook/
Content-Type: application/json

{"invoiceId": "<любой>", "status": "success", "orderId": 12345}
```
Поиск заказа идёт в т.ч. по `orderId` = `Order.id` (`_get_order_by_payment_refs`: `qs.get(id=order_id)`) — это **последовательный целочисленный PK**, легко перебирается. Также по `orderRef` = `order_number`. Успешный матч → заказ помечается оплаченным, шлётся Telegram-уведомление админу «оплачено» и e-mail-квитанция клиенту. Итог: отгрузка товара без реальной оплаты + фиктивные уведомления.

### Важное уточнение (что НЕ уязвимо)
Ветки, где `_get_order_by_payment_refs` не нашёл заказ, но найден `WholesaleInvoice` или IG-bot invoice, — защищены: они игнорируют статус из тела и делают **pull-проверку** через acquiring-токен (`management.services.invoice_payments.process_webhook`, `bot_payments.handle_webhook_invoice`). Комментарии в коде это подтверждают. Уязвим именно основной путь заказов магазина.

### Рекомендация по фиксу (приоритет №1)
1. В начале `monobank_webhook` вызвать `_verify_monobank_signature(request)`; при `False` → `return HttpResponse(status=400)` до любой обработки.
2. Дополнительно (defense in depth): для заказов магазина не доверять `status` из тела, а делать pull-проверку статуса инвойса через Monobank API по токену — так же, как уже сделано для wholesale/IG.
3. Не искать заказ по сырому `orderId=PK`. Использовать непубличный `payment_invoice_id` / случайный `order_number`.

---

## Смежные проверки (сделаны)
- `monobank_create_invoice` (реальная, `monobank.py:352`) — основной путь оплаты жив.
- `monobank_create_checkout` (quick/express) — заглушка, см. Находку 4 в `audit_report_legacy_stubs.md`.
- Публичный ключ кэшируется (`MONOBANK_PUBLIC_KEY_CACHE_KEY`), инфраструктура для проверки готова — нужно лишь подключить вызов.

---

## Дополнение CRO-043: webhook, статусы, идемпотентность и failed payment (06.07.2026)

### Что проверялось

Пункт `CRO-043` из глобального чеклиста требует проверить:

1. вебхук Monobank идемпотентен и повторный callback не создаёт второй `purchase` event;
2. подпись webhook проверяется;
3. отказ оплаты ведёт на `order_failed.html` с восстановимой корзиной.

Эксплойт-запросы в боевые endpoints не отправлялись, потому что текущий код позволяет менять реальные заказы. Проверка ниже основана на статическом анализе кода, read-only DB-замерах и логах.

### P0-1: подпись webhook всё ещё не проверяется

Это подтверждает основную «Находку 9»: `_verify_monobank_signature()` есть, но `monobank_webhook()` её не вызывает.

Актуальный путь:

1. `storefront/urls.py` регистрирует `payments/monobank/webhook/` как `csrf_exempt(views.monobank_webhook)`.
2. `monobank_webhook()` парсит JSON body.
3. Ищет заказ по `payment_invoice_id`, `order_number` или сырому `Order.id`.
4. Берёт `status` из body.
5. Передаёт статус в `_apply_monobank_status()` без cryptographic verification и без pull-check через Monobank API.

Для основного магазина это означает: `status=success` из неподписанного body считается источником истины. Ветки wholesale/IG в этом же webhook лучше защищены: если основной Order не найден, они делегируют обработку сервисам с pull-verify.

### P0-2: return URL имеет unsafe fallback `status_value or 'success'`

Найден дополнительный риск, которого не было явно в первой версии отчёта.

`monobank_return()`:

1. получает `invoiceId` из query или session;
2. получает `orderId` из query или `monobank_pending_order_id` в session;
3. ищет Order через `_get_order_by_payment_refs()`;
4. если есть `invoiceId`, пытается сделать pull-check `/api/merchant/invoice/status`;
5. затем вызывает:
   - `_apply_monobank_status(order, status_value or 'success', payload=status_payload, source='return')`.

Проблема: если `status_value` не получен, код подставляет `success`. Это опасно в двух сценариях:

1. Monobank status API временно недоступен или вернул неожиданный формат: заказ может быть помечен paid/prepaid без подтверждения.
2. URL вызван с `orderId`/session pending id без валидного `invoiceId`: status не проверяется вообще, fallback становится `success`.

Это не проверялось exploit-запросом на бою, потому что может изменить реальные payment_status и отправить purchase/уведомления.

### Идемпотентность purchase-event: частично корректно

Код `_apply_monobank_status()` делает правильную защиту от повторных `purchase` событий:

1. сохраняет `old_payment_status`;
2. применяет новый статус;
3. вызывает `record_order_action('purchase', ...)` только если итоговый `order.payment_status in ('paid', 'prepaid')` и он отличается от `old_payment_status`.

Read-only DB-замер:

1. `UserAction(action_type='purchase')` всего 3.
2. По `order_id` дублей purchase не найдено: в выборке каждый из order_id 261, 269, 271 имеет `c=1`.
3. При этом `payment_payload.history` у реальных заказов показывает повторные success:
   - order 271: `created → processing → success → success → success`;
   - order 269: `created → processing → success → success`;
   - order 257: `created → processing → success → success`;
   - order 255: `created → processing → success → success`;
   - order 251: `created → processing → success → success`;
   - order 250: `processing → success → success`.

Вывод: повторные success callbacks/returns уже встречаются, история растёт, но второй `purchase` event и повторные Telegram/e-mail по этому коду не должны отправляться, потому что они завязаны на изменение payment_status. Это хорошая часть реализации.

### Failed payment и восстановимость корзины

Факты по коду:

1. При создании invoice корзина **не очищается**: комментарий в `monobank_create_invoice()` прямо говорит, что cart очищается только после успешной оплаты в `monobank_return` или webhook.
2. При успешном return вызывается `_cleanup_after_success()`, который удаляет `cart`, promo и monobank session keys.
3. При failure status `_apply_monobank_status()` ставит `payment_status='unpaid'`.
4. `monobank_return()` при failure не ведёт на `order_failed.html`; он ставит message и делает `redirect('cart')`.
5. `pages/order_failed.html` существует, но в Monobank flow фактически не используется: `redirectUrl` у invoice указывает только на `/payments/monobank/return/`, а failure branch этого handler возвращает в cart.

Вывод: восстановимость корзины в целом задумана правильно (cart не очищается до success), но требование чеклиста «отказ оплаты ведёт на `order_failed.html`» не выполняется. Пользователь возвращается в корзину с message, а dedicated failed page остаётся мёртвым/полумёртвым элементом.

### Read-only DB-база по Monobank

Замер на боевой БД 06.07.2026:

| Метрика | Значение |
|---|---:|
| Order всего | 41 |
| Order с `payment_provider='monobank_pay'` | 32 |
| Order с непустым `payment_invoice_id` | 32 |
| Monobank `checking` | 2 |
| Monobank `paid` + `online_full` | 15 |
| Monobank `paid` + `prepay_200` | 10 |
| Monobank `prepaid` + `prepay_200` | 3 |
| Monobank `unpaid` + `prepay_200` | 2 |
| UserAction `purchase` | 3 |
| UserAction `lead` | 5 |

Два `checking` заказа и два `unpaid` Monobank заказа стоит отдельно разобрать владельцу/исполнителю: они показывают, что pending/failure состояния реально существуют, а не только теоретически.

### Задачи исполнителю по CRO-043

1. В начале `monobank_webhook()` обязательно вызвать `_verify_monobank_signature(request)` для storefront merchant token; при False возвращать 400 до поиска/изменения Order.
2. Для основного магазина дополнительно делать pull-check invoice status через Monobank API по `invoiceId`; body webhook использовать как уведомление, а не как истину.
3. Убрать lookup по сырому `orderId=Order.id` из webhook/return или разрешать его только после проверенного invoice/status match.
4. В `monobank_return()` убрать fallback `status_value or 'success'`. Если status неизвестен:
   - не менять payment_status на paid/prepaid;
   - показывать pending/error;
   - логировать проблему;
   - предложить повторить оплату или связаться с менеджером.
5. Определить UX для failed payment:
   - либо реально вести на `order_failed.html` и оставить cart восстановимым;
   - либо обновить чеклист/документацию, что failure UX — это redirect в cart с message.
6. Добавить regression tests:
   - webhook без `X-Sign` не меняет Order;
   - webhook с invalid signature не меняет Order;
   - repeated success webhook создаёт максимум один `purchase` UserAction;
   - return без invoice status не помечает заказ paid;
   - failure status сохраняет cart и показывает восстановимый UX.

### Обновлённый статус CRO-043

`CRO-043` проаудирован. Идемпотентность `purchase` event при повторном success в основном выдержана, но подпись webhook не проверяется, основной webhook доверяет body, return fallback может подставить `success`, а `order_failed.html` не используется Monobank flow.

## Приоритет исправлений (сводно по всем отчётам)
1. **Находка 9** — подделка оплаты через вебхук (деньги). 🔴
2. **Находка 2** — PII-утечка `/orders/success-preview/`. 🔴
3. **Находка 1** — гостевой COD-заказ 500 (потеря заказов). 🔴
4. **Находка 4** — Monobank quick-оплата не работает. 🔴
5. Находки 3, 5, 6, 8 — HIGH. Находка 7 — MEDIUM.
