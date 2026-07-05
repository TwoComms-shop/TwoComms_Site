# CRO-026 — Блок отзывов на карточке товара (аудит)

**Дата:** 05.07.2026 · **Статус:** аудит завершён · **Вердикт: код полный и корректный, но КОНТЕНТ-ПРОВАЛ — 0 approved-отзывов на весь каталог**

---

## 1. Что проверялось

| Вопрос из чеклиста | Ответ |
|---|---|
| Выводятся ли отзывы на PDP? | Да, блок рендерится на **65/65** live PDP (`partials/product_reviews.html`, include в `product_detail.html:675`) |
| Schema Review / AggregateRating? | Реализована корректно, но **live не эмитится ни на одном PDP (0/65)** — нет approved-отзывов |

## 2. Архитектура (код — состояние хорошее)

- **Единый источник правды:** `reviews/services/aggregate.py::aggregate_rating_for_product` — count/avg/histogram одним дешёвым запросом по индексу `rev_status_product_idx`. Порог `MIN_APPROVED_REVIEWS_FOR_RATING = 1` (снижен с 3 в SEO Phase 12).
- **JSON-LD** (`seo_utils.py:916–980`): `aggregateRating` + top-5 вложенных `Review` эмитятся ТОЛЬКО при `show_rating=True` — зеркалит видимый UI, что закрывает GSC-ошибку «review/aggregateRating без видимого контента». Гейтинг корректный.
- **Submission** (`reviews/views.py::submit_review`): публичный POST, honeypot, rate-limit 2/час на IP+товар (только гости), фото ≤5 шт ≤5 МБ (JPEG/PNG/WebP), CSRF, всё уходит в `status=pending` на модерацию. `is_verified_purchase` авто-проставляется по оплаченному заказу.
- **Модерация:** Django-admin c bulk approve/reject + Telegram-пинг модератору при новом pending; IndexNow-пинг при первом approve.

## 3. Находки

### P1 — Нулевой контент: 0/65 PDP имеют aggregateRating (live-скан 05.07.2026)
Прямой HTTP-скан всех 65 канонических PDP из `sitemap-products.xml`: блок отзывов есть везде, `aggregateRating` — нигде. При пороге = 1 это означает: **ни один товар каталога не имеет ни одного approved-отзыва**. Весь SEO-uplift от rich-snippets (звёзды в SERP, +5–15% CTR по расчёту из Phase 12) не реализован — инфраструктура простаивает.

### P1 — «Coupon → review» петля сбора отзывов НЕ существует в коде
Комментарий в `aggregate.py:37` ссылается на «companion review-collection loop (Phase 13.x coupon → review)», но в кодовой базе нет ни post-purchase email/Telegram-запроса отзыва, ни купона за отзыв (grep по `review.*coupon|review_request` — 0 совпадений вне комментария). Без активного сбора отзывов при текущем трафике каталог не наберёт отзывы органически.

### P2 — Bulk approve в админке обходит сигналы
`reviews/admin.py::approve_selected` использует `queryset.update(...)`, который **не вызывает pre_save/post_save** → `ping_indexnow_on_first_approval` (reviews/signals.py) никогда не сработает при массовой публикации из админки (основной сценарий модерации). Google/Bing не узнают о новом aggregateRating до планового пере-краула. Фикс: итерировать queryset и вызывать `.save(update_fields=...)` либо пинговать IndexNow прямо в action.

### P3 — Мелочи
- `product_detail.html:121` — рендер звёзд `{% if forloop.counter <= avg %}` сравнивает int с float: avg 4.6 → 4 звезды (округление вниз, визуально занижает).
- CSS отзывов грузится `media="print" onload` (async) — при отключённом JS стили не применятся (есть noscript-фоллбек — ок).

## 4. Рекомендации (по приоритету)

1. **Запустить сбор отзывов** — минимальный вариант: через N дней после `status=paid` отправлять клиенту (email/Telegram) ссылку `PDP#product-reviews` + промокод за отзыв с фото. Один approved-отзыв на товар = звёзды в SERP.
2. **Пофиксить bulk approve** → IndexNow-пинг при массовой публикации.
3. Первично отмодерировать pending-очередь (если есть) — Telegram-пинги могли теряться.

## 5. Методология
- Статический аудит: `reviews/` (models, views, forms, signals, admin, services), `partials/product_reviews.html`, `seo_utils.py`, `views/product.py:552–635`.
- Live: HTTP-скан 65 канонических PDP + 12 локализованных (ru/en) — `aggregateRating` grep по отрендеренному HTML.
- SSH-проба БД (точные счётчики pending/rejected) не выполнена — хост 195.191.24.169:22 сбрасывал соединения (fail2ban после серии сессий); live-скан даёт достаточное покрытие для вердикта, т.к. порог = 1 и schema зеркалит БД.
