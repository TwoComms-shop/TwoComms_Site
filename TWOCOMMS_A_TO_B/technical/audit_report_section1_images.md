# Аудит CRO-022: Фото товара — варианты и webp

**Дата:** 05.07.2026
**Метод:** анализ кода (`image_optimizer.py`, `storefront/services/image_variants.py`, `storefront/signals.py`, `storefront/tasks.py`) + скан всех 65 живых PDP (кэш из CRO-021) + HTTP-проверка media-файлов. SSH недоступен (rate-limit), метод эквивалентен.

## Вывод: ЧАСТИЧНО OK. Alt-тексты идеальны, но у 31% товаров (20/65) главное фото (LCP) грузится тяжёлым оригиналом без optimized-вариантов — P1.

## Что проверялось и результаты

### 1. Основное фото — 65/65 OK
Все 65 PDP имеют главное изображение (`#mainProductImage`), 0 страниц без фото.

### 2. Alt-тексты — 65/65 OK
- Пустых alt на главном фото: **0**
- Дублирующихся alt: **0** — каждый уникален, шаблон: «Худі «Дрони навколо 2.0» — принт …, худі TwoComms»
- Декоративные изображения корректно имеют `alt=""`.

### 3. Механизм оптимизации (код) — спроектирован правильно
- `ImageOptimizer`: webp (q80) + avif (q75), responsive-ширины 320–1440w в `optimized/` рядом с оригиналом.
- `image_variants.py::build_optimized_image_payload` — собирает srcset, graceful fallback на оригинал.
- Триггер: `post_save`-сигнал → `optimize_image_field_task` (Celery / inline-fallback).
- Есть management-команды: `optimize_images`, `enqueue_optimize_images`, `convert_originals_to_webp`, `audit_product_images`.

### 4. **P1: 20/65 PDP — LCP-фото без optimized-вариантов**
На 45/65 страниц `<picture>` содержит avif/webp `<source>` из `optimized/` (проверено HTTP: 200 для `*_320w.webp` и `.avif`). На **20 страницах** `<source>` пустые, и `<img fetchpriority="high">` грузит оригинал:

| Пример | Оригинал (LCP) | Размер |
|---|---|---|
| twocomms-reality-bends-dark-neon-edition | `/media/product_colors/1.png` | **276 KB (PNG!)** |
| v2-0-pokrovsk | `/media/products/Худі2.webp` | 145 KB |
| 225-hoodie | `/media/products/5_FZKBu4W.webp` | 118 KB |
| 20-twocomms-legend | `/media/products/post.webp` | 100 KB |

HTTP-проверка: `optimized/post_640w.webp`, `optimized/1_640w.webp`, `optimized/H2_640w.webp` и др. → **404** — варианты не существуют на диске (для сравнения, у «здоровых» товаров — 200).

Для 320w-вариант ≈ 12 KB — т.е. на этих 20 страницах LCP-изображение в **8–20 раз тяжелее необходимого**.

**Причина (код):** docstring в `tasks.py::optimize_image_field_task` прямо признаёт: ранее из-за `bind=True` «the inline fallback was silently broken» — товары, загруженные в тот период, не получили вариантов. Баг уже исправлен, но **backfill не выполнен**.

**Полный список (20):** 20-twocomms-legend, 225-hoodie, 225-tshirt, bentejne-hd/ls/ts, glory-of-ukraine-hd, hd-twocomms-reality-bends-future-2026, hoodie-silent-winter, hool-ts, idea-hd, longsleeve-limited-edition, pojuy-hd/ls/ts, ts-twocomms-reality-bends-mentol, twocomms-beliveidea-ts, twocomms-reality-bends-dark-neon-edition, twocomms-reality-bends-future-2026, v2-0-pokrovsk.

### 5. Прочие наблюдения
- Один оригинал вообще PNG (`1.png`, 276 KB) — конвертация оригиналов в webp (`convert_originals_to_webp`) тоже пропустила его.
- Кириллические/пробельные имена файлов (`худи4-upscale-1x---2.webp`, `ChatGPT_Image_28_апр._…`) — работают, но хрупко для CDN/кэшей.

## Рекомендации
1. **P1:** выполнить на сервере backfill: `python manage.py optimize_images` (или `enqueue_optimize_images`) — одноразово; проверить, что 20 товаров получили `optimized/*`-варианты.
2. **P2:** добавить в `audit_product_images` (или мониторинг) алерт «товар published, а optimized-вариантов нет».
3. **P3:** нормализовать имена загружаемых файлов (транслит, без пробелов).
