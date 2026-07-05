# TWOCOMMS — ГЛОБАЛЬНЫЙ АУДИТ-ЧЕКЛИСТ (twocomms_global_audit.md)

**Дата создания:** 05.07.2026 · **Версия:** 2.0 (итерация 2 — глубокий аудит кодовой базы + матрица рисков)
**Автор анализа:** AI-архитектор (глубокий reverse engineering репозитория + живой БД MySQL на сервере; итерация 2 — статический аудит всей кодовой базы, скриптов, статики, гигиены репозитория)
**Назначение:** исчерпывающий чек-лист (150+ атомарных пунктов) для агентов-исполнителей. Здесь НИЧЕГО не исправлено — только анализ и задачи.
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

## ФАКТЫ, ПОДТВЕРЖДЁННЫЕ СТАТИЧЕСКИМ АУДИТОМ КОДА (итерация 2, замер 05.07.2026)

Все цифры получены прямыми замерами по репозиторию (wc/grep/du/git ls-files). Это базовая линия для оптимизации.

| Метрика | Значение | Вывод |
|---|---|---|
| Размер репозитория (без .git) | **328 MB** | скриншоты/артефакты/бинарники в git раздувают клон |
| Файлов-артефактов в git (`artifacts/`, `output/`, `tmp/`) | **132** | PNG-скриншоты и lighthouse-JSON закоммичены — мусор |
| .md-отчётов в корне репо | **202** (+4 .txt) | корень нечитаем; история фиксов вместо docs/archive |
| Loose-скриптов в корне | **30 .py + 19 .sh** | 6+ конкурирующих deploy-скриптов, никто не знает какой рабочий |
| Backup-файлов, отслеживаемых git | 4 (`views.py.backup` 7790 строк, `styles.css.bak2` 445KB, `order_success_old.html`, `tmp_old_index.html`) | мёртвый код в git |
| Широких `except Exception`/`except: pass` | **697** | ошибки массово глотаются молча — отладка вслепую |
| `print()` в боевом Python-коде | **120** | вместо logging; засоряет stdout Passenger |
| TODO/FIXME/HACK в коде | 29 | зафиксированный, но не оттреканный долг |
| Крупнейший файл | `management/views.py` — **8188 строк** | god-file; +3270 строк `storefront/views/admin.py`, 2935 `storefront/models.py` |
| Middleware в цепочке | **26** | каждый запрос проходит 26 слоёв; порядок хрупкий |
| `@csrf_exempt` в коде | **39** | каждое место = потенциальная дыра CSRF |
| `mark_safe` / `\|safe` в шаблонах | 24 / 17 | точки XSS-риска при генерируемом контенте |
| Миграций суммарно | 264 (storefront 74, management 74, orders 46, accounts 28…) | долгие деплой-миграции; кандидаты на squash |
| CSS суммарно | 3.5 MB: `styles.css` **565KB**, `management.css` 234KB, `custom-print-configurator.css` 160KB, `finance.css` 141KB | не минифицировано, монолиты |
| JS суммарно | 1.0 MB: `custom-print-configurator.js` 148KB, `main.js` 83KB, `analytics-loader.js` 56KB | + **56 console.log** в бою |
| Дублирующиеся static-директории | `static/img/` 12MB и `static/images/` 6.3MB | два соглашения, дубли ассетов |
| Inline `<script>` в base.html | 16 (файл 1403 строки) | блокирующий инлайн-JS, кэш не работает |
| requirements.txt | 81 строка, **3 без пина версии** (openai, google-auth, google-analytics-data) | недетерминированный деплой |
| Тестовых файлов | 177 (почти все — finance/management) | storefront/orders (деньги!) почти без тестов |
| SSH-доступ к серверу | сервер сбрасывает частые подключения (`kex_exchange_identification: reset`) | rate-limit: автоматизация должна батчить команды в 1 сессию |

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
- [ ] **CRO-027. Рекомендации на карточке.** Где: `storefront/recommendations.py`. Что: бло�� «с этим покупают/похожие» не делает N+1 и не рекомендует out-of-stock.

### 1.4 Мини-корзина и корзина

- [ ] **CRO-030. Логика мини-корзины vs full-page корзины.** Где: `storefront/views/cart.py` (1850 строк — кандидат на декомпозицию), `accounts/cart_middleware.py`, `cart_models.py`, `cart_signals.py`, `cart_sync.py`, `pages/cart.html`. Что: описать в отчёте фактическую архитектуру (session-корзина + синхронизация в БД для залогиненных); проверить сценарии: добавил анонимно → залогинился → корзина слилась без дублей.
- [ ] **CRO-031. Баги добавления/удаления.** Где: cart.py (AJAX-эндпоинты), JS в base.html/cart.html. Что: быстрый двойной клик «добавить» не создаёт 2 позиции; удаление последней позиции корректно обнуляет счётчик в шапке; смена количества пересчитывает сумму и промокод.
- [ ] **CRO-032. Кэширование не ломает мини-корзину.** Где: `twocomms/cache_headers.py`, `twocomms/media_cache_middleware.py`, `cache_utils.py`, `storefront/cache_signals.py`. Что: страницы с корзиной/счётчиком не отдаются из полного page-cache другому пользователю; заголовки Cache-Control на HTML с динамикой = private/no-cache; счётчик корзины подтягивается AJAX-ом после загрузки кэшированной страницы.
- [ ] **CRO-033. Событие add_to_cart — сервер + пиксели.** Где: cart.py (вызовы `record_add_to_cart`), `static` JS (fbq/ttq AddToCart). Что: одно добавление = ровно одно серверное UserAction + одно событие в каждый пиксель; протестировать вживую через Meta Test Events / TikTok Test Events.
- [ ] **CRO-034. Кастом-принт позиции в корзине.** Где: `storefront/custom_print_config.py` (SESSION_CUSTOM_CART_KEY), checkout.py (split approved/pending). Что: pending-кастом не блокирует оформление обычных товаров; UI корзины ясно объясняет, почему кастом «ждёт».
- [ ] **CRO-035. Восстановление корзины.** Где: cart_middleware.py, отчёты `CART_RESTORATION_REPORT.md` (корень репо). Что: корзина переживает закрытие браузера для залогиненных; отчёт в корне числит фиксы «сделанными» — проверить фактически.

### 1.5 Чекаут

- [ ] **CRO-040. Полный прогон COD-чекаута.** Где: `storefront/views/checkout.py::create_order`, https://twocomms.shop/ (тестовый заказ с пометкой). Что: минимальный набор полей, валидация телефона (`normalize_checkout_phone`), Нова Пошта подбор отделени�� (`orders/nova_poshta_*`); 0 JS-ошибок в консоли на всём пути.
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

- [x] **TD-020. Бэкапы MySQL.** ✅ Аудит 05.07.2026: **P0 — регулярных бэкапов НЕТ**, внедрение — исполнителю → `audit_report_section3_techdebt.md` Где: сервер (cron владельца/hostsila-панель). Что: расписание дампов; тест восстановления на копии (TECH-042); дампы не лежат в web-доступной папке.
- [ ] **TD-021. Секреты не в репозитории.** Где: весь репо. Что: `git log -p | grep -iE "(password|secret|token)"` выборочно; `.env*` в .gitignore; в settings.py нет захардкоженных ключей (SECRET_KEY, monobank token, FB access token — только из env); pixel ID в base.html — публичные, ок.
- [ ] **TD-022. Мониторинг 5xx и JS-ошибок.** Где: нет системы (проверить). Что: внедрить минимум: серверный error-лог → телеграм-алерт (телеграм-бот уже есть: `accounts/telegram_bot.py`, `orders/telegram_notifications.py`) — TECH-041.
- [ ] **TD-023. Rate limiting.** Где: django-ratelimit в requirements. Что: реально ли навешан на чекаут/логин/API (grep `@ratelimit`); если нет — тех. долг: пакет установлен, но н�� используется.
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
- [ ] **SEO-006. Битые внутренние ссылки и 410.** Где: `404.html`, `410.html`, GSC (доступ у владел��ца), краулинг site (screaming frog/линк-чекер по sitemap). Что: внутренних 404 нет; удалённые товары отдают 410 или redirect на категорию.
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
- [ ] **AEO-005. «С 2014» и ложные факты.** Где: все шаблоны + БД-тексты (grep «2014»). Что: исправить историю бренда ��а корректную (TECH-034).
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

## РАЗДЕЛ 6. КОДОВАЯ БАЗА — ГЛУБОКИЙ АУДИТ (ИТЕРАЦИЯ 2)

### 6.1 Гигиена репозитория

- [ ] **CB-001. Артефакты и скриншоты в git (132 файла).** Где: `artifacts/`, `output/`, `tmp/`, `opros/win-*.png`, `newCatalog/*.png`, `BrandDNA/me.JPG` (личное фото!). Что: `git rm -r --cached` + добавить в .gitignore; репо 328MB → цель < 100MB. НЕ трогать `git filter-repo` без согласования (перепишет историю — сломает клоны у всех).
- [ ] **CB-002. 202 md-отчёта в корне.** Где: корень репо (`CART_*`, `META_PIXEL_*`, `GTM_*`, `MODAL_*` и т.д.). Что: одним PR перенести в `docs/archive/2025/` и `docs/archive/2026/`; в корне оставить README.md, DEPLOY.md, CHANGELOG.md. Перед переносом grep по коду на ссылки на эти файлы (их не должно быть).
- [ ] **CB-003. 49 loose-скриптов в корне.** Где: корень: `fix_all.py`, `fix_ui.py`, `fix_telegram.py`, `fix_fabrics.py`, `generate_assets*.py`, `analyze_*.py`, `check_*.py/.sh`, `crawl_all_pages.py`, `optimize_images.py`, `deploy*.sh` (6 штук!), `deploy_paramiko.py`. Что: инвентаризация «жив/мёртв»: (а) какой deploy-скрипт реально используется (сверить с процессом владельца), остальные → `scripts/archive/`; (б) одноразовые fix_* — удалить; (в) полезные (crawl, optimize_images) → `scripts/` с README. ВАЖНО: сначала проверить crontab на сервере — некоторые могут дергаться по cron (SSH-проверка обязательна до удаления).
- [ ] **CB-004. Backup-файлы под git.** Где: `twocomms/storefront/views.py.backup` (7790 строк), `static/css/styles.css.bak2` (445KB), `pages/order_success_old.html`, `tmp_old_index.html`. Что: grep на импорты/include каждого → git rm. Дублирует TD-001/TD-002 — закрыть одним PR.
- [ ] **CB-005. xlsx с закупочными ценами в публичном репо.** Где: `twocomms/wholesale_prices.xlsx`, `twocomms/Оптові ціни*.xlsx`, `pricelist.html`. Что: если репо публичный или станет публичным — коммерческая утечка; вынести из git, хранить в приватном хранилище.
- [ ] **CB-006. .gitignore аудит.** Где: `.gitignore` (3.9KB). Что: покрывает ли `artifacts/`, `output/`, `tmp/`, `*.bak*`, `*.backup`, `*.log`, `*.xlsx`; `.env*` игнорируется (подтверждено: .env.development.local не в индексе) — оставить как есть.
- [ ] **CB-007. Каталоги-сироты.** Где: `Promt/`, `Ideas/`, `opros/`, `newCatalog/`, `BrandDNA/`, `.claude/`, `.codex/`, `.cursor/`, `.kiro/`, `.serena/`, `.zenflow/`, `.superpowers/` (конфиги 7 разных AI-инструментов). Что: решить владельцем, какие AI-конфиги живы; остальное удалить или в docs.

### 6.2 Мёртвый и дублирующийся код

- [ ] **CB-010. Кандидаты в мёртвые сервисы.** Где: `storefront/services/product_copy_v2.py`, `services/color_seo_copy.py` — из боевого кода их использует только management-команда `recraft_product_seo.py` и офлайн-скрипты `scripts/fill_translations.py`. Что: подтвердить, что не импортируются из views/signals; пометить как offline-tooling (переместить в scripts/) либо удалить.
- [ ] **CB-011. scripts/ внутри twocomms.** Где: `twocomms/scripts/fill_translations.py` (**3706 строк** — 2-й по величине файл проекта!), `wrap_themes_lazy.py`. Что: это одноразовые фазовые скрипты (Phase 17) — проверить, завершена ли фаза; если да — в архив.
- [x] **CB-012. Дублирование настроек.** ✅ Аудит 05.07.2026: боевой = `twocomms.production_settings` → `audit_report_section6_codebase.md` Где: `twocomms/settings.py` (1000+ строк) vs `twocomms/production_settings.py` (переопределяет DEBUG/SECRET_KEY/логирование). Что: выяснить, какой модуль реально указан в `passenger_wsgi.py`/env `DJANGO_SETTINGS_MODULE` на сервере; двойная точка правды = риск «поправили не тот файл»; свести к settings.py + env-переменным.
- [ ] **CB-013. Дублирующиеся static-директории img/ и images/.** Где: `static/img/` (12MB), `static/images/` (6.3MB). Что: карта использования (grep по шаблонам/CSS на `/static/img/` vs `/static/images/`); свести к одной; удалить неиспользуемые исходники (для многих PNG уже есть webp-версии рядом — PNG-оригиналы, вероятно, не отдаются).
- [ ] **CB-014. dtf переопределяет collectstatic.** Где: `dtf/management/commands/collectstatic.py` + комментарий в INSTALLED_APPS «DTF app first to override collectstatic». Что: глобальный побочный эффект: ЛЮБОЙ collectstatic проходит через dtf-логику; проверить, что это не ломает деплой других приложений и задокументировать; рассмотреть переименование команды в `collectstatic_dtf`.
- [ ] **CB-015. Мёртвые management-команды.** Где: 60+ команд в `*/management/commands/`. Что: сверить с crontab сервера: команды, которые не в cron и не в доках → кандидаты на удаление (например, `finance_seed_demo`, `notify_test_shops`, `parser_recovery_dry_run`); составить таблицу «команда → где вызывается → вердикт».
- [ ] **CB-016. Пакеты-призраки в requirements.** Где: `requirements.txt` (81 строка). Что: для каждого пакета grep на импорт: кандидаты на неиспользуемые (django-ratelimit — TD-023 подтверждает 0 использований; google-analytics-data — AN-040). Удалять только после grep по ВСЕМУ коду включая scripts/.

### 6.3 Качество кода (обработка ошибок, логирование, размер)

- [ ] **CB-020. 697 широких except.** Где: весь Python-код (grep `except Exception`/`except:`). Что: НЕ чинить массово. Приоритетно исправить в местах, где глотаются ошибки денег/данных: `orders/` (чекаут, вебхуки, CAPI), `storefront/views/checkout*.py`, `monobank.py` — каждый except должен минимум `logger.exception(...)`. Составить топ-20 самых опасных мест.
- [ ] **CB-021. 120 print() в бою.** Где: grep `^\s*print(` по storefront/orders/accounts/twocomms. Что: заменить на `logger.debug/info`; print под Passenger попадает в stderr-лог хостинга без ротации.
- [ ] **CB-022. God-files: план декомпозиции.** Где: `management/views.py` 8188, `management/models.py` 3985, `storefront/views/admin.py` 3270, `storefront/models.py` 2935, `storefront/views/cart.py` 1850, `storefront/views/static_pages.py` 1950. Что: НЕ рефакторить сейчас; зафиксировать план: выделение по доменам (views/admin.py → admin_products.py + admin_orders.py + admin_analytics.py); правило: новый код в новые модули.
- [ ] **CB-023. 56 console.log в боевом JS.** Где: `static/js/*.js` (grep console.log). Что: обернуть в debug-флаг или вырезать при минификации; проверить, что не логируются PII (телефоны из форм чекаута).
- [ ] **CB-024. Тестовое покрытие перекошено.** Где: 177 тест-файлов, из них почти все — finance/management; для checkout/cart/UTM — тестов почти нет. Что: минимальный набор смок-тестов на деньги: создание COD-заказа, monobank-вебхук (идемпотентность), слияние корзины при логине, link_order_to_utm (после фикса CRO-041). Это страховка ПЕРЕД любыми фиксами воронки.
- [ ] **CB-025. 29 TODO/FIXME.** Где: grep TODO/FIXME/HACK. Что: перенести содержательные в TECHNICAL_TASKS.md с ID, остальные удалить из кода.

### 6.4 Фронтенд-статика (вес страницы)

- [ ] **CB-030. styles.css 565KB — монолит.** Где: `static/css/styles.css` (+ styles.base.css 119KB грузятся вместе?). Что: карта подключений по шаблонам (какие страницы что грузят); PurgeCSS-анализ мёртвых селекторов (доля мёртвого CSS, вероятно, > 60%); минификация через django-compressor реально включена в проде? (`COMPRESS_ENABLED`, `COMPRESS_OFFLINE`).
- [ ] **CB-031. Пошаговый план CSS-диеты.** Где: те же файлы. Что: НЕ переписывать монолит; шаги: (1) включить/проверить минификацию, (2) убрать styles.css.bak2 из git, (3) critical-CSS уже есть (critical-home.min.css) — проверить актуальность, (4) новые страницы — только модульные CSS.
- [ ] **CB-032. custom-print-configurator: 148KB JS + 160KB CSS.** Где: static/js+css. Что: грузятся ли они ТОЛЬКО на /custom-print/ (grep по base.html/шаблонам); если глобально — вынести на страницу.
- [ ] **CB-033. 16 inline-script в base.html.** Где: base.html (1403 строки). Что: инвентаризация каждого блока: что делает, можно ли вынести в файл (кэшируется) или удалить; особо — самописный GTM-загрузчик (см. CRO-004/AN-002).
- [ ] **CB-034. analytics-loader.js 56KB.** Где: static/js/analytics-loader.js. Что: аудит содержимого: какие пиксели/логика внутри, есть ли мёртвые ветки (Celery-эпоха, старые эксперименты ab_testing); дублирует ли GTM-теги (двойной счёт событий — связка с AN-001).
- [ ] **CB-035. Шрифты 1.5MB.** Где: `static/fonts/`. Что: subsetting (только кириллица+латиница), woff2-only, `font-display: swap`; удалить неиспользуемые начертания.
- [ ] **CB-036. vendor/ 1.1MB.** Где: `static/vendor/`. Что: инвентаризация: какие библиотеки, используются ли (grep подключений), нет ли дублей с CDN-подключениями из base.html.

### 6.5 Конфигурация и деплой

- [x] **CB-040. Незапиненные зависимости.** ✅ Аудит 05.07.2026: боевые версии сняты (openai==2.30.0, google-auth==2.52.0, google-analytics-data==0.22.0); пин — исполнителю → `audit_report_section6_codebase.md` Где: requirements.txt: `openai`, `google-auth`, `google-analytics-data` без `==`. Что: запинить текущие боевые версии (узнать через `pip freeze` на сервере в ОДНОЙ SSH-сессии); мажорный апдейт openai молча сломает AI-генерацию.
- [ ] **CB-041. ImageOptimizationMiddleware на shared-хостинге.** Где: `twocomms/image_middleware.py`: ThreadPoolExecutor(2 workers) на КАЖДЫЙ процесс Passenger + PIL-оптимизация на лету. Что: сколько процессов Passenger → сколько тредов суммарно; память PIL при больших PNG; включён ли флаг `IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED` в проде; если да — рассмотреть офлайн-оптимизацию по cron вместо runtime.
- [ ] **CB-042. 26 middleware: порядок задокументировать.** Где: settings.py MIDDLEWARE. Что: цепочка содержит хрупкие зависимости (комментарии «ПОСЛЕ статики!», «ПЕРЕД SimpleAnalyticsMiddleware!»); зафиксировать инварианты порядка в комментарии-шапке; кандидаты на удаление: RequestTraceMiddleware (если X-DTF-Debug не используется), ImageOptimizationMiddleware (если выключен флагом — убрать из цепочки совсем).
- [x] **CB-043. Git-состояние сервера.** ✅ Аудит 05.07.2026 → `audit_report_section6_codebase.md` Где: сервер `~/TWC/TwoComms_Site` (в одной SSH-сессии: `git status`, `git stash list`, `git log -3`). Что: деплой = `git pull` — проверить, нет ли на сервере незакоммиченных правок (правки «на бою» будут конфликтовать с pull и молча теряться).
- [x] **CB-044. Crontab-инвентаризация.** ✅ Аудит 05.07.2026 → `audit_report_section6_codebase.md` Где: сервер `crontab -l` (одна SSH-сессия). Что: полный список задач → таблица «задача → команда → скрипт существует в репо? → лог»; выявить cron-задачи, ссылающиеся на удалённые/переименованные скрипты (тихо падают).
- [ ] **CB-045. Логи сервера и ротация.** Где: `~/TWC/TwoComms_Site/twocomms/*.log`. Что: размер каждого; logrotate; в логах нет секретов/PII; celery.log мёртв — удалить (TD-003).

---

## РАЗДЕЛ 7. МАТРИЦА РИСКОВ ПРИ ПРОВЕДЕНИИ РАБОТ

Каждый агент ОБЯЗАН свериться с этой таблицей перед выполнением пункта. Правило: если действие попадает в риск-класс — сначала митигация, потом действие.

| ID | Риск | Триггер (какие пункты) | Вероятность/Ущерб | Митигация |
|---|---|---|---|---|
| RISK-01 | Удаление «мёртвого» файла, который дергается cron-ом на сервере | CB-003, CB-015, TD-001…007 | Средняя / Высокий (тихий отказ фидов, синков) | СНАЧАЛА CB-044 (crontab-инвентаризация), потом любые удаления |
| RISK-02 | SSH rate-limit: серия подключений блокирует доступ | все SSH-проверки | Подтверждено 05.07.2026 / Средний | Батчить ВСЕ команды в один вызов; пауза ≥ 60s между сессиями; не параллелить |
| RISK-03 | Правка checkout/monobank без тестов ломает приём денег | CRO-040…047, CB-020 | Средняя / Критический | Сначала CB-024 (смок-тесты), фиксы — мелкими PR, тест-заказ после каждого деплоя |
| RISK-04 | Массовая чистка except/print меняет поведение (код полагается на глотание ошибок) | CB-020, CB-021 | Высокая / Средний | Только добавлять логирование, НЕ менять control flow; никаких авто-замен по всему репо |
| RISK-05 | Чистка CSS/JS ломает страницы, стили которых видны только в проде | CB-030…034 | Высокая / Средний | PurgeCSS только с whitelist динамических классов (JS-генерируемые, admin); скриншот-сравнение до/после на 5 ключевых шаблонах |
| RISK-06 | git filter-repo/переписывание истории ломает клоны и сервер | CB-001 | Низкая / Критический | Только `git rm --cached` + .gitignore; filter-repo — отдельное согласованное окно |
| RISK-07 | Миграции на бою без бэкапа | TD-030, CRO-041 (новые поля Order) | Средняя / Критический | TD-020 (бэкап) обязателен перед КАЖДОЙ миграцией; миграции только аддитивные (add column NULL), без drop/rename в том же релизе |
| RISK-08 | Двойная точка правды settings.py vs production_settings.py: фикс не того файла | CB-012, любые правки настроек | Высокая / Высокий | Перед правкой настроек выяснить фактический DJANGO_SETTINGS_MODULE на сервере (одна SSH-сессия) |
| RISK-09 | Правка middleware-порядка ломает UTM/кэш/сессии | CB-042, AN-030…036 | Средняя / Высокий | Порядок менять только с интеграционным тестом «utm → login → заказ»; по одному изменению за раз |
| RISK-10 | Включённый runtime-оптимизатор изображений съедает память shared-хостинга при нагрузке | CB-041 | Средняя / Высокий | Проверить флаг в проде до любых экспериментов с медиа; нагрузочный тест не на бою |
| RISK-11 | Удаление «неиспользуемого» пакета, который импортируется лениво/условно | CB-016 | Средняя / Средний | grep по всем веткам импортов (importlib, try/except ImportError — их 697!); удалять по одному пакету на релиз |
| RISK-12 | Тест-заказы и тест-события загрязняют боевую аналитику | CRO-050, AN-010…021 | Высокая / Средний | Все тест-прогоны с utm_source=audit + пометка заказа; test_event_code для пикселей (AN-015); после аудита — очистка тест-записей по согласованию |
| RISK-13 | Правки текстов/SEO стирают наработанные позиции | AEO-004, SEO-007 | Средняя / Высокий | Перед переписыванием title/description зафиксировать текущие позиции (GSC-экспорт); менять батчами по 10 страниц с контролем через 2 недели |
| RISK-14 | «Оптимизация» дублей img/ vs images/ бьёт по кэшированным URL в GMC-фиде/пикселях | CB-013 | Средняя / Средний | Сначала карта внешних потребителей URL (фиды, og:image), редиректы со старых путей |
| RISK-15 | Секреты утекают в отчёты/коммиты при аудите | все | Низкая / Критический | Перед каждым коммитом `git diff` на пароли/токены/IP; SSH-реквизиты нигде не фиксируются письменно |

---

## ПОРЯДОК ВЫПОЛНЕНИЯ (ПРИОРИТЕТЫ)

0. **P0 — предохранители (делать ПЕРВЫМИ, до любых фиксов):** CB-044 (crontab), CB-043 (git-состояние сервера), CB-012/RISK-08 (какой settings-модуль боевой), TD-020 (бэкап БД), CB-024 (смок-тесты на деньги). Без них любой следующий шаг — игра в рулетку.
1. **P0 — данные (без этого всё остальное слепо):** CRO-041, CRO-042, AN-031, AN-013, AN-035, CRO-050 (acceptance), TD-033. Соответствуют TECH-060…063.
2. **P0 — верификация пикселей:** AN-010…012, AN-020, CRO-045 (TECH-064, TECH-066).
3. **P1 — статусная модель и точность:** TD-030…032, AN-014 (TECH-070…074).
4. **P1 — SEO-критика:** SEO-020 (Product schema), SEO-007, SEO-008, SEO-003.
5. **P1 — быстрый тех. долг с высокой отдачей:** CB-004 (backup-файлы), CB-030/031 (CSS-диета: минификация), CB-032 (изоляция конфигуратора), CB-040 (пин версий), CB-021 (print→logger в orders/).
6. **P2 — CRO/UX:** разделы 1.1–1.5 по порядку; замер CRO-051 до/после.
7. **P2 — гигиена репозитория:** CB-001…003, CB-005…007, CB-013 (по RISK-14), TD-001…007.
8. **P2 — AEO и декомпозиция:** AEO-001…006, CB-022 (план, не исполнение).

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
| 05.07.2026 | CB-001 | 132 файла артефактов (PNG/JSON) в git; репо 328MB без .git; личное фото BrandDNA/me.JPG | P2 | — |
| 05.07.2026 | CB-002 | 202 md-отчёта в корне репозитория | P2 | — |
| 05.07.2026 | CB-003 | 30 py + 19 sh loose-скриптов в корне, из них 6 конкурирующих deploy-скриптов | P1 | — |
| 05.07.2026 | CB-004 | 4 backup-файла под git: views.py.backup (7790 строк), styles.css.bak2 (445KB), order_success_old.html, tmp_old_index.html | P1 | — |
| 05.07.2026 | CB-005 | xlsx с оптовыми/закупочными ценами лежат в репозитории | P1 | — |
| 05.07.2026 | CB-020 | 697 широких except в Python-коде — ошибки глотаются | P1 | — |
| 05.07.2026 | CB-021 | 120 print() в боевом коде вместо logging | P2 | — |
| 05.07.2026 | CB-022 | management/views.py 8188 строк; storefront/views/admin.py 3270; models.py 2935 — god-files | P2 | — |
| 05.07.2026 | CB-023 | 56 console.log в боевом JS | P2 | — |
| 05.07.2026 | CB-024 | 177 тест-файлов, но чекаут/корзина/UTM почти без тестов — деньги не покрыты | P0 | — |
| 05.07.2026 | CB-030 | styles.css 565KB + management.css 234KB + configurator 160KB; CSS суммарно 3.5MB | P1 | — |
| 05.07.2026 | CB-040 | openai/google-auth/google-analytics-data без пина версий в requirements.txt | P1 | — |
| 05.07.2026 | CB-042 | 26 middleware в цепочке с хрупкими зависимостями порядка | P2 | — |
| 05.07.2026 | TD-023 | django-ratelimit установлен, @ratelimit в коде не найден (0 использований) — но есть свой SimpleRateLimitMiddleware | P2 | — |
| 05.07.2026 | RISK-02 | Сервер сбрасывает частые SSH-подключения (kex reset) — rate-limit подтверждён; батчить команды в одну сессию | info | — |
| 05.07.2026 | CB-013 | Дубли static/img (12MB) и static/images (6.3MB) | P2 | — |
| 05.07.2026 | CB-014 | dtf-app глобально переопределяет команду collectstatic | P2 | — |
| 05.07.2026 | CB-044 | Аудит crontab: 7 задач, все скрипты в репо; НЕТ бэкап-cron, НЕТ feed-cron; логи в logs/ и tmp/ | P0 done | audit_report_section6_codebase.md |
| 05.07.2026 | CB-043 | Git сервера: tracked чисто, 10 stash (возможна потерянная работа), untracked диаг-скрипты на бою | P0 done | audit_report_section6_codebase.md |
| 05.07.2026 | CB-012 | Боевой settings = twocomms.production_settings (passenger_wsgi); на сервере .env И .env.production; env-флаг DISABLE_ANALYTICS может отключать UTM-мидлвари — проверить значение | P0 done | audit_report_section6_codebase.md |
| 05.07.2026 | CB-040 | Боевые версии: openai==2.30.0, google-auth==2.52.0, google-analytics-data==0.22.0 — пин исполнителю | P1 done | audit_report_section6_codebase.md |
| 05.07.2026 | TD-020 | **P0: регулярных бэкапов MySQL НЕТ; последний ручной дамп от 24.10 (>8 мес). Блокирует все миграции (RISK-07)** | P0 | audit_report_section3_techdebt.md |
| 05.07.2026 | TD-016 | Ротация django/stderr есть (5 покол.); 8 мёртвых логов; ai_generation.log мёртв с 09.2025, image_optimization.log с 10.2025 | P2 partial | audit_report_section3_techdebt.md |
