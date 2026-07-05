# Аудит — Раздел 1: CRO / Воронка заказа

> Файл дополняется по мере прохождения пунктов чеклиста `twocomms_global_audit.md`.
> Аудитор ничего не менял в коде — только фиксация фактов для агента-исполнителя.

---

## CRO-041. КРИТИЧНО: COD-заказ не пишет UTM — ПОДТВЕРЖДЕНО (05.07.2026)

### Статус: P0, корневая причина найдена и локализована до строк кода

### Факты (код, ветка main, коммит c4d81dda)

**`storefront/views/checkout.py::create_order` (строки 36–262) — единственная точка создания заказа из корзины. В ней ПОЛНОСТЬЮ отсутствует UTM-привязка:**

1. Импортов из `utm_tracking` в файле НЕТ вообще (проверено: в `checkout.py` импортируются только `orders.*`, `storefront.models`, `productcolors`, `accounts`, `.utils`, `.monobank`).
2. `Order(...)` создаётся на ~строке 152 с полями `user, full_name, phone, email, city, np_office, pay_type, status='new', payment_status='unpaid'` — **без** `session_key`, **без** `utm_session`, **без** `utm_*`.
3. Ни `link_order_to_utm(request, order)`, ни `record_order_action(...)`, ни `record_initiate_checkout(...)` не вызываются нигде в `create_order`.
4. Единственная «аналитика» в этом потоке — `CheckoutCapture.objects.filter(session_key=...).update(converted=True)` (~строка 168) — это спасение брошенной корзины, не UTM.

**Полная карта вызовов UTM-трекинга по проекту (grep по main):**

| Вызов | Файл:строка | Поток |
|---|---|---|
| `record_initiate_checkout` | `storefront/views/monobank.py:565` | только создание invoice Monobank |
| `link_order_to_utm` | `storefront/views/monobank.py:584` | только Monobank |
| `record_lead` | `storefront/views/monobank.py:986` | только webhook/подтверждение Monobank |
| `record_order_action` | `storefront/views/monobank.py:1241` | только Monobank |
| в `checkout.py` | **отсутствуют** | COD-поток |

### Важный нюанс № 1: COD — это не «часть» заказов, а дефолтный путь

В `create_order` ветвление: `if pay_type in ['online_full', 'prepay_200']: return monobank_create_invoice(...)`, иначе — `redirect('order_success')`. То есть **любой** заказ с `pay_type` вне этих двух значений (наложка) уходит без единого UTM-вызова.

### Важный нюанс № 2: даже Monobank-заказ получает UTM только если invoice создан

`monobank_create_invoice` вызывается **после** `order.save()` в `checkout.py`. Если создание invoice упадёт (ошибка API Monobank) — заказ уже существует в БД, но `link_order_to_utm` на строке 584 monobank.py мог не выполниться (нужно проверить порядок внутри `monobank_create_invoice`: `record_initiate_checkout` на 565 и `link_order_to_utm` на 584 идут до/после вызова API — исполнителю проверить обработку исключений между этими строками).

### Важный нюанс № 3: `Order.session_key` тоже не заполняется

`orders/models.py:58` — поле `session_key` есть, но в `create_order` оно не записывается. Это блокирует и «ленивый» бэкофилл: `record_order_action` умеет fallback `session_key or getattr(order, 'session_key', None)` (utm_tracking.py:279), но у COD-заказа поле пустое.

### Подтверждение из БД (снято ранее, зафиксировано в чеклисте)

- 0 из 41 заказов имеют `utm_source`.
- Повторная сверка 05.07.2026 не выполнена: SSH-доступ к серверу временно блокируется (kex reset — вероятно anti-bruteforce hosting'а). TODO-повтор при следующем SSH-окне: `Order.objects.exclude(utm_source__isnull=True).count()`, разбивка по `pay_type`, `payment_payload.tracking`.

### Рекомендация исполнителю (TECH-060)

В `create_order` сразу после `order.save()` (первого) добавить:
1. `order.session_key = request.session.session_key or ''` + сохранить;
2. `link_order_to_utm(request, order)` — идемпотентно, безопасно (внутри try/except);
3. `record_order_action('lead' | 'purchase_intent', order, request=request, cart_value=float(order.total_sum))` — тип согласовать с CRO-045 (определение purchase-момента);
4. Также вызывать `record_initiate_checkout` в начале `create_order` (сейчас есть только в monobank-потоке) — иначе воронка COD не имеет шага initiate_checkout.
5. Расширение: копировать fbclid/gclid/fbc/fbp в Order — см. AN-013 в `audit_report_section2_analytics.md`.

---

## CRO-042. `is_converted` никогда не проставляется — ПОДТВЕРЖДЕНО (05.07.2026)

### Статус: P0, прямое следствие CRO-041 + самостоятельный дефект

### Факты (код)

`mark_as_converted` вызывается только из трёх мест `storefront/utm_tracking.py`:
- `record_lead` (строка ~190) — поиск UTMSession по `request.session.session_key`;
- `record_purchase` (строка ~215) — аналогично;
- `record_order_action` (строка ~330) — только если `utm_session is not None` и `action_type in {'lead','purchase'}`.

Все три вызываются **только из monobank-потока** (см. таблицу выше). COD-заказы не помечают конверсию никогда.

### Самостоятельный дефект: даже для Monobank конверсия может теряться

`record_lead`/`record_purchase` ищут UTMSession строго по `request.session.session_key`. Но:
1. **`record_lead` на monobank.py:986 вызывается в контексте вебхука/подтверждения** — если это серверный callback Monobank, `request.session` там — сессия **сервера Monobank**, не покупателя → `UTMSession.DoesNotExist` → конверсия не помечается. Исполнителю проверить, из какого view вызывается строка 986 (redirect покупателя или webhook). `record_order_action` (1241) устойчивее — умеет брать `order.utm_session`/`order.session_key`.
2. **Смена session_key при логине** (см. AN-031): UTMSession привязана к старому ключу → lookup по новому ключу пуст → конверсия теряется даже в happy-path.

### БД (снято ранее): 1015 UTM-сессий, 0 converted. Повторная сверка — при следующем SSH-окне.

### Рекомендация исполнителю (TECH-061)

1. После фикса CRO-041 конверсия для COD пойдёт через `record_order_action` — убедиться, что `action_type='lead'` или `'purchase'` (иные типы не триггерят `mark_as_converted`, см. utm_tracking.py:330).
2. Переписать `record_lead`/`record_purchase` на использование `order.utm_session`/`order.session_key` fallback (как в `record_order_action`) либо на `visitor_id`.
3. Регресс-тест: заказ с UTM → COD → `UTMSession.is_converted=True`; заказ с UTM → логин → COD → тоже True (после фикса AN-031).
