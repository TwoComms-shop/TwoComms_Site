# TWOCOMMS — ГЛОБАЛЬНЫЙ АУДИТ-ЧЕКЛИСТ (twocomms_global_audit.md)

**Дата создания:** 05.07.2026
**Автор анализа:** AI-архитектор (глубокий reverse engineering репозитория + живой БД MySQL на сервере)
**Назначение:** исчерпывающий чек-лист (>100 атомарных пунктов) для агентов-исполнителей. Здесь НИЧЕГО не исправлено — только анализ и задачи.
**Связанные документы:** `TECHNICAL_TASKS.md` (реестр TECH-NNN), `../SITE_IMPROVEMENT_BACKLOG.md`, `../ANALYTICS_AND_TRACKING_BLUEPRINT.md`, `../MASTER_PLAN_V2.md`

---

## 0. ПРАВИЛА ДЛЯ АГЕНТОВ-ИСПОЛНИТЕЛЕЙ

1. **Один пункт = одна атомарная проверка/фикс.** Выполнил → ставь `[x]` и запись в «Журнал» внизу файла (дата, ID, коммит/PR).
2. **Формат каждого пункта:** «Где проверять» (путь/URL) + «Что искать/тестировать». Если баг подтверждён — сначала фиксируй находку в журнале, потом чини отдельным коммитом с ID пункта в сообщении.
3. **Безопасность:** SSH/MySQL-реквизиты — ТОЛЬКО у владельца. ЗАПРЕЩЕНО записывать пароли, ключи, DSN в любые файлы репозитория, коммиты, логи и отчёты. Перед каждым коммитом — проверка `git diff` на секреты.
4. **Боевая БД — MySQL на сервере** (не локальная SQLite). Все проверки данных — через SSH → `source ~/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && cd ~/TWC/TwoComms_Site/twocomms && python manage.py shell`. Только read-only запросы без явного согласования записи.
5. **Бренд-контекст:** эстетика TwoComms строится вокруг «трудностей/преодоления» (difficulties), НЕ спорт/бейсбол. Основной продукт — лонгсливы и худи (футболки вторичны). Тексты магазина транслируют концепцию бренда, а не личность владельца («я» — убирать).
6. Ничего не удалять/не рефакторить «заодно». Анти-задачи из `TECHNICAL_TASKS.md` действуют.

---

## ФАКТЫ, ПОДТВЕРЖДЁННЫЕ ЖИВОЙ БД (замер 05.07.2026)

Эти цифры — базовая линия. Агенты сверяются с ними до/после фиксов.

| Метрика | Значение | Вывод |
|---|---|---|
| Товары (Product) | 68 | каталог небольшой, аудит покрывает 100% карточек |
| Заказы (Order) | 41 (36 done, 5 cancelled) | статусная модель примитивна: нет shipped/delivered/RTS |
| UTM-сессии (UTMSession) | 1015 | трекинг сессий работает |
| UTM-сессии `is_converted=True` | **0** | **разрыв №2 подтверждён: конверсия источника не считается** |
| Заказы с utm_source | **0 из 41** | **разрыв №1 подтверждён: ни один заказ не привязан к UTM** |
| Заказы с utm_session FK | **0 из 41** | link_order_to_utm фактически не срабатывает в COD-потоке |
| CustomPrintLead | 28, **все в статусе `new`** | разрыв №3 подтверждён: воронка кастома мертва |
| UserAction всего | 36 859 | события пишутся |
| product_view | 36 009 (97,7% всех событий) | аномально много → подозрение на ботов/двойной счёт |
| add_to_cart | 44 | CTR view→cart = 0,12% — либо трекинг завышает views, либо UX-проблема |
| initiate_checkout / lead / purchase | 5 / 5 / 3 | purchase-события есть только у monobank-потока |
| SiteSession `is_bot=True` | 0 из 2899 | бот-детект не помечает никого → шум в данных |
| Top utm_source | google 542, ig 198, Instagram 128, **chatgpt.com 109** | AI-трафик уже 3-й источник; ig/Instagram — дубли (нет нормализации) |
| Кэш (django cache roundtrip) | OK (Redis работает) | Redis подключён и жив |
| Celery | удалён, работают no-op шимы | тех. долг: мёртвые упоминания в коде/логах |

---

## РАЗДЕЛ 1. UX/UI И ВОРОНКА КОНВЕРСИИ (CRO)

### 1.1 Главная страница

- [ ] **CRO-001. Соответствие концепции «difficulties».** Где: `twocomms_django_theme/templates/pages/index.html`, https://twocomms.shop/. Что: hero-заголовок, подзаголовки и CTA транслируют «преодоление/трудности», нет спортивной/бейсбольной лексики; лонгсливы и худи визуально приоритетнее футболок.
- [ ] **CRO-002. Работа всех CTA-кнопок главной.** Где: index.html + https://twocomms.shop/. Что: каждая кнопка (в каталог, в карточку, в кастом) кликабельна, ведёт на корректный URL, не даёт 404/редирект-цепочек; проверить на мобильном viewport 360px.
- [ ] **CRO-003. Скорость рендера главной (LCP/CLS/INP).** Где: PageSpeed Insights / Lighthouse mobile для https://twocomms.shop/. Что: LCP < 2.5s, CLS < 0.1, INP < 200ms; зафиксировать текущие числа в журнале как базовую линию.
- [ ] **CRO-004. Отложенная загрузка GTM не ломает атрибуцию.** Где: `base.html` (строки ~929–996: GTM грузится по interaction или таймауту 12–20s). Что: пользователь, зашедший с рекламы и ушедший до interaction, теряется для Pixel/GA4 — замерить долю таких сессий; решить: сократить таймаут для сессий с utm_* в URL.
- [ ] **CRO-005. Блок отзывов на главной.** Где: index.html, `reviews/`. Что: есть ли вывод реальных отзывов; если блока нет — задача TECH-013.
- [ ] **CRO-006. Мёртвые/неиспользуемые секции главной.** Где: index.html. Что: секции, скрытые через `display:none`/комментарии, которые всё равно рендерятся и грузят DOM.

### 1.2 Каталог

- [ ] **CRO-010. Фильтрация лонгсливов/худи.** Где: `storefront/views/catalog.py` (1215 строк), `pages/catalog.html`, URL `/catalog/long-sleeve/`, `/catalog/hoodie/`. Что: фильтры по категории (slug: long-sleeve id=5, hoodie id=4, tshirts id=3), цвету, размеру работают без полной перезагрузки; выбранный фильтр сохраняется в URL (шаримая ссылка).
- [ ] **CRO-011. Пагинация каталога.** Где: `storefront/pagination.py`, catalog.py. Что: canonical/prev/next на страницах пагинации; переход по страницам не сбрасывает фильтры; нет дублирующихся товаров между страницами.
- [ ] **CRO-012. Lazy-load изображений каталога.** Где: catalog.html, `optimized_image.html`, `responsive_image.html`, `image_optimizer.py`. Что: `loading="lazy"` на всех карточках ниже фолда, первые 4–8 карточек eager; корректные width/height (нет CLS).
- [ ] **CRO-013. Порядок сортировки: лонгсливы/худи первыми.** Где: catalog.py (queryset ordering), главная витрина. Что: дефолтная сортировка не выпячивает футболки вопреки позиционированию.
- [ ] **CRO-014. Статусы наличия в каталоге.** Где: catalog.html, `storefront/models.py` (Product). Что: карточки «нет в наличии» либо честно помечены, либо скрыты (TECH-010); нет phantom-SKU, на которые можно кликнуть и застрять.
- [ ] **CRO-015. N+1 при рендере каталога.** Где: catalog.py + `storefront/services/catalog/`. Что: `select_related`/`prefetch_related` для категорий, цветовых вариантов (`productcolors`), изображений; замерить кол-во SQL-запросов на страницу каталога (django-debug-toolbar локально или логирование запросов), норма ≤ 15.

### 1.3 Карточка товара

- [ ] **CRO-020. Корректность данных из MySQL.** Где: `storefront/views/product.py` (755 строк), `pages/product_detail.html`; живая БД: 68 товаров. Что: для 10 случайных товаров сверить название, цену, описания, цвета (`productcolors.ProductColorVariant`), фото на сайте против значений в БД (через SSH shell, read-only).
- [ ] **CRO-021. Тексты без «я», в tone of voice бренда.** Где: описания в БД (`Product.description*`, модели переводов django-modeltranslation: name_uk/ru/en), product_detail.html. Что: grep по описаниям на «я », «мне», «мой» от первого лица владельца; спорт/бейсбол-лексика; несоответствие концепции «difficulties».
- [ ] **CRO-022. Фото товара: варианты и вебп.** Где: `image_optimizer.py`, `storefront/services/image_variants.py`, media на сервере. Что: все карточки имеют основное фото; генерируются ли webp/размеры; alt-тексты не пустые и не дублируются.
- [ ] **CRO-023. Размерная сетка с карточки.** Где: `storefront/services/size_guides.py`, product_detail.html. Что: таблица замеров в см доступна с каждой карточки лонгслива/худи; событие `view_size_guide` (TECH-005) пока НЕ существует — создать задачу на добавление.
- [ ] **CRO-024. Событие product_view — завышение.** Где: `storefront/tracking.py`, `storefront/utm_tracking.py::record_product_view`, вызовы в product.py. Что: 36 009 product_view против 44 add_to_cart — проверить: (а) пишется ли view при каждом AJAX/прелоаде/боте; (б) исключаются ли краулеры (`is_bot_user_agent`); (в) нет ли двойного вызова (сервер + JS).
- [ ] **CRO-025. Выбор цвета/размера — UX и трекинг.** Где: product_detail.html JS, `productcolors/`. Что: смена цвета обновляет фото и цену без перезагрузки; события `select_size`/`select_color` (TECH-008) отсутствуют — задача на добавление.
- [ ] **CRO-026. Блок отзывов на карточке.** Где: `reviews/`, product_detail.html. Что: выводятся ли отзывы; schema Review/AggregateRating (см. SEO-раздел).
- [ ] **CRO-027. Рекомендации на карточке.** Где: `storefront/recommendations.py`. Что: блок «с этим покупают/похожие» не делает N+1 и не рекомендует out-of-stock.

### 1.4 Мини-корзина и корзина

- [ ] **CRO-030. Логика мини-корзины vs full-page корзины.** Где: `storefront/views/cart.py` (1850 строк — кандидат на декомпозицию), `accounts/cart_middleware.py`, `cart_models.py`, `cart_signals.py`, `cart_sync.py`, `pages/cart.html`. Что: описать в отчёте фактическую архитектуру (session-корзина + синхронизация в БД для залогиненных); проверить сценарии: добавил анонимно → залогинился → корзина слилась без дублей.
- [ ] **CRO-031. Баги добавления/удаления.** Где: cart.py (AJAX-эндпоинты), JS в base.html/cart.html. Что: быстрый двойной клик «добавить» не создаёт 2 позиции; удаление последней позиции корректно обнуляет счётчик в шапке; смена количества пересчитывает сумму и промокод.
- [ ] **CRO-032. Кэширование не ломает мини-корзину.** Где: `twocomms/cache_headers.py`, `twocomms/media_cache_middleware.py`, `cache_utils.py`, `storefront/cache_signals.py`. Что: страницы с корзиной/счётчиком не отдаются из полного page-cache другому пользователю; заголовки Cache-Control на HTML с динамикой = private/no-cache; счётчик корзины подтягивается AJAX-ом после загрузки кэшированной страницы.
- [ ] **CRO-033. Событие add_to_cart — сервер + пиксели.** Где: cart.py (вызовы `record_add_to_cart`), `static` JS (fbq/ttq AddToCart). Что: одно добавление = ровно одно серверное UserAction + одно событие в каждый пиксель; протестировать вживую через Meta Test Events / TikTok Test Events.
- [ ] **CRO-034. Кастом-принт позиции в корзине.** Где: `storefront/custom_print_config.py` (SESSION_CUSTOM_CART_KEY), checkout.py (split approved/pending). Что: pending-кастом не блокирует оформление обычных товаров; UI корзины ясно объясняет, почему кастом «ждёт».
- [ ] **CRO-035. Восстановление корзины.** Где: cart_middleware.py, отчёты `CART_RESTORATION_REPORT.md` (корень репо). Что: корзина переживает закрытие браузера для залогиненных; отчёт в корне числит фиксы «сделанными» — проверить фактически.

### 1.5 Чекаут

- [ ] **CRO-040. Полный прогон COD-чекаута.** Где: `storefront/views/checkout.py::create_order`, https://twocomms.shop/ (тестовый заказ с пометкой). Что: минимальный набор полей, валидация телефона (`normalize_checkout_phone`), Нова Пошта подбор отделения (`orders/nova_poshta_*`); 0 JS-ошибок в консоли на всём пути.
- [ ] **CRO-041. КРИТИЧНО: COD-заказ не пишет UTM.** Где: checkout.py — в нём НЕТ вызовов `link_order_to_utm`/`record_purchase`/`record_lead` (они есть только в `storefront/views/monobank.py:565,584,986`). Подтверждено БД: 0 из 41 заказов имеют utm_source. Что: задача = вызывать `link_order_to_utm(request, order)` + `record_order_action` при создании ЛЮБОГО заказа (COD и предоплата). Это TECH-060.
- [ ] **CRO-042. is_converted никогда не проставляется.** Где: `storefront/utm_tracking.py::record_lead/record_purchase` вызываются только из monobank-потока; БД: 1015 UTM-сессий, 0 converted. Что: после фикса CRO-041 проверить, что `mark_as_converted` срабатывает и для COD. Это TECH-061.
- [ ] **CRO-043. Monobank-поток: вебхук и статусы.** Где: `storefront/monobank.py`, `storefront/views/monobank.py` (986 строк вызова record_lead). Что: вебхук идемпотентен (повторный callback не создаёт второй purchase-event); подпись вебхука проверяется; отказ оплаты ведёт на `order_failed.html` с восстановимой корзиной.
- [ ] **CRO-044. Страница «Спасибо за покупку».** Где: `pages/order_success.html` (+ мёртвый `order_success_old.html`). Что: серверное purchase-событие не дублируется при F5 (event_id-дедуп); есть ли рекомендации/апселл; `order_success_old.html` — удалить (тех. долг, см. TD-раздел).
- [ ] **CRO-045. Определение purchase-момента.** Где: monobank.py (purchase при оплате) vs COD (оплата при получении). Что: зафиксировать документально: purchase = создание заказа + отдельное серверное событие оплаты/доставки (TECH-066); привести GA4/Pixel/CAPI к одному определению.
- [ ] **CRO-046. Промокоды в чекауте.** Где: `storefront/models.py` (PromoCode), cart.py/checkout.py. Что: невалидный/просроченный код даёт понятную ошибку; события `coupon_apply` (TECH-023) нет — задача на добавление.
- [ ] **CRO-047. Ошибочные состояния чекаута.** Где: checkout.py. Что: пустая корзина → редирект с message; товар кончился между корзиной и заказом → понятное сообщение, а не 500; проверить транзакционность (`transaction.atomic`) создания Order+OrderItem.

### 1.6 Воронка целиком (замер)

- [ ] **CRO-050. Сквозной тест-прогон воронки с UTM.** Где: браузер: `https://twocomms.shop/?utm_source=audit&utm_medium=test&utm_campaign=funnel_check` → каталог → карточка → мини-корзина → корзина → чекаут → заказ (тестовый). Что: после прогона в БД (SSH shell): UTMSession создана, UserAction содержит page_view→product_view→add_to_cart→initiate_checkout→purchase/lead, Order имеет utm_source='audit'. Сейчас последний шаг ГАРАНТИРОВАННО провалится (см. CRO-041) — это acceptance-тест фикса.
- [ ] **CRO-051. Конверсия по шагам — базовая линия.** Где: живая БД UserAction. Что: зафиксировать в журнале текущие цифры (36009 views → 44 ATC → 5 IC → 3 purchase) и пересчитать после фикса бот-фильтра (см. AN-раздел), чтобы отличить «грязные данные» от реальной UX-проблемы.

---

## РАЗДЕЛ 2. СКВОЗНАЯ АНАЛИТИКА И ПИКСЕЛИ

### 2.1 GTM / GA4

- [ ] **AN-001. Инвентаризация GTM-контейнера GTM-PRLLBF9H.** Где: base.html:973–992, GTM-интерфейс (доступ у владельца). Что: список всех тегов/триггеров/переменных; мёртвые теги; дубли GA4-событий (сервер+клиент).
- [ ] **AN-002. Отложенный GTM vs paid-трафик.** Где: base.html (interaction-триггер + таймаут 12–20s, `isPassiveAnalytics`). Что: см. CRO-004; вариант — форсировать немедленную загрузку при наличии utm_*/fbclid/gclid/ttclid в URL.
- [ ] **AN-003. GA4-события воронки.** Где: GTM + GA4 DebugView. Что: view_item, add_to_cart, begin_checkout, add_shipping_info, add_payment_info, purchase с items[] и value/currency; параметр payment_type (cod/prepay) — TECH-007.
- [ ] **AN-004. Internal/staff-трафик исключён.** Где: `storefront/analytics_exclusions.py` (is_request_excluded), GA4-фильтры. Что: staff-пользователи и офисные IP не пишутся ни в UserAction, ни в GA4 (проверить оба слоя).

### 2.2 Meta Pixel + CAPI

- [ ] **AN-010. Pixel ID 823958313630148 — события клиента.** Где: base.html:12, `analytics-loader.js` (static). Что: PageView, ViewContent, AddToCart, InitiateCheckout, Purchase реально стреляют (Meta Pixel Helper); advanced matching div#am заполняется.
- [ ] **AN-011. CAPI-события сервера.** Где: `orders/facebook_conversions_service.py` (850 строк: send_purchase_event, send_lead_event, send_add_payment_info_event, send_event_for_order_status). Что: события реально отправляются на боевом сервере (логи), access token валиден; retry-логика `_send_request_with_retry` не создаёт дублей.
- [ ] **AN-012. Дедупликация Pixel↔CAPI.** Где: facebook_conversions_service.py (event_id), клиентский fbq-вызов, `META_PIXEL_CAPI_DEDUPE_IMPLEMENTATION.md` (корень). Что: одинаковый event_id в браузерном и серверном событии; в Events Manager дедуп подтверждён; EMQ зафиксировать в журнале. Это TECH-064 — числится сделанным, но не проверено.
- [ ] **AN-013. fbc/fbp/fbclid доходят до CAPI.** Где: utm_middleware.py (platform_data), Order (полей fbclid НЕТ в orders/models.py — только utm_*!), facebook_conversions_service.py. Что: клик-ID живут только в UTMSession/сессии; при COD-заказе (0 привязок к UTMSession) CAPI-события уходят БЕЗ fbc/fbp → низкий match quality. Задача: копировать fbclid/fbc/fbp/gclid/ttclid в Order (расширение TECH-060).
- [ ] **AN-014. Offline-конверсии delivered.** Где: facebook_conversions_service.py::send_event_for_order_status, orders/status_management.py. Что: возможна ли отправка события по факту доставки; сейчас статусов shipped/delivered нет вообще (только done/cancelled) — блокируется TECH-070/071.
- [ ] **AN-015. test_event_code изоляция.** Где: base.html (data-tiktok-test-event-code), настройки CAPI. Что: тестовые события не загрязняют боевую статистику (TECH-043).

### 2.3 TikTok Pixel

- [ ] **AN-020. TikTok Pixel D43L7DBC77UA61AHLTVG.** Где: base.html:13, analytics-loader.js, `orders/tiktok_events_service.py` (308 строк). Что: клиентские события ViewContent/AddToCart/InitiateCheckout/CompletePayment стреляют; серверные события из tiktok_events_service реально отправляются; дедуп event_id клиент↔сервер.
- [ ] **AN-021. ttclid сквозной путь.** Где: utm_middleware.py PLATFORM_PARAMS → session → (разрыв) → Order. Что: как AN-013, но для TikTok.

### 2.4 UTM-механика

- [ ] **AN-030. UTM переживает всю воронку.** Где: `storefront/utm_middleware.py` (session['utm_data']). Что: UTM из первого URL доступна на чекауте после 30+ минут и переходов; проверить SESSION_COOKIE_AGE и не рвётся ли session_key при логине (`django.contrib.auth.login` меняет session_key — UTMSession привязана к старому ключу! Проверить `cycle_key` эффект).
- [ ] **AN-031. КРИТИЧНО: смена session_key при логине рвёт связку.** Где: utm_tracking.py (поиск UTMSession по `request.session.session_key`), Django auth. Что: воспроизвести: зайти с utm → залогиниться → оформить заказ; проверить, находит ли `link_order_to_utm` сессию. Если нет — мигрировать UTMSession.session_key при логине или искать по visitor_id.
- [ ] **AN-032. Нормализация utm_source.** Где: живая БД: 'ig' (198), 'Instagram' (128), 'IGShopping' (6), 'Inst_Vid' (10) — 4 написания одного канала; 'fb'/'fb-SiteLink'; '120233970682840302' (сырой ad id). Что: словарь нормализации при записи или на уровне отчётов; UTM governance-конвенция (TECH-009).
- [ ] **AN-033. AI-источники как отдельный канал.** Где: БД: utm_source='chatgpt.com' 109 сессий + referrer chatgpt.com 18. Что: детект chatgpt.com/perplexity.ai/gemini/claude.ai по utm и referrer → канал «AI» в отчётах (TECH-065).
- [ ] **AN-034. Кэш не сбрасывает UTM.** Где: cache_headers.py, whitenoise, hosting-кэш (Hostsila/LiteSpeed?). Что: страница с `?utm_...` не отдаётся из кэша без прохода через UTMTrackingMiddleware; проверить, что HTML с query-параметрами — MISS или что мидлварь стоит до кэш-слоя.
- [ ] **AN-035. Бот-фильтр фактически мёртв.** Где: `utm_utils.py::is_bot_user_agent`, БД: SiteSession is_bot=True = 0 из 2899. Что: либо ботов реально нет (маловероятно при 36k product_view), либо детект не работает/не пишет флаг; протестировать с UA «Googlebot»; расширить список; referrer-спам чёрный список (TECH-063).
- [ ] **AN-036. increment_visit на каждый запрос без UTM.** Где: utm_middleware.py (ветка `else: utm_session.increment_visit()`). Что: КАЖДЫЙ запрос любой страницы делает SELECT+UPDATE UTMSession — нагрузка и искажение visit_count (это pageviews, не визиты); оценить и переработать.
- [ ] **AN-037. Атрибуционная модель first/last touch.** Где: utm_middleware.py — при новом UTM существующая сессия НЕ обновляет utm_* (get_or_create только defaults). Что: задокументировать фактическую модель (first-touch в рамках session_key); решить, нужен ли last-touch слой; `analytics_first_touch_data` в SiteSession — сверить консистентность.
- [ ] **AN-038. Отчётность UTM в админке.** Где: `storefront/utm_analytics.py`, `utm_api_views.py`, `utm_cohort_analysis.py`, `storefront/admin_analytics_api.py`, `storefront/services/admin_analytics.py`, вкладка в админ-панели `/admin-panel/` (pages/admin_panel.html). Что: цифры вкладки сходятся с прямыми запросами к БД; отчёт «источник → сессии → конверсии» сейчас покажет 0 конверсий везде (следствие CRO-041/042) — после фикса перепроверить; экспорт работает.
- [ ] **AN-039. Событие search и PII.** Где: utm_tracking.py::record_search (пишет query в metadata). Что: query не содержит телефонов/email (санитизация); 181 запись — просмотреть выборку на PII.
- [ ] **AN-040. GA-Data API интеграция.** Где: requirements (google-analytics-data), `storefront/services/external_analytics.py`. Что: используется ли реально; ключ сервис-аккаунта не в репозитории; если не используется — кандидат на удаление.

### 2.5 Consent и приватность

- [ ] **AN-050. Cookie-consent баннер.** Где: base.html, шаблоны partials. Что: есть ли баннер вообще; пиксели грузятся до согласия? (TECH-077); consent mode в GTM.
- [ ] **AN-051. IP и геолокация — законность хранения.** Где: utm_middleware.py (ip_address, geo), UTMSession model. Что: privacy policy упоминает сбор; retention-политика (сессии старше N месяцев — чистка).

---

## РАЗДЕЛ 3. ТЕХНИЧЕСКОЕ СОСТОЯНИЕ И ТЕХ. ДОЛГ

### 3.1 Мёртвый код и файлы

- [ ] **TD-001. storefront/views.py.backup.** Где: `twocomms/storefront/views.py.backup`. Что: удалить из репо (git rm), убедиться, что ничего его не импортирует.
- [ ] **TD-002. order_success_old.html.** Где: `twocomms_django_theme/templates/pages/order_success_old.html`. Что: grep по render/template_name — если не используется, удалить.
- [ ] **TD-003. Celery-шимы и следы.** Где: `twocomms/__init__.py` (try-import celery), `orders/tasks.py`, `storefront/tasks.py` (no-op шимы), `celery.log` на сервере. Что: Celery удалён осознанно (хостинг без воркеров) — проверить, что ни один вызов не рассчитывает на асинхронность (feeds_queue, web_push, ai-генерация); удалить celery.log с сервера; зафиксировать решение «Celery не возвращаем» в TECHNICAL_TASKS.
- [ ] **TD-004. Мусор в корне twocomms/.** Где: `494cb80b2da94b4395dbbed566ab540d.txt`, `replacement.txt`, `pricelist.html`, `wholesale_prices.xlsx`, `Оптові ціни....xlsx`, `analyze_views_migration.py`, `check_views_*.py`, `_audit*`, `Promt/`, `Ideas/`. Что: рассортировать: рабочие скрипты → `scripts/`, документы → docs, мусор → удалить; xlsx с ценами не должны лежать в публичном репо, если содержат закупочные цены.
- [ ] **TD-005. Отчёты *.md в корне репозитория (100+ файлов).** Где: корень репо (CART_*, PERF_*, SEO_* и т.д.). Что: перенести в `docs/archive/` одним PR; в корне оставить README + активные план-файлы.
- [ ] **TD-006. legacy_stubs.py.** Где: `storefront/views/legacy_stubs.py`. Что: какие вьюхи-заглушки живы в urls; вернуть 410 для мёртвых URL или удалить.
- [ ] **TD-007. Дублирующиеся системы аналитики.** Где: `storefront/tracking.py` vs `utm_tracking.py` vs `ab_testing.py` vs `ai_signals.py`. Что: карта «кто что пишет и куда»; выявить неиспользуемые (0 записей в соотв. таблицах БД — проверить через SSH) и завести задачу на удаление.

### 3.2 Производительность

- [ ] **TD-010. Узкие места рендера.** Где: `twocomms/settings.py` (шаблоны, context_processors — `storefront/context_processors.py`). Что: контекст-процессоры не делают тяжёлых запросов на каждый запрос (категории, счётчики — кэшировать); замерить TTFB главной/каталога/карточки (curl -w) и записать базовую линию.
- [ ] **TD-011. Redis-кэш: покрытие и инвалидация.** Где: settings.py:859+ (django-redis, REDIS_IGNORE_EXCEPTIONS=true), `cache_signals.py`, `cache_utils.py`. Что: живой roundtrip подтверждён; проверить: какие вьюхи реально кэшируются, сигналы инвалидации при сохранении Product/Category срабатывают (изменить товар → страница обновилась без ручного сброса).
- [ ] **TD-012. REDIS_IGNORE_EXCEPTIONS маскирует падения.** Где: settings.py:875. Что: при падении Redis сайт молча работает без кэша и деградирует — добавить алерт/лог-мониторинг (TECH-041).
- [ ] **TD-013. Статика: compressor + whitenoise.** Где: settings (django-compressor, whitenoise), `static/`. Что: COMPRESS_ENABLED в проде; заголовки Cache-Control на static (>30d, immutable); нет блокирующих render CSS/JS в head, которые можно defer.
- [ ] **TD-014. Медиа-кэш мидлвари.** Где: `twocomms/image_middleware.py`, `media_cache_middleware.py`. Что: не перехватывают ли лишние пути; корректный 404 на отсутствующие изображения (не 500).
- [ ] **TD-015. passenger_wsgi + лимиты хостинга.** Где: `passenger_wsgi.py`, сервер (Hostsila shared). Что: сколько воркеров/память; долгие операции (AI-генерация, feed-генерация openai/feeds) выполняются синхронно в запросе? — риск таймаутов; вынести в management-команды по cron.
- [ ] **TD-016. Логи на сервере.** Где: `~/TWC/TwoComms_Site/twocomms/*.log` (ai_generation.log, celery.log и др.). Что: ротация настроена; логи не съедают диск; в логах нет секретов.

### 3.3 Надёжность и безопасность

- [ ] **TD-020. Бэкапы MySQL.** Где: сервер (cron владельца/hostsila-панель). Что: расписание дампов; тест восстановления на копии (TECH-042); дампы не лежат в web-доступной папке.
- [ ] **TD-021. Секреты не в репозитории.** Где: весь репо. Что: `git log -p | grep -iE "(password|secret|token)"` выборочно; `.env*` в .gitignore; в settings.py нет захардкоженных ключей (SECRET_KEY, monobank token, FB access token — только из env); pixel ID в base.html — публичные, ок.
- [ ] **TD-022. Мониторинг 5xx и JS-ошибок.** Где: нет системы (проверить). Что: внедрить минимум: серверный error-лог → телеграм-алерт (телеграм-бот уже есть: `accounts/telegram_bot.py`, `orders/telegram_notifications.py`) — TECH-041.
- [ ] **TD-023. Rate limiting.** Где: django-ratelimit в requirements. Что: реально ли навешан на чекаут/логин/API (grep `@ratelimit`); если нет — тех. долг: пакет установлен, но не используется.
- [ ] **TD-024. DRF API поверхность.** Где: `storefront/viewsets.py`, `api_urls.py`, drf-spectacular. Что: какие эндпоинты публичны; права доступа (permission_classes); /api/schema не светит внутренние модели.
- [ ] **TD-025. db_routers.py.** Где: `twocomms/db_routers.py`. Что: зачем роутер, есть ли вторая БД; мёртвый код?

### 3.4 Статусная модель заказа (фундамент точных данных)

- [ ] **TD-030. Текущие статусы: только done/cancelled.** Где: `orders/models.py` (Order.status), `orders/status_management.py`; БД: 36 done + 5 cancelled. Что: спроектировать модель `created → deposit_paid → paid → shipped → delivered / refused_rts / cancelled` + timestamps каждой смены (TECH-070); НЕ внедрять без согласования схемы.
- [ ] **TD-031. Нова Пошта API-синк статусов.** Где: `orders/nova_poshta_service.py`, `nova_poshta_documents.py` (ТТН). Что: трекинг по ТТН существует? если нет — задача TECH-071 (авто-simulate shipped/delivered/refused_rts).
- [ ] **TD-032. COGS-снапшот в заказе.** Где: orders/models.py (полей себестоимости нет), `finance/` app. Что: сверить с finance-моделями; задача TECH-072 — фиксировать компоненты себестоимости на момент заказа.
- [ ] **TD-033. CustomPrintLead воронка мертва.** Где: `storefront/models.py` (CustomPrintLead), БД: 28 лидов, все `new`. Что: статусная модель new→in_progress→quoted→won/lost + движение из админки (TECH-062); отчёт «лиды → выигранные».

---

## РАЗДЕЛ 4. SEO И AEO

### 4.1 Техническое SEO

- [ ] **SEO-001. robots.txt.** Где: `storefront/views` (robots_txt view), https://twocomms.shop/robots.txt. Что: не закрыты нужные разделы; закрыты /admin-panel/, служебные; указан sitemap-index; фолбэк /static/robots.txt отдаёт то же.
- [ ] **SEO-002. Sitemap-index и секции.** Где: urls.py:44–55 (sitemap-static/products/product-variants/categories/blog/color-categories/thematic/images), `storefront/sitemaps.py`. Что: каждый под-sitemap отдаёт 200, URL в них живые (выборка по 10), lastmod честный; product-variants не плодит дубли против canonical.
- [ ] **SEO-003. Canonical и дубли.** Где: base.html/шаблоны (link rel=canonical), пагинация, цветовые лендинги (`category_color_landing.html`), локали /ru/ /en/. Что: карточка с ?utm или параметрами фильтра каноникалится на чистый URL; вариантные страницы цвета не конкурируют с основной карточкой.
- [ ] **SEO-004. hreflang uk/ru/en.** Где: base.html, urls.py:107 (i18n-префиксы /ru/, /en/). Что: hreflang-кластеры взаимные + x-default; переводы страниц реально существуют (modeltranslation поля name_ru/name_en не пустые — выборка из БД).
- [ ] **SEO-005. H1-иерархия.** Где: index.html, catalog.html, product_detail.html, blog. Что: ровно один H1 на страницу; H1 карточки = название товара; нет прыжков H2→H4.
- [ ] **SEO-006. Битые внутренние ссылки и 410.** Где: `404.html`, `410.html`, GSC (доступ у владельца), краулинг site (screaming frog/линк-чекер по sitemap). Что: внутренних 404 нет; удалённые товары отдают 410 или redirect на категорию.
- [ ] **SEO-007. Мета-титлы/дескрипшены из БД.** Где: `storefront/seo_utils.py`, `services/product_seo_autofill.py`, `product_seo_block.py`, `category_seo_blocks.py`; БД (meta-поля Product/Category). Что: у всех 68 товаров и категорий заполнены уникальные title/description; длина title ≤ 60, description 120–160; нет шаблонного дубляжа.
- [ ] **SEO-008. Google Merchant feed.** Где: `google_merchant_feed.xml` (корень twocomms/ — статический файл!), `storefront/feeds.py`, `services/marketplace_feeds.py`, `AUTO_GOOGLE_MERCHANT_FEED_UPDATE.md`. Что: фид генерируется автоматически или лежит устаревший статикой; цены/наличие в фиде = БД; фид отдаётся по URL и принят в Merchant Center.
- [ ] **SEO-009. IndexNow и Google Indexing.** Где: `services/indexnow.py`, `services/google_indexing.py`. Что: ключи валидны, реально дергаются при публикации/изменении; не спамят при массовых пересохранениях.
- [ ] **SEO-010. Скорость как ранж-фактор.** Где: см. CRO-003; каталог и карточка тоже. Что: CWV зелёные на 3 ключевых шаблонах mobile (TECH-040).

### 4.2 Structured Data (Schema.org)

- [ ] **SEO-020. КРИТИЧНО: Product schema на карточке.** Где: `pages/product_detail.html` — grep JSON-LD по шаблону НЕ нашёл `application/ld+json` (schema есть в catalog/index/blog, но не в product_detail!). Что: подтвердить рендером живой карточки (view-source); если отсутствует — добавить Product+Offer (price, priceCurrency, availability, brand, image, sku) — TECH-030; Rich Results Test зелёный.
- [ ] **SEO-021. Review/AggregateRating schema.** Где: product_detail.html, reviews. Что: после появления отзывов — валидная разметка (TECH-031); НЕ размечать фейковые рейтинги.
- [ ] **SEO-022. Организация/сайт schema.** Где: index.html, footer.html (schema.org найдено). Что: Organization с logo/sameAs (Instagram, TikTok); BreadcrumbList на каталоге/карточке.
- [ ] **SEO-023. FAQPage schema.** Где: custom_print.html, support_page.html (schema найдена — валидировать). Что: FAQ-разметка соответствует видимому контенту; расширить на 5+ страниц (TECH-032).

### 4.3 AEO (Answer Engine Optimization)

- [ ] **AEO-001. AI-трафик уже идёт — усилить.** Где: БД: 109 сессий utm_source=chatgpt.com. Что: понять, какие страницы цитирует ChatGPT (landing_page этих UTM-сессий — запрос к БД); усилить эти страницы фактологией (состав, замеры, сроки, цены).
- [ ] **AEO-002. llms.txt.** Где: корень сайта (проверить https://twocomms.shop/llms.txt). Что: если нет — создать с описанием бренда/каталога/политик (TECH-035), не противореча robots.txt.
- [ ] **AEO-003. Q&A-структура контента.** Где: карточки, `docs/seo`, blog-шаблоны. Что: прямые ответы на вопросы («Из чего лонгслив?», «Сроки отправки?») в первых абзацах; таблицы замеров в HTML-таблицах (не картинках) — AI-парсеры читают текст.
- [ ] **AEO-004. Тон бренда во всех текстах.** Где: все шаблоны pages/*, описания в БД, `services/product_copy_v2.py`, `_product_themes.py`. Что: grep-аудит на «спорт», «бейсбол», от-первого-лица «я»; соответствие «difficulties/преодоление»; составить список страниц на переписывание (само переписывание — отдельные задачи).
- [ ] **AEO-005. «С 2014» и ложные факты.** Где: все шаблоны + БД-тексты (grep «2014»). Что: исправить историю бренда на корректную (TECH-034).
- [ ] **AEO-006. AI-автогенерация контента.** Где: `storefront/services/product_seo_autofill.py`, openai в requirements, ai_generation.log на сервере. Что: сгенерированные тексты прошли ручную вычитку на тон бренда; нет галлюцинаций про состав/происхождение.

---

## РАЗДЕЛ 5. БАЗА ДАННЫХ MYSQL И ЦЕЛОСТНОСТЬ

- [ ] **DB-001. Медленные запросы.** Где: сервер: MySQL slow query log (включён ли — спросить у владельца/hostsila), либо `connection.queries` при DEBUG на staging. Что: топ-10 медленных; кандидаты: отчёты utm_analytics (JOIN UserAction 36k строк), каталог.
- [ ] **DB-002. Индексы под аналитические запросы.** Где: `storefront/models.py` (UserAction, UTMSession, SiteSession Meta.indexes). Что: составные индексы под частые фильтры (action_type+created, utm_session_id, session_key везде indexed); EXPLAIN ключевых отчётных запросов.
- [ ] **DB-003. Целостность Order ↔ UTMSession ↔ User.** Где: живая БД. Что: после фикса CRO-041 — доля заказов с utm_session > 0; consistency-запрос: UserAction с order_id, у которого нет Order (осиротевшие записи).
- [ ] **DB-004. Рост UserAction (36 859 строк).** Где: UserAction таблица. Что: политика retention (агрегировать записи старше 6–12 мес.); оценить размер таблицы (`SHOW TABLE STATUS`).
- [ ] **DB-005. N+1 в админ-отчётах.** Где: `services/admin_analytics.py`, `utm_cohort_analysis.py`. Что: prefetch/annotate вместо циклов по объектам; время загрузки вкладки аналитики < 3s.
- [ ] **DB-006. Charset/collation.** Где: MySQL: `SHOW CREATE TABLE` выборочно. Что: utf8mb4 везде (эмодзи в отзывах/поиске не падают).
- [ ] **DB-007. Миграции синхронны с БД.** Где: SSH: `python manage.py makemigrations --check --dry-run`. Что: нет незакоммиченных изменений моделей; все миграции применены (`showmigrations | grep '\[ \]'` пуст).
- [ ] **DB-008. PyMySQL vs mysqlclient.** Где: requirements (PyMySQL), settings. Что: PyMySQL медленнее mysqlclient — зафиксировать как осознанный выбор (ограничение хостинга) или задача на замену.
- [ ] **DB-009. Транзакционность заказа.** Где: checkout.py/monobank.py (transaction.atomic). Что: создание Order+OrderItem+снятие остатков атомарно; конкурентный заказ последнего размера не уводит остаток в минус.
- [ ] **DB-010. Дропшип/опт-контуры.** Где: `orders/dropshipper_views.py`, `wholesale_signals.py`, `warehouse/` app. Что: заказы дропшипперов размечаются отдельно (не загрязняют retail-аналитику); связи выплат/статистики целостны.

---

## ПОРЯДОК ВЫПОЛНЕНИЯ (ПРИОРИТЕТЫ)

1. **P0 — данные (без этого всё остальное слепо):** CRO-041, CRO-042, AN-031, AN-013, AN-035, CRO-050 (acceptance), TD-033. Соответствуют TECH-060…063.
2. **P0 — верификация пикселей:** AN-010…012, AN-020, CRO-045 (TECH-064, TECH-066).
3. **P1 — статусная модель и точность:** TD-030…032, AN-014 (TECH-070…074).
4. **P1 — SEO-критика:** SEO-020 (Product schema), SEO-007, SEO-008, SEO-003.
5. **P2 — CRO/UX:** разделы 1.1–1.5 по порядку; замер CRO-051 до/после.
6. **P2 — тех. долг и AEO:** TD-001…007, AEO-001…006.

## КАК ФИКСИРОВАТЬ БАГИ

- Найден баг → запись в журнал ниже: `дата | ID пункта | что найдено (факт, шаги воспроизведения) | severity P0–P2`.
- Фикс → отдельная ветка/коммит `fix(ID): описание`, ссылка на PR в журнале, пункт → `[x]`.
- Изменения в БД (миграции) — только после ревью владельцем; на бою сначала бэкап (TD-020).
- Данные для сверки брать ТОЛЬКО из живой MySQL через SSH (read-only), не из локальной SQLite.

## ЖУРНАЛ НАХОДОК И ВЫПОЛНЕНИЯ

| Дата | ID | Находка / что сделано | Severity | Коммит/PR |
|---|---|---|---|---|
| 05.07.2026 | CRO-041 | Подтверждено БД: 0/41 заказов имеют utm_source; checkout.py не вызывает link_order_to_utm | P0 | — |
| 05.07.2026 | CRO-042 | Подтверждено БД: 0/1015 UTM-сессий is_converted | P0 | — |
| 05.07.2026 | TD-033 | Подтверждено БД: 28/28 CustomPrintLead в статусе new | P0 | — |
| 05.07.2026 | AN-032 | БД: utm_source дубли ig/Instagram/IGShopping/Inst_Vid; сырой ad id как source | P1 | — |
| 05.07.2026 | AN-035 | БД: 0/2899 SiteSession помечены is_bot при 36k product_view | P1 | — |
| 05.07.2026 | AEO-001 | БД: 109 UTM-сессий с utm_source=chatgpt.com — AI-канал уже 3-й по объёму | P1 | — |
| 05.07.2026 | TD-030 | БД: статусы заказов только done/cancelled — нет shipped/delivered/RTS | P1 | — |
| 05.07.2026 | SEO-020 | В product_detail.html не найден JSON-LD Product (grep по репо) — требуется подтверждение рендером | P1 | — |
| 05.07.2026 | TD-011 | Redis live-check: cache roundtrip OK на бою | info | — |
