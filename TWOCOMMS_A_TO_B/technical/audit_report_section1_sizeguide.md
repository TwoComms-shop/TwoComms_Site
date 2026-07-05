# Аудит: CRO-023 — Размерная сетка с карточки товара

Дата: 05.07.2026. Метод: анализ кода (`storefront/services/size_guides.py`, 515 строк; `product.py:610`; `product_detail.html:317,475+`) + скан всех 65 живых PDP (кэш /tmp/pdp из CRO-021/022).

## Вердикт: ЧАСТИЧНО. Блок сетки есть на 65/65 PDP, но у 30/65 (46%) — заглушка без таблицы замеров в см

## Как устроено

1. `size_guides.py` содержит пресеты с таблицами замеров в см: **hoodie** (XS–XXL, довжина/ширина) и **basic_tshirt** (S–XXL, довжина/ширина/плечі). Определение — по alias'ам категории (`худі`, `hoodie`, `футболка`, `tee`…).
2. Источники в порядке приоритета: `product_override` → `catalog_default` → `preset_detected` → `fallback`.
3. `product.py:610` кладёт `resolved_size_guide` в контекст; `product_detail.html:475` рендерит карточку `tc-size-guide-card` инлайн на PDP + ссылка на полный гайд `/rozmirna-sitka/` (HTTP 200) на строке 317.

## Проверка живых страниц (65/65)

| Метрика | Результат |
|---|---|
| Карточка size guide на PDP | 65/65 ✅ |
| Таблица замеров в см | **35/65** ⚠️ |
| Заглушка-fallback («Якщо для товару ще немає окремої сітки…») | **30/65** ❌ |
| Событие `view_size_guide` (код/страницы) | 0 — не существует (подтверждает TECH-005) |

## Находка P1: у лонгсливов НЕТ размерной сетки вообще

Требование чеклиста — «таблица замеров в см доступна с каждой карточки **лонгслива**/худи» — **не выполняется для лонгсливов**:

- В `SIZE_GUIDE_PRESETS` есть только `hoodie` и `basic_tshirt`. Пресета **longsleeve нет**, alias'ов «лонгслів/longsleeve» нет ни в одном пресете.
- Все 18 `*-ls` товаров получают fallback без таблицы.
- Кроме того, fallback получают и 5 худи (`death-gbs-ass-hd`, `death-grabs-ass-hd`, `last-breath-hd`, `lord-of-the-lending-hd`, `hoodie-silent-winter`) и ряд футболок — у их категорий alias не совпал с пресетом (вероятно, нестандартное имя категории в БД).

Полный список 30 товаров с заглушкой: bentejne-ls, business-money-ls, death-gbs-ass-hd, death-gbs-ass-ls, death-gbs-ass-ts, death-grabs-ass-hd, death-grabs-ass-ls, death-grabs-ass, dvoznachni-summy-ls, hoodie-silent-winter, in-shee-ls, kha-edition-ls, kha-edition-ts, kha-style-ls, kharkiv-district-ls, last-breath-hd, last-breath-ls, longsleeve-classic, longsleeve-limited-edition, lord-of-the-lending-hd, lord-of-the-lending-ls, lord-of-the-lending, my-little-baby-ls, pojuy-ls, pokrovsk-girl-ls, red-leaves-ls, twocomms-beliveidea-ts, twocomms-reality-bends-dark-neon-edition, where-mi-present-ls.

## Задачи (ремедиация)

1. **P1:** добавить пресет `longsleeve` (таблица замеров в см) в `SIZE_GUIDE_PRESETS` + alias'ы «лонгслів/лонгслив/longsleeve/long sleeve/ls».
2. **P1:** выяснить, почему 5 худи и часть футболок не матчатся на существующие пресеты (alias категории в БД), и починить матчинг либо назначить `catalog_default`.
3. **P2 (TECH-005):** добавить событие `view_size_guide` (открытие/просмотр карточки сетки) в трекинг.

## Побочные наблюдения

- Хорошо: сетка рендерится инлайн (не требует перехода), есть legend, notes и fit-советы; у худи/футболок текст качественный.
- `product_override`/`catalog_default` через `CatalogOption` — механизм для ручного назначения сетки уже существует, т.е. ремедиация возможна и без деплоя (через админку), но системно лучше добавить пресет.
