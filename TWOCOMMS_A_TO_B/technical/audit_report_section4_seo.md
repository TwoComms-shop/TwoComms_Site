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
2. **P2 — availability захардкожен.** `FeedOffer.available` — всегда `return True` (marketplace_feeds.py:245), `stock` из БД для Google игнорируется (все 384 = `in_stock`). Комментарий в коде объясняет: made-to-order DTF, все залишк�� 0 (согласуется с CRO-014: у всех 75 вариантов stock=0). Осознанное решение, но: снятый с производства published-товар фид продолжит продавать. Связка с CRO-014 (механики наличия нет системно).
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

## Журнал раздела

| Дата | Пункт | Резюме |
|---|---|---|
| 07.07.2026 | SEO-022 | Organization/WebSite/founder глобально через теги в base.html со стабильными @id, logo есть, BreadcrumbList на каталоге/карточке/индексе/контактах; P3 — sameAs без TikTok (нужен подтверждённый handle); остаток — Rich Results Test |
| 07.07.2026 | SEO-023 | FAQPage через единый тег faq_schema на 6 страницах, разметка строится из видимого контента (один источник), TECH-032 фактически закрыт; остаток — выборочная валидация владельцем |
