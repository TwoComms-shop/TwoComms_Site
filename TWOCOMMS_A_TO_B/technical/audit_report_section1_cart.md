# Аудит: Раздел 1 — Корзина (CRO-030, CRO-031, CRO-032, CRO-033)

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
| 6 | **P2** | Нет защиты от двойного клика «добавить» | `main.js:1669-1760` | Ни `inFlight`-флага, н�� `btn.disabled` на время запроса (в отличие от степпера qty в cart.html, где guard есть). Следствия: qty удваивается, дублируются серверные UserAction и пиксельные AddToCart (искажение аналитики/оптимизации кампаний). `CartRemoveKey` (ui-fallback.js:17) — тоже без guard (менее критично: повторный remove идемпотентен). |
| 7 | **P2** | Fallback в `remove_from_cart` удаляет ВСЕ варианты товара | `cart.py:981-985` | Если exact key не найден и в ключе есть `:`, удаляются все позиции с тем же `product_id` (другие размеры/цвета). Рассинхрон ключа (legacy-формат, регистр) → пользователь удаляет одну позицию, исчезают все варианты товара. Fallback должен возвращать 404/`ok:false`, а не «жадно» удалять. |
| 8 | P3 | `update_cart` не ограничивает qty стоком | `cart.py:887-904` | Клиент ограничивает 99, но прямой POST принимает любое qty ≥ 1; проверки наличия нет (согласуется с CRO-025: сток вообще не блокируется). |
| 9 | P3 | Мёртвый/противоречивый код | `cart.py:813-814, 1067` | `if qty <= 0:` недостижим (qty форсируется ≥1 на 788); `return redirect('cart')` после `return JsonResponse` (1067) недостижим. Ошибки `update_cart` используют ключ `success: False`, успех — `ok: True` — клиенты вынуждены проверять оба. |

### Рекомендации (порядок внедрения)

5. Добавить `_reset_monobank_session(request, drop_pending=True)` в `update_cart` и `remove_from_cart` (P1, 2 строки).
6. In-flight guard + `btn.disabled` в обработчике `[data-add-to-cart]` в `main.js` (по образцу степпера qty).
7. Убрать «жадный» fallback-делит по `product_id` в `remove_from_cart`; при ненайденном ключе возвращать `ok: false`.
8. Почистить мёртвый код и унифицировать контракт ошибок (`ok` везде) — вместе с декомпозицией `cart.py`.

---

## CRO-032. Кэширование не ломает мини-корзину

Дата аудита: 06.07.2026. Проверено: `twocomms/cache_headers.py`, `twocomms/media_cache_middleware.py`, `cache_utils.py`, `storefront/cache_signals.py`, `twocomms/cache_views.py`, `storefront/views/utils.py` (`cache_page_for_anon`), `storefront/views/catalog.py`, `storefront/views/cart.py`, `base.html`, `partials/header.html`, `partials/language_switcher.html`, `static/js/main.js`, `static/js/modules/cart.js`, `static/js/ui-fallback.js`, `twocomms/settings.py`; **живые HTTP-тесты на https://twocomms.shop (curl, 2 независимые cookie-сессии)**.

### Фактическая архитектура page-cache (задокументировано)

1. **Единственный page-cache — самописный декоратор `cache_page_for_anon`** (`storefront/views/utils.py:45`). Django `UpdateCache/FetchFromCacheMiddleware` НЕ используются (в MIDDLEWARE их нет — проверено `settings.py:199-226`). Декоратор:
   - кэширует **только** `GET/HEAD` и **только** для `request.user.is_authenticated == False`; авторизованные всегда получают свежий рендер;
   - ключ = sha256 от `scheme://host + path + ?query | LANGUAGE_CODE | Accept-Language` + версионный префикс (`public_product_listing_cache_prefix` — версии product-order/category, инвалидируются сигналами из `storefront/cache_signals.py`);
   - хранит **целиком объект HttpResponse** в default-Redis; TTL: главная (`catalog.py:333`), каталог (`catalog.py:551`, `catalog.py:1110`) — 600s;
   - на cache-hit вызывает `get_token(request)` → `CsrfViewMiddleware` выставляет посетителю СВОЮ `csrftoken`-cookie (подтверждено live: `Set-Cookie: csrftoken=...` на закэшированной главной).
2. **PDP не кэшируется** (`product.py:201` — декоратор закомментирован ради `?size/?color`).
3. **Бейдж корзины cache-safe by design:** в `partials/header.html:59,198` SSR-значение всегда `0`; реальное число подтягивает `refreshCartSummary()` (`main.js:635`) → `GET /cart/summary/` c `cache: 'no-store'`. Никакой HTML корзины в кэшируемые страницы не попадает.
4. **SSR-хинт синхронизации НЕ используется** — вместо него **localStorage-хинт** (`base.html:428-431`: `window.__TC_SYNC_CART = isAuthenticated || localStorage['twc-sync-cart']==='1'`), выставляется клиентом в `updateCartBadge → setCartSyncEnabled` (`main.js:588-628`). Комментарий в base.html прямо фиксирует замысел: «avoid binding cached anonymous HTML to per-session state». Дизайн корректный.
5. **Страница корзины и её API:** `view_cart` (`cart.py:432`) и `cart_items_api` (`cart.py:1557`) — `@never_cache`. Live-подтверждение: `GET /cart/` → `Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private`. ✅
6. `media_cache_middleware.py`/`cache_headers.py` касаются только static/media (whitenoise-колбэк + `MEDIA_URL`-префикс) — на HTML с корзиной не влияют. `cache_signals.py` — только инвалидация каталожных версий/категорий, корректно через `transaction.on_commit`.

### Проверка сценария чек-листа

| Сценарий | Статус | Детали |
|----------|--------|--------|
| Страница с корзиной/счётчиком не отдаётся из page-cache другому пользователю | ✅ (HTML корзины) / ❌ (CSRF-токен — см. баг №10) | Кэш только для анонимов; бейдж SSR=0 + AJAX. НО в закэшированном HTML остаётся **чужой `csrfmiddlewaretoken`** (формы language switcher в футере каждой страницы). |
| Cache-Control на HTML с динамикой = private/no-cache | ⚠️ Частично | `/cart/` — ✅ no-store/private. Главная/каталог — **заголовка Cache-Control нет ��ообще** (live-замер), есть только `Vary: Cookie, Accept-Language, Accept-Encoding`. |
| Счётчик корзины подтягивается AJAX-ом после загрузки кэшированной страницы | ✅ (с оговорками) | `refreshCartSummary()` на DOMContentLoaded (`main.js:948-951`), пропускается только если localStorage-хинт = false (см. №13). |

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 10 | **P1 (подтверждён live)** | **Кэшированные страницы отдают чужой CSRF-токен → language switcher даёт 403 всем анонимам на cache-hit** | `utils.py:45-97` + `partials/language_switcher.html:12` (включён в `base.html:1138` на каждой странице) | Live-тест 06.07.2026: два curl с чистыми cookie-jar получили **идентичный** `csrfmiddlewaretoken` (`TZFS5Joo…`) при **разных** `csrftoken`-cookie; POST `/i18n/setlang/` с токеном страницы + своей cookie → **403**. Механика: HTML кэшируется с маскированным токеном сессии А; посетителю B `get_token()` ставит cookie с секретом B; unmask(токен A) ≠ секрет B → отказ CSRF. Инлайн-скрипт свитчера делает `e.preventDefault(); form.submit()` → анон, кликнувший смену языка uk/ru/en на закэшированной главной/каталоге (TTL 600s), получает **страницу 403** вместо смены языка. Затронуты ВСЕ inline `{% csrf_token %}`-формы на кэшируемых страницах (сейчас это только language_switcher; survey/корзина ходят через meta/cookie — безопасно, см. «Смягчения»). Родной Django `cache_page` от этого защищается (не кэширует ответы, где использован CSRF-токен / патчит Vary) — самописный декоратор эту логику не воспроизвёл. Варианты фикса: (а) в инлайн-скрипте свитчера перед submit переписывать hidden-input значением из `csrftoken`-cookie (3 строки, самый дешёвый); (б) сменить POST `/i18n/setlang/` на переход по `href` (`item.url` уже есть в разметке, языковые URL работают); (в) в `cache_page_for_anon` не кэшировать ответ, если `request.META.get('CSRF_COOKIE_USED')`. |
| 11 | **P2** | `/cart/summary/` и `/cart/count/` — персональные JSON без `Cache-Control: no-store` | `cart.py:1283` (`cart_summary` — без `@never_cache`), `cart.py:1096` (`get_cart_count` — без декораторов) | Live-замер `/cart/count/`: в ответе НЕТ Cache-Control (только `Vary: Accept-Language, Cookie`). `main.js` ходит с `cache:'no-store'`, но это только один из потребителей: `modules/cart.js` (`summaryEndpoint`), bfcache/прокси/LiteSpeed (сервер = LiteSpeed, при включении LSCache ответ без Cache-Control — кандидат на кэширование) не защищены. Контраст: `cart_items_api` (`cart.py:1557`) `@never_cache` имеет. Фикс: `@never_cache` на оба эндпоинта. Усугубляется тем, что GET `cart_summary` ещё и мутирует сессию (баг №3 CRO-030) — кэшируемый GET с side-effect. |
| 12 | **P3 (коррекция CRO-030 №1)** | `user_state_hint` — мёртвый код: НЕ зарегистрирован в context_processors | `storefront/context_processors.py:40-74`; `settings.py:236-249` | В `TEMPLATES.context_processors` его нет (проверен полный список), `sync_cart_badge`/`sync_favorites_badge` не встречаются ни в одном шаблоне (grep по templates = 0). Функцию вытеснил localStorage-хинт из base.html. Следствие: баг №1 из CRO-030 (`custom_cart` vs `custom_print_cart`) **не имеет продакшн-эффекта** — но код надо удалить, чтобы исполнители не «чинили» мёртвую ветку. |
| 13 | P3 | Анон с непустой корзиной, но без localStorage-хинта → бейдж навсегда 0 до нового add-to-cart | `base.html:428-431`, `main.js:948-951` | Хинт живёт в localStorage. Кейсы потери: приватный режим с блокировкой localStorage, очистка site data при живой session-cookie, восстановление сессии на другом профиле браузера. Тогда `__TC_SYNC_CART=false` → `refreshCartSummary()` пропускается → бейдж 0 при непустой сессионной корзине. Дёшевый фикс: при `readSyncHint()===false` всё равно делать один отложенный (idle) fetch summary. |
| 14 | P3 | Главная/каталог отдаются вообще без Cache-Control | `cache_page_for_anon` не выставляет заголовки; live-замер главной | Нет ни `no-cache`, ни `max-age` → браузерная эвристика (10% от Last-Modified) и промежуточные кэши решают сами; `Vary: Cookie` смягчает, но `Set-Cookie` в ответе — единственная фактическая защита. Рекомендация: явно `Cache-Control: private, no-cache` (браузеру) при сохранении серверного Redis-кэша, либо честный `public, s-maxage` только если убран CSRF из HTML (№10). |
| 15 | P3 | `cache_views.py` — опасные неиспользуемые декораторы | `twocomms/cache_views.py` | `cache_api_response`/`cache_dynamic_content` ставят `Cache-Control: public` на ЛЮБОЙ ответ view без проверки авторизации + `ETag = hash(content)` (нестабилен между процессами Python из-за PYTHONHASHSEED). Сейчас нигде не импортируются (grep = 0 использований) — но это заряженное ружьё для исполнителей. Рекомендация: удалить файл. |

### Смягчения, которые уже работают (важно НЕ сломать при фиксах)

- `base.html:19-45`: `<meta name="csrf-token" content="">` рендерится ПУСТЫМ и заполняется на клиенте из `csrftoken`-cookie → все AJAX-потоки (`modules/cart.js:30-33` meta→input→cookie; `main.js:1705` cookie; `ui-fallback.js:43-46` cookie первым) фактически используют СВОЙ токен, а не закэшированный. Именно поэтому add-to-cart/remove с кэшированных страниц работают, а страдает только «настоящая» POST-форма свитчера языков.
- `base.html:32-37`: self-heal двойной `csrftoken`-cookie (host-only vs `.twocomms.shop`).
- Cache-hit путь декоратора принудительно ставит CSRF-cookie через `get_token()` — без этого AJAX с кэшированных страниц падал бы 403 (задокументировано в docstring `utils.py:52-56`).

### Рекомендации (порядок внедрения)

9. **P1:** фикс language switcher — вариант (а) переписывать hidden-token из cookie перед `form.submit()` или (б) переход по href вместо POST; плюс в `cache_page_for_anon` не кэшировать ответы с `CSRF_COOKIE_USED` (страховка от будущих inline-форм).
10. **P2:** `@never_cache` на `cart_summary` и `get_cart_count` (2 строки; вместе с фиксом «GET мутирует сессию» из CRO-030 №3).
11. P3: удалить мёртвые `user_state_hint` (context_processors.py) и `twocomms/cache_views.py`.
12. P3: явный `Cache-Control: private, no-cache` для HTML главной/каталога; fallback-fetch summary при отсутствии localStorage-хинта.

---

## CRO-033. Событие add_to_cart — сервер + пиксели

Дата аудита: 06.07.2026. Проверено: `storefront/views/cart.py::add_to_cart` (841), `storefront/utm_tracking.py` (`record_add_to_cart` → `record_user_action`), `static/js/main.js` (delegated-handler 1669-1758, `trackAddToCartAnalytics` 1535-1667, `pushAddToCartEvent` 356-466), `static/js/ui-fallback.js` (window.AddToCart 339-423), `static/js/analytics-loader.js` (`trackEvent` 169-430: fbq/ttq/gtag/ym), `static/js/custom-print-configurator.js` (3288), `storefront/views/static_pages.py::custom_print_add_to_cart` (1558), grep по templates на inline `onclick="AddToCart"`.

### Фактическая цепочка одного клика (задокументировано)

```
Клик [data-add-to-cart]
  └─ main.js:1669 delegated click (пропускает кнопки с onclick — их в templates НЕТ, grep=0,
     поэтому ui-fallback.js::AddToCart — мёртвая ветка, дублирования fetch нет)
       └─ ровно 1 × POST /cart/add/
            └─ сервер: ровно 1 × record_add_to_cart → 1 × UserAction(add_to_cart)
               (analytics_exclusions фильтрует staff/админов; если session_key нет — создаёт)
       └─ на d.ok: ровно 1 × trackAddToCartAnalytics(d, btn, qty)
            ├─ trackEvent('AddToCart', payload+event_id)  [analytics-loader]
            │    ├─ fbq('track','AddToCart', …, {eventID}) — 1 событие Meta Pixel
            │    ├─ ttq.track('AddToCart', …, event_id)   — 1 событие TikTok
            │    ├─ gtag('event','AddToCart') ЛИБО dataLayer.push({event:'AddToCart'})  ← см. баг №16
            │    └─ ym reachGoal (если подключена)
            └─ pushAddToCartEvent({eventId,…}) → dataLayer.push({event:'add_to_cart', ecommerce})  ← GA4-ecommerce
```

Соответствие чек-листу «одно добавление = ровно одно серверное UserAction + одно событие в каждый пиксель»: **Meta ✅, TikTok ✅, сервер ✅ (на уровне кода), GA4 ❌ (двойная запись в dataLayer, №16)**. Одинаковый `event_id` прокидывается во все каналы (fbq eventID / ttq event_id / dataLayer event_id) — задел под дедуп корректный.

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 16 | **P2** | Один клик кладёт в dataLayer ДВА события: `AddToCart` и `add_to_cart` | `analytics-loader.js:344-353` (ветка gtag/dataLayer внутри `trackEvent`) + `main.js:1641-1655` (`pushAddToCartEvent`) | `trackEvent('AddToCart')` при отсутствии gtag пушит `{event:'AddToCart', eventParameters}` в dataLayer, а при наличии gtag шлёт `gtag('event','AddToCart')` — нестандартное имя попадает в GA4 как custom event. Следом `pushAddToCartEvent` пушит канонический GA4-ecommerce `add_to_cart`. Если в GTM есть триггеры на оба имени (проверить контейнер!) — двойной счёт ATC в GA4; как минимум — мусорный custom event `AddToCart` в отчётах. `event_id` у обоих одинаковый, но GA4 по event_id НЕ дедуплицирует. Фикс: в `trackEvent` исключить GA/GTM-ветку для событий, у которых есть отдельный ecommerce-пуш (AddToCart/InitiateCheckout/Purchase…), либо маппить имя в каноническое и убирать `pushAddToCartEvent`. |
| 17 | **P2** | Server-side дедуп-пары нет: `event_id` генерится, но Meta CAPI/TikTok Events API с сервера не отправляются вообще | grep по `*.py`: 0 вызовов graph.facebook / conversions — весь трекинг только браузерный | При блокировщиках (uBlock режет и fbq, и ttq, и GTM) добавление фиксируется ТОЛЬКО в UserAction — пиксели слепнут. Это прямой кандидат в объяснение аномалии воронки 36009 views → 44 ATC (CRO-051): реальные ATC теряются на клиенте. Инфраструктура под дедуп уже готова (`event_id` в payload). Рекомендация: серверный CAPI-вызов из `add_to_cart` view с тем же event_id (передавать его с клиента или генерить на сервере и возвращать в JSON). |
| 18 | **P2 (связка с CRO-031 №5)** | Двойной клик = 2 UserAction + 2 события в каждый пиксель с РАЗНЫМИ event_id | `main.js:1669` (нет in-flight guard) + `cart.py:841` (нет серверного дедупа) | Каждый POST честно пишет UserAction и триггерит полный каскад пикселей; event_id разные → ни Meta, ни TikTok не склеят. Фикс двойного клика из CRO-031 закрывает и это. |
| 19 | **P3** | Кастом-принт: пиксели получают AddToCart, а серверная воронка — нет | `custom-print-configurator.js:3288` (`trackEvent('AddToCart', cartPayload)`) vs `static_pages.py::custom_print_add_to_cart` (только `record_custom_print_event`, `record_add_to_cart` НЕ вызывается) | Расхождение сервер/пиксели: в UserAction-воронке кастомные добавления невидимы как add_to_cart → занижает серверный ATC и ломает сверку пиксели↔БД (CRO-050). Решить осознанно: либо писать add_to_cart с metadata `{custom_print: true}`, либо задокументировать исключение. |
| 20 | P3 | TikTok: события до готовности пикселя уходят в очередь-заглушку без гарантии доставки | `analytics-loader.js:374-378` (`isTikTokReady` требует `_ttqLoaded && _ttqScriptLoaded`) | Если скрипт TikTok ещё грузится, ttq — стаб-очередь; отдельные ветки кладут событие в буфер, но ранний ATC (быстрый к��ик на PDP из кэша) может уйти до `ttq.page()` → недоучёт. Проверяется только вживую через TikTok Test Events. |

### Что требует живой проверки (вне sandbox — нужен доступ к кабинетам)

- **Meta Test Events:** один клик по «В кошик» на PDP → должен прийти ровно 1 браузерный AddToCart с event_id; проверить, что событий-дублей от GTM-контейнера нет.
- **TikTok Test Events:** то же + сценарий «клик в первые 2 секунды после загрузки страницы» (баг №20).
- **GTM-контейнер:** есть ли триггеры одновременно на `AddToCart` и `add_to_cart` (баг №16 — решает, двойной это счёт или просто мусор).

### Рекомендации (порядок внедрения)

13. **P2:** убрать GA/GTM-ветку из `trackEvent` для ecommerce-событий с отдельным dataLayer-пушем (№16) — 1 условие в analytics-loader.js.
14. **P2:** серверный Meta CAPI (и опционально TikTok Events API) для add_to_cart с тем же event_id (№17) — вместе с purchase/lead из CRO-043/045, чтобы строить один модуль.
15. **P2:** in-flight guard на `[data-add-to-cart]` (уже рекомендация №4 CRO-031) закрывает №18.
16. P3: `record_add_to_cart(metadata={'custom_print': True})` в `custom_print_add_to_cart` (№19).

---

## CRO-034. Кастом-принт позиции в корзине

Дата аудита: 06.07.2026. Проверено: `storefront/custom_print_config.py` (SESSION_CUSTOM_CART_KEY), `storefront/views/cart.py` (`_collect_custom_cart_state` 350-429, `_build_custom_cart_entry_payload` 196-347, `_promote_legacy_custom_draft` 174-193, `view_cart` 688-772, `cart_summary` 1283-1344, `cart_items_api` 1680-1725), `storefront/views/checkout.py::create_order` (41-247), `storefront/views/monobank.py` (`_split_custom_cart_entries` 95-133, invoice-flow 390-660, `_cleanup_after_success` 1187-1208, `_cleanup_expired_monobank_orders` 136-149), `storefront/views/static_pages.py` (`custom_print_remove` 1612-1642, `custom_print_submit_review` 1665+), `storefront/models.py` (`CustomPrintModerationStatus` 623-627, `CustomPrintLead.final_price_value` 802-805), `pages/cart.html` (88-247, 507-515, 690-698, 788-800, 882-954), `static/js/modules/cart.js` (599-630, 375-430), `static/js/ui-fallback.js` (`CustomCartRemoveKey` 219+).

### Ответ на вопрос чек-листа

**Pending-кастом НЕ блокирует оформление обычных товаров — подтверждено кодом на обоих потоках (COD + Monobank).** UI корзины ясно объясняет, почему кастом «ждёт». Ядро сценария работает корректно, но найдено 8 проблем (2×P2 в деньгах/данных, остальные P3).

### Фактическая архитектура (задокументировано)

**Хранение.** Кастом-позиции живут в `request.session['custom_print_cart']` (`SESSION_CUSTOM_CART_KEY`, `custom_print_config.py:10`), ключ записи — `custom:<lead_id>`. Каждая запись ссылается на `CustomPrintLead` (`storefront/models.py:630`) с полем `moderation_status` (`models.py:623-627`): `draft → awaiting_review → approved | rejected`. Цена берётся из `lead.final_price_value` (`models.py:802-805`): `approved_price` (выставляет менеджер) → фолбэк на snapshot `final_total` → 0.

**Сборка состояния для страницы корзины** — `_collect_custom_cart_state` (`cart.py:350-429`) + `_build_custom_cart_entry_payload` (`cart.py:196-347`):
- на каждый рендер `/cart/` статус и цена перечитываются из БД по `lead_id` (смена `approved_price` менеджером сразу видна в корзине);
- `rejected`-позиции автоматически удаляются из сессии с messages-уведомлением (`cart.py:380-382, 700-708`) — фантомных отклонённых позиций нет;
- legacy-`draft` автоматически промоутится в `awaiting_review` + Telegram-уведомление менеджеру (`_promote_legacy_custom_draft`, `cart.py:174-193`);
- session-снапшот записи обновляется из БД (`cart.py:385-389`) — консистентность сессия↔БД поддерживается.

**Split approved/pending на чекауте:**
- **COD** — inline-логика в `checkout.py::create_order:46-71`: approved-лиды присоединяются к заказу (`lead.order = order`, `checkout.py:212-218`), их цена добавляется в `total_sum`; pending-ключи сохраняются в сессии после чекаута (`checkout.py:241-247`).
- **Monobank** — `_split_custom_cart_entries` (`views/monobank.py:95-133`): то же + **guard на нулевую цену** (approved-лид с `final_price_value <= 0` уходит в `missing_price_leads` → HTTP 400 «Вкажіть ціну в адмінці», `monobank.py:401-407`). Approved-ключи запоминаются в `session['monobank_approved_custom_keys']` (`monobank.py:995-996`) и удаляются из custom-корзины **только после успешной оплаты** (`_cleanup_after_success`, `monobank.py:1187-1208`) — при брошенном инвойсе кастом не теряется. Правильный дизайн.

**Гейтинг оплаты** (`cart.py:710-729` + `cart.html:882-954`):
- monopay-кнопка disabled только при `has_payable_items == False` (`approved_total <= 0`, `cart.html:920`) — обычные товары оплачиваются при pending-кастоме, кнопка получает подпись «(без кастомного одягу)» (`cart.html:925`);
- серверная валидация зеркальна: Monobank отклоняет только «нет ни regular, ни approved» (`monobank.py:409-425`), COD — аналогично (`checkout.py:66-71`);
- **prepay_200 запрещена при ЛЮБОМ кастоме в корзине** — на UI (option disabled + hint, `cart.html:507-515, 690-698`), в COD (`checkout.py:126-131`) и в Monobank (`monobank.py:415-419`). Согласовано на всех трёх слоях.

**Объяснение «почему кастом ждёт» на UI** — выполнено образцово: бейдж статуса на позиции (`cart.html:105`), `payment_note` «Не входить до оплати зараз» + «Ціна узгоджується після модерації» (`cart.py:313-314`), отдельная строка «Орієнтовно за кастомний друк» в саммари (`cart.html:788-800`), модерационный блок с 4 состояниями (all_approved / pending / rejected / прочее) + кнопка «Написати менеджеру в Telegram» (`cart.html:887-916`), контекстные подписи под кнопкой оплаты (`cart.html:946-953`).

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 21 | **P2** | COD-чекаут НЕ проверяет цену approved-кастома: лид с `final_price_value <= 0` присоединяется к заказу, добавив 0 грн — клиент получает кастом бесплатно | `checkout.py:59-64` (split без price-guard) + `checkout.py:213-218` (`except: pass` при сложении цены) | Monobank-поток блокирует этот кейс 400-кой (`monobank.py:126-129, 401-407`), COD — нет. Сценарий: менеджер нажал «approve» в Telegram, не выставив `approved_price`, а snapshot `final_total` пуст/0 → заказ уедет с недоплатой, ошибка проглатывается `except Exception: pass`. Фикс: переиспользовать `_split_custom_cart_entries` (вынести в общий модуль) вместо дублированной inline-логики. |
| 22 | **P2** | COD-чекаут молча УДАЛЯЕТ из сессии кастом-записи без `lead_id` (и любые не-dict записи) | `checkout.py:51-64` (в `key_to_lead_id` попадают только записи с `lead_id`; остальные не попадают в `pending_custom_keys`) + `checkout.py:242-247` (`remaining` оставляет только `pending_custom_keys`) | Запись без `lead_id` отображается в корзине как pending (в `_collect_custom_cart_state` `lead=None` → статус из снапшота или `DRAFT` → `is_pending`), но после оформления ЛЮБОГО обычного COD-заказа исчезает без следа — потеря данных пользователя. Monobank-поток этим не страдает (`_cleanup_after_success` удаляет только approved-ключи). Дубль-логика двух split-ов разъехалась — ещё один аргумент за общий модуль (№21). |
| 23 | P3 | Флаг `payment_allowed = all_approved` противоречит фактическому гейтингу и мёртв на клиенте | `cart.py:712-713` → `cart.html:883` (`data-payment-allowed`) | При pending-кастоме `payment_allowed=False`, но кнопка оплаты активна (гейтинг по `has_payable_items`) и сервер оплату разрешает. grep по JS: `data-payment-allowed` не читается нигде (`main.js`, `modules/*.js`). Тот же флаг дублируется третьей формулой в JSON `/cart/items/` (`cart.py:1712-1713`) — три источника истины. Удалить или привести к одному определению. |
| 24 | P3 | Рассинхрон UI↔сервер при удалённом из БД лиде: корзина может показать «approved» и включить сумму в «До сплати», а чекаут посчитает её pending | `cart.py:241` (фолбэк `moderation_status` на session-снапшот при `lead=None`) vs `monobank.py:119` / `checkout.py:60-64` (нет лида → pending) | Если `CustomPrintLead` удалён из админки, а снапшот в сессии хранит `moderation_status='approved'`: `approved_total` на странице включает его цену, monopay-кнопка активна, но серверный split отнесёт ключ к pending → инвойс создастся на МЕНЬШУЮ сумму, чем показано клиенту. Фикс: при `lead_id` без лида в БД — удалять запись (как rejected) либо принудительно считать pending и в `_collect_custom_cart_state`. |
| 25 | P3 | Промокод дисконтирует и согласованную менеджером цену кастома | `checkout.py:222-232` и `monobank.py:618-631` (`promo.calculate_discount(total_sum)`, где `total_sum` уже включает approved-кастом) | Менеджер согласовал финальную цену → промокод срежет с неё ещё N% — маржа кастома не защищена. Бизнес-правило нигде не задокументировано; решить осознанно (исключать кастом из базы расчёта скидки или зафиксировать текущее поведение). |
| 26 | P3 | `custom_print_remove` не сбрасывает Monobank-инвойс и не трекает удаление | `static_pages.py:1612-1642` | Родственник P1-бага №5 (CRO-031): после удаления кастом-позиции pending-инвойс со старой суммой остаётся в сессии; `_reset_monobank_session` не вызывается, `monobank_pending_custom_keys`/`monobank_approved_custom_keys` не чистятся. Также нет серверного `remove_from_cart`-события для кастома (зеркало бага №19 по add). |
| 27 | P3 | Брошенный monobank-инвойс оставляет approved-лид привязанным к неоплаченному заказу | `monobank.py:618-620` (`lead.order = order` при СОЗДАНИИ инвойса) + `_cleanup_expired_monobank_orders` (`monobank.py:136-149`: заказ → cancelled, лид НЕ отвязывается) | Лид числится за cancelled-заказом, оставаясь в custom-корзине (cleanup удаляет ключи только при success) → повторная оплата перепривяжет `lead.order` к новому заказу (само-починка), но в интервале админ-отчёты видят кастом «в заказе», которого нет. Минимум — отвязывать лид в `_cleanup_expired_monobank_orders`. |
| 28 | P3 | Malformed-атрибут в разметке кнопки оплаты | `cart.html:925`: `<span class="cart-monobank-subtext" data-without-custom-label">` | Лишняя кавычка: атрибут парсится как `data-without-custom-label"` (мусорное имя). JS по нему сейчас не обращается, но любой будущий селектор `[data-without-custom-label]` элемент не найдёт. Однострочный фикс. |

### Проверенные сценарии — OK (важно НЕ сломать при фиксах)

- **Pending-кастом + обычные товары → оплата обычных проходит** (COD и Monobank), pending остаётся в корзине; подписи «(без кастомного одягу)» и «Оплата покриє лише звичайні товари…» соответствуют действительности.
- **Только pending-кастом → оплата корректно заблокирована** с понятным сообщением на всех слоях (кнопка disabled, COD-message `checkout.py:66-71`, Monobank-JSON `monobank.py:421-425`).
- **Rejected-кастом** авточистится из корзины с уведомлением; **draft** автопромоутится на модерацию с Telegram-нотификацией менеджеру.
- **Monobank: кастом не теряется при брошенном инвойсе** — удаление approved-ключей только в `_cleanup_after_success`.
- **Approved-кастом без цены** в Monobank-потоке блокируется 400-кой с номерами заявок (после фикса №21 так же должен вести себя COD).
- **`/cart/items/` (AJAX-ресинк)** возвращает `approved_total`/`custom_items`, и `modules/cart.js:626-630` честно пере-гейтит кнопку по `approved_total > 0` — одобрение менеджером «оживляет» кнопку без F5 (при следующем ресинке).

### Рекомендации (порядок внедрения)

17. **P2:** вынести `_split_custom_cart_entries` из `views/monobank.py` в общий модуль (напр. `storefront/custom_print_cart.py`) и использовать в `checkout.py::create_order` — закрывает №21 (price-guard в COD) и №22 (потеря записей без `lead_id`) одним рефакторингом.
18. P3: единое определение гейтинга (`has_payable_items`) — удалить/переименовать `payment_allowed` (№23) и синхронизировать формулы `view_cart` и `/cart/items/`.
19. P3: обработка «лид удалён из БД» в `_collect_custom_cart_state` — считать pending или удалять запись (№24).
20. P3: `_reset_monobank_session` в `custom_print_remove` (№26) — тем же коммитом, что и фикс №5 из CRO-031.
21. P3: отвязка `lead.order` в `_cleanup_expired_monobank_orders` (№27); решение по промокодам на кастом задокументировать (№25); поправить кавычку в `cart.html:925` (№28).

---

## CRO-035. Восстановление корзины

Дата аудита: 06.07.2026. Проверено: `accounts/cart_middleware.py` (весь), `accounts/cart_sync.py` (весь), `accounts/cart_models.py` (UserCart), `accounts/cart_signals.py` + `accounts/apps.py:8-14` (регистрация), `twocomms/settings.py:195-226` (MIDDLEWARE), `:968-990` (сессии), `production_settings.py:40-57, 522-524`, `storefront/views/cart.py:798, 951-1058, 1344`, `static/js/ui-fallback.js:40-75` (CartRemoveKey), `storefront/tests/test_cart_sync.py`, `CART_RESTORATION_REPORT.md` (корень репо).

### Ответ на вопрос чек-листа

**Корзина переживает закрытие браузера — подтверждено для ОБОИХ типов пользователей.** Все 4 фикса из `CART_RESTORATION_REPORT.md` фактически присутствуют в текущем коде (проверено построчно). Найдено 5 проблем, все P3.

### Механизм персистентности (задокументировано)

**Гости.** Корзина живёт в session-cookie: `SESSION_ENGINE='cached_db'` (`settings.py:973`, prod то же — `production_settings.py:522`), `SESSION_COOKIE_AGE = 30 дней` (`settings.py:977`), `SESSION_EXPIRE_AT_BROWSER_CLOSE` не задан (Django-default `False`) → корзина гостя переживает закрытие браузера до 30 дней на том же устройстве.

**Залогиненные — DB-слой поверх сессии:**
- Модель `UserCart` (`accounts/cart_models.py`) — OneToOne к юзеру, JSONField-снапшоты `cart_data` + `custom_cart_data` + `promo_code_id`. Синхронизируется даже промокод.
- `CartSyncMiddleware` (`settings.py:221`, корректно ПОСЛЕ `AuthenticationMiddleware:215`; в prod присутствует — `production_settings.py:48` фильтрует только аналитические мидлвари): на входе `hydrate_session_from_db` — если content-hash-ревизия БД (`_db_revision`, sha256 содержимого, а не `updated_at` — устойчиво к грубому datetime SQLite) отличается от виденной сессией → REPLACE сессии данными БД; на выходе `persist_session_to_db` — запись в БД только если сессия реально изменилась против deepcopy-снапшота запроса (экономия записей, `SESSION_SAVE_EVERY_REQUEST=False`) и только при статусе 200–399.
- `user_logged_in`-сигнал (`cart_signals.py`, подключён в `apps.py:12`) → `merge_session_into_db`: гостевая корзина мержится с DB-корзиной (qty суммируются по ключам, кастом — union по lead_id, промо — приоритет свежей сессии). Гость, добавивший товары и залогинившийся, ничего не теряет.
- `select_for_update` + `transaction.atomic` при записи; все функции обёрнуты в try/except с логированием — синхронизация никогда не роняет запрос. Есть тестовое покрытие: `storefront/tests/test_cart_sync.py` (гидрация, персист, мерж, middleware).

### Проверка заявлений CART_RESTORATION_REPORT.md — все 4 фикса реально в коде

| Заявление отчёта | Факт в текущем коде | Статус |
|---|---|---|
| Формат ключа `{product.id}:{size}:{color_variant_id or 'default'}` | `cart.py:798` — точное совпадение | ✅ |
| Ответ `{'ok': True, 'count': N, 'total': S}` | `cart.py:851, 1344` | ✅ |
| Параметр удаления `'key'` (не `'cart_key'`) | сервер `cart.py:958` + JS `ui-fallback.js:48` (`URLSearchParams({key})`) — согласованы | ✅ |
| Временный `views.py` (7799 строк) удалён | `storefront/views.py` не существует, только пакет `views/` | ✅ |

### Найденные проблемы

| # | Приоритет | Проблема | Где | Детали |
|---|-----------|----------|-----|--------|
| 29 | P3 | Кросс-девайс-гидрация НЕ сбрасывает Monobank-сессию | `cart_sync.py::hydrate_session_from_db` (REPLACE корзины) — `_reset_monobank_session` не вызывается | Устройство A создало monobank-инвойс → устройство B изменило корзину → следующий запрос с A заменяет корзину в сессии, но pending-инвойс со СТАРОЙ суммой остаётся. Родственник №5 (CRO-031) и №26 (CRO-034), но с новым вектором: изменение прилетает «извне» мимо всех cart-views. При централизованном фиксе №5 учесть этот путь. |
| 30 | P3 | Lost-update при одновременных мутациях с двух устройств | `persist_session_to_db` — полный REPLACE `cart_data` состоянием сессии | Запросы A и B гидрировались с одной ревизии; A добавил item1 и записал; B добавил item2 и записал ПОЗЖЕ → item1 потерян (`select_for_update` защищает строку, но не делает merge). Окно узкое (секунды), для e-commerce приемлемо, но стоит задокументировать осознанность выбора last-write-wins. |
| 31 | P3 | 1–2 лишних SQL-запроса на КАЖДЫЙ авторизованный запрос | `hydrate_session_from_db` → `get_user_cart` → `get_or_create` на каждом не-skip запросе | `_SKIP_PATH_PREFIXES` покрывает статику/health/rum, но не JSON-эндпоинты навигации (`/cart/summary/`, `/api/nova-poshta/*`, поисковые подсказки). Смягчено `cached_db`-сессиями, но UserCart читается напрямую из БД. Возможный фикс: кэшировать ревизию в Django-cache по user_id. |
| 32 | P3 | Устаревший docstring: формат ключа «product_id:size:color:fit» | `cart_sync.py::_merge_standard_carts` docstring | Фактический ключ — `product_id:size:color_variant_id` (`cart.py:798`). Мерж работает корректно (ключи непрозрачные), но комментарий вводит в заблуждение — привести в соответствие. |
| 33 | P3 | `CART_RESTORATION_REPORT.md` лежит в корне репо и предшествует DB-sync-слою | корень репо, датирован 24.10.2025 | Отчёт описывает восстановление session-логики и НЕ упоминает `UserCart`/`CartSyncMiddleware` (добавлены позже) — читатель может решить, что персистентность ограничена сессией. Перенести в `TWOCOMMS_A_TO_B/technical/` или дополнить ссылкой на cart_sync. |

### Проверенные сценарии — OK (важно НЕ сломать при фиксах)

- **Закрыл браузер → открыл** (гость и залогиненный): корзина на месте (cookie 30 дней; для залогиненного — даже при потере cookie БД восстановит на первом же запросе после логина).
- **Добавил на десктопе → открыл на телефоне** (залогиненный): content-hash-ревизия ловит изменение → REPLACE сессии из БД.
- **Гость наполнил корзину → залогинился**: мерж без потерь (qty суммируются, кастом union, промо сохраняется).
- **Ошибка синхронизации** не роняет запрос (try/except + logger.warning), 4xx/5xx-ответы не персистятся (защита от записи отkatанного состояния).
- **Регрессия формата ключей** (главная причина инцидента из отчёта) покрыта: сервер и JS согласованы, есть tests.

### Рекомендации (порядок внедрения)

22. P3: при фиксе №5 (CRO-031, централизованный сброс Monobank-сессии) добавить сброс и в `hydrate_session_from_db` при фактическом REPLACE корзины (№29).
23. P3: кэш ревизии UserCart в Django-cache по user_id — убрать get_or_create с каждого запроса (№31).
24. P3: поправить docstring `_merge_standard_carts` (№32); перенести/обновить `CART_RESTORATION_REPORT.md` (№33); задокументировать выбор last-write-wins (№30).
