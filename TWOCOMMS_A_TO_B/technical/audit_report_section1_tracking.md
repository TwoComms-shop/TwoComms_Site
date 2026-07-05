# АУДИТ РАЗДЕЛ 1 — ТРЕКИНГ И АНАЛИТИКА СОБЫТИЙ (product_view и воронка)

**Дата:** 05.07.2026 · **Метод:** статический аудит кода + живые read-only замеры на боевой MySQL (SSH, батч-сессии `manage.py shell`)
**Связанные пункты чек-листа:** CRO-024 (этот файл), связки: CRO-020 (двойной product_view), CRO-033, CRO-050, DIA-раздел

---

## CRO-024. Событие product_view — завышение. ПОДТВЕРЖДЕНО: завышение ~27x, причина №1 — боты

### Резюме (для агента-исполнителя)

Из **36 263** записей `product_view` в боевой БД только **1 319 (3,6%)** привязаны к `SiteSession` (т.е. прошли человеческий фильтр middleware). **34 944 записи (96,4%) — «сироты»** без SiteSession и без UTMSession — это трафик, который `SimpleAnalyticsMiddleware` отсёк как ботов/не-навигацию, но `record_product_view` всё равно записал. Честная базовая линия человеческих просмотров товаров ≈ **1 000–1 319** (сверено с `PageView` по путям `/product/…` = 1 000). Реальный CTR view→cart = 44/1319 ≈ **3,3%**, а не 0,12% — воронка не так мертва, как выглядела.

### Архитектура записи (как устроено сейчас)

Цепочка на каждый GET `/product/<slug>/…`:

1. `AnalyticsIdentityMiddleware` (`storefront/tracking.py`) — ставит cookie `twc_vid` ВСЕМ, включая ботов (нет бот-фильтра). Бот без cookie получает **новый visitor_id на каждый запрос**.
2. `SimpleAnalyticsMiddleware` (`storefront/tracking.py`) — создаёт `SiteSession`+`PageView`. Фильтрует: `is_bot(ua)` (богатый список `BOT_SIGNALS`, ~30 паттернов), noise-пути, не-GET, `Sec-Fetch-Mode` не navigate, для анонимов — только `Accept: text/html`. **Боты сюда не попадают → SiteSession для них не создаётся вовсе** (этим объясняется факт базовой линии «SiteSession is_bot=True: 0 из 2899» — бот-сессии не помечаются, их просто нет).
3. `UTMTrackingMiddleware` (`storefront/utm_middleware.py`) — тоже фильтрует ботов (`is_bot_user_agent`), но более слабым списком (`utm_utils.py:372`: 12 паттернов).
4. View `product_detail` (`storefront/views/product.py:247`) — вызывает `record_product_view(request, …)` **безусловно**.
5. `record_user_action` (`storefront/utm_tracking.py:21`) — единственный фильтр: `is_request_excluded()` (ручной admin-список `AnalyticsExclusion`; на ботов не влияет). **Никакой проверки `is_bot_user_agent` здесь НЕТ.** Более того: если у запроса нет session_key — `request.session.save()` принудительно создаёт новую django_session-строку (раздувание таблицы сессий ботами).

### (а) Пишется ли view при AJAX/прелоаде/боте — ДА, при боте пишется всегда

- **Боты: подтверждено количественно.** Замер на бою (05.07.2026):

| Метрика | Значение |
|---|---|
| product_view всего | 36 263 |
| без site_session (сироты) | **34 944 (96,4%)** |
| без utm_session | 36 207 (99,8%) |
| сироты и без обеих FK | 34 944 |
| с авторизованным user | **0** |
| с visitor_id в metadata | 20 611 |
| distinct товаров | 65 (все published) |
| PageView всего (бот-фильтр пройден) | 207 540 |
| PageView по путям /product/… | **1 000** |
| product_view с site_session («люди») | **1 319** |

- Почасовое распределение (UTC) — плоское, ночные часы 00–05 дают 1 000–1 300 событий/час суммарно за период — сигнатура круглосуточного краулинга, не украинской аудитории.
- Диапазон данных: 22.04.2026 → 05.07.2026 (74 дня), ~490 product_view/день, из них сироты ~93–97% каждый день (напр. 20.06: 1193/1211; 03.07: 906/954).
- **AJAX/прелоад:** JS-прелоада PDP нет (grep prefetch/preload по main.js — пусто; speculation rules не используются). Эндпоинты `quick_view`, `get_product_images`, `get_product_variants` НЕ вызывают record_product_view — двойного счёта с них нет.
- **Amplification через path-variant URLs (Phase 7.2):** каждый товар имеет крауляемое пространство URL `/product/<slug>/<color>/<size>/<fit>/` (все комбинации + языковые префиксы `/en/…`). Каждый hit любого варианта = +1 product_view. Живой пример из сирот: один visitor_id записал `/product/idea-hd/` и `/product/idea-hd/s/` с разницей 0,6 сек. Топ-путей: `/product/idea-hd/` 611, `/product/hool-ts/` 394 и т.д.

### (б) Исключаются ли краулеры — НЕТ (в записи действий)

- `is_bot_user_agent` вызывается ТОЛЬКО в `utm_middleware.py:62` (гейт на создание UTMSession) — **`record_user_action` не проверяет UA вообще**.
- В кодовой базе живут **два разных бот-детектора**: слабый `utm_utils.is_bot_user_agent` (12 паттернов) и сильный `tracking.BOT_SIGNALS` (~30 паттернов, включая petalbot/semrush/ahrefs/go-http-client). Рассинхрон = разные слои видят разных «ботов».
- Косвенное доказательство состава сирот: у сирот нет SiteSession → они не прошли фильтры `SimpleAnalyticsMiddleware` (бот-UA / не-navigate / не-HTML-Accept). Человеческий браузер с обычной навигацией эти фильтры проходит всегда.

### (в) Двойной вызов сервер+JS — НЕТ; но есть серверный дубль на legacy-URL

- JS не шлёт product_view: эндпоинт `/api/track-event/` (`storefront/views/api.py:119`, `@csrf_exempt`) технически ПРИНИМАЕТ `product_view` (валидация только по `UserAction.ACTION_TYPES`), но единственный фронт-потребитель — `custom-print-configurator.js` (события custom_print_*). Пиксели (fbq/ttq/GA4) идут отдельным слоем и в UserAction не пишут.
- **Серверный дубль подтверждён (P1, связка с CRO-020):** в `product_detail` вызов `record_product_view` стоит на строке **247**, а 301-редирект legacy query-string URL (`?size=M&color=123&fit=…` → path-style) — на строках **319–330**. Порядок: записали view → отдали 301 → браузер открыл каноничный URL → записали view второй раз. Один человеческий заход по старой ссылке = 2 product_view.
- Масштаб дублей у людей: 215 пар (site_session, product) имеют >1 product_view, суммарно **+444 лишних просмотра** (часть — легитимные повторные заходы, часть — редирект-дубль; для точного сплита нужен лог-анализ).

### Побочные находки (обязательны к фиксации)

1. **P1: MySQL без timezone-таблиц — `CONVERT_TZ` возвращает NULL.** Проверено на бою: `SELECT CONVERT_TZ('2026-01-01 00:00:00','UTC','Europe/Kyiv')` → `NULL`. Следствие: ВСЕ Django ORM датные агрегации с TZ (`TruncDate`, `TruncDay`, `ExtractHour` при USE_TZ=True) молча возвращают `None` → любые графики «по дням/часам» в админ-аналитике (`services/admin_analytics.py`, `send_utm_report.py`, `utm_analytics.py`), использующие Trunc*, группируют всё в одну корзину `None`. Фикс: `mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql mysql` (нужны права) либо перевод агрегаций на naive-даты. Требует отдельной проверки каждого дашборда (кандидат в DIA-раздел).
2. **P2: бот-хиты принудительно создают django_session** (`record_user_action` → `request.session.save()`): ~35k мусорных строк в таблице сессий + накладные расходы на запись при каждом бот-хите.
3. **P2: `product_view` с авторизованным user = 0 из 36 263** — либо залогиненные не смотрят товары (маловероятно за 74 дня), либо есть разрыв в трекинге для авторизованных. Требует ручной проверки: залогиниться → открыть PDP → проверить UserAction.user. Кандидат на до-проверку в CRO-050.
4. info: `UserAction.action_type='page_view'` — 0 записей за всю историю: `record_page_view` определён, но нигде не вызывается. Воронка «page_view→product_view» в отчётах всегда будет пустой на первом шаге.
5. info: сироты имеют `metadata.visitor_id` (20 611 записей с visitor_id) — но для ботов это одноразовые id (cookie не сохраняется), склейка невозможна.

### Что чинить (задачи для исполнителя, по приоритету)

1. **P0 (данные):** в `record_user_action` (utm_tracking.py) добавить ранний выход: `if is_bot(request.META.get('HTTP_USER_AGENT','')): return None` — использовать СИЛЬНЫЙ детектор `tracking.is_bot`/`BOT_SIGNALS`, не слабый `is_bot_user_agent`. Плюс не записывать действие, если для запроса нет SiteSession (или хотя бы не форсить `session.save()` для запросов без cookie).
2. **P1:** перенести `record_product_view` НИЖЕ ветки 301-редиректа (после строки ~330) в `product_detail`, чтобы legacy-URL не давал двойной счёт.
3. **P1:** объединить два бот-детектора в один модуль (единый источник правды), прогнать `is_bot` по UA сирот при бэкфилле.
4. **P1 (гигиена данных):** после фикса — решение по историческим 34 944 сиротам: пометить/мигрировать в отдельный статус или исключить из всех отчётов через `action_exclusion_q`-подобный фильтр `site_session__isnull=True` (быстрый вариант без миграции: во всех аналитических queryset'ах считать product_view только с site_session).
5. **P1:** починить timezone-таблицы MySQL (побочная находка №1) — до этого не доверять никаким датным графикам аналитики.
6. **P2:** рейт-лимит/дедуп product_view в рамках сессии (одно событие на товар в N минут) — уберёт двойные хиты и F5.

### Acceptance-критерии после фикса

- Новые product_view за сутки: доля записей с `site_session__isnull=True` < 5% (сейчас 96,4%).
- Заход по legacy `?size=M`-URL создаёт ровно 1 product_view (сейчас 2).
- `UserAction.objects.filter(action_type='product_view', timestamp__date=X).count()` ≈ `PageView.objects.filter(path__startswith='/product/', when__date=X).count()` (±10%).
- CTR view→cart в отчётах пересчитан от честной базы (~3,3%, а не 0,12%).
