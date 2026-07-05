# Аудит: Раздел 1 — Корзина (CRO-030)

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
