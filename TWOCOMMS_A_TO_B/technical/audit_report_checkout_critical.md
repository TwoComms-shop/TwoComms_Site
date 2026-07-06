# Аудит: критические находки — Checkout (оформление заказа)

Дата: 2026-07-06
Статус: подтверждено live-тестами на https://twocomms.shop

---

## Находка 1 (CRITICAL): Гостевое оформление заказа (COD) полностью сломано — 500

### Симптом (подтверждено live)
POST на `/cart/` с `form_type=guest_order` (кнопка «Оформити замовлення» для гостя, оплата наложенным платежом) возвращает **HTTP 500**. Гость физически не может оформить заказ с оплатой при получении.

### Причина (код)
- `twocomms/storefront/views/cart.py:525-527`:
  ```python
  elif form_type == 'guest_order':
      from storefront import views as legacy_views
      return legacy_views.process_guest_order(request)
  ```
- Функция `process_guest_order` **нигде не определена** в новом пакете `storefront/views/` (проверено grep по всему проекту). Она существует только в `storefront/views.py.backup` (строка ~1174), который не импортируется.
- В `storefront/views/__init__.py` в списке экспортируемых/легаси-имён (`__all__`, строки 292-483) имя `process_guest_order` отсутствует.
- Итог: `AttributeError: module 'storefront.views' has no attribute 'process_guest_order'` → 500.

### Последствия для бизнеса
- Все гостевые заказы с наложенным платежом теряются. Это, вероятно, основная причина падения конверсии/отсутствия заказов от новых покупателей (COD — самый популярный способ у гостей).
- Авторизованный флоу (`legacy_views.order_create`) работает: `order_create = create_order` определён в `views/__init__.py:374`.

### Рекомендация по фиксу (проверено)
`create_order` в `storefront/views/checkout.py` **уже поддерживает гостей**: при `not request.user.is_authenticated` берёт `full_name`/`phone` из POST (checkout.py:~86-112). Простейший и безопасный фикс — в `cart.py:525-527` заменить вызов несуществующей функции:
```python
elif form_type == 'guest_order':
    from storefront import views as legacy_views
    return legacy_views.order_create(request)  # вместо process_guest_order
```
Дополнительно проверить, что имена POST-полей гостевой формы в `pages/cart.html` совпадают с ожидаемыми в `create_order` (full_name, phone, city, np_office / Nova Poshta refs).

Проверен весь пакет `views/` на аналогичные ошибки: `process_guest_order` — **единственное** имя, вызываемое через `legacy_views.*`, но не существующее.

---

## Находка 2 (CRITICAL, безопасность/PII): `/orders/success-preview/` публично раскрывает данные последнего реального заказа

### Симптом (подтверждено live)
GET `https://twocomms.shop/orders/success-preview/` → **200** для анонимного пользователя, страница рендерит **последний реальный заказ** магазина: ФИО, телефон (+380…), адрес доставки, состав заказа.

### Причина (код)
- `twocomms/storefront/views/checkout.py:289-299`:
  ```python
  def order_success_preview(request):
      last_order = Order.objects.last()
      return render(request, 'pages/order_success.html', {'order': last_order})
  ```
- URL зарегистрирован публично: `storefront/urls.py:350` (`orders/success-preview/`), без проверки прав.
- Дополнительно URL включён в список страниц в `storefront/views/static_pages.py:126` (проверить, не попадает ли в sitemap).

### Последствия
- Утечка персональных данных клиентов (нарушение GDPR/защиты ПД). Любой может периодически опрашивать URL и собирать телефоны/адреса всех новых клиентов.

### Рекомендация по фиксу
- Минимум: обернуть в `@staff_member_required` (или удалить view + URL полностью).
- Убрать URL из `static_pages.py` / sitemap.
- Смежный риск: `order_success(request, order_id)` (checkout.py:281) отдаёт любой заказ по перебору `order_id` без проверки владельца — тот же тип утечки PII через `/orders/success/<id>/` (live: `/orders/success/1/` → 200). Требуется проверка владельца (user или session key) либо непредсказуемый токен в URL.

---

## Находка 3 (HIGH): «Змінити спосіб оплати» и «Підтвердити оплату» в кабинете молча не работают

### Подтверждено кодом
- `pages/my_orders.html:865` делает `fetch('{% url "update_payment_method" %}')`, `my_orders.html:1011` — `fetch('{% url "confirm_payment" %}')` и ожидают JSON.
- Но оба view в `storefront/views/checkout.py:305-320` — заглушки: `# Stub implementation` → `redirect('my_orders')`. Fetch получает HTML вместо JSON → JS падает/ничего не происходит.
- Рабочие реализации существуют в `views.py.backup`: `update_payment_method` (строка 2679, AJAX + JsonResponse) и `confirm_payment` (строка 3831). Потеряны при рефакторинге на пакет `views/`.

### Последствия
Клиент, выбравший COD и решивший оплатить онлайн (или наоборот), не может сменить способ оплаты; «подтверждение оплаты» не фиксируется. Тихая деградация без ошибок в UI.

### Рекомендация
Перенести обе функции из `views.py.backup` в `storefront/views/checkout.py` (с проверкой владельца заказа!) и убрать заглушки.

---

## Проверка совместимости гостевой формы с `create_order` (для Находки 1)

Поля формы в `pages/cart.html`: `full_name`, `phone`, `email`, `city`, `np_office`, `np_city_ref/np_settlement_ref/np_warehouse_ref` (+tokens), `pay_type` — полностью совпадают с тем, что читает `create_order` (`POST.get('full_name'|'phone'|'email'|'pay_type')` + Nova Poshta refs через `resolve_delivery_selection`). Фикс «направить guest_order на order_create» безопасен по полям.

---

## Смежные наблюдения (для дальнейшей проверки)

1. `views.py.backup` лежит в репозитории и содержит рабочие версии функций, потерянных при рефакторинге на пакет `views/`. Проверено: через `legacy_views.*` отсутствует только `process_guest_order`, но заглушки (`update_payment_method`, `confirm_payment`) — та же категория потерь. Стоит продиффать backup против нового пакета целиком.

---

## Дополнение CRO-040: полный guest COD-прогон и границы уже найденного бага (06.07.2026)

### Что перепроверено, чтобы не дублировать старую находку

Этот блок не заменяет «Находку 1», а уточняет её по требованиям `twocomms_global_audit.md` для `CRO-040`: минимальные поля, телефон, Нова Пошта, live-submit, состояние БД до/после и console errors.

### Новые факты по UI

1. В гостевой форме `pages/cart.html` есть `id="guest-form"`, `method="post"` и hidden `form_type=guest_order`, но внутри формы нет отдельной submit-кнопки для COD.
2. В `pay_type_guest` доступны только:
   - `online_full` — полная онлайн-оплата;
   - `prepay_200` — предоплата 200 грн, остаток при получении.
3. Значение `cod` существует в `orders/models.py::Order.PAY_TYPE_CHOICES`, но в guest UI корзины не выводится.
4. Единственная видимая кнопка оформления в��зле итогов корзины — `type="button"` Monobank Pay (`data-monobank-pay-trigger="cart"`), а JS `static/js/modules/checkout-mono.js` отправляет JSON на `/cart/monobank/create-invoice/`.
5. Вывод: даже после исправления `process_guest_order` пользователь всё равно не получит полноценный COD, пока `cod` не будет возвращён в UI и routing.

### Live-проверка Новой Почты

1. `GET /cart/delivery/cities/?q=Київ&limit=5` на бою вернул HTTP 200, `ok=true`, 5 вариантов, включая `м. Київ, Київська обл.`.
2. `GET /cart/delivery/warehouses/?settlement_ref=...&city_ref=...&q=1&kind=all&limit=5` вернул HTTP 200, `ok=true`, 5 отделений.
3. Оба endpoint отдали signed tokens (`np_city_token`, `np_warehouse_token`), после чего live-submit использовал валидные server-generated значения, а не ручные refs.
4. Серверная защита `resolve_delivery_selection` корректная: без signed city/warehouse token checkout должен вернуть controlled validation error, а не доверять plain-text `city`/`np_office`.

### Live-submit guest_order

1. Перед submit на сервере через Django shell:
   - `Order.objects.count() == 41`;
   - последний заказ `id == 274`.
2. В анонимной live-сессии товар был добавлен в корзину через `POST /cart/add/`:
   - HTTP 200;
   - `ok=true`;
   - `count=1`;
   - `total=880.0`;
   - тестовый товар `product_id=106`, размер `M`.
3. Затем выполнен `POST /cart/` с `form_type=guest_order`, тестовой пометкой в ФИО, валидным украинским телефоном, валидными NP signed tokens и `pay_type=prepay_200`.
4. Результат:
   - HTTP **500**;
   - production body `Server Error (500)`;
   - заказ не создан.
5. После submit:
   - `Order.objects.count() == 41`;
   - последний заказ всё ещё `id == 274`;
   - тестовый заказ с пометкой `AUDIT TEST DO NOT PROCESS CRO-040` отсутствует.
6. Боевой лог подтвердил root cause:
   - `Internal Server Error: /cart/`;
   - `return legacy_views.process_guest_order(request)`;
   - `AttributeError: module 'storefront.views' has no attribute 'process_guest_order'`.

### Проверка телефона

Прямой `POST /orders/create/` с той же сессией, валидными NP-token полями, но невалидным телефоном `12345`, вернул HTTP 302 обратно в `/cart/` и не создал заказ. Значит `checkout.py::create_order` умеет валидировать телефон через `normalize_checkout_phone`, но текущая guest form до этой функции не доходит из-за `process_guest_order`.

### Browser / console

1. Playwright загрузил карточку товара без console error до взаимодействия.
2. Кнопка `ДОДАТИ В КОШИК` есть в accessibility tree и кликается.
3. После AddToCart товар добавляется, но появляются 2 CSP console errors от Google Ads:
   - блокируется `https://www.google.com.ua/pagead/1p-conversion/...`;
   - текущий `connect-src` разрешает Google domains, но не покрывает региональный `www.google.com.ua`.
4. Это не ломает добавление в корзину, но нарушает требование `CRO-040` про `0 JS-ошибок в консоли на всём пути` и может ломать Google Ads conversion ping.

### Что должен сделать исполнитель

1. Принять продуктовое решение: возвращаем ли настоящий `pay_type=cod` в корзину. Если да, добавить его явно в UI и не путать с `prepay_200`.
2. Убрать вызов `legacy_views.process_guest_order` из `cart.py`; не копировать backup-функцию вслепую, а вести поток в общий сервис создания заказа.
3. Вынести общий order builder для COD и Monobank, чтобы не расходились Product/Variant/Fit/Promo/CustomPrint/NP/UTM правила.
4. Для `cod` создавать Order без Monobank, с `payment_status='unpaid'`, заполненными NP refs и последующим redirect на success page.
5. Для `online_full/prepay_200` не использовать уже найденный опасный вызов `monobank_create_invoice(request, order.id)`, потому что фактическая сигнатура принимает только `request`.
6. Добавить acceptance tests: valid guest COD, invalid phone, missing/stale NP token, online/prepay without TypeError, no duplicate order.
7. Починить CSP для Google Ads AddToCart ping (`www.google.com.ua` или настройка тега) и повторить browser console check.

### Обновлённый статус CRO-040

`CRO-040` считается проаудированным: баг из «Находки 1» подтверждён повторно live-submit, дополнительно доказано отсутствие полноценного COD в UI, валидность NP endpoints, рабочая phone validation в прямом `create_order` и отдельная CSP console-проблема после AddToCart.

---

## Дополнение CRO-044: страница «Спасибо за покупку», дедуп purchase и old template (06.07.2026)

### Что проверялось

Пункт `CRO-044` требует проверить:

1. серверное purchase-событие не дублируется при F5;
2. есть ли рекомендации/апселл;
3. `pages/order_success_old.html` является мёртвым legacy-файлом и должен быть удалён исполнителем.

JS на live thank-you page намеренно не запускался в браузере для реальных заказов, потому что страница может отправить Pixel/GTM Purchase и загрязнить аналитику. Live-проверки ниже выполнены через `curl` и read-only Django shell.

### Live-доступ и PII-риск

Анонимный `GET https://twocomms.shop/orders/success/274/` вернул HTTP 200. Страница содержит:

1. `purchase-payload`;
2. блок `Телефон`;
3. дет����л�� заказа;
4. order number;
5. клиентские analytics data attributes.

Анонимный `GET https://twocomms.shop/orders/success-preview/` также вернул HTTP 200 и содержит `purchase-payload` + телефонный блок.

Это расширяет уже описанную PII-находку: проблема не только в `/orders/success-preview/`, но и в прямом `/orders/success/<id>/`. View `order_success(request, order_id)` делает `get_object_or_404(Order, id=order_id)` и не проверяет владельца, session_key, token или staff-доступ.

### Purchase-дедуп: что работает, а что нет

Факты по коду:

1. Серверный `checkout.py::order_success()` сам не создаёт `UserAction('purchase')`; он только рендерит `pages/order_success.html`.
2. Серверный purchase в Monobank-потоке создаётся в `_apply_monobank_status()` через `record_order_action('purchase', ...)` при смене `payment_status` на `paid/prepaid`.
3. По DB-замеру дублей `UserAction` для `purchase/lead` по `order_id` не найдено.
4. Для live order 274:
   - `payment_status='paid'`;
   - `data-facebook-purchase-sent="false"`;
   - `UserAction purchase` для order 274 = 0;
   - `UserAction lead` для order 274 = 0.
5. В шаблоне есть client-side дедуп:
   - deterministic `purchaseEventId = '{{ order.get_purchase_event_id }}'`;
   - `sessionStorage['gtm_purchase_' + orderId]`;
   - server flag `data-facebook-purchase-sent`.

Вывод:

1. F5 в той же вкладке/сессии браузера должен быть защищён `sessionStorage`.
2. F5/открытие в новом браузере/анонимный внешний переход по публичному URL НЕ защищён `sessionStorage`.
3. Если `payment_payload.facebook_events.purchase_sent` false, публичная success page может повторно отправить client-side GTM/Pixel Purchase при каждом новом browser context.
4. Пока `/orders/success/<id>/` публичен, эта страница одновременно PII leak и источник потенциального analytics pollution.

### Event ID

`Order.get_purchase_event_id()` deterministic: `{order_number}_{created_timestamp}_purchase`. Это хорошо для Pixel/CAPI дедупликации, потому что event_id не меняется между reloads.

Но deterministic event_id сам по себе не блокирует повторную отправку в GA4/GTM и не защищает от повторных browser contexts; он только помогает платформе дедуплицировать при корректной настройке тегов.

### Рекомендации / апселл

На live HTML и в шаблоне есть:

1. CTA на главную;
2. CTA `Переглянути каталог`;
3. предложение создать аккаунт/войти;
4. email receipt block;
5. Instagram review block с промокодом `REVIEW10`.

Нет персональных товарных рекомендаций, cross-sell/upsell блока, «похожие товары», «добавьте к заказу», подборки по категории или повторного мини-каталога. То есть базовые CTA и review-инцентив есть, но полноценный post-purchase upsell отсутствует.

### `order_success_old.html`

Проверено:

1. Файл `twocomms_django_theme/templates/pages/order_success_old.html` отслеживается git.
2. По grep он не используется в render/template_name/include.
3. Единственные актуальные упоминания — глобальный чеклист, TD-002/CB-004 и сам legacy-файл.
4. Старый шаблон содержит собственный `purchase-payload` и клиентский tracking-код; если его случайно вернуть в routing, можно получить параллельную старую аналитику.

Вывод: `order_success_old.html` подтверждён как backup/legacy мусор. Код не удалялся, потому что текущая задача audit-only; удаление остаётся для TD-002/CB-004 исполнителю.

### Задачи исполнителю по CRO-044

1. Закрыть `/orders/success/<id>/` от чужого доступа:
   - проверять `order.user == request.user`, или
   - проверять session ownership, или
   - перейти на непредсказуемый signed token в URL.
2. Удалить или закрыть `/orders/success-preview/`; минимум `staff_member_required`.
3. Не отправлять client-side Purchase на публичной странице без server-side флага/одноразового подтверждения.
4. Синхронизировать `payment_payload.facebook_events.purchase_sent` с реальными CAPI/Pixel событиями, чтобы success page не стреляла повторно.
5. Добавить тесты:
   - anonymous чужой `/orders/success/<id>/` не отдаёт PII;
   - owner/session может видеть свой success;
   - repeated render не создаёт server-side UserAction;
   - client payload содержит stable event_id;
   - `order_success_old.html` нигде не используется.
6. Для CRO/выручки добавить post-purchase блок:
   - персональные рекомендации из категории/похожих товаров;
   - аксессуары/допродажа;
   - релевантный CTA на лонгсливы/худи, а не только общий каталог.
7. Удалить `order_success_old.html` отдельным cleanup PR/commit вместе с TD-002/CB-004 после финального grep.

### Обновлённый статус CRO-044

`CRO-044` проаудирован и отмечен в чеклисте ссылкой на этот отчет. Главный риск — публичный thank-you URL с PII и потенциальной повторной client-side отправкой purchase. Серверных дублей `UserAction purchase/lead` по order_id сейчас не найдено; old template подтверждён как неиспользуемый tracked backup.

---

## CRO-045. Определение purchase-момента: полная матрица по всем слоям (аудит 06.07.2026, code-level)

**Задача пункта:** зафиксировать документально, в какой момент каждая аналитическая система считает заказ «покупкой», и выявить расхождения между GA4/GTM, Meta Pixel/CAPI, TikTok и внутренним UserAction-слоем (TECH-066).

### Фактическая матрица purchase-моментов (каждая ячейка подтверждена чтением кода, файлы и строки указаны)

| Слой | COD (наложенный платёж) | Monobank prepay_200 | Monobank полная оплата |
|---|---|---|---|
| **UserAction (внутр.)** | **НИЧЕГО, никогда** (`checkout.py::create_order` не вызывает record_*; NP-delivery-путь UserAction не пишет) | `lead` при **создании инвойса** (monobank.py:986, payment_status='checking', ДО оплаты) + `purchase` при success (см. справа) | `lead` при создании инвойса + `purchase` через `record_order_action('purchase')` в `_apply_monobank_status` (monobank.py:1241) при смене payment_status на paid/prepaid — из webhook ИЛИ return |
| **Meta CAPI (сервер)** | `Purchase` при **получении посылки НП** (`nova_poshta_service.py:379→481`: StatusCode received → payment_status='paid' → send_purchase_event), работает ТОЛЬКО если крон `update_tracking_statuses` запущен | `Purchase` при webhook-успехе, payment_status='prepaid' (`views/utils.py:641`) | `Purchase` при webhook-успехе, payment_status='paid' (utils.py:641) |
| **TikTok Events (сервер)** | **НИЧЕГО, никогда** (в NP-delivery-пути TikTok-вызовов нет — grep=0) | `Lead` при webhook-успехе (utils.py:674); Purchase не отправляется даже при последующей доставке | `Purchase` при webhook-успехе (utils.py:694) |
| **GA4/GTM (клиент, order_success.html:1880–2170)** | **НИЧЕГО, никогда**: payment_status='unpaid' → `shouldSendPurchase=false`; в момент доставки клиент страницу не открывает | dataLayer `purchase` (полная стоимость заказа) при первом открытии success-страницы | dataLayer `purchase` при первом открытии success-страницы |

Прочие факты: единственное dataLayer-событие на success-странице — `purchase` (grep `event:` = 1 совпадение); прямых `fbq()`/`ttq.track()` в шаблоне нет — пиксели полностью за GTM.

### Ключевые находки

1. **P0 (главный вывод матрицы): COD-заказы — основной поток магазина — как «покупка» видимы только Meta CAPI, и только через НП-крон.** GA4/GTM и TikTok не видят COD-покупки вообще ни в какой момент; внутренний UserAction-слой — тоже. Отсюда системная недооценка выручки/ROAS во всех отчётах, кроме Meta (и Meta — при условии живого крона `update_tracking_statuses`; наличие в crontab сверить с CB-044-инвентаризацией).
2. **P0-уточнение к прежним итерациям аудита: в проекте ДВЕ функции записи purchase.** `record_purchase()` (utm_tracking.py:198, требует request) — мёртвая, 0 call-sites. Но `record_order_action('purchase', order)` (utm_tracking.py:261) — живая, вызывается из `_apply_monobank_status` (monobank.py:1241) при paid/prepaid. Именно она объясняет 3 purchase-записи UserAction в БД. При этом `mark_as_converted` внутри неё срабатывает только если найдена utm_session (order.utm_session FK — 0/41, lookup по session_key — заказы session_key не пишут) → is_converted остаётся 0 даже в живом пути. Полностью согласуется с CRO-041/042.
3. **P1: внутренний `lead` = создание инвойса, а не оплата.** `record_lead` (monobank.py:986) срабатывает при payment_status='checking', ДО оплаты. Брошенный неоплаченный инвойс всё равно даёт внутренний lead, тогда как Meta CAPI Lead (TikTok — тоже) уходит только после успешной предоплаты. Два слоя называют «lead» разные события — внутренняя воронка и кабинеты платформ несравнимы по определению.
4. **P1: внутренний `purchase` включает предоплату.** `record_order_action('purchase')` фиксируется и для prepaid (200 грн), и для paid, с `cart_value=order.total_sum` (полная сумма). То же в CAPI (`facebook_conversions_service.py`: value=`order.total_sum`) и в клиентском dataLayer (order_success.html:1926 — «всегда полная стоимость»). Prepaid-заказ, который потом не выкупили, навсегда остаётся во всех системах покупкой на полную сумму: refund/cancel-событий CAPI в коде нет (grep=0).
5. **P2: асимметрия дедупа TikTok Purchase.** В utils.py ветка paid читает `tiktok_events`, но НЕ проверяет `purchase_sent` перед отправкой (в отличие от FB-ветки и NP-пути, где pre-check есть); флаг пишется только после. Защита — лишь guard `previous_status != payment_status`; сценарий checking→paid→(ручной сброс в админке)→paid даст дубль TikTok Purchase.
6. **P2: Meta-дедуп между «предоплата» и «доставка» корректен.** prepaid→paid при доставке второй FB Purchase не шлёт: `facebook_events.purchase_sent` проверяется и в utils.py, и в nova_poshta_service.py:490. `event_id` детерминирован (`{order_number}_{ts}_purchase`, orders/models.py:270) и совпадает у клиента и CAPI — архитектурный задел под Pixel↔CAPI-дедуп есть (фактическая проверка в Events Manager — AN-012).
7. **P2: purchase-момент Monobank фактически «первый из return/webhook».** Из-за unsafe fallback `monobank_return` (`status_value or 'success'`, monobank.py:1317, см. CRO-043) вся purchase-цепочка (UserAction + Telegram + email-квитанция) может стартовать из браузерного редиректа без криптографического подтверждения оплаты.
8. **P3: `get_lead_event_id()` (orders/models.py:260) в success-шаблоне больше не используется** — клиент шлёт только `purchase` для paid/prepaid/partial («предоплата считается покупкой»). Метод жив только для CAPI Lead. Корневые документы `FACEBOOK_PIXEL_LEAD_VS_PURCHASE_FIX.md` описывают устаревшее поведение.

### Целевое определение для TECH-066 (рекомендация, код в рамках аудита не менялся)

- Единое определение по всем слоям: **`purchase` = подтверждённая оплата (webhook Monobank с проверенной подписью) ИЛИ факт получения посылки (COD/prepaid-остаток, NP received)**; создание заказа — отдельное событие (`place_order`/`lead`) во всех слоях одновременно, с одинаковой семантикой.
- Минимальный список фиксов из матрицы: (а) record-слой в COD `create_order`; (б) UserAction purchase в NP-delivery-пути (сейчас там только Meta); (в) TikTok Purchase в NP-delivery-путь + pre-check `purchase_sent` в paid-ветке utils.py; (г) для GA4 — server-side purchase (GTM SS / Measurement Protocol) для COD, иначе честно зафиксировать «GA4 не видит COD»; (д) value = фактически полученная сумма либо отдельный параметр `paid_value`; (е) fallback-цепочка utm_session (order FK → session_key → visitor_id) — без неё is_converted не оживёт (связка TECH-060/061).

### Перенесено в CRO-050-батч серверных проверок

- crontab: есть ли `update_tracking_statuses` и частота (без него Meta не видит НИ ОДНОЙ COD-покупки);
- логи monobank/facebook_conversions за 30 дней: реальные отправки Purchase и их источник (webhook vs return vs NP);
- БД: доля из 36 done-заказов с `payment_payload.facebook_events.purchase_sent=true`; источники 3 существующих purchase-UserAction (metadata.source).

### Обновлённый статус CRO-045

`CRO-045` проаудирован: матрица purchase-моментов зафиксирована, 4 слоя × 3 потока дают 4 разных определения покупки. Ссылка на этот отчёт проставлена в чеклисте.

---

## CRO-046. Промокоды в чекауте (аудит 06.07.2026, code-level)

**Задача пункта:** невалидный/просроченный код даёт понятную ошибку; проверить наличие события `coupon_apply` (TECH-023).

### Архитектура промокодов (проверено кодом)

- Модель: `storefront/models.py:1323` `PromoCode` (типы regular/voucher/grouped; percentage/fixed; `max_uses`/`current_uses`, `one_time_per_user`, `min_order_amount`, `valid_from/until`, `PromoCodeGroup.one_per_account`), `PromoCodeUsage` (:1511) — журнал использований per-user.
- Применение: AJAX `apply_promo_code` (`storefront/views/cart.py:1112`) → пишет в сессию `promo_code_id` + `promo_code_data`; `remove_promo_code` (:1231) чистит оба ключа.
- Потребление при заказе: два независимых пути — COD `checkout.py::create_order:224` и Monobank `monobank.py:623`.

### Находки

1. **P0: в COD-чекауте промокод НЕ применяется никогда — блок трижды мёртвый.** `checkout.py:224` читает `request.session.get('promo_code')` — этот ключ никто не пишет (apply-endpoint пишет `promo_code_id`). Даже если бы ключ совпал: `PromoCode.objects.get(code=..., active=True)` — поля `active` не существует (есть `is_active`) → FieldError, который НЕ ловится (except только DoesNotExist); `promo.is_valid()` — метода не существует (есть `is_valid_now()`/`can_be_used()`). Итог: покупатель видит скидку в корзине, выбирает наложенный платёж — и заказ создаётся на ПОЛНУЮ сумму (`discount_amount=0`, `promo_code=NULL`), полную сумму он платит при получении. Прямой обман ожиданий клиента + расхождение корзина↔заказ. Комментарий в коде «# Increment usage? (Maybe later)» подтверждает, что блок никогда не дорабатывался.
2. **P0: лимиты «one_time_per_user» и групповые «один на аккаунт» фактически не работают.** Проверки в `can_be_used_by_user` (models.py:1429-1437) опираются на `PromoCodeUsage`, но `record_usage()` (создающий эти записи) имеет **0 call-sites** во всём проекте (grep: только само определение). Monobank-путь вызывает голый `promo.use()` (только счётчик `current_uses`), COD-путь мёртв. Следствие: любой «одноразовый» или групповой промокод можно применять неограниченно (сдерживает только глобальный `max_uses`).
3. **P1: при prepay_200 с промокодом клиенту озвучивают ЗАВЫШЕННЫЙ остаток.** `order.total_sum` при промо не уменьшается (скидка живёт только в `discount_amount`), но описание платежа (monobank.py:651-652, :679-680) считает `total_sum_without_discount = order.total_sum + discount_amount` — прибавляет скидку к и так полной сумме. Остаток в описании инвойса завышен ровно на сумму скидки. `Order.get_remaining_amount()` (orders/models.py:287) также игнорирует `discount_amount`. Для online_full математика корректна (:663 `total_sum - discount_amount`).
4. **P1: `promo.use()` вызывается при создании инвойса, а не при оплате** (monobank.py:631, до редиректа на оплату). Брошенный неоплаченный инвойс сжигает лимит `max_uses`. Промокод с max_uses=50 может быть «исчерпан» без единой оплаты.
5. **P2: асимметричная очистка сессии.** Финализация (monobank.py:1203-1204) чистит `promo_code` (мёртвый ключ) и `promo_code_id`, но НЕ `promo_code_data` — UI корзины может продолжать показывать применённый промокод после заказа. COD-путь не чистит промо-ключи вовсе.
6. **P2 (TECH-023 подтверждён): события `coupon_apply` нет нигде.** Grep по templates/static на coupon_apply/select_promotion + analytics-вызовы вокруг applyPromoCode (cart.js:838) = 0. Применение промокода невидимо для GA4/Meta/TikTok/UserAction.
7. **Позитив (требование пункта выполнено на AJAX-слое):** ошибки понятны и специфичны — «Промокод не знайдено» (404), «Промокод неактивний», «недійсний за часом», «вичерпано», «Ви вже використали цей промокод», «Мінімальна сума замовлення: X грн», auth_required-ветка для гостей (403). UI cart.js показывает их через showPromoMessage.
8. **P3: промокоды доступны только авторизованным** (cart.py:1130, жёсткий 403 для гостей) — осознанное продуктовое решение, но это трение в чекауте: гость с промокодом из рекламы обязан регистрироваться. Зафиксировать как продуктовый вопрос.
9. **P3: расчёт скидки в apply и в create — от разных баз.** В apply дисконт считается от корзины с учётом сайтовых скидок (`calculate_cart_total`), в monobank-пути — от `total_sum` заказа (+кастом-принты). Проценты дадут одинаковый результат, fixed/voucher — возможное расхождение с тем, что показали в корзине, если состав изменился.

### Минимальный список фиксов (задача на реализацию, вне аудита)

(а) COD: заменить мёртвый блок на логику Monobank-пути (`promo_code_id` + `can_be_used_by_user` + `record_usage(user, order)`); (б) везде заменить `promo.use()` на `record_usage()` и вызывать его при УСПЕШНОЙ оплате (webhook), а не при создании инвойса; (в) починить математику остатка при prepay_200 (`get_remaining_amount` должен учитывать `discount_amount`); (г) чистить `promo_code_data` при финализации и в COD-пути; (д) добавить `coupon_apply` в dataLayer + UserAction (TECH-023).

### Перенесено в CRO-050-батч серверных проверок

- БД: `SELECT COUNT(*) FROM storefront_promocodeusage` (ожидание: 0 — подтвердит вывод №2) и заказы с `discount_amount > 0` по pay_type (ожидание: только Monobank-заказы).

### Обновлённый статус CRO-046

`CRO-046` проаудирован: сообщения об ошибках — ок; но найдены два P0 (COD-путь промокодов мёртв; per-user/групповые лимиты не работают из-за неиспользуемого `record_usage`) и подтверждено отсутствие `coupon_apply`. Ссылка проставлена в чеклисте.

---

## CRO-047. Ошибочные состояния чекаута и транзакционность (аудит 06.07.2026, code-level)

**Задача пункта:** пустая корзина → редирект с message; товар кончился между корзиной и заказом → понятное сообщение, а не 500; транзакционность Order+OrderItem.

### Что выполнено корректно (требования пункта)

- **Пустая корзина:** `create_order` (checkout.py:42-46) → `messages.error("Ваш кошик порожній")` + redirect('cart'). Отдельная info-ветка для «кастом ждёт модерации» (:68-73). ОК.
- **Транзакционность:** и COD-путь (checkout.py:134 `with transaction.atomic()`), и Monobank-путь (monobank.py:539) оборачивают Order+`OrderItem.objects.bulk_create` в atomic. Частичных заказов (Order без items при падении) не будет. ОК.
- **Нет сырых 500:** весь COD-путь обёрнут в `try/except Exception` → `logger.error(exc_info=True)` + понятный message + redirect('cart') (:258-261). Валидации до транзакции: обязательные поля, телефон, NP-selection, запрет prepay_200 с кастомом — все с messages+redirect. ОК.

### Находки

1. **P1: «товар исчез между корзиной и заказом» → товар МОЛЧА выбрасывается из заказа.** В обоих путях цикл по корзине делает `if not product: continue` (checkout.py:187-189, monobank.py:547-549) — без сообщения пользователю. Заказ создаётся на оставшиеся позиции; клиент узнаёт о недостаче только по сумме/составу. Требуемого «понятного сообщения» нет ни в одном пути.
2. **P1: COD-путь не имеет guard `total_sum <= 0`.** Monobank-путь его имеет (monobank.py:559: JSON-ошибка), а checkout.py — нет: если все товары корзины исчезли из БД, создаётся **заказ на 0 грн с нулём позиций**, клиент попадает на success-страницу. Комбинация с находкой 1.
3. **P1: контроля остатков не существует в принципе.** У `Product` нет поля stock/quantity (поля `quantity` в models.py принадлежат CustomPrintLead и др.), в cart/checkout нет ни одной проверки доступности при добавлении или оформлении. «Товар кончился» в смысле склада система выразить не может — только физическое удаление товара из БД. Вероятно осознанно (DTF-печать под заказ) — зафиксировать продуктовое решение письменно.
4. **P2: внешний HTTP-вызов Monobank внутри транзакции.** checkout.py:253 вызывает `monobank_create_invoice` ИЗНУТРИ `transaction.atomic()` — создание инвойса (внешний API с таймаутами) держит открытую DB-транзакцию. Под нагрузкой — исчерпание пула коннектов; при таймауте API — откат заказа, замаскированный под generic error.
5. **P2: при сбое после `clear_cart` пользователь теряет корзину БЕЗ заказа.** `clear_cart(request)` (checkout.py:240) выполняется внутри atomic ДО вызова Monobank; сессии — `cached_db` (settings.py:973), их запись происходит в middleware ПОСЛЕ ответа и НЕ участвует в откате DB-транзакции. Если `monobank_create_invoice` бросает исключение → Order откатывается, но очищенная сессия сохраняется → корзина пуста, заказа нет. Правильный порядок: commit заказа → инвойс → при успехе clear_cart.
6. **P3: `checkout.py::monobank_webhook` — стаб, возвращающий `{'status': 'ok'}` на всё** (:268-274). Если роутинг когда-либо укажет на него вместо реального обработчика из monobank.py, webhooks будут молча «проглатываться». Сверить экспорт в `views/__init__.py` и удалить стаб (кандидат в TD-раздел).
7. **P3: generic message для всех сбоев** («Сталася помилка... Спробуйте ще раз») — без error-id, который клиент мог бы назвать поддержке. Рекомендация: короткий инцидент-код в message из лог-записи.

### Минимальный список фиксов (задача на реализацию, вне аудита)

(а) вместо `continue` — собрать `missing_items`, message «Товар X більше недоступний, кошик оновлено» + redirect('cart') без создания заказа; (б) guard `total_sum <= 0` в COD-путь; (в) вынести `monobank_create_invoice`/`clear_cart` за пределы atomic; (г) удалить стаб `monobank_webhook` из checkout.py; (д) письменно зафиксировать решение об отсутствии склада.

### Обновлённый статус CRO-047

`CRO-047` проаудирован: базовые требования (пустая корзина, отсутствие 500, atomic) выполнены; найдены P1 «молчаливое выбрасывание исчезнувшего товара» + заказ на 0 грн в COD-пути + отсутствие складского контроля как класса, и P2-риски внешнего API и потери корзины внутри транзакции. Ссылка проставлена в чеклисте.
