# АУДИТ-ОТЧЁТ · РАЗДЕЛ 1.2 — КАТАЛОГ (CRO-010 … CRO-015)

**Дата:** 05.07.2026 · **Метод:** статический анализ кода + живые HTTP-проверки https://twocomms.shop + read-only запросы к боевой MySQL через SSH (`manage.py shell`).
**Связано:** `twocomms_global_audit.md` (раздел 1.2), `TECHNICAL_TASKS.md`.

---

## CRO-010. Фильтрация лонгсливов/худи (категория / цвет / размер)

### Статус: ПРОВЕРЕНО. Категория и цвет работают; РАЗМЕРНОГО ФИЛЬТРА НЕ СУЩЕСТВУЕТ; фильтрация идёт с полной перезагрузкой страницы.

### 1. Проверенные факты (живая БД, 05.07.2026)

| Категория | id | slug | Опубликовано товаров |
|---|---|---|---|
| Футболки | 3 | `tshirts` | 23 |
| Худі | 4 | `hoodie` | 24 |
| Лонгсліви | 5 | `long-sleeve` | 18 |

- Всего товаров: **65 published + 3 archived = 68** (сходится с базовой линией аудита).
- Slug-и из чек-листа подтверждены: `long-sleeve` (id=5), `hoodie` (id=4), `tshirts` (id=3).
- Цветовые slug-и вариантов (published): `black` 61, `coyote` 7, `pink` 2, `white-burgundy` 1, `menthol` 1. Пустых slug у вариантов нет (0).
- Опубликованные colour-category лендинги: `long-sleeve/black`, `hoodie/black`, `tshirts/black`, `tshirts/coyote`.

### 2. HTTP-проверки (живой сайт, 05.07.2026)

| URL | Код | Комментарий |
|---|---|---|
| `/catalog/` | 200 | корень каталога, showcase-карточки категорий |
| `/catalog/long-sleeve/` | 200 | категория работает |
| `/catalog/hoodie/` | 200 | категория работает |
| `/catalog/tshirts/` | 200 | категория работает |
| `/catalog/longslivy/`, `/catalog/hudi/`, `/catalog/futbolki/` | 404 | легаси-slug-и из showcase-конфига НЕ существуют как URL (см. находку №4) |
| `/catalog/?color=black` | 200 | 16 карточек (= PRODUCTS_PER_PAGE, пагинация) |
| `/catalog/?color=coyote` | 200 | **ровно 7 уникальных data-product-id — 1:1 с БД (7 coyote-вариантов)** — фильтр точен |
| `/catalog/?color=black,coyote` | 200 | мульти-выбор (OR) работает |
| `/catalog/?color=nonexistent` | 200 | 0 карточек + корректный empty-state (см. п.5) |
| `/catalog/tshirts/black/`, `/catalog/hoodie/black/`, `/catalog/long-sleeve/black/`, `/catalog/tshirts/coyote/` | 200 | colour-лендинги живы |

### 3. Архитектура фильтрации (код)

- **Категория** — не query-param, а path-сегмент: `storefront/urls.py:102` → `catalog/<slug:cat_slug>/` → `views/catalog.py::catalog` (строка 552). `get_object_or_404(Category, slug=…, is_active=True)`.
- **Цвет** — `?color=black,coyote` (и повторный `&color=`), парсинг `storefront/services/color_filter.py::parse_color_filter`: нормализация к `[a-z0-9-]`, дедуп, максимум **10** slug-ов (анти-abuse). Фильтр: `color_variants__slug__in=… .distinct()`.
- **Чипы цветов** строятся из *неотфильтрованного* queryset (`build_available_colors`) — пользователь всегда может OR-нуть ещё один цвет. Чипы сохраняют остальные query-параметры и сбрасывают `page` (`_build_chip_url` исключает `color`/`page`, переносит остальное) — состояние фильтра целиком живёт в URL → **ссылка шарима, критерий чек-листа выполнен**.
- Чип уже выбранного цвета = toggle-off; есть ссылка «Скинути» (`build_reset_url`).
- Активный чип помечается `aria-current="true"` (a11y OK, Phase 21/T20).
- **Landing-swap:** если для `(категория, цвет)` есть published `CategoryColorLanding`, чип ведёт не на `?color=slug`, а на `/catalog/<cat>/<color>/` (перелив PageRank на индексируемый лендинг). Подтверждено вживую: на `/catalog/tshirts/` чипы: `black` → `/catalog/tshirts/black/`, `coyote` → `/catalog/tshirts/coyote/`, `menthol`/`pink` → `?color=…`.
- **SEO-гигиена:** `?color=…` и поиск → `noindex, follow` + canonical на базовый путь (подтверждено вживую: `/catalog/?color=coyote` отдаёт `noindex, follow` + canonical `https://twocomms.shop/catalog/`; `/catalog/tshirts/` — `index, follow…` + self-canonical). Дублей в индексе фасеты не создают.
- **Кэш:** `@cache_page_for_anon(600)` (10 мин, только анонимы). Ключ (`views/utils.py::_build_anon_cache_key`) включает scheme+host+path+**полный отсортированный query string**+язык → разные фильтры НЕ пересекаются в кэше. Версионные префиксы `product-order-v{n}:category-v{n}` инвалидируют кэш при пересортировке в админке.

### 4. НАХОДКИ (для агента-исполнителя)

1. **[P1] Размерного фильтра НЕ СУЩЕСТВУЕТ.** Чек-лист требует «фильтры по категории, цвету, размеру». В `catalog.py` (1216 строк), `catalog.html` (533 строки), `color_filter.py` и URL-слое нет ни одного упоминания size-фасета. На модели Product единственное size-поле — `size_grid` (гайд замеров), т.е. размеры НЕ являются складскими атрибутами SKU (нет per-size остатков). Вывод: фильтр по размеру невозможен без изменения модели данных. Задача исполнителю: (а) зафиксировать продуктовое решение — нужен ли размерный фасет при отсутствии складского учёта размеров; (б) если нужен — это отдельный эпик (модель размерных остатков), НЕ быстрый фикс.
2. **[P2] Фильтрация идёт с ПОЛНОЙ перезагрузкой страницы.** Чипы — обычные `<a href>` (`partials/color_filter_chips.html`), JS-слоя для фильтра нет (в `static/js/` нет color-filter скриптов; `color-filter.css` — только стили). Критерий чек-листа «без полной перезагрузки» — НЕ выполнен. Смягчение: 10-мин anon-кэш делает переходы быстрыми. Рекомендация: AJAX-подмена грида + history.pushState — P2, не блокер.
3. **[P2] Потеря контекста мульти-фильтра при landing-swap.** `color_filter.py::build_available_colors`: если чип НЕ выбран и для него есть лендинг — URL чипа заменяется на лендинг **безусловно**, даже когда уже выбраны другие цвета. Сценарий: юзер на `/catalog/tshirts/?color=menthol` кликает `black`, ожидая `menthol+black` → попадает на `/catalog/tshirts/black/`, выбор `menthol` молча теряется. Фикс: делать landing-swap только при пустом текущем выборе (`if landing_url and not is_selected and not selected`).
4. **[P3] Расхождение showcase-slug-конфига с боевыми slug-ами.** `catalog.py::CATALOG_SHOWCASE_CARD_CONFIG` перечисляет slug-ы `longslivy/longsleeves/hudi/futbolki…`, которых в БД НЕТ (боевые: `long-sleeve`, `hoodie`, `tshirts`). Матчинг спасают token-ы (`long`, `hood`, `футбол`) — работает, но конфиг вводит в заблуждение и хрупок (переименование категории «Худі» без токена сломает карточку). Рекомендация: добавить боевые slug-ы в конфиг первыми.
5. **[OK] Empty-state 0-результатов** — образцовый: заголовок «Під цей вибір зараз немає готових речей», CTA «Змінити фільтр» (→ `/catalog/`) + «Створити в кастомі» (→ `/custom-print/?source=catalog_empty` — даже с UTM-источником). Мёртвого тупика нет.
6. **[P3] Счётчик на чипе = число ВАРИАНТОВ, а не товаров.** `build_available_colors` инкрементирует `entry["count"]` на каждую строку `ProductColorVariant`; если у товара 2 варианта одного цвета — он посчитается дважды. Сейчас на данных 65 товаров расхождение не выявлено (coyote: чип 7 = товаров 7), но контракт хрупкий. Фикс: считать `Count('product_id', distinct=True)` как уже сделано в `_compute_showcase_swatches`.
7. **[P3] Кэш-ключ включает сырой `HTTP_ACCEPT_LANGUAGE`.** У каждого пользователя свой Accept-Language (`uk-UA,uk;q=0.9,en;q=0.8…`) → комбинаторика вариантов ключа взрывается, hit-rate анонимного кэша падает. LANGUAGE_CODE уже в ключе — сырой заголовок избыточен. Рекомендация: убрать `accept_lang` из fingerprint (проверив, что language-negotiation не зависит от него после LocaleMiddleware).
8. **[INFO] `?color=<мусор>` возвращает 200 с пустым гридом** (slug «nonexistent» проходит нормализацию). Из-за `noindex, follow` SEO-риска нет; в кэш такие страницы попадают на 10 мин — DoS-поверхность ограничена MAX_COLOR_SLUGS=10, приемлемо.
9. **[INFO] Цветовой фильтр на корне `/catalog/` скрывает showcase-карточки категорий** и показывает грид товаров (иначе юзер не увидел бы результат) — осознанное решение, работает.

### 5. Acceptance для будущих фиксов
- После фикса №3: на `/catalog/tshirts/?color=menthol` клик по `black` должен дать `?color=menthol,black` (или явное продуктовое решение о приоритете лендинга).
- После фикса №6: чип-count == `Product.objects.filter(category=…, status='published', color_variants__slug=slug).distinct().count()` для каждого slug.

---

## CRO-011. Пагинация каталога

### Статус: ПРОВЕРЕНО. Canonical/prev/next есть и корректны для «чистой» пагинации; НО пагинация СБРАСЫВАЕТ цветовой фильтр, а `?page=N` на корне каталога плодит SEO-дубли showcase.

### Проверенные факты (живой сайт, 05.07.2026)
- `/catalog/?page=2`: self-canonical `https://twocomms.shop/catalog/?page=2`; `rel="prev"` → `/catalog/` (без `?page=1` — правильно), `rel="next"` → `/catalog/?page=3`. Шаблон: `pages/catalog.html:61-63` (block `pagination_links`).
- Дублей товаров между страницами НЕТ: порядок детерминирован — `apply_public_product_order` = `order_by("-priority", "-id")` (`services/catalog_helpers.py:130`), tie-breaker `-id` уникален. Live: пересечение data-product-id страниц 1/2 категорийного грида = 0.
- `?page=99999` не 500-ит: `paginator.get_page` кламит к последней странице.

### НАХОДКИ
1. **[P1] Переход по страницам СБРАСЫВАЕТ фильтры.** Ссылки пагинации — `href="?page={{ i }}"` (`catalog.html:425-447`): относительный URL с `?` **заменяет весь query string**. Сценарий: `/catalog/?color=black` (26 черных товаров, 2 страницы) → клик «стр. 2» → `/catalog/?page=2` — фильтр потерян, юзер видит нефильтрованный грид (на корне — вообще showcase без товаров). Критерий чек-листа «переход по страницам не сбрасывает фильтры» — **НЕ выполнен**. Фикс: генерировать ссылки через copy запроса (`request.GET.copy()` → set `page`) или template-tag `{% url_replace page=i %}`.
2. **[P1-SEO] `rel="prev"/"next"` на фильтрованных страницах указывают на НЕфильтрованные URL.** На `/catalog/?color=black&page=2` prev/next = `/catalog/` и `/catalog/?page=3` (без `color`). Т.к. фасеты noindex — индекс не отравляется, но сигналы пагинации врут. Фикс тем же механизмом, что и №1.
3. **[P2-SEO] `/catalog/?page=N` (корень, без фильтра) отдаёт showcase БЕЗ товаров, но с self-canonical `?page=N` и prev/next.** Причина: `catalog.py:601-607` всегда строит `page_obj` по product_qs, а шаблон при `show_category_cards=True` рендерит showcase — товарного грида нет, содержимое всех `?page=N` идентично. Итог: бесконечный ряд индексируемых дублей витрины (`?page=2..8` все 200 + canonical на самих себя). Фикс: на корне-showcase либо 301 `?page=N` → `/catalog/`, либо canonical на `/catalog/` + не выводить pagination_links.
4. **[P3] `pagination.py::build_homepage_page_url`** также не переносит query-параметры (для главной приемлемо — фильтров там нет; фиксировать не требуется, но не переиспользовать для каталога).
5. **[OK] Окно страниц** в UI: `i > number-3 и i < number+3` — компактно, active помечен, prev/next disabled на краях, `aria-label` есть.

---

## CRO-012. Lazy-load изображений каталога

### Статус: ПРОВЕРЕНО, в целом ОК. width/height есть (CLS-safe), lazy по умолчанию; но eager — только первые 2 карточки (чек-лист рекомендует 4–8).

### Проверенные факты
- Карточка (`partials/product_card.html` → tag `optimized_image`, `templatetags/responsive_images.py:142`): `loading="lazy"` по умолчанию; `eager=True` передаётся только при `forloop.counter0 < 2` (`catalog.html:388-392`). Live `/catalog/?color=black&page=2`: 1 eager + 12 lazy; `/catalog/tshirts/`: 8 lazy карточных img.
- **width/height присутствуют на 100% карточных img** (live: 13/13 и 8/8) → CLS от карточек нет. Плюс `decoding="async"`, `fetchpriority` (auto/low), `sizes`, srcset `320w…1024w+` из `/media/products/optimized/*.webp`.
- Hero каталога: `<link rel="preload" as="image" fetchpriority="high">` + `loading="eager"` (`catalog.html:12,264`) — LCP-кандидат прогревается корректно.

### НАХОДКИ
1. **[P2] Eager только у 2 первых карточек, а первый ряд на desktop — 4 (row-cols-lg-4).** Карточки 3–4 первого ряда лоадятся lazy → возможна поздняя отрисовка above-the-fold на широких экранах. Рекомендация: `forloop.counter0 < 4` eager (границу вынести в константу). Учесть fragment-cache `product_card_home_catalog_v6…` — ключ уже включает `forloop.counter0`, инвалидация не нужна, но bump версии ключа обязателен при изменении разметки.
2. **[INFO] Fallback-ветка тега** (нет optimized-версий файла) рендерит `<img>` без srcset — на выборке не встретилась (все карточки шли из `/optimized/`), риск низкий.

---

## CRO-013. Порядок сортировки: лонгсливы/худи первыми

### Статус: ЧАСТИЧНО. Управляется admin-priority (норм. механизм), но showcase-карточки на корне каталога ставят ФУТБОЛКИ ВЫШЕ ХУДІ.

### Факты
- Единый источник порядка: `apply_public_product_order` → `order_by("-priority", "-id")`; `Product.priority` управляется drag-and-drop в админке, версия порядка (`public_product_order_version`) инвалидирует кэши. Товарные гриды и главная сортируются одинаково — «выпячивание футболок» кодом НЕ зашито.
- **Showcase-порядок на `/catalog/` (live): `long-sleeve` → `tshirts` → `hoodie`.** Порядок задаёт `CATALOG_SHOWCASE_CARD_CONFIG` (catalog.py), а не Category.order.

### НАХОДКИ
1. **[P2] Showcase ставит футболки на 2-е место, худі — последним.** Позиционирование «лонгсливы/худи — первыми» выполнено наполовину. Фикс: переставить конфиг (long-sleeve, hoodie, tshirts) — однострочник + bump кэш-версии.
2. **[PENDING] Фактические значения `Product.priority` в БД не сверены** (SSH-доступ на момент проверки блокировался, см. журнал). Проверить: не выставлены ли приоритеты так, что первые 8 позиций главной — футболки. Скрипт готов: `/tmp/audit_cro015.py` (см. CRO-015).

---

## CRO-014. Статусы наличия в каталоге

### Статус: ПРОВЕРЕНО. Модель — made-to-order (DTF-печать): понятия «нет в наличии» в витрине НЕТ by design; phantom-SKU не обнаружено.

### Факты
- У `Product` нет поля остатков; единственный складской атрибут — `ProductColorVariant.stock` (`productcolors/models.py:38`), и **витрина его нигде не читает**: ни `catalog.py`, ни `product.py`, ни шаблоны карточек. Используется только в `services/marketplace_feeds.py:444-459`, где stock=0 сознательно поднимается до минимума фида («made-to-order DTF» — прямая цитата из комментария кода).
- Скрытие товара = смена `status` (published/archived и т.д.), есть поле `unpublished_reason`. Live-проверка CRO-010 подтвердила: archived (3 шт.) в гриды не попадают.
- «Кликнуть и застрять» невозможно: все карточки в гриде published и покупабельны.

### НАХОДКИ
1. **[P3/продуктовое] Правило «наличия» нигде не задокументировано.** Если вещь временно нельзя произвести (нет заготовок нужного цвета/размера) — единственный механизм = ручная расп��бликация всего товара; per-variant «недоступен» нет (stock есть на модели, но UI/логика отсутствуют). Зафиксировать решение: либо официально «всё всегда под заказ» (тогда SLA производства на карточке), либо доделать variant-level наличие. Связка: TECH-010.
2. **[INFO] Для маркетплейс-фидов stock искусственно ≥1** — при аудите фидов (SEO-раздел) помнить, что это не реальные остатки.

---

## CRO-015. N+1 при рендере каталога

### Статус: СТАТИЧЕСКИЙ АНАЛИЗ ПРОЙДЕН (архитектура анти-N+1 корректна); живой замер числа SQL — ОТЛОЖЕН (SSH недоступен).

### Факты (код)
- Базовый queryset `_product_cards_queryset` (catalog.py:316): `select_related('category')` + `prefetch_related('images', 'color_variants__images')` + `defer(9 тяжёлых текстовых полей)` — все обращения карточки покрыты.
- `build_color_preview_map` (catalog_helpers.py:239): один bulk-запрос по `product_ids` списка страницы, чтение картинок из `_prefetched_objects_cache` (комментарий «This prevents N+1 queries»); fallback `variant.images.all()` может дать N+1 только если prefetch не сработал — на текущем вызове prefetch есть.
- `_build_catalog_showcase_cards`: счётчики товаров одним `values('category_id').annotate(Count)` — bulk.
- Три слоя кэша душат SQL: page-cache анонимов (600с) → fragment-cache грида (600с) → fragment-cache карточки (900с).
- ⚠️ Потенциальные лишние запросы: `build_available_colors` и `color_seo_copy` ходят по отдельным queryset-ам (приемлемо), `attach_preferred_card_image` — проверить при замере.

### НАХОДКИ
1. **[PENDING/P2] Живой замер `len(connection.queries)` не выполнен**: SSH к 195.191.24.169 отдаёт `kex_exchange_identification: connection reset` (вероятно fail2ban после серии аудит-сессий; ранее в этот же день доступ работал). Скрипт замера готов и проверен синтаксически: `/tmp/audit_cro015.py` (django `test.Client` + `CaptureQueriesContext` для `/catalog/`, `/catalog/tshirts/`, `/catalog/hoodie/`, `/catalog/?color=black`, `/` + распределение по таблицам + топ-12 priority). Выполнить при следующем SSH-окне; норма из чек-листа: ≤15 запросов/страница.

---

## ЖУРНАЛ
- 05.07.2026 · CRO-010 · аудит завершён, 4 находки P1–P3 + 2 INFO. Код не менялся (analysis-only).
- 05.07.2026 · CRO-011…CRO-015 · аудит завершён (analysis + live HTTP). Ключевое: пагинация сбрасывает фильтры (P1), `?page=N` дублирует showcase с self-canonical (P2-SEO), showcase-порядок ставит футболки выше худі (P2). Отложено до SSH-окна: живой замер SQL-запросов и сверка Product.priority (скрипт `/tmp/audit_cro015.py`).
