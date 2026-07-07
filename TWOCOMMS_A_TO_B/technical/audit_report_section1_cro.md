# ОТЧЁТ АУДИТА — РАЗДЕЛ 1: UX/UI И ВОРОНКА КОНВЕРСИИ (CRO)

**Связан с:** `twocomms_global_audit.md` (раздел 1)
**Правило безопасности:** реквизиты доступа в этом файле НЕ фиксируются.
**Дата начала:** 05.07.2026

---

## CRO-041. COD-заказ не пишет UTM (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **P0 ПОДТВЕРЖДЁН + 2 ДОП. НАХОДКИ**

### Архитектура создания заказа (реверс-инжиниринг кода)

В проекте **ДВЕ независимые реализации создания Order** — это корень проблемы:

| Поток | Вход | Файл/функция | UTM-привязка | session_key в Order |
|---|---|---|---|---|
| COD (наложка) | POST формы корзины → url `create_order` | `storefront/views/checkout.py::create_order` (строки 36–260) | **НЕТ вообще** | **НЕ записывается** |
| Онлайн-оплата | JS корзины → POST `/cart/monobank/create-invoice/` (`storefront/urls.py:440`) | `storefront/views/monobank.py::monobank_create_invoice` (строка 352; Order создаётся ~строка 568) | `link_order_to_utm(request, order)` (строка 584) + `record_initiate_checkout` + `record_lead` (строка 986) | записывается (`session_key=request.session.session_key`) |

`checkout.py::create_order` подтверждённо НЕ содержит ни одного вызова из `utm_tracking.py`:
нет `link_order_to_utm`, нет `record_order_action`, нет `record_initiate_checkout`, нет `record_lead/record_purchase`. Grep по файлу на `utm|record_|tracking|fbclid` — 0 совпадений.

### Подтверждение живой БД (замер 05.07.2026, read-only)

| Метрика | Значение |
|---|---|
| Заказов всего | 41 |
| Заказов с `utm_source` | **0** |
| Заказов с `utm_session` FK | **0** |
| Заказов с `session_key` | **6** (все — monobank-поток; поле, видимо, добавлено недавно) |
| По провайдеру | monobank_pay: 32, пусто (COD): 9 |
| По pay_type | online_full: 22, prepay_200: 19 |
| Заказов от залогиненных | 3 из 41 |

### КРИТИЧЕСКАЯ ДОП. НАХОДКА №1: link_order_to_utm не работает ДАЖЕ в monobank-потоке

32 заказа прошли через monobank-поток, где `link_order_to_utm` вызывается, — и **0 из них** получили utm_source/utm_session. Причины (по коду `utm_tracking.py::link_order_to_utm`):

1. Функция ищет строго `UTMSession.objects.get(session_key=...)`. UTMSession создаётся **ТОЛЬКО при визите с utm_* в URL** (`utm_middleware.py`: `if has_utm: _create_or_update_utm_session`). UTMSession всего 1015 при 2902 SiteSession — у большинства покупателей UTMSession просто нет под текущим session_key.
2. При логине Django `cycle_key()` меняет session_key → UTMSession остаётся привязанной к старому ключу (см. AN-031 в `audit_report_section2_analytics.md`).
3. Разрыв во времени: SESSION_COOKIE_AGE и повторный визит через другой канал создают новый session_key.
4. `session['utm_data']` при этом ЖИВА (данные сессии переживают cycle_key) — но `link_order_to_utm` её НЕ использует. Fallback на `request.session['utm_data']`/`platform_data` отсутствует.

### КРИТИЧЕСКАЯ ДОП. НАХОДКА №2: сломанный вызов в create_order (латентный TypeError)

`checkout.py::create_order`, ветка онлайн-оплаты (строка ~254):

```python
if pay_type in ['online_full', 'prepay_200']:
    return monobank_create_invoice(request, order.id)
```

Но фактическая сигнатура — `def monobank_create_invoice(request):` (monobank.py:352, второй аргумент НЕ принимает) → **TypeError при каждом попадании в эту ветку**. Исключение ловится общим `except Exception` → транзакция откатывается → пользователь получает generic-ошибку «Сталася помилка...» и остаётся в корзине. Основной UI обходит этот путь (JS корзины бьёт напрямую в `/cart/monobank/create-invoice/`), поэтому баг латентный, но: любой submit формы create_order с pay_type=online_full/prepay_200 (отключённый JS, боты, старые закладки, edge-case UI) = гарантированный сбой. Это же означает, что COD-форма и monobank-JS — два никогда не синхронизируемых кода создания заказа (дублирование логики промокодов, кастом-лидов, NP-refs).

### Задача исполнителю (конкретно)

1. В `create_order` (checkout.py) после `order.save()`:
   - записывать `order.session_key = request.session.session_key`;
   - вызывать `link_order_to_utm(request, order)`;
   - вызывать `record_initiate_checkout` (до создания) и `record_order_action('purchase'|'lead', order, request=request, cart_value=float(order.total_sum))` — семантику purchase для COD согласовать с CRO-045.
2. Расширить `link_order_to_utm` fallback-цепочкой: (а) UTMSession по session_key → (б) по `visitor_id` (`request.analytics_visitor_id`) → (в) `request.session['utm_data']` + `platform_data` напрямую в поля заказа. Без этого фикс формально «есть», а атрибуция останется ~0 (доказано monobank-потоком).
3. Починить/удалить ветку `monobank_create_invoice(request, order.id)` — либо привести сигнатуры в соответствие, либо COD-форма не должна принимать online-типы оплаты (валидация).
4. Долгосрочно: единый сервис создания заказа (общий для COD и monobank) — устранит расхождение логики. Зафиксировать как отдельную задачу TECH-060+.
5. **RISK-07:** новые поля в Order не нужны (utm_* уже есть) → миграций нет, но любые схемные правки — только после TD-020 (бэкапы).

### Acceptance-тест

Прогон CRO-050: визит с `?utm_source=audit...` → COD-заказ → в БД у заказа заполнены utm_source='audit', utm_session FK, session_key; создан UserAction c order_id.

---

## CRO-042. is_converted никогда не проставляется (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **P0 ПОДТВЕРЖДЁН**

### Факты

- БД 05.07.2026: **0 из 1015** UTMSession имеют `is_converted=True`.
- `mark_as_converted` (storefront/models.py:2012) вызывается только из 3 мест `utm_tracking.py`: `record_lead` (:188), `record_purchase` (:216), `record_order_action` (:321).
- `record_purchase` **не вызывается нигде в проекте вообще** (grep: 0 call-sites) — мёртвая функция.
- `record_lead` — только monobank.py:986; `record_order_action('purchase', ...)` — только monobank.py:1241 (`_apply_monobank_status`, при переходе payment_status → paid/prepaid из вебхука).
- При этом в UserAction есть 5 lead + 3 purchase событий — они писались, но `mark_as_converted` внутри них не сработал, т.к.:
  - в `record_lead`: конверсия ставится через ОТДЕЛЬНЫЙ lookup `UTMSession.objects.get(session_key=...)` → DoesNotExist (те же причины, что CRO-041, находка №1);
  - в `record_order_action`: `utm_session` берётся из `order.utm_session` (всегда NULL, см. CRO-041) или по session_key (см. выше) → None → ветка `mark_as_converted` пропускается.

### Вывод

CRO-042 — не самостоятельный баг, а **следствие двух разрывов**: (1) COD-поток вообще не вызывает трекинг; (2) lookup UTMSession по одному лишь session_key почти никогда не находит сессию. Фиксится автоматически при выполнении задач CRO-041 п.1–2 (fallback-цепочка поиска UTMSession).

### Acceptance-тест

После фикса CRO-041: сессия с UTM → заказ (COD и monobank) → `UTMSession.is_converted=True`, `conversion_type` заполнен, `converted_at` проставлен. Проверка по БД: `UTMSession.objects.filter(is_converted=True).count() > 0`.

---

## CRO-051. Конверсия по шагам — базовая линия UserAction (АУДИТ ВЫПОЛНЕН, 07.07.2026)

**Источник:** read-only SSH/Django shell batch, сырой вывод сохранён в `TWOCOMMS_A_TO_B/technical/data/server_audit_batch_output.txt`.

### Сырые счётчики UserAction

| Шаг | Событий |
|---|---:|
| `page_view` | 0 |
| `product_view` | 40 490 |
| `add_to_cart` | 55 |
| `initiate_checkout` | 6 |
| `lead` | 6 |
| `purchase` | 3 |

Расчёт по сырым событиям: `product_view → add_to_cart` = **0,136%**, `add_to_cart → initiate_checkout` = **10,9%**, `initiate_checkout → purchase` = **50%**, `product_view → purchase` = **0,0074%**. Эти проценты нельзя использовать как UX-истину без очистки трекинга: база сама показывает, что `product_view` сильно загрязнён/разорван.

### Уникальные site_session по шагам

| Шаг | Уникальных `site_session_id` |
|---|---:|
| `page_view` | 0 |
| `product_view` | 836 |
| `add_to_cart` | 16 |
| `initiate_checkout` | 2 |
| `lead` | 2 |
| `purchase` | 1 |

Сессионная воронка выглядит реалистичнее, но тоже неполная: большинство `product_view` вообще не привязаны к `SiteSession`.

### Качество product_view

| Метрика | Значение |
|---|---:|
| Всего `product_view` | 40 490 |
| Без `site_session` | **38 965** (96,23%) |
| Без `utm_session` | **40 431** (99,85%) |
| Без `user` | 40 490 (100%) |
| Distinct `site_session × product` | 1 067 |
| Distinct products | 65 |
| `SiteSession.is_bot=True` всего | 0 из 3 443 |
| Bot-UA среди сессий с product_view | 2 сессии / 2 product_view |

Вывод: главная причина «страшной» raw-воронки не доказана как UX-провал. 96% product_view не имеют `site_session`, а бот-флаг не работает (`is_bot=True` = 0), поэтому сначала нужно чинить слой записи/атрибуции событий (AN-035/CRO-024), и только потом сравнивать UX-конверсию.

### Динамика по 30-дневным окнам

| Окно | product_view | ATC | IC | lead | purchase |
|---|---:|---:|---:|---:|---:|
| 2026-06-07..2026-07-07 | 21 636 | 26 | 3 | 3 | 2 |
| 2026-05-08..2026-06-07 | 16 701 | 12 | 1 | 1 | 1 |
| 2026-04-08..2026-05-08 | 2 153 | 9 | 2 | 2 | 0 |
| 2026-03-09..2026-04-08 | 0 | 1 | 0 | 0 | 0 |
| 2026-02-07..2026-03-09 | 0 | 0 | 0 | 0 | 0 |
| 2026-01-08..2026-02-07 | 0 | 3 | 0 | 0 | 0 |
| 2025-12-09..2026-01-08 | 0 | 3 | 0 | 0 | 0 |
| 2025-11-09..2025-12-09 | 0 | 1 | 0 | 0 | 0 |

Наблюдение: `product_view` появился только с апреля 2026, а `add_to_cart` есть ещё в декабре/январе. Значит временной ряд UserAction неоднороден: разные события включались в разные периоды, поэтому long-term сравнение без нормализации версии трекинга некорректно.

### Топ товаров по просмотрам

Топ-10 по `product_view` полностью занят футболками:

1. `Футболка 225ОШП` — 954.
2. `Футболка «Життя Бентежне»` — 827.
3. `Футболка класична` — 767.
4. `Чорна футболка унісекс TwoComms «Довіряй своїй божевільній ідеї»` — 762.
5. `ФУТБОЛКА «мені поЖуй — це філософія»` — 754.
6. `Футболка «Печатка Хулігана»` — 753.
7. `Футболка «Череп с дупою»` — 714.
8. `Футболка «My Little Baby»` — 696.
9. `Футболка «Харків Edition»` — 684.
10. `Футболка «Де Мої Подарунки, Мразота?»` — 569.

Это усиливает уже найденный конфликт позиционирования из CRO-013: фактический просмотренный ассортимент и сортировка продолжают выпячивать футболки, хотя брендовый приоритет заявлен как лонгсливы/худи.

### Что делать исполнителю

1. Не принимать raw `40 490 → 55 → 6 → 3` как чистую UX-конверсию.
2. Сначала закрыть AN-035/CRO-024: единый bot-detect, запись `site_session`/`utm_session` для product_view, дедуп `session × product × окно`.
3. После фикса пересчитать CRO-051 тем же batch-скриптом и сравнить raw events, unique sessions, conversion by clean human sessions.
4. Для маркетинга отдельно проверить, почему AI/каталог ведут пользователей преимущественно в tshirts, а не hoodie/long-sleeve.

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | CRO-041 | **P0 подтверждён кодом+БД. +2 находки: link_order_to_utm не работает и в monobank-потоке (0/32); латентный TypeError в create_order (ветка онлайн-оплаты)** |
| 05.07.2026 | CRO-042 | **P0 подтверждён: 0/1015 конверсий; record_purchase — мёртвая функция (0 вызовов); фикс = следствие CRO-041 п.2** |
| 07.07.2026 | CRO-051 | Baseline снят с живой БД: raw 40 490 product_view → 55 ATC → 6 IC/lead → 3 purchase; 96,23% product_view без site_session, поэтому сначала чинить трекинг/бот-фильтр, потом оценивать UX |
