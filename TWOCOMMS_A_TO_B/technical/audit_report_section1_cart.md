# Аудит: Раздел 1 — Корзина (CRO-030, CRO-031)

Дата: 06.07.2026. Ветка: `v0/ai0xqw8fkc-5459-31d9823e`.

## CRO-030. Логика мини-корзины vs full-page корзины

### Фактическая архитектура (задокументировано)

**Session-first + DB-снапшот для залогиненных:**

1. **Источник истины — сессия.** Обычная корзина: `request.session['cart']` (dict, ключ `product_id:size:color:fit`, значение `{product_id, qty, size, color, ...}`). Кастомный принт: `request.session['custom_print_cart']` (`SESSION_CUSTOM_CART_KEY` в `storefront/custom_print_config.py:10`), ключ — `lead_id`, значение `{quantity, final_total, ...}`.

2. **Синхронизация в БД** (`accounts/cart_models.py` — `UserCart` c JSON-снапшотом + ревизия sha256; `accounts/cart_sync.py`): для залогиненных весь снапшот сессионной корзины сохраняется в `UserCart` (last-write-wins), при загрузке — восстанавливается в сессию (`accounts/cart_middleware.py`).

3. **Merge при логине** (`accounts/cart_signals.py`, receiver `user_logged_in` → `cart_sync.merge_session_into_db`):
   - `_merge_standard_carts` (`cart_sync.py:63`): одинаковые ключи `product_id:size:color:fit` — **qty складываются**, дублей позиций нет. ✅ Сценарий «добавил анонимно → залогинился → корзина слилась без дублей» работает корректно.
   - `_merge_custom_carts` (`cart_sync.py:93`): уникальность по `lead_id`, при конфликте берётся большее quantity — без удвоения. ✅

4. **Мини-корзина vs full-page:**
   - Full-page: `view_cart` (`storefront/views/cart.py`, ~строка 432) → `pages/cart.html`.
   - Мини-корзина: `cart_mini` (`cart.py:1347`) — рендерит HTML-партиал; бейдж — `cart_summary` (`cart.py:1283`, JSON `{ok, count, total}`) и `get_cart_count` (`cart.py:1096`, JSON `{cart_count}`).
   - `cart.py` — ~1850 строк, кандидат на декомпозицию (подтверждаем).

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 1 | **P2 (баг)** | `user_state_hint` читает несуществующий ключ сессии `custom_cart` вместо `custom_print_cart` | `storefront/context_processors.py:57-58` | Реальный ключ — `SESSION_CUSTOM_CART_KEY = "custom_print_cart"`. Следствие: серверный hint не учитывает кастомные позиции → при корзине только из кастом-принтов бейдж на первом рендере = 0 (до AJAX-обновления). Фикс — одна строка: `request.session.get(SESSION_CUSTOM_CART_KEY)`. |
| 2 | P2 | `/cart/count/` игнорирует кастомную корзину | `cart.py:1096-1108` (`get_cart_count`) | Считает только `session['cart']` через `get_cart_from_session`, `custom_print_cart` не суммируется. `cart_summary` при этом кастом учитывает — два эндпоинта дают разный count. Любой JS, использующий `/cart/count/`, занижает бейдж. |
| 3 | P2 | GET-эндпоинт `cart_summary` мутирует сессию и сбрасывает Monobank-инвойс | `cart.py:1324-1332` | При обнаружении несуществующих товаров GET-запрос удаляет позиции из сессии и вызывает `_reset_monobank_session(drop_pending=True)`. GET с побочными эффектами: (а) нарушает идемпотентность, (б) фоновый poll бейджа может сбросить активный платёжный инвойс пользователя, (в) конфликтует с кэшированием GET. |
| 4 | P2 | Multi-device lost-update | `cart_models.py` / `cart_sync.py` | Синхронизация — whole-snapshot LWW: устройство Б, сохранившее снапшот позже, полностью перезаписывает изменения устройства А (позиции, добавленные на А, теряются). Merge выполняется ТОЛЬКО при логине, не при конкурентных сессиях. Для типового трафика редкий кейс — фиксируем как известное ограничение. |

### Проверенные сценарии — OK

- Аноним добавил → залогинился → корзина слита без дублей, qty просуммированы (стандарт) / по lead_id без удвоения (кастом). ✅
- Ревизия снапшота (sha256) предотвращает лишние записи в БД при неизменной корзине. ✅
- `cart_middleware` восстанавливает корзину из `UserCart` при новой сессии залогиненного пользователя. ✅

### Рекомендации (порядок внедрения)

1. Фикс ключа `custom_cart` → `SESSION_CUSTOM_CART_KEY` в `context_processors.py` (1 строка, P2-баг).
2. Учесть `custom_print_cart` в `get_cart_count` либо удалить эндпоинт в пользу `cart_summary`.
3. Вынести очистку несуществующих товаров из GET `cart_summary` в мутирующие эндпоинты (add/update/remove) или в `view_cart`; не трогать monobank-инвойс из summary.
4. Декомпозиция `cart.py` (~1850 строк) — отдельной задачей (связано с TD-задачами раздела 3).

---

## CRO-031. Баги добавления/удаления (AJAX-эндпоинты + JS)

Проверено: `cart.py` — `add_to_cart` (775), `update_cart` (873), `remove_from_cart` (950), `clear_cart` (1070); JS — `main.js` (обработчик `[data-add-to-cart]`, 1669+), `pages/cart.html` (степпер qty, inline-скрипт), `ui-fallback.js` (`CartRemoveKey`, 26+), `modules/cart.js`.

### Проверка сценариев чек-листа

| Сценарий | Статус | Детали |
|----------|--------|--------|
| Двойной клик «добавить» не создаёт 2 позиции | ⚠️ Частично | Сервер мержит по ключу `product_id:size:color:fit` → 2 позиции НЕ создаются. НО: у обработчика в `main.js` **нет in-flight guard и disable кнопки** → двойной клик = 2 POST → qty=2 вместо 1 + двойные события аналитики (`record_add_to_cart` ×2, пиксели ×2). |
| Удаление последней позиции обнуляет счётчик | ✅/⚠️ | `remove_from_cart` возвращает `count`, `CartRemoveKey` вызывает `updateCartBadge(d.count)` → бейдж = 0. НО `count` не учитывает `custom_print_cart` (см. CRO-030 #2): при оставшемся кастоме бейдж ошибочно покажет 0. |
| Смена количества пересчитывает сумму и промокод | ✅ | `update_cart` пересчитывает `line_total`, `subtotal`, `discount` (через `PromoCode.calculate_discount`), `total`. Степпер в cart.html: in-flight guard есть, оптимистичное обновление с откатом, MAX_QTY=99, после ответа — `cartUpdated` + `refreshCartSummary()`. |

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 5 | **P1** | `update_cart` и `remove_from_cart` НЕ сбрасывают Monobank-инвойс | `cart.py:873-947, 950-1065` | `add_to_cart` (818) и `clear_cart` (1081) вызывают `_reset_monobank_session(drop_pending=True)`, а update/remove — нет. Пользователь создал инвойс → изменил qty / удалил товар → pending-инвойс со СТАРОЙ суммой остаётся оплачиваемым. Смягчение: JS после update/remove зовёт `refreshCartSummary()` → GET `cart_summary` сбрасывает инвойс при изменении суммы (1329), но это клиент-зависимо: сбой JS/сети = оплата неактуальной суммы. Фикс: перенести reset на сервер в оба эндпоинта. |
| 6 | **P2** | Нет защиты от двойного клика «добавить» | `main.js:1669-1760` | Ни `inFlight`-флага, ни `btn.disabled` на время запроса (в отличие от степпера qty в cart.html, где guard есть). Следствия: qty удваивается, дублируются серверные UserAction и пиксельные AddToCart (искажение аналитики/оптимизации кампаний). `CartRemoveKey` (ui-fallback.js:17) — тоже без guard (менее критично: повторный remove идемпотентен). |
| 7 | **P2** | Fallback в `remove_from_cart` удаляет ВСЕ варианты товара | `cart.py:981-985` | Если exact key не найден и в ключе есть `:`, удаляются все позиции с тем же `product_id` (другие размеры/цвета). Рассинхрон ключа (legacy-формат, регистр) → пользователь удаляет одну позицию, исчезают все варианты товара. Fallback должен возвращать 404/`ok:false`, а не «жадно» удалять. |
| 8 | P3 | `update_cart` не ограничивает qty стоком | `cart.py:887-904` | Клиент ограничивает 99, но прямой POST принимает любое qty ≥ 1; проверки наличия нет (согласуется с CRO-025: сток вообще не блокируется). |
| 9 | P3 | Мёртвый/противоречивый код | `cart.py:813-814, 1067` | `if qty <= 0:` недостижим (qty форсируется ≥1 на 788); `return redirect('cart')` после `return JsonResponse` (1067) недостижим. Ошибки `update_cart` используют ключ `success: False`, успех — `ok: True` — клиенты вынуждены проверять оба. |

### Рекомендации (порядок внедрения)

5. Добавить `_reset_monobank_session(request, drop_pending=True)` в `update_cart` и `remove_from_cart` (P1, 2 строки).
6. In-flight guard + `btn.disabled` в обработчике `[data-add-to-cart]` в `main.js` (по образцу степпера qty).
7. Убрать «жадный» fallback-делит по `product_id` в `remove_from_cart`; при ненайденном ключе возвращать `ok: false`.
8. Почистить мёртвый код и унифицировать контракт ошибок (`ok` везде) — вместе с декомпозицией `cart.py`.
