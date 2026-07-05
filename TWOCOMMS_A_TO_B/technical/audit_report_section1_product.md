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
| 3 | ��бновить базовую линию: published-товаров 65 (не 68) | info |
| 4 | SEO-020 переформулировать: валидация 5 существующих JSON-LD блоков, а не добавление | info |

---

## CRO-023. Размерная сетка с карточки ⚠️ (доступ есть, но **30 из 65 PDP — без таблицы замеров**; событие `view_size_guide` отсутствует)

**Дата аудита:** 05.07.2026 · **Методика:** статический анализ `storefront/services/size_guides.py` (541 строка), `pages/product_detail.html`, `static/js/product-detail.js`, `storefront/models.py` (ACTION_TYPES) + read-only Django shell на бою (SizeGrid/Product/CatalogOption) + curl-рендер 6 живых PDP + `/rozmirna-sitka/`.

### Как устроена система (фактическая архитектура)

1. **Точки доступа с PDP — есть, целых три:** (а) soft-link «Розмірна сітка» → `/rozmirna-sitka/` рядом с выбором размера (product_detail.html:317); (б) таб «Розмірна сітка» (`tab-size` → `panel-size`, строки 406/474) с рендером структурированной таблицы; (в) trust-link в purchase-блоке (строка 647). Формально пункт «таблица доступна с каждой карточки» — выполнен по UI-каркасу.
2. **Резолвинг гайда** (`resolve_product_size_guide`): `product.size_grid` (override) → `catalog.size_grids` (активная сетка каталога) → визуальный fallback (если у SizeGrid есть картинка) → **глобальный fallback без таблицы** (`_build_global_fallback` — только текст «звіряйтеся зі своєю річчю», columns=[] rows=[]).
3. **Пресеты с см-таблицами** (`SIZE_GUIDE_PRESETS`) существуют только для `hoodie` и `basic_tshirt`. **Пресета для лонгсливов НЕТ вообще** — при том, что лонгсливы по позиционированию основной продукт (правило №5 чек-листа).

### НАХОДКА 1 (P1, бренд-критично). Все 18 лонгсливов рендерят PDP БЕЗ таблицы замеров

Подтверждено живой БД (read-only shell 05.07.2026):

- `SizeGrid` в БД всего 3: «Basic tee size guide» (каталог Футболки, guide_rows=True), «Hoodie size guide» (каталог Худі, guide_rows=True), «Standard Hoodie Sizes» (каталог **Test Catalog - Hoodie** — тестовый артефакт, guide_rows=False, кандидат на удаление).
- `Product.size_grid` (override): **0 из 68 товаров**.
- Категория `long-sleeve`: **18 published, у ВСЕХ `catalog=None`** → сетка каталога недостижима → все 18 PDP падают в `_build_global_fallback`.
- Live-подтверждение: `/product/red-leaves-ls/`, `/product/kha-edition-ls/` — `tc-size-table` = 0, заголовок гайда «Підбір розміру», eyebrow «Fit guide» (generic fallback). Для сравнения `/product/bentejne-hd/` (каталог Худі) — 8 вхождений `tc-size-table`, заголовок «Худі» — структурированная таблица работает.

### НАХОДКА 2 (P1). Ещё 7 футболок и 5 худи тоже без таблицы (catalog=None)

Полный список published-товаров с `catalog=None` (30 из 65 published; всего в БД 31 из 68):

- **long-sleeve (18/18):** longsleeve-classic, my-little-baby-ls, where-mi-present-ls, in-shee-ls, business-money-ls, last-breath-ls, kharkiv-district-ls, pokrovsk-girl-ls, death-grabs-ass-ls, dvoznachni-summy-ls, lord-of-the-lending-ls, red-leaves-ls, death-gbs-ass-ls, kha-edition-ls, kha-style-ls, pojuy-ls, bentejne-ls, longsleeve-limited-edition
- **tshirts (7/23):** last-breath, death-grabs-ass, lord-of-the-lending, death-gbs-ass-ts, kha-edition-ts, twocomms-reality-bends-dark-neon-edition, twocomms-beliveidea-ts
- **hoodie (5/24):** last-breath-hd, death-grabs-ass-hd, lord-of-the-lending-hd, death-gbs-ass-hd, hoodie-silent-winter

Live-подтверждение: `/product/last-breath-hd/` (худи без каталога) — 0 таблиц, «Підбір розміру»; `/product/twocomms-beliveidea-ts/` (футболка без каталога) — 0 таблиц. Побочный эффект: `detect_size_profile` по названию категории «Худі» даёт hoodie-профиль → fallback-набор размеров ВКЛЮЧАЕТ XS (`DEFAULT_SIZE_SETS['hoodie']`), т.е. у 5 худи-«сирот» чипы размеров (XS…XXL) отличаются от худи с каталогом только по стечению дефолтов, а не по данным склада.

### НАХОДКА 3 (P2, логика кода). Пресет-таблица недостижима без SizeGrid-записи

`resolve_product_size_guide` строит структурированную таблицу из пресета ТОЛЬКО при `size_grid is not None and profile_key` (ветка `_build_structured_guide(profile_key, source, size_grid=size_grid)`). Если профиль детектирован (например, футболка по alias «футболки»), но SizeGrid-записи нет — код игнорирует готовый пресет `basic_tshirt` с полной см-таблицей и отдаёт пустой fallback. Т.е. данные для таблицы «зашиты в код и лежат рядом», но не показываются. Фикс-кандидат: в `_build_global_fallback`/резолвере разрешить `_build_structured_guide(profile_key, 'preset_detected')` без size_grid (метка источника `preset_detected` уже существует в `SOURCE_LABELS`, но нигде не используется — мёртвый источник).

### НАХОДКА 4 (P1, задача из чек-листа). Событие `view_size_guide` НЕ существует — подтверждено

- `UserAction.ACTION_TYPES` (models.py:2042–2068): 26 типов, `view_size_guide` отсутствует.
- `storefront/tracking.py`, `storefront/utm_tracking.py`: 0 упоминаний size_guide.
- `product-detail.js` (обработчик `data-pdp-tab`, строки 442–486): переключение таба «Розмірна сітка» не шлёт ни dataLayer.push, ни серверного бекенда — клики по сетке полностью невидимы для аналитики.
- Задача TECH-005 актуальна: добавить action_type `view_size_guide` + пуш при активации `tab-size` и клике по soft-link на `/rozmirna-sitka/`.

### НАХОДКА 5 (P2). На глобальной `/rozmirna-sitka/` нет сетки лонгсливов

Live: страница содержит ровно 2 таблицы (`support-table`) — «Худі» (2 вхождения заголовка) и «Футболка базова» (1). Лонгслив упоминается один раз — только как deep-link на каталог. Т.е. даже переход по soft-link с лонгслив-PDP НЕ даёт покупателю замеры в см — тупик воронки для основного продукта. `build_public_size_guide_blocks` рендерит блоки только для профилей `('hoodie','basic_tshirt')` — лонгслив архитектурно не предусмотрен.

### ПОБОЧНАЯ НАХОДКА 6 (P1, вне скоупа CRO-023, найдена при проверке). Битые ID-ссылки в SEO-блоке «Найкращі ціни» на страницах категорий

При выборе живого лонгслива для теста обнаружено: на `/catalog/long-sleeve/` таблица `seo-pricing` («Найкращі ціни») рендерит **8 ссылок вида `/product/33/`, `/product/36/` … — все отдают 404** (продуктовый роут slug-based: `path('product/<slug:slug>/')`). Причина: seed-миграция `storefront/migrations/0053_phase10b_seed_category_seo.py:384` записала `url=f"/product/{p.id}/"` с комментарием «view will rewrite to slug-based URL on render», но обещанный rewrite НЕ реализован: `services/category_seo_blocks.py::_hydrate_product_items` подцепляет `item.product` только для заголовка/цены, а шаблон `partials/category_seo_blocks.html:42` выводит `href="{{ item.url|default:'#' }}"` как есть. Эффект: битые внутренние ссылки в индексируемом SEO-блоке на каждой категории (масштаб: все категории с блоком best_prices; на long-sleeve — 8/8 битых). Фикс-кандидат: в шаблоне для best_prices использовать `{% url 'product' item.product.slug %}` при наличии `item.product` (fallback на item.url), плюс data-миграция для перезаписи сохранённых URL.

### Что проверено и НЕ является проблемой

- UI-каркас гайда на PDP полный: eyebrow/заголовок/intro/legend/таблица/notes/fit_notes/CTA + image-fallback c width/height (CLS-safe, Phase 21).
- Размерные чипы (`available_sizes`) на всех проверенных PDP рендерятся (из CatalogOption либо дефолтов) — покупатель может выбрать размер везде.
- Для товаров С каталогом (Худі/Футболки с привязкой) структурированные см-таблицы работают: hoodie live-подтверждён; у Футболок SizeGrid «Basic tee size guide» с guide_rows=True в БД есть и подхватится для 16 привязанных футболок.
- CatalogOption(size) в БД корректны: Худі XS–XXL, Футболки S–XXL.

### Задачи исполнителю (из CRO-023)

| # | Задача | Приоритет |
|---|---|---|
| 1 | Привязать 30 published-товаров с `catalog=None` к каталогам (создать каталог «Лонгсліви» + SizeGrid с замерами лонгслива; допривязать 7 футболок и 5 худи) — данные, без кода | P1 |
| 2 | Добавить пресет `longsleeve` в `SIZE_GUIDE_PRESETS` (aliases: long-sleeve, longsleeve, лонгслів, лонгслив; колонки длина/ширина/рукав) + блок на `/rozmirna-sitka/` | P1 |
| 3 | TECH-005: action_type `view_size_guide` + клиентский пуш при активации tab-size / клике soft-link | P1 |
| 4 | Разрешить рендер пресет-таблицы без SizeGrid (источник `preset_detected` — сейчас мёртвый код) | P2 |
| 5 | Починить ID-ссылки best_prices (`category_seo_blocks.html` + data-миграция поверх 0053) | P1 |
| 6 | Удалить тестовый артефакт SizeGrid id=1 «Standard Hoodie Sizes» (Test Catalog - Hoodie) с бою | P3 |
