# Раздел 4 — SEO/AEO: отчёт аудита

## SEO-022. Organization / WebSite / BreadcrumbList schema (код-слой, 07.07.2026)

**Вывод: выполнено, архитектура образцовая.**

1. Organization — единый источник правды: `{% organization_schema %}` в base.html:902 (глобально на всех страницах) → `StructuredDataGenerator.generate_organization_schema()` (seo_utils.py:~1448). Стабильный `@id` `{base}#organization` для дедупликации Google; `logo` (ImageObject c url/contentUrl/caption) присутствует.
2. Рядом глобально: `{% website_schema %}` (WebSite + SearchAction, base.html:903) и `founder_schema` (закрывает висячий `#founder`).
3. **P3: `sameAs` = только Instagram + Telegram** — комментарий в коде: «adding more requires owner-confirmed handles». Чеклист требовал Instagram + TikTok; TikTok-профиль в sameAs отсутствует во всём seo_utils.py. Действие владельца: подтвердить handle TikTok → добавить одну строку.
4. BreadcrumbList: есть на catalog.html (unified @graph, 2 варианта), product_detail.html (Product+BreadcrumbList в одном @graph), index, contacts (через `{% breadcrumb_schema %}` тег), category_color_landing, support_page. Требование чеклиста (каталог + карточка) выполнено.
5. Дублей Organization нет: inline-копии удалены в Phase 21 (base.html:868 комментарий), страничные Organization (cooperation/custom_print/wholesale) — только как `publisher`-вложение WebPage, без конфликтующего `@id`.

**Остаток (владелец):** Rich Results Test на 3 шаблонах; подтвердить TikTok-handle для sameAs.

## SEO-023. FAQPage schema (код-слой, 07.07.2026)

**Вывод: выполнено, требование TECH-032 (5+ страниц) уже достигнуто.**

1. Единый тег `{% faq_schema faq_items %}` (seo_tags.py:377) → partials/faq_schema.html. Генерация из тех же `faq_items`, что рендерятся видимым контентом страницы — соответствие «разметка = видимый контент» обеспечено конструктивно (один источник данных), gettext_lazy корректно резолвится в str.
2. Покрытие — 6 страниц: cooperation, custom_print, pro_brand, product_detail (product_faq_items из product_seo_block), support_page, wholesale. Плюс FAQPage в blog_blocks.py и category_color_landing. TECH-032 «расширить на 5+ страниц» — фактически закрыт.
3. Пустые faq_items → тег возвращает `{}` и partial не рендерит script — мусорной пустой разметки нет.

**Остаток (владелец):** выборочная валидация 2–3 страниц в Rich Results Test; убедиться, что product_faq_items не дублируют FAQ категории на цветных лендингах (риск self-competition в FAQ-сниппетах).

## SEO-008. Google Merchant feed (код + live-замер, 07.07.2026)

**Вывод: фид динамический и живой (384 оффера / 65 групп), НО есть P1 — все `<g:link>` дают 301-редирект с потерей цвета, и P2 — availability захардкожен `in_stock`.**

### Архитектура (как реально работает)

1. **Фид — динамический, а не статический.** URL-ы `/google_merchant_feed.xml`, `/google-merchant-feed-v2.xml`, `/merchant/product-feed` (twocomms/urls.py:95–97) + `/google-merchant-feed.xml` (storefront/urls.py:533) — все ведут в один view `google_merchant_feed` (storefront/views/static_pages.py:1173) → `build_google_merchant_feed_xml()` (services/marketplace_feeds.py:1140). Live-замер: все 3 корневых URL отдают HTTP 200, `application/xml`, 2 154 088 байт, генерация 2.3–3.3 s, идентичный контент.
2. **Статический файл `twocomms/google_merchant_feed.xml` (378 байт, ПУСТОЙ — только channel без items) — мёртвый артефакт в git.** Он НЕ отдаётся по URL (роут перехватывает Django-view), но это дефолтный `--output` команды `generate_google_merchant_feed` — т.е. при запуске команды без аргументов пустышка перезаписывается в корень проекта. P3: убрать из git / сменить дефолтный output на `tmp/feeds/`.
3. **Автообновление:** сигналы post_save/post_delete на Product/Category/ProductImage/Color/ProductColorVariant/ProductColorImage (storefront/signals.py:44–98) → `mark_feeds_dirty()` (flag-файл `tmp/feeds/feeds_dirty.flag`) → cron должен гонять `manage.py regenerate_feeds_if_dirty` (services/feeds_queue.py, файловый debounce вместо Celery). Поскольку боевой URL рендерит XML на лету из БД — «свежесть» фида по HTTP гарантирована даже без cron; cron-обвязка актуальна только для файловых снапшотов. **Остаток (SSH):** проверить crontab на сервере — есть ли `regenerate_feeds_if_dirty`.

### Live-валидация контента фида (замер 07.07.2026)

| Проверка | Результат |
|---|---|
| Офферов `<item>` | 384 (65 item_group — сходится с 65 published-товарами из CRO-020) |
| Уникальность `g:id` | 384/384, дублей 0 |
| id/title/description/link/image_link/price/brand/mpn/condition | 384/384 у всех |
| color/size/gender/age_group/item_group_id/google_product_category/product_type | 384/384 |
| gtin | 0/384 (barcode пуст в БД; `is_valid_gtin` фильтрует), `identifier_exists` НЕ проставлен |
| sale_price | 373/384 (пара price/sale_price корректна: price=base, sale_price=final) |
| Длины: title max 79 (<150 OK), description 341–2350 (<5000 OK) | OK |
| image_link (выборка 5 случайных) | все HTTP 200 |
| availability | 384/384 = `in_stock` (захардкожено, см. находку 2) |

### Находки

1. **P1 — ВСЕ 384 `<g:link>` дают 301 с потерей цвета.** `_product_url()` (marketplace_feeds.py:662) строит `?size=S&color=Чорний`, а сайт 301-редиректит на канонический путь `/product/{slug}/{size}/`, ВЫБРАСЫВАЯ `color` (проверено live: и «Чорний», и «Білий» редиректят на один и тот же `/product/kharkiv-district-hd/m/`). Последствия: (а) политика Merchant требует финальный URL — предупреждения «URL redirect», лишний crawl; (б) клик по объявлению конкретного цвета ведёт на дефолтный цвет — риск disapproval «mismatched landing page» и падение конверсии платного трафика; (в) атрибуция цвета ломается. Фикс: строить в фиде сразу канонический `/product/{slug}/{size_slug}/` + цвет в форме, которую PDP реально принимает.
2. **P2 — availability захардкожен.** `FeedOffer.available` — всегда `return True` (marketplace_feeds.py:245), `stock` из БД для Google игнорируется (все 384 = `in_stock`). Комментарий �� коде объясняет: made-to-order DTF, все залишк�� 0 (согласуется с CRO-014: у всех 75 вариантов stock=0). Осознанное решение, но: снятый с производства published-товар фид продолжит продавать. Связка с CRO-014 (механики наличия нет системно).
3. **P3 — gtin 0/384 и `identifier_exists=false` не проставлен.** Работает через brand+mpn, но mpn синтетический (`{article}-{product.id}`), не настоящий производительский номер — формально нарушение политики MPN, при ручной ревизии Merchant может дать warning.
4. **P3 — `<g:shipping>` не задан** — доставка должна быть настроена на уровне аккаунта Merchant Center (проверить у владельца), иначе офферы не пройдут.
5. **P3 — 4 дублирующихся URL фида** (3 в корневом urls.py + 1 в storefront/urls.py); лишние стоит закрыть/301-нуть на один канонический, чтобы не плодить точки генерации тяжёлого XML.
6. **P3 — нет кэширования ответа фида.** Каждый GET = полный проход по БД + сборка 2 MB XML ~3 s CPU на shared-хостинге, включая запросы ботов. Отдавать из файлового снапшота (`tmp/feeds/`) или кэшировать 10–30 мин.

**Остаток (владелец/SSH):** статус фида в Merchant Center (принят/warnings — доступ у владельца); crontab `regenerate_feeds_if_dirty`; точечная сверка цен фида с БД (по live-данным цены согласованы с PDP-выборкой CRO-020).

## SEO-009. IndexNow и Google Indexing (код, 07.07.2026)

**Вывод: обе интеграции архитектурно добротные (on_commit, retry, host-фильтр, audit-log с rolling-quota для Google), НО сигнальный путь Google Indexing идёт БЕЗ дедупа и БЕЗ quota_limit — массовые пересохранения товаров сжигают дневную квоту 200 запросов.**

### Как устроено

- **IndexNow** (`services/indexnow.py`): ключ из env `INDEXNOW_KEY`, отдаётся по `/{key}.txt` (urls.py:72, view сверяет с env и 404-ит чужие ключи — корректно). Отправка: батчи по 100, retry 2 на timeout/5xx, 4xx фатален, host-фильтр отсекает чужие домены, синхронно внутри `transaction.on_commit` (Celery-хоп осознанно убран — на хостинге нет брокера). Таймаут 2.5 s — запрос пользователя не блокируется заметно.
- **Google Indexing** (`services/google_indexing.py`, 860 строк): service-account JWT → OAuth2 token с in-process кэшем; 1 URL = 1 HTTP-вызов (ограничение API); audit-log в БД (`GoogleIndexingSubmission`, миграция 0064) с rolling-window квотой 24h/200 и честным «next slot at»; admin-панель со статусом/историей. Креды: env `GOOGLE_INDEXING_CREDENTIALS_PATH` либо дефолт `json/totemic-life-471601-g7-408d1ee6dcf2.json` — **файл НЕ закоммичен в git (проверено `git ls-files`) — хорошо**; лежит ли он на сервере — остаток SSH.
- **Триггеры** (`storefront/signals.py`): pre_save запоминает старый URL, post_save на Product шлёт old+new URL в оба сервиса через `on_commit`; post_delete шлёт `URL_DELETED`; отдельные сигналы на color-landing. Плюс ручные пути: admin-панель и команды `reindex_indexnow` / `submit_indexnow_urls`.

### Находки

1. **P2 — сигнальный путь Google Indexing без дедупа и квоты.** `enqueue_google_indexing_urls()` вызывает `submit_google_indexing_urls()` с дефолтами: `skip_recent_success_hours=0`, `quota_limit=None` — вся продуманная rolling-window-механика дедупа/квоты работает ТОЛЬКО для admin/cron-путей, но не для сигналов. Каждый `Product.save()` = до 2 вызовов Google API. Массовое пересохранение (65 товаров в цикле — типичная админ-операция или management command) = ~130 вызовов, 65% дневной квоты за один проход, повторный прогон — квота исчерпана, реальные обновления не доедут. Чек-лист прямо спрашивал «не спамят при массовых пересохранениях» — **Google-путь спамит**; IndexNow-путь безопасен (квоты нет, батчинг есть). Фикс: в `enqueue_google_indexing_urls` передавать `skip_recent_success_hours=get_quota_window_hours()` и `quota_limit` из остатка квоты.
2. **P3 — сигнал без changed-fields-проверки.** post_save шлёт пинг при ЛЮБОМ save (смена цены, порядка сортировки, служебных полей) — не только при изменениях, влияющих на индексацию. В сочетании с находкой 1 усиливает расход квоты.
3. **P3 — Google Indexing API формально только для JobPosting/BroadcastEvent.** Комментарий в коде это честно признаёт («works as a hint»). Риск малый, но при жёсткой ревизии Google может игнорировать пинги; полагаться стоит на sitemap + IndexNow.
4. **P3 — pre_save на Product делает лишний SELECT на каждый save** (`Product.objects.filter(pk).only("slug","status").first()`) — на массовых операциях это N лишних запросов; можно кэшировать в update_fields-aware форме.
5. **P3 — таблица `GoogleIndexingSubmission` без retention-политики** — audit-log растёт бесконечно (каждый пинг = строка); нужен cron-cleanup (>90 дней).

**Остаток (SSH/владелец):** значение `INDEXNOW_KEY` в env и live-проверка `/{key}.txt` (ключ знает только сервер); наличие `json/totemic-life-…json` на сервере; фактический расход квоты в `GoogleIndexingSubmission` (read-only SQL); статус в Bing/Yandex Webmaster (IndexNow) — доступ у владельца.

## SEO-021. Review/AggregateRating schema (код + live, 07.07.2026)

**Вывод: разметка реализована дисциплинированно и БЕЗ фейков — `aggregateRating` эмитится только при ≥1 одобренном отзыве из единого источника истины; live сейчас ни один PDP не отдаёт рейтинг (отзывов нет — корректное поведение). Замечаний уровня P1/P2 нет.**

### Как устроено

- **Единый источник истины:** `reviews/services/aggregate.py` — `aggregate_rating_for_product()` считает count/avg/histogram только по `status=APPROVED` (индекс `rev_status_product_idx`); порог `MIN_APPROVED_REVIEWS_FOR_RATING = 1` (осознанно снижен с 3 в Phase 12 против cold-start, решение задокументировано прямо в коде).
- **С��ема:** `seo_utils.py:916–973` — `aggregateRating` (ratingValue 1dp, reviewCount, best/worst) добавляется ТОЛЬКО когда `review_summary.show_rating=True`; рядом nested top-5 `Review` блоков (по helpful_count, body обрезан до 600 симв., author fallback). Никаких хардкодов `ratingValue` в шаблонах/коде НЕТ (grep чистый).
- **Проводка:** view `product.py:560` → контекст `product_review_summary` → шаблон `product_detail.html` → `{% product_graph ... review_summary=product_review_summary %}` — цепочка целая. ProductGroup-объект намеренно БЕЗ review/aggregateRating (фикс GSC-ошибки «position требует review», задокументирован seo_utils.py:1191, 1232).
- **Тесты:** `reviews/tests/test_aggregate.py` + `storefront/tests/test_seo_regressions.py` (3 теста прямо проверяют схему с review_summary) — регрессионная защита есть.

### Live-проверка (07.07.2026)

2 PDP (kharkiv-district-hd, red-leaves-hd): 5 JSON-LD блоков валидно парсятся, Product БЕЗ aggregateRating/review — т.е. одобренных отзывов сейчас 0, и система честно молчит. Требование чек-листа «НЕ размечать фейковые рейтинги» — выполнено.

### Находки

1. **P3 — мёртвый шаблон `product_detail_new.html`** вызывает `{% product_schema product %}` без review_summary — не используется ни одним view (рендерится только `pages/product_detail.html`), но при случайном включении рейтинг молча пропадёт. Кандидат на удаление (связка с CB-015 мёртвый код).
2. **P3 — nested Review-блоки: +1 запрос к БД внутри генератора схемы** (top-5 живым запросом) — активируется только при наличии отзывов, сейчас не влияет.
3. **Наблюдение (не дефект):** порог 1 отзыв допустим по политике Google, но «1 отзыв, 5.0★» в SERP может выглядеть тонко; компенсирующий механизм сбора отзывов (Phase 13 купон→отзыв) должен реально работать — иначе рейтинг так и не появится. Фактическое количество отзывов в БД — остаток SSH (read-only SQL по `reviews_review`).

**Остаток (владелец/SSH):** count по `reviews_review` в БД; после появления первого одобренного отзыва — прогнать PDP через Rich Results Test.

## SEO-010. Скорость как ранж-фактор — CWV на 3 ключевых шаблонах mobile (live-замеры, 07.07.2026)

**Вывод: CWV НЕ зелёные ни на одном из 3 ключевых шаблонов — но проблема на 90–95% серверная (TTFB), а не фронтендовая. CLS идеальный (0.0) везде, рендер после получения HTML быстрый. Корневая причина — комбинация `Vary: Cookie` + `Set-Cookie` (csrftoken + twc_vid) на КАЖДОМ анонимном GET, которая полностью выключает LiteSpeed page cache, плюс перегрузка/cold-start воркеров Passenger на shared-хостинге (интермиттентные 503).**

### Замеры (agent-browser, эмуляция iPhone 14, 07.07.2026)

| Шаблон | TTFB | FCP | LCP | LCP-элемент | CLS | Вердикт LCP |
|---|---|---|---|---|---|---|
| Главная `/` | 3 442 мс | 4 216 мс | 4 784 мс | `logo.svg` (img) | 0.00 | КРАСНЫЙ (>2.5s) |
| Каталог `/catalog/` | 15 352 / 16 122 мс (2 прогона) | 16 188 мс | 16 400 мс | `catalog-hero.webp` (div bg) | 0.00 | КАТАСТРОФА (~6.5× порога) |
| Карточка `/product/my-little-baby/` | 2 097 мс | 2 520 мс | 2 520 мс | AVIF 1080w | 0.00 | Погранично красный |

### Распределение TTFB (curl, 12+ сэмплов)

- **Бимодальное:** тёплые ответы 0.53–0.97s vs холодные 8.5–17.9s (главная 13.5s, каталог 12s, карточка 17.9s). Медиана главной ~0.85s — т.е. сам Django-рендер быстрый, пики = очередь/спавн воркеров Passenger.
- **1 из 12 запросов к главной → 503** (13.6s). Те же интермиттентные 503 поймал краулер SEO-006 (9 URL из sitemap: при повторной проверке все 200). Для Googlebot это сигнал «хост перегружен» → снижение crawl rate.

### Корневые причины (по убыванию веса)

1. **P1 — кэш полностью выключен для анонимов:** ответ несёт `Vary: Cookie` И одновременно `Set-Cookie: csrftoken=...` + `Set-Cookie: twc_vid=...` на каждом анонимном GET. LiteSpeed Cache при такой комбинации не кэширует НИЧЕГО — каждый визит (и каждый заход Googlebot) = полный Django-рендер. Фикс: не сажать csrftoken на GET без формы (лениво через `{% csrf_token %}` только где нужно), выдавать twc_vid только после первого действия или через JS, либо настроить LiteSpeed cache-vary ignore для этих кук.
2. **P1 — ёмкость Passenger:** холодные хвосты 8–18s и 503 = мало воркеров/памяти на Hostsila shared (связка с TD-015: сколько воркеров, что выполняется синхронно). 
3. **P3 — LCP-элемент главной = logo.svg** (143KB SVG в шапке) — стоит проверить вес/инлайн; но при TTFB <1s LCP главной был бы ~1.5–2s, т.е. фронт вторичен.

### Что уже хорошо

- CLS 0.0 на всех трёх шаблонах (размеры зарезервированы корректно).
- Карточка отдаёт AVIF 1080w как LCP — современный формат, приоритизация работает.
- Разрыв FCP→LCP минимален (0–570 мс) — критический путь рендера короткий.

**Связки:** CRO-003 (те же замеры), TECH-040 (CWV mobile), TD-015 (воркеры Passenger), SEO-006 (интермиттентные 503 для краулера). **Остаток (владелец):** полевые данные CrUX/GSC Core Web Vitals (лаборатория подтверждает красный статус; поле покажет реальную долю затронутых визитов), настройки LiteSpeed Cache в панели Hostsila.

## SEO-006. Битые внутренние ссылки и 410 (полный live-краул, 07.07.2026)

**Методика:** rate-limited краулер `scripts/seo_combined_slow_crawl.py` (1 req/2.5s, retry 12с на 503/000, resumable JSONL) — IP-бана за весь краул НЕ было. Сырьё: `data/seo_crawl_results.jsonl` (500 записей), сводка: `data/seo_crawl_analysis_report.md`. Анализатор: `scripts/seo_crawl_analyze.py`.

### Результаты

- **Sitemap: 489/489 URL → все HTTP 200, 0 редиректов, 0 ошибок.** Ни одного 3xx/4xx/5xx в финальном состоянии.
- **Внутренние ссылки вне sitemap: 39 уникальных → все 200, 0 битых, 0 redirect-chains.**
- **Тесты несуществующих URL (6/6):** выдуманные product-slug, catalog-slug, blog-slug, случайные пути → **чистый 404 без редиректов**, отдаётся кастомная 404-страница (~93KB body). 410 для удалённых товаров НЕ реализован (сейчас 404) — P3, допустимо по Google-докам (404 и 410 обрабатываются почти одинаково).
- Единственная аномалия: 1 интермиттентный 500 на `/blog/` в ранней фазе краула (повтор → 200) — паттерн Passenger cold-start/overload, задокументирован в SEO-010/TD-022, НЕ битая ссылка.

### Вывод

Внутренних 404 нет, ссылочная целостность образцовая. Опциональная TECH-задача (низкий приоритет): отдавать 410 для намеренно удалённых товаров, чтобы ускорить выпадение из индекса.

## SEO-007. Мета-титлы/дескрипшены — уникальность и длина (полный live-краул, 07.07.2026)

**Методика:** тот же краул 489 страниц; собраны title/description/canonical/robots/og/H1. Полные списки проблемных URL — в `data/seo_crawl_analysis_report.md`.

### Структурная гигиена — чисто

- 0 страниц без title / description / canonical / OG / H1; 0 multiple-H1; 0 noindex среди sitemap-URL; **canonical == финальный URL на 100% страниц**.

### Замечания по качеству (P3)

| Проблема | Кол-во | Примеры |
|---|---|---|
| title < 30 симв. | 8 | /ru/delivery/ (24), /en/blog/ (16), /doglyad-za-odyagom/ локали |
| title > 65 симв. | 41 | product-варианты цвет+фит: reality-bends-future-2026 до 82; /pro-brand/ (3 локали), /en/cooperation/ |
| description > 165 | 121 | шаблон product-description раздувается на вариантах; /custom-print/, /wholesale/ до 204 |
| description < 70 | 6 | /blog/category/news/ локали — 13–16 симв., фактически пустышки |
| Дубли title | 13 групп | см. ниже |

### Дубли — детально

- **2 реальные кросс-товарные группы:** ru- и en-версии **трёх разных товаров** серии Reality Bends шарят один и тот же title «Футболка "Reality Bends"…» — генератор мета не различает товары внутри серии. Это единственные настоящие дубли, требующие уникализации.
- **11 групп — en-версии блог-постов с непереведёнными украинскими title** (en-страница отдаёт украинский title = дубль своей uk-версии). hreflang смягчает, но title стоит перевести — это и пробел локализации.

### Рекомендации исполнителю (батчами по 10 стр., GSC-контроль — RISK-13)

1. Поджать шаблон product title ≤ 60 и description ≤ 160 (обрезка/приоритизация атрибутов вариантов).
2. Уникализировать title/description трёх товаров Reality Bends (ru + en).
3. Заполнить description категорий блога (/blog/category/news/ и локали).

**Post-fix 16.07.2026 (F-008):** четыре статических commercial outlier-группы
из таблицы закрыты в `7fa568b1`. Live UK/RU/EN descriptions для
`/cooperation/`, `/custom-print/`, `/wholesale/` и `/catalog/` дают 12/12
значений длиной 120–160. Остальные исторические product/blog outliers из
краула 489 страниц этим исправлением не закрываются.
4. Перевести title 11 en-блог-постов.

**Связки:** SEO-005 (hreflang), TECH-030 (шаблоны мета), RISK-13 (батчи при массовой правке мета).

### Follow-up 16.07.2026 — F-028 PDP variant locale

`da910c46` закрыл runtime-протекание UK merchandising в RU/EN PDP, variants API,
quick view и Product JSON-LD. Локально и на сервере: focused **16/16**, integer API
routes **7/7**, Django check clean; `origin/main` и server HEAD =
`da910c469fd91b8b5bb3535890e74ad9acf384b4`, Passenger перезапущен,
`/healthz/` = 200. Live-матрица **13 SKU × 3 локали = 39/39** согласована по
HTTP/title base/H1/variant data/Product JSON-LD; RU/EN variants API корректны,
quick-view/images = 200. Примеры во всех четырёх слоях: RU
`death-grabs-ass-hd` = `Худи «Сердце И Деньги»`, EN =
`Hoodie «death grabs ass»`; RU `last-breath-ls` =
`Лонгслив «Череп С Розой»`, EN = `Longsleeve «Skull and Rose»`.
Миграций и изменения данных не было.

Статус **[o] PARTIAL**: runtime-дефект исправлен, но коммерческое решение о
различиях EN print identity и RU/UK требует owner-approved slug-family mapping.
Автономное переименование контента/данных не выполнялось и не разрешено.

## AEO-001. AI-трафик уже идёт — какие страницы цитирует ChatGPT (БД, 07.07.2026)

**Источник:** read-only SSH/Django shell batch, `data/server_audit_batch_output.txt`.

### Факты

- `utm_source=chatgpt.com`: **119 сессий**.
- Referrer-based hits: `chatgpt` — **19**, `perplexity` — 0, `gemini` — 0.
- Период: first seen **2026-02-02 19:22:44 UTC**, last seen **2026-07-07 19:47:35 UTC**.
- Устройства: **100 mobile**, 19 desktop.
- `is_converted=True`: **0** — ожидаемо, потому что CRO-041/042/DB-003 подтверждают, что UTM-конверсии сейчас не проставляются.

### Landing pages из chatgpt.com

| URL | Сессий | Вывод |
|---|---:|---|
| `/` | 52 | Главная — основной AI-вход; должна ясно отвечать «что такое TwoComms», состав/товары/доставка/возврат/цены |
| `/catalog/` | 13 | Каталог как общий ответ AI |
| `/en/catalog/tshirts/` | 10 | AI ведёт на английскую tshirts-страницу, хотя брендовый приоритет — лонгсливы/худи |
| `/catalog/tshirts/` | 10 | То же для UA |
| `/en/catalog/` | 5 | Английский каталог |
| `/ru/catalog/` | 4 | Русский каталог |
| `/en/` | 3 | Английская главная |
| `/en/custom-print/` | 3 | Custom print важен для AI-выдачи |
| `/catalog/hoodie/` | 3 | Hoodie уже цитируется, но слабее tshirts |
| `/en/pro-brand/` | 2 | B2B/brand story |
| `/catalog/long-sleeve/` | 2 | Long-sleeve цитируется слабо |
| `/pro-brand/` | 2 | Brand story UA |

Единичные попадания: `/ru/catalog/hoodie/`, `/en/catalog/long-sleeve/`, `/en/product/20-twocomms-legend/black/`, `/en/product/hool-ts/`, `/product/-v2-0_Pokrovsk/`, `/product/kha-style-hd/`, `/en/catalog/hoodie/`, `/ru/product/kha-edition-ts/`.

### Вывод

AI-трафик уже не гипотеза, а стабильный канал: 119 UTM-сессий + 19 referrer hits. ChatGPT чаще всего цитирует главную и каталог, но заметный кусок ведёт в `tshirts`, что конфликтует с позиционированием «лонгсливы/худи первичны». Конверсии AI-канала сейчас не измеряются из-за общей поломки UTM/order linkage, а не обязательно из-за качества AI-трафика.

### Что делать исполнителю/контент-агенту

1. На `/`, `/catalog/`, `/catalog/hoodie/`, `/catalog/long-sleeve/`, `/custom-print/`, `/pro-brand/` добавить явные answer blocks, которые AI легко цитирует: состав ткани, плотность, размерная сетка, сроки печати/отправки, доставка/возврат, цены/диапазоны, отличие TwoComms от generic print shop.
2. Уменьшить AI-перекос в tshirts: усилить hoodie/long-sleeve страницы фактологией и внутренними ссылками с главной/каталога.
3. После фикса CRO-041/042 повторить batch и смотреть AI-конверсию по `UTMSession.is_converted`, а не только visits.
4. Добавить AI-channel grouping в аналитику (AN-033), чтобы `chatgpt.com`/referrer AI не смешивались с обычным referral/direct.

## Журнал раздела

| Дата | Пункт | Резюме |
|---|---|---|
| 16.07.2026 | F-028 / SEO-007 | `da910c46`: locale runtime fixed; local/server 16/16 + API 7/7 + check clean, live 39/39 aligned, health 200; [o] PARTIAL до owner-approved cross-locale naming map; без data mutation |
| 07.07.2026 | SEO-022 | Organization/WebSite/founder глобально через теги в base.html со стабильными @id, logo есть, BreadcrumbList на каталоге/карточке/индексе/контактах; P3 — sameAs без TikTok (нужен подтверждённый handle); остаток — Rich Results Test |
| 07.07.2026 | SEO-023 | FAQPage через единый тег faq_schema на 6 страницах, разметка строится из видимого контента (один источник), TECH-032 фактически закрыт; остаток — выборочная валидация в��адельцем |
| 07.07.2026 | SEO-010 | CWV mobile НЕ зелёные: LCP главная 4.8s, каталог 16.4s, карточка 2.5s; CLS 0.0 везде; корень — Vary:Cookie + Set-Cookie на каждом GET выключают LiteSpeed cache + cold-start Passenger (TTFB бимодальный 0.5s/8–18s, интермиттентные 503); остаток — CrUX/GSC поле + панель Hostsila |
| 07.07.2026 | SEO-006 | Полный краул 489/489: все 200 без редиректов, 0 битых внутренних ссылок; несуществующие URL → чистый 404 (410 не реализован — P3) |
| 07.07.2026 | SEO-007 | Структурно чисто (0 missing title/desc/canonical/OG/H1, canonical=final везде); P3: 41 title >65, 121 desc >165, 13 групп дублей (3 Reality Bends кросс-товарных + 11 непереведённых en-блогов) |
| 07.07.2026 | AEO-001 | БД: chatgpt.com = 119 сессий, referrer chatgpt = 19; топ landing pages `/` 52, `/catalog/` 13, tshirts 20 суммарно; конверсии 0 из-за общей UTM-поломки |
