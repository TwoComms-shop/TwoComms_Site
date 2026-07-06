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

## Журнал раздела

| Дата | Пункт | Резюме |
|---|---|---|
| 07.07.2026 | SEO-022 | Organization/WebSite/founder глобально через теги в base.html со стабильными @id, logo есть, BreadcrumbList на каталоге/карточке/индексе/контактах; P3 — sameAs без TikTok (нужен подтверждённый handle); остаток — Rich Results Test |
| 07.07.2026 | SEO-023 | FAQPage через единый тег faq_schema на 6 страницах, разметка строится из видимого контента (один источник), TECH-032 фактически закрыт; остаток — выборочная валидация владельцем |
