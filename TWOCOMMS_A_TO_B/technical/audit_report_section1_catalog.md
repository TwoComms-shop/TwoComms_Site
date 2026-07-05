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

## ЖУРНАЛ
- 05.07.2026 · CRO-010 · аудит завершён, 4 находки P1–P3 + 2 INFO. Код не менялся (analysis-only).
