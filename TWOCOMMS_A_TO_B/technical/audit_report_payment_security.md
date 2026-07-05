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

## Приоритет исправлений (сводно по всем отчётам)
1. **Находка 9** — подделка оплаты через вебхук (деньги). 🔴
2. **Находка 2** — PII-утечка `/orders/success-preview/`. 🔴
3. **Находка 1** — гостевой COD-заказ 500 (потеря заказов). 🔴
4. **Находка 4** — Monobank quick-оплата не работает. 🔴
5. Находки 3, 5, 6, 8 — HIGH. Находка 7 — MEDIUM.
