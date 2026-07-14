# Единое определение Purchase (W2-3 / CRO-045 → TECH-066)

Канонические определения событий заказа для ВСЕХ слоёв аналитики
(Meta CAPI, TikTok Events API, GA4, внутренний UserAction).

## Определения

| Событие | Определение | Триггер в коде |
|---|---|---|
| **place_order / Lead** | Заказ создан (COD-заявка или инвойс выставлен) | `checkout.py` (COD create), `monobank.py` (invoice created → prepaid Lead) |
| **Purchase** | Подтверждённая оплата ИЛИ получение посылки | (1) Monobank webhook/API с подтверждённым `paid`/`prepaid`; (2) подтверждённый paid-state в admin/manual/Instagram; (3) NP «посылка получена» → `nova_poshta_service._apply_tracking_update` |

Создание заказа ≠ покупка. Purchase фиксируется только при движении денег.

## Матрица слоёв (после W2-3)

| Поток | Meta CAPI | TikTok | UserAction | GA4 |
|---|---|---|---|---|
| Онлайн-оплата (webhook paid) | Purchase ✅ | CompletePayment ✅ | purchase ✅ | purchase (client, order_success) ✅ |
| Предоплата (webhook prepaid) | Purchase (value=full, `paid_value`=факт) ✅ | PlaceAnOrder (Lead) ✅ | purchase ✅ | client ✅ |
| COD-выкуп (NP received) | Purchase ✅ | CompletePayment ✅ (W2-3в) | purchase ✅ (W2-3б) | ❌ **ПРОБЕЛ** — см. ниже |

## Внутренний UserAction (F-083)

Все серверные confirmed-пути вызывают
`storefront.utm_tracking.ensure_order_purchase_action()`. Helper принимает
только `paid` / `prepaid` / `partial`, пропускает явно бесплатный ручной заказ,
и повторно использует уже существующую строку вместо создания дубля.

- Monobank webhook и API/poll повторно вызывают helper даже для уже
  подтверждённого статуса: повторная доставка лечит пропущенную аналитику, но
  внешние Meta/TikTok/Telegram-события остаются transition-only.
- Ручное создание/редактирование и admin payment-status пишут purchase в той же
  транзакции. Перевод ручного заказа в preset `free` удаляет ошибочный purchase
  и пересчитывает состояние связанной UTM-сессии.
- Paid Instagram deal пишет purchase при построении заказа; safety-net повторяет
  попытку для уже созданного заказа без аналитики.
- Повторный delivered poll Новой Почты лечит отсутствующий purchase независимо
  от дедупа уведомлений.
- Публичный `/api/track-event/` не является доверенным writer: `lead` и
  `purchase` возвращают HTTP 400 и создаются только серверными путями.

## Известный пробел: GA4 server-side (W2-3г)

COD-выкуп не отправляется в GA4: нужен Measurement Protocol
(`api_secret` из GA4 Admin → Data Streams → Measurement Protocol API secrets).
Это `[OWNER]`-шаг; после получения секрета добавить отправку в
`nova_poshta_service` рядом с `_send_tiktok_purchase_event`.
До тех пор GA4 занижает purchases на долю COD-выкупов — учитывать при
сравнении с Meta/внутренними отчётами.

## Дедуп

- Флаги в `Order.payment_payload`: `facebook_events.purchase_sent`,
  `tiktok_events.purchase_sent`, `tiktok_events.lead_sent` — проверяются
  ПЕРЕД отправкой в обоих путях (webhook и NP).
- `event_id` детерминированный по заказу (`order.get_purchase_event_id()`)
  — одинаков на клиенте и сервере → Meta/TikTok дедупят Pixel↔API.
- UserAction purchase: max 1 строка на `order_id`; migration 0083 добавляет
  MariaDB unique constraint `(action_type, order_id)`, а helper использует
  идемпотентный `get_or_create`. Это защищает и от повторов, и от гонок.

## Reconciliation / recovery

`manage.py reconcile_purchase_actions` по умолчанию работает как dry-run;
запись разрешается только с `--apply`. Команда восстанавливает лишь отсутствующие
события для доказуемо подтверждённых web/Monobank-заказов, использует
историческое время первого success callback (fallback: delivery/order time), не
отправляет внешние события и не переписывает существующие purchase-строки.

Старые ручные `paid`-заказы без сохранённого preset намеренно исключены: по ним
невозможно отличить реальную оплату от подарка. Явный preset `free` также всегда
исключён. Production cron ежедневно запускает идемпотентный `--apply`; повторный
запуск обязан возвращать `created=0`, если новых пропусков нет.

## paid_value (W2-3д)

Meta Purchase несёт `value` = полная стоимость заказа (для ROAS) и
`custom_properties.paid_value` = фактически внесённая сумма
(предоплата в грн из `paidAmount`-копеек, либо total_sum при COD-выкупе).

## Refund / Cancel (W2-3е — TODO)

События возврата/отмены (невыкуп НП) пока НЕ отправляются ни в один слой —
отдельная задача после накопления чистых purchase-данных.
