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
