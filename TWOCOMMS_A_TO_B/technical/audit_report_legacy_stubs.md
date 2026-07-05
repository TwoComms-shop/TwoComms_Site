# Аудит: потерянная функциональность после рефакторинга views.py → пакет views/

Дата: 2026-07-06. Все находки проверены по коду И (где возможно) live-запросами к https://twocomms.shop.

## Контекст / механика проблемы

При рефакторинге монолита `storefront/views.py` (сохранён как `views.py.backup`, ~4000+ строк) на пакет `storefront/views/` часть функций была заменена однострочными заглушками в `storefront/views/legacy_stubs.py` (48 штук). Для части маршрутов существует «ленивый» загрузчик реальных функций из backup:

- `storefront/urls.py:11` — `_legacy_view(name)`: при запросе вызывает `views._load_legacy_views(force=True)` и берёт атрибут по имени.
- `storefront/views/__init__.py:292` — `_LEGACY_VIEW_NAMES`: **белый список** имён, которые загружаются из `views.py.backup` и перезаписывают заглушки в `globals()`.

Итого три категории маршрутов:
1. **Работают**: через `_legacy_view('X')` И `X` есть в `_LEGACY_VIEW_NAMES` → реальная функция из backup (например, wholesale-маршруты, admin_product_edit).
2. **СЛОМАНЫ (тип А)**: через `_legacy_view('X')`, но `X` НЕТ в `_LEGACY_VIEW_NAMES` → срабатывает заглушка из package-level импорта.
3. **СЛОМАНЫ (тип Б)**: привязаны напрямую `views.X` в urls.py, где `X` — заглушка из `legacy_stubs.py`. Прямая привязка фиксирует объект-заглушку при импорте urls.py, и даже последующая загрузка legacy не помогает.

---

## Находка 4 (CRITICAL): экспресс-оплата Monobank сломана — заглушка вместо создания инвойса

- Маршрут: `storefront/urls.py:441` — `path('cart/monobank/quick/', _legacy_view('monobank_create_checkout'), name='monobank_quick_invoice')` — тип А.
- `monobank_create_checkout` ОТСУТСТВУЕТ в `_LEGACY_VIEW_NAMES` → берётся заглушка `legacy_stubs.py:221`: `return redirect('cart')`.
- Реальная реализация существует в `views.py.backup` (не загружается) и/или должна жить в `views/monobank.py` (там её нет — только `monobank_create_invoice`, `monobank_return`, `monobank_webhook`).
- Фронтенд активно вызывает этот URL: `static/js/modules/checkout-mono.js:220` и `:272` — `fetch('/cart/monobank/quick/', ...)` и ожидает JSON.
- **Live-подтверждение**: `GET https://twocomms.shop/cart/monobank/quick/` → `302 Location: https://twocomms.shop/cart/`. JS получает HTML корзины вместо JSON → оплата не стартует.
- Примечание: основная кнопка «Оплатити онлайн» использует `monobank_create_invoice` (реальный, `views/monobank.py:352`) — этот путь жив. Сломан именно quick/express-поток из `checkout-mono.js`.

### Фикс
Либо добавить `'monobank_create_checkout'` в `_LEGACY_VIEW_NAMES` (быстро), либо перенести реализацию из backup в `views/monobank.py` и импортировать её в `__init__.py` вместо заглушки (правильно). Убрать имя из импорта `legacy_stubs`.

---

## Находка 5 (HIGH): управление офлайн-магазинами в админке — полностью заглушки (тип Б)

Привязаны напрямую `views.X` в urls.py → заглушка навсегда:
- `urls.py:414` `admin_offline_stores` → рендерит `admin/stub.html`
- `urls.py:420` `admin_store_management` → `admin/stub.html`
- Плюс 13 связанных AJAX-заглушек (`admin_store_add_product_to_order`, `admin_store_get_order_items`, `admin_store_mark_product_sold`, ...) возвращают пустые/фиктивные JSON `{'status':'ok'}` — **тихо имитируют успех**, ничего не делая.
- Реальные реализации всех этих функций есть в `views.py.backup`.

## Находка 6 (HIGH): страница «Додати принт» (add-print) и «Співпраця» частично мертвы (тип Б)

- `urls.py:453` `add_print` → заглушка `render('pages/stub.html')` (реальная в backup). Публичная страница `/add-print/`.
- `urls.py:498` `cooperation` → заглушка в `legacy_stubs.py:152` (упрощённая, рендерит шаблон; реальная в backup — с обработкой форм).
- `urls.py:431` `admin_print_proposal_update_status` + award_points/award_promocode → фиктивные `{'status':'ok'}`: админ «одобряет» заявки на принты, но ничего не происходит.

## Находка 7 (MEDIUM): dropship-админка — фиктивные ответы

`admin_update_dropship_status`, `admin_get_dropship_order`, `admin_update_dropship_order`, `admin_delete_dropship_order` — заглушки (тип зависит от привязки в urls.py), реальные в backup.

## Находка 8 (архитектурный риск, HIGH): продакшен зависит от файла `views.py.backup`

Оптовый (wholesale) поток — инвойсы, оплата, вебхук оплаты (`urls.py:570-584`) — работает ТОЛЬКО потому, что `_load_legacy_views` в рантайме исполняет `views.py.backup` через `SourceFileLoader`. Если кто-то «почистит» backup-файл — молча отвалится весь опт, включая платёжный вебхук. Файл с расширением `.backup` не выглядит как боевой код — крайне высокая вероятность случайного удаления.

### Фикс
Перенести wholesale-функции из backup в нормальный модуль `views/wholesale.py`, после чего backup удалить.

---

## Сводная таблица

| # | Что сломано | Тип | Серьёзность | Live-проверка |
|---|---|---|---|---|
| 1 | Гостевой COD-заказ (500) | отсутствует `process_guest_order` | CRITICAL | 500 на POST form_type=guest_order |
| 2 | PII-утечка success-preview | публичный превью последнего заказа | CRITICAL | телефон клиента виден |
| 3 | Смена способа оплаты / подтверждение оплаты в кабинете | заглушки `update_payment_method`, `confirm_payment` | HIGH | код |
| 4 | Monobank quick-оплата | заглушка вместо инвойса | CRITICAL | 302→/cart/ |
| 5 | Офлайн-магазины (админ) | заглушки тип Б | HIGH | код |
| 6 | add-print / cooperation / print-proposals | заглушки тип Б | HIGH | код |
| 7 | Dropship-админка | фиктивные JSON | MEDIUM | код |
| 8 | Опт работает через .backup-файл | архитектурный риск | HIGH | код |

Находки 1-3 детально: `audit_report_checkout_critical.md`.
