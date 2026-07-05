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

### Рекомендация по фиксу
Перенести функцию `process_guest_order` из `views.py.backup` в `storefront/views/checkout.py` (или `cart.py`), адаптировать импорты, добавить в `__all__` в `views/__init__.py`. Либо заменить ветку `guest_order` вызовом общей `create_order`, которая уже поддерживает гостей (проверить).

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

## Смежные наблюдения (для дальнейшей проверки)

1. `update_payment_method` и `confirm_payment` в `checkout.py` — заглушки («Stub implementation»), просто redirect на `my_orders`. Если UI их вызывает — функциональность оплаты «после заказа» не работает.
2. `views.py.backup` лежит в репозитории и содержит рабочие версии функций, потерянных при рефакторинге на пакет `views/`. Нужен полный дифф-аудит: какие ещё имена вызываются через `legacy_views.*`, но не существуют.
