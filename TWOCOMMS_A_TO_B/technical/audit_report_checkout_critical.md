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
4. Единственная видимая кнопка оформления возле итогов корзины — `type="button"` Monobank Pay (`data-monobank-pay-trigger="cart"`), а JS `static/js/modules/checkout-mono.js` отправляет JSON на `/cart/monobank/create-invoice/`.
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
