# АУДИТ-ОТЧЁТ · РАЗДЕЛ 1.3 — КАРТОЧКА ТОВАРА (PDP)

**Дата:** 05.07.2026 · **Аудитор:** AI-агент (read-only, код + живая БД MySQL через SSH + живой рендер https://twocomms.shop)
**Связан с чек-листом:** `twocomms_global_audit.md` → Раздел 1.3 (CRO-020…CRO-027)
**Методика:** статический анализ `storefront/views/product.py` (755 строк) и `pages/product_detail.html` (769 строк) + read-only Django shell на бою + curl-сверка живых страниц. Никакие данные не изменялись.

---

## CRO-020. Корректность данных из MySQL ✅ (данные корректны; 4 побочных находки)

### Методика замера

1. Read-only Django shell на сервере: выборка **10 случайных товаров** из `Product.objects.filter(status='published')` с фиксированным seed `random.seed(20260705)` — замер воспроизводим.
2. Для каждого товара из БД сняты: `title` (uk/ru/en), `price`, `discount_percent`, `final_price`, категория, длины описаний (uk/ru/en), наличие `main_image`, `main_image_alt`, число доп. изображений, ID цветовых вариантов (`productcolors.ProductColorVariant`), статус.
3. Живой рендер: curl каждой из 10 страниц `/product/<slug>/` (UA помечен `audit-CRO-020`), извлечены: HTTP-код, `<h1>`, `og:product:price:amount`, `og:product:availability`, видимые цены, число JSON-LD блоков.

### Выборка (seed 20260705, 10 из 65 published)

| ID | slug | Категория | БД price | disc% | БД final | Сайт H1 = title? | og:price | Сходится |
|---|---|---|---|---|---|---|---|---|
| 5 | my-little-baby-hd | Худі | 1850 | 10 | 1665 | ✅ | 1665 | ✅ |
| 8 | where-mi-present-hd | Худі | 1850 | 10 | 1665 | ✅ | 1665 | ✅ |
| 12 | in-shee-ls | Лонгсліви | 1650 | 8 | 1518 | ✅ | 1518 | ✅ |
| 16 | last-breath | Футболки | 950 | 17 | 788 | ✅ | 788 | ✅ |
| 21 | kharkiv-district-ls | Лонгсліви | 1650 | 8 | 1518 | ✅ | 1518 | ✅ |
| 29 | dvoznachni-summy-hd | Худі | 2000 | 10 | 1800 | ✅ | 1800 | ✅ |
| 36 | red-leaves-ls | Лонгсліви | 1650 | 8 | 1518 | ✅ | 1518 | ✅ |
| 43 | kha-style-ts | Футболки | 950 | 17 | 788 | ✅ | 788 | ✅ |
| 49 | bentejne-ts | Футболки | 950 | 17 | 788 | ✅ | 788 | ✅ |
| 92 | 225-hoodie | Худі | 1650 | — | 1650 | ✅ | 1650 | ✅ |

**Вердикт по основному вопросу: 10/10 совпадений.** Название (H1 = `title_uk`), цена со скидкой (`final_price`), старая цена (`price`), категория и цветовые варианты на живых страницах полностью соответствуют БД. Все 10 страниц — HTTP 200, редиректов нет. `og:availability=instock` на всех (ожидаемо: механики склада нет, см. CRO-014).

### Побочные находки (важно для исполнителя)

**НАХОДКА 1 (уточнение базовой линии, info).** Published-товаров **65**, а не 68. Базовая линия «68 товаров» в шапке чек-листа считала все статусы (published + archived/draft). Для аудита карточек рабочая цифра — 65.

**НАХОДКА 2 (P2, SEO/i18n).** RU/EN-переводы описаний — заглушки. Во всей выборке `description_uk` = 207–1028 символов, а `description_ru` = 104–143 и `description_en` = 114–170 символов — т.е. RU/EN версии в 2–8 раз короче украинской и выглядят как однотипный короткий шаблон (~120–130 символов у всех товаров). Это прямо влияет на SEO-004 (hreflang: страницы /ru/ /en/ формально существуют, но контент тонкий → риск thin content / soft-дубли). Задача исполнителю: выборочно вычитать `description_ru/en` (10 товаров хватит для подтверждения шаблонности) и решить — либо полноценный перевод, либо пересмотр hreflang-стратегии.

**НАХОДКА 3 (P1→в копилку CRO-024, двойной product_view на legacy-URL).** В `product.py`: `record_product_view(request, ...)` вызывается на **строке 247**, а 301-редирект с legacy query-URL (`?size=M&color=123`) на path-URL строится **позже** (строки 323–330, `_build_path_variant_redirect`). Следствие: заход по старой ссылке `?size=m` записывает product_view → 301 → целевая страница записывает **второй** product_view. Один клик = 2 события. Это один из механизмов завышения 36 009 product_view. Фикс тривиален: перенести `record_product_view` ПОСЛЕ ветки редиректа. Полная квантификация — в CRO-024.

**НАХОДКА 4 (info, опровержение гипотезы SEO-020).** На живых карточках **5 блоков `application/ld+json`** (все 10 страниц). Гипотеза чек-листа «в product_detail.html нет JSON-LD» опровергнута рендером: разметка инжектится не из шаблона product_detail.html напрямую (потому grep по шаблону её не видел), а из base.html/контекст-процессора/`seo_utils.StructuredDataGenerator`. Для SEO-020 задача меняется с «добавить schema» на «**провалидировать** существующие 5 блоков (Product+Offer? Breadcrumb? Organization?) через Rich Results Test». Детали — в SEO-020.

**НАХОДКА 5 (info, галерея).** У 7 из 10 товаров `extra_images = 0` — галерея PDP живёт почти целиком на изображениях цветовых вариантов (`ProductColorVariant.images`). `main_image` есть у 10/10, `main_image_alt` заполнен у 10/10 (осмысленный, с названием товара — не пустой и не дублирующийся в выборке). Это соответствует логике `auto_select_first_color` в product.py:371.

**НАХОДКА 6 (info, ценовая политика).** Скидки строго шаблонные по категориям: худі 10%, лонгсліви 8%, футболки 17% (кроме коллаб-товара 225-hoodie без скидки). Футболки имеют максимальный процент скидки — ещё один сигнал «выпячивания футболок» вопреки позиционированию (связка с CRO-013/CRO-001).

### Что проверено и НЕ является проблемой

- select_related/prefetch_related в запросе PDP покрывают category, catalog, size_grid, images, fit_options, faqs (product.py:236–246) — N+1 на основной выборке нет.
- 301-редирект с legacy `?size/color/fit` сохраняет utm_*/gclid/fbclid параметры (product.py:179–195) — атрибуция при редиректе не теряется.
- Невалидные query-параметры (`?color=junk`) не роняют страницу — тихо игнорируются (product.py:168–171).
- Path-вариант URL (`/product/<slug>/<color>/<size>/`) 404-ит неизвестные сегменты — мусорные URL не индексируются как 200.

### Задачи исполнителю (из CRO-020)

| # | Задача | Приоритет |
|---|---|---|
| 1 | Перенести `record_product_view` после ветки 301-редиректа в product.py (двойной счёт) | P1 |
| 2 | Аудит и наполнение `description_ru/en` (заглушки ~120 символов) или пересмотр hreflang | P2 |
| 3 | Обновить базовую линию: published-товаров 65 (не 68) | info |
| 4 | SEO-020 переформулировать: валидация 5 существующих JSON-LD блоков, а не добавление | info |

---

## CRO-023. Размерная сетка с карточки ✅ (аудит 05.07.2026; UI есть, но у ОСНОВНОГО продукта — лонгсливов — сетки с замерами НЕТ вообще)

### Методика замера

1. Полный разбор `storefront/services/size_guides.py` (пресеты, резолвинг источника сетки, фолбэки).
2. Разбор `pages/product_detail.html` (все точки входа в size guide на PDP).
3. Read-only Django shell на бою (одна батч-SSH-сессия): инвентаризация `SizeGrid`, привязок `Product.size_grid` / `Product.catalog` по категориям, `Catalog.size_grids`.
4. Живой curl-рендер PDP лонгсливов (`bentejne-ls`, `last-breath-ls`, `red-leaves-ls`, `longsleeve-limited-edition`), худи (`225-hoodie`) и страницы `/rozmirna-sitka/`.
5. Grep всего репозитория (py/js/html) на событие `view_size_guide`.

### Как устроена система (для исполнителя)

Резолвинг сетки на PDP (`resolve_product_size_guide`, size_guides.py) идёт каскадом:
`Product.size_grid` (product_override) → `Product.catalog.size_grids` (catalog_default) → пресет по alias-детекту названия/категории (`SIZE_GUIDE_PRESETS`) → **глобальный фолбэк без таблицы** («Підбір розміру» + совет «напишіть нам»). Точки входа на PDP: таб «Розмірна сітка» (`data-pdp-tab="size"`, product_detail.html:406), ссылка «Як обрати розмір?» (:317), CTA внутри карточки гида (:549), trust-link в мобильной покупочной панели (:647) — все ведут на `/rozmirna-sitka/` (`urls.py:520`, view `static_pages.size_guide`).

### ГЛАВНАЯ НАХОДКА (P1, конверсионная): у всех 19 лонгсливов НЕТ размерной таблицы в см

Подтверждено тремя независимыми срезами:

1. **Код:** в `SIZE_GUIDE_PRESETS` ровно 2 пресета — `hoodie` и `basic_tshirt`. Пресета для лонгсливов НЕ СУЩЕСТВУЕТ; ни один alias («лонгслів», «long sleeve», «longsleeve», «ls») нигде в size_guides.py не встречается (grep по «long|лонг» — 0 совпадений).
2. **Живая БД:** `SizeGrid` всего 3 — id=2 «Худі» (catalog 2), id=3 «Basic tee» (catalog 3) и id=1 **«Test Catalog - Hoodie»** (тестовый артефакт в проде, guide_data пустой). Привязки по категориям (total / with size_grid / with catalog): **long-sleeve 19 / 0 / 0**, tshirts 24 / 0 / 17, hoodie 25 / 0 / 20. То есть НИ ОДИН товар не имеет product_override, лонгсливы не имеют даже catalog.
3. **Живой рендер:** все 4 проверенных PDP лонгсливов отдают глобальный фолбэк `tc-guide-title">Підбір розміру` / `eyebrow">Fit guide` — ни таблицы (`tc-size-table` = 0), ни изображения. Для контраста: `/product/225-hoodie/` отдаёт полную таблицу («Hoodie fit guide», 8 ячеек tc-size-table, замеры Length/Width в см).

**Бизнес-эффект:** лонгслив — заявленный основной продукт бренда, а покупатель на его карточке получает вместо замеров «звіряйтеся зі своєю річчю або напишіть нам». Это прямой конверсионный тормоз (сомнение в размере = главный барьер покупки одежды онлайн) и рассинхрон с позиционированием (связка с CRO-001/CRO-013).

**Каскадные следствия:**
- Глобальная страница `/rozmirna-sitka/` строится через `build_public_size_guide_blocks`, которая жёстко перебирает только `("hoodie", "basic_tshirt")` → лонгсливов нет и там (live-проверка: 5 упоминаний «худі», 3 «футболк», 0 «лонгслів» в контенте). Покупатель лонгслива не найдёт замеры НИГДЕ на сайте.
- 5 худи и 7 футболок без `catalog` тоже падают в фолбэк (не только лонгсливы страдают).
- `resolve_product_sizes` для лонгсливов возвращает захардкоженный `DEFAULT_SIZE_SETS["default"]` = S–XXL — размеры на PDP показываются «из воздуха», без связи с реальным ассортиментом.

**Рецепт фикса (для исполнителя):** (а) создать `SizeGrid` «Лонгслів» с guide_data (columns/rows с реальными замерами от владельца) + пресет `longsleeve` с aliases `['лонгслів','лонгслив','long sleeve','longsleeve','long-sleeve']` в SIZE_GUIDE_PRESETS + добавить ключ в перебор `build_public_size_guide_blocks`; (б) создать Catalog «Лонгсліви» и привязать 19 товаров (+ добрать 5 худи и 7 футболок без catalog); (в) удалить тестовый SizeGrid id=1 «Test Catalog - Hoodie» из прода. Требуются реальные замеры лонгсливов от владельца — без них таблицу не заполнить.

### НАХОДКА 2 (подтверждение чек-листа): событие `view_size_guide` не существует — TECH-005 актуальна

Grep по всему репо (py/js/html): 0 вхождений `view_size_guide`. В `UserAction.ACTION_TYPES` (models.py:2042) его нет (26 типов, size-guide среди них отсутствует). Открытие таба `data-pdp-tab="size"` и клики по 3 ссылкам на `/rozmirna-sitka/` не трекаются ни серверно, ни в dataLayer. Задача TECH-005: добавить action_type `view_size_guide` + JS-пуш при первом открытии таба и клике по ссылкам (с product_id в metadata) — это даст сигнал «сомневается в размере» для ремаркетинга и приоритизации фикса из находки 1.

### НАХОДКА 3 (P1, СЕО/UX, побочная — битые внутренние ссылки): SEO-блок «Найкращі ціни» ссылается на 404

Обнаружено при live-обходе каталога: на страницах `/catalog/long-sleeve/`, `/catalog/hoodie/`, `/catalog/tshirts/` блок `seo-pricing` (таблица «Найкращі ціни на …») содержит по 8 ссылок вида `/product/{id}/` (например `/product/33/` «Лонгслів „Це Моя Посадка"») — **все 24 отдают HTTP 404** (роутинг принимает только slug: `product/<slug:slug>/`, numeric-id маршрута/редиректа нет).

**Root cause (точный):** сид-миграция `storefront/migrations/0053_phase10b_seed_category_seo.py:384` записала в `CategorySeoBlockItem.url` значение `f"/product/{p.id}/"` с комментарием «view will rewrite to slug-based URL on render via Product hydration» — но обещанный rewrite так и не был реализован: сервис `category_seo_blocks.py::_hydrate_product_items` привязывает `item.product`, а шаблон `partials/category_seo_blocks.html:42` рендерит `href="{{ item.url|default:'#' }}"` — сырое значение из БД, а не `item.product.get_absolute_url`. Название при этом берётся из `item.product.title` (строка 43) — т.е. гидрация работает, но именно для URL не используется.

**Эффект:** 24 битые внутренние ссылки на 3 ключевых категорийных страницах = слив внутреннего ссылочного веса на 404, плохой сигнал краулерам, тупик для пользователя, кликнувшего по цене. **Фикс — 1 строка шаблона:** `href="{% if item.product %}{{ item.product.get_absolute_url }}{% else %}{{ item.url|default:'#' }}{% endif %}"` (+ опционально data-миграция, переписывающая устаревшие `item.url`).

### Что проверено и НЕ является проблемой

- Для товаров С сеткой (20 худи, 17 футболок через catalog) таблица рендерится корректно: columns/rows/legend/notes из guide_data, `_serialize_image_payload` отдаёт width/height картинки против CLS (Phase 21).
- Нормализация размеров `_normalize_size_value` схлопывает 2XL/XXL/X2L → XXL — дублей размеров не будет.
- Доступность таба: role="tab"/aria-controls/aria-selected на кнопках табов корректны.
- `resolve_product_size_context` вызывается в product-view с prefetch (catalog options/values, size_grids) — N+1 на резолвинге сетки нет.

### Задачи исполнителю (из CRO-023)

| # | Задача | Приоритет |
|---|---|---|
| 1 | Создать SizeGrid + пресет `longsleeve` (замеры — у владельца); привязать 19 лонгсливов к catalog; добавить longsleeve в `build_public_size_guide_blocks` | **P1** |
| 2 | Фикс битых ссылок seo-pricing: шаблон category_seo_blocks.html:42 → `item.product.get_absolute_url` (24 ссылки × 404) | **P1** |
| 3 | Привязать catalog оставшимся 5 худи и 7 футболкам (сейчас фолбэк без таблицы) | P2 |
| 4 | TECH-005: action_type `view_size_guide` + трекинг открытия таба/кликов по size-guide ссылкам | P2 |
| 5 | Удалить тестовый SizeGrid id=1 «Test Catalog - Hoodie» из боевой БД (артефакт) | P3 |

---

## CRO-024. Событие product_view — завышение ✅ (аудит 05.07.2026; 3 root cause найдены в коде, все подтверждаемы построчно)

### Методика замера

1. Построчный разбор всей цепочки записи: `views/product.py::product_detail` → `utm_tracking.py::record_product_view` → `record_user_action` → `analytics_exclusions.py::is_request_excluded`.
2. Сравнительный разбор bot-фильтрации во ВСЕХ трёх слоях трекинга: `utm_middleware.py` (UTM-сессии), `tracking.py` (PageView/SiteSession), `utm_tracking.py` (UserAction).
3. Grep всего репо (js/html) на клиентский пуш `product_view` (проверка двойного сервер+JS счёта).
4. Проверка prefetch/speculation rules в base.html и rum.js.
5. Live-DB срез: **НЕ выполнен** — SSH на бой отдаёт `kex_exchange_identification: Connection reset` (4 попытки с бэкоффом до 2 мин; вероятно fail2ban/rate-limit после предыдущих аудит-сессий). Количественная декомпозиция 36 009 просмотров — отложенная задача (см. задачу 5).

### ROOT CAUSE 1 (P1, главный): `record_user_action` НЕ фильтрует ботов — единственный слой трекинга без bot-фильтра

В проекте ТРИ слоя аналитики, и в двух из них боты отсекаются, а в третьем (том самом, что пишет product_view) — НЕТ:

| Слой | Файл | Bot-фильтр |
|---|---|---|
| UTM-сессии | utm_middleware.py:62 | ✅ `is_bot_user_agent(user_agent)` → skip |
| PageView/SiteSession | tracking.py:35 `is_bot()` + BOT_SIGNALS (30 паттернов, включая curl/wget/lighthouse/headless) | ✅ skip |
| **UserAction (product_view!)** | utm_tracking.py:49–62 | ❌ только `is_request_excluded` (ручной админ-список IP/UA/user) — автоматической bot-проверки НЕТ |

Итог: каждый хит googlebot/bingbot/ahrefs/curl по PDP пишет строку `product_view`. Хуже того — бот не хранит cookies, поэтому строки 56–58 (`request.session.save()`) создают НОВУЮ Django-сессию на КАЖДЫЙ хит бота (побочный эффект: раздувание django_session). Наши собственные аудит-curl'ы по PDP тоже записались как product_view. Косвенное подтверждение масштаба: сайт активно краулится (в BOT_SIGNALS перечислены baiduspider/petalbot/semrush/ahrefs/mj12 — их добавляли не просто так, это «specific crawlers commonly hitting the site» по комментарию в коде).

**Фикс (3 строки):** в начале `record_user_action` добавить `from .utm_utils import is_bot_user_agent` + `if is_bot_user_agent(request.META.get('HTTP_USER_AGENT', '')): return None`. Ещё лучше — использовать более полный `tracking.is_bot` (30 паттернов против 15 в is_bot_user_agent; is_bot ловит yandex/petalbot/lighthouse, is_bot_user_agent — нет). Заодно унифицировать два дублирующихся bot-детектора в один (сейчас `tracking.py::is_bot` и `utm_utils.py::is_bot_user_agent` — разные списки, рассинхрон).

### ROOT CAUSE 2 (P1, подтверждение находки из CRO-018): двойной счёт на 301-редиректе

`record_product_view` вызывается в product.py:247 — ДО ветки Phase 7.5 (строки 319–330), которая 301-редиректит legacy query-string URL (`?size=M&color=123`) на канонический path-URL. Итог: пользователь, пришедший по старой ссылке (email-рассылки, соцсети, закладки), генерирует **2 строки product_view** — одну на редиректе, одну на целевой странице. **Фикс:** перенести вызов `record_product_view` после строки 330 (после ветки `return HttpResponsePermanentRedirect`).

### ROOT CAUSE 3 (P2): нет дедупликации перезагрузок/повторных заходов

Никакого окна дедупликации (та же сессия + тот же товар в течение N минут = 1 просмотр) нет — F5, возврат «назад» из корзины, переключение цветовых path-URL вариантов (`/product/x/black/` → `/product/x/white/` — каждый вариант это отдельный GET того же product_detail) пишут отдельные строки. GA4-стандарт — считать view per session per product. **Фикс:** перед `UserAction.objects.create` проверка «existing за последние 30 мин с тем же site_session+product_id» (индекс по (action_type, site_session, product_id, created_at) уже частично покрывается, проверить).

### Что проверено и НЕ является проблемой

- **Двойного сервер+JS счёта НЕТ:** grep по всем js/html — клиентский пуш `product_view` отсутствует, запись только серверная (единственные вхождения — вывод статистики в admin_dispatcher_section.html).
- **Prefetch/prerender НЕ триггерит:** в base.html только `dns-prefetch` (не загружает страницы); speculation rules не используются; rum.js явно гвардит prerender.
- **AJAX-эндпоинтов, дёргающих product_detail, нет** — view вызывается только полноценной загрузкой PDP.
- `is_request_excluded` корректно отсекает админ-офис/staff (ручной список), кэш снапшота 30 сек — оверхед незначителен.

### Ответ на вопрос чек-листа «36 009 product_view против 44 add_to_cart»

Соотношение ~818:1 объясняется комбинацией: (а) боты — главный вклад, механизм доказан root cause 1; (б) двойной счёт редиректов — root cause 2; (в) отсутствие дедупа — root cause 3; (г) add_to_cart при этом фильтруется тем же нефильтрующим слоем, но боты корзиной не пользуются — поэтому знаменатель «чистый», а числитель «грязный». Точная декомпозиция (доля строк без site_session = бот-прокси, т.к. SiteSession ботам не создаётся) — после восстановления SSH-доступа.

### Задачи исполнителю (из CRO-024)

| # | Задача | Приоритет |
|---|---|---|
| 1 | Добавить bot-фильтр в `record_user_action` (utm_tracking.py, ~3 строки) — закрывает главный канал завышения + попутно останавливает создание django_session на каждый бот-хит | **P1** |
| 2 | Перенести `record_product_view` после 301-ветки (product.py:247 → после :330) — двойной счёт legacy-URL | **P1** |
| 3 | Унифицировать 2 дублирующихся bot-детектора (`tracking.is_bot` 30 паттернов vs `utm_utils.is_bot_user_agent` 15) в один модуль | P2 |
| 4 | Дедуп product_view: окно 30 мин на (site_session, product_id) | P2 |
| 5 | После фиксов — очистить историю: пометить/удалить старые бот-строки (эвристика: site_session IS NULL) и перезамерить воронку | P2 |

---

## CRO-025. Выбор цвета/размера — UX и трекинг ✅ (аудит 05.07.2026; ядро UX работает, но цена НЕ обновляется при выборе цвета + размеры не блокируются по стоку)

### Методика замера

1. Построчный разбор `product-detail.js` (1237 строк): `initColorSelection`, `initSizeSelection`, `currentSelection`, `updateCurrentOfferId`, `trackCustomizeProduct`, `trackViewContent`.
2. Разбор `product-variant-history.js` (161 строка, Phase 8) — синхронизация URL/title/canonical.
3. Разбор модели `productcolors/models.py::ProductColorVariant` (stock, price_override, slug) и всех потребителей `price_override` (grep по репо).
4. Проверка серверной стороны: `views/cart.py` (какая цена реально попадает в корзину), ACTION_TYPES в models.py.
5. Grep на `select_size`/`select_color` (TECH-008) по всему репо.
6. Live-DB срез недоступен (SSH connection reset — тот же rate-limit, что в CRO-024).

### Что РАБОТАЕТ корректно (ядро UX)

- **Смена цвета без перезагрузки:** клик по свотчу → `initColorSelection` обновляет active-класс, `dataset.currentVariant`, пересобирает миниатюры (`renderThumbnails`), меняет главное фото (`setMainImage` с preload), пересчитывает offerId (`updateCurrentOfferId` через карту `state.offerIdMap[colorVariant:size]`).
- **Phase 8 URL-sync — образцовый:** `product-variant-history.js` строит path-URL `/product/<slug>/<color>/<size>/<fit>/` через `history.replaceState` (не pushState — нет мусорных back-записей), обновляет `document.title` и `<link rel=canonical>` по стратегии Phase 7.3 (0–1 сегмент → self, 2+ → base). SEO и UX не конфликтуют.
- **`?size=` из URL:** `selectSizeFromURL` корректно проставляет размер из legacy query-string и диспатчит `change` (совместимость со старыми ссылками).
- **Meta CustomizeProduct трекается** на выбор и цвета, и размера (variant_id, size, value, currency) — с очередью-стабом `window._trackEventQueue` из base.html (FIX 2026-06-12), т.е. ранние клики не теряются.
- **Двойного счёта view_item нет**, dataLayer-push не зависит от загрузки analytics-loader.

### НАХОДКА 1 (P2→P1 при активации price_override): цена на PDP НЕ обновляется при смене цвета

В модели `ProductColorVariant` есть `price_override` («Ціна для варіанту (грн)»), он редактируется в product_builder и **уже используется в marketplace_feeds.py:782** (фиды отдают вариантные цены). Но на PDP: (а) в JS-обработчике смены цвета нет НИКАКОГО обновления DOM-цены (grep «updatePrice|priceEl» = 0 вхождений — цена рендерится сервером в `.tc-current-price` и больше не трогается); (б) в `trackCustomizeProduct` и `view_item` цена всегда берётся из `payload.dataset.price` — статичного атрибута базового товара. **Сейчас риск скрыт**, только если ни у одного варианта price_override не заполнен (live-проверка отложена из-за SSH). Но серверная корзина (`cart.py:560` и ещё 8 мест) тоже игнорирует price_override — комментарий в коде прямо признаёт: «Цена всегда берется из Product». Итог: если админ заполнит вариантную цену — фиды покажут одну цену, PDP/корзина/заказ возьмут другую → расхождение фид↔сайт (риск отклонения в Merchant Center) и недополученная выручка. **Решение:** либо (а) довести price_override до PDP+корзины (JS-пересчёт из offerMap + cart.py), либо (б) осознанно задокументировать «вариантные цены только для фидов» и скрыть поле из product_builder.

### НАХОДКА 2 (P2, UX): размеры/цвета НЕ блокируются по остаткам

У `ProductColorVariant` есть поле `stock` («Залишок»), но в шаблоне PDP grep «stock|disabled» в блоках выбора = 0: все размеры S–XXL всегда кликабельны, все свотчи всегда активны, независимо от остатков. Покупатель может выбрать распроданную комбинацию, добавить в корзину и узнать о недоступности только от менеджера (в cart.py валидации стока при добавлении тоже нет). Это классический генератор отменённых заказов и разочарования. **Решение:** прокинуть stock в offerMap (JSON уже есть на странице), дизейблить радио-кнопки размеров с классом «нет в наличии» + бейдж; MVP — хотя бы для вариантов с ненулевым складским учётом (warehouse-модуль уже ведёт остатки).

### НАХОДКА 3 (подтверждение чек-листа): событий select_size/select_color НЕТ — TECH-008 актуальна

Grep по репо: `select_size`/`select_color` = 0 вхождений (только внутренняя функция `selectSizeFromURL`). В `UserAction.ACTION_TYPES` их нет. GA4-события `select_item`-типа не пушатся в dataLayer при выборе цвета/размера — уходит только Meta CustomizeProduct. Итог: в GA4 невозможно построить воронку «посмотрел → выбрал вариант → в корзину» и увидеть, какие цвета/размеры выбирают чаще (вход для решения о допечатке). Задача TECH-008: dataLayer-push `select_color`/`select_size` (item_id, variant, size) в тех же обработчиках, где уже стоит trackCustomizeProduct, — ~10 строк.

### Мелкие замечания (P3)

- Свотчи цвета — `<button>` без `aria-pressed`/`role=radiogroup`: скринридер не сообщает выбранное состояние (класс active — только визуальный). Размеры сделаны правильно (нативные radio).
- `trackCustomizeProduct` имеет guard `if (!window.trackEvent) return` (строка 1118) — но стаб из base.html определяет trackEvent сразу, так что клики не теряются; guard мёртвый, можно убрать.

### Задачи исполнителю (из CRO-025)

| # | Задача | Приоритет |
|---|---|---|
| 1 | Решить судьбу `price_override`: довести до PDP+cart.py (9 мест) ЛИБО задокументировать «только фиды» и скрыть из билдера; сейчас три поверхности (фид/PDP/корзина) могут отдавать разные цены | **P2 (P1 если заполнен хоть у одного варианта)** |
| 2 | Блокировка размеров/цветов по stock: прокинуть остатки в offerMap, дизейбл радио + валидация в cart.py при добавлении | P2 |
| 3 | TECH-008: dataLayer `select_color`/`select_size` в существующих обработчиках (~10 строк) | P2 |
| 4 | a11y свотчей: `role="radiogroup"` + `aria-checked` (или переделать на нативные radio, как размеры) | P3 |
| 5 | Live-проверка после восстановления SSH: `ProductColorVariant.objects.exclude(price_override=None).count()` — если >0, задача 1 становится P1 | P3 |
