# ОТЧЁТ АУДИТА — РАЗДЕЛ 2: СКВОЗНАЯ АНАЛИТИКА И ПИКСЕЛИ

**Связан с:** `twocomms_global_audit.md` (раздел 2)
**Правило безопасности:** реквизиты доступа в этом файле НЕ фиксируются.
**Дата начала:** 05.07.2026

---

## AN-031. Смена session_key при логине рвёт связку UTM (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **ПОДТВЕРЖДЁН КОДОМ**

### Факты

1. Django `login()` вызывает `session.cycle_key()` → session_key меняется, **данные сессии копируются** в новый ключ. Значит `request.session['utm_data']` и `platform_data` логин ПЕРЕЖИВАЮТ.
2. `UTMSession` привязана к строковому `session_key` и **никем не мигрируется**. Проверены все ресиверы `user_logged_in` в проекте:
   - `accounts/cart_signals.py` — мердж корзины (есть);
   - `storefront/signals.py:382` — `claim_anonymous_survey_promocode_on_login` (промокоды опросов);
   - миграции UTMSession.session_key — **НЕТ нигде**.
3. Все lookup'ы (`link_order_to_utm`, `record_lead/purchase`, `record_user_action`) ищут строго `UTMSession.objects.get(session_key=request.session.session_key)` → после логина `DoesNotExist` гарантирован.
4. Смягчающий факт: заказов от залогиненных всего 3/41 → на текущих данных вклад этого разрыва мал; главный разрыв — CRO-041 (COD вообще без трекинга). Но при росте доли залогиненных (кабинет, программы лояльности) разрыв станет системным.
5. `UTMSession.visitor_id` (first-party cookie `twc_vid`, живёт 365 дней, не зависит от session_key) заполнен только в 128/1015 сессий — поле добавлено позже; для новых сессий может служить стабильным ключом связки.

### Задача исполнителю

1. Добавить ресивер `user_logged_in`: перепривязывать `UTMSession` (и `SiteSession`) со старого session_key на новый. Старый ключ доступен ДО login() — проще всего сохранить `request.session.session_key` в переменную в кастомных вьюхах логина, либо (надёжнее) искать UTMSession по `visitor_id` как первичному ключу связки.
2. В `link_order_to_utm`/`record_*` — fallback-цепочка: session_key → visitor_id → session['utm_data'] (см. CRO-041 п.2 в `audit_report_section1_cro.md`).
3. Тест: визит с UTM → логин → заказ → utm_source у заказа заполнен (регресс-тест на cycle_key).

---

## AN-013. fbc/fbp/fbclid не доходят до CAPI (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **ПОДТВЕРЖДЁН КОДОМ+БД**

### Как устроен путь клик-идентификаторов сейчас

1. **Захват:** `utm_middleware.py` пишет `fbclid/gclid/ttclid` из URL + cookies `_fbc/_fbp` в `session['platform_data']` и в UTMSession (при создании).
2. **БД (05.07.2026):** UTMSession с fbclid — **349**, gclid — **533**, fbc — 15, fbp — 21, ttclid — 0.
3. **Order:** полей fbclid/fbc/fbp/gclid/ttclid **НЕТ** (orders/models.py — только utm_*). Заказов с utm_session — 0 → через FK идентификаторы тоже недоступны.
4. **CAPI:** `orders/facebook_conversions_service.py:371–381` берёт fbp/fbc ТОЛЬКО из `order.payment_payload['tracking']`. Этот payload формируется исключительно в monobank-потоке (monobank.py, invoice-создание: cookies `_fbc/_fbp` + client_ip + user_agent кладутся в tracking_context).
5. **Итог:** COD-заказы (9 шт., без payment_payload) → CAPI-события уходят только с хешами phone/name/city → низкий Event Match Quality. Monobank-заказы получают fbc/fbp только если cookie реально стояла в момент создания инвойса.

### Доп. находка: fbc=15 при fbclid=349 (4,3%)

Cookie `_fbc` создаёт Meta Pixel на клиенте. GTM/Pixel грузится отложенно (interaction/таймаут 12–20s, base.html) → у ушедших до interaction пользователей `_fbc` не появляется, fbclid из URL при этом захвачен серверно. **CAPI разрешает конструировать fbc вручную**: `fb.1.{timestamp_ms}.{fbclid}` — сервис этого не делает. 334 сессии с fbclid, но без fbc = потерянный match quality, который можно вернуть без изменения клиентской части.

### Задача исполнителю

1. Расширение TECH-060: при создании ЛЮБОГО заказа копировать в Order (новые поля или в payment_payload['tracking']) — fbclid, fbc, fbp, gclid, ttclid из `session['platform_data']` / UTMSession.
2. В `facebook_conversions_service._build_user_data`: если fbc пуст, но есть fbclid — конструировать `fb.1.<ms>.<fbclid>`.
3. gclid (533 сессии!) — аналогично прокидывать для Google (offline conversions / GA4 measurement protocol), сейчас он умирает в UTMSession.
4. ttclid: 0 в БД — TikTok-трафик не размечен клик-ID; проверить, добавляет ли TikTok Ads `ttclid` к URL в текущих кампаниях (вопрос владельцу).
5. **RISK-07:** новые поля Order = миграция → только после TD-020 (бэкапы). Альтернатива без миграции — писать в существующий JSON `payment_payload`.

---

## AN-035. Бот-фильтр фактически мёртв (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **МЕХАНИЗМ ВСКРЫТ: is_bot мёртв BY DESIGN, product_view раздут ботами на ~96%**

### Почему SiteSession.is_bot = 0 из 2902

`storefront/tracking.py::SimpleAnalyticsMiddleware.process_request` при `is_bot(ua)==True` делает **ранний return ДО создания SiteSession**. Т.е. сессии ботов вообще не создаются → строк с is_bot=True быть не может (кроме экзотики: человек-UA создал сессию, потом тот же session_key пришёл с бот-UA). Поле мёртво «по построению», все admin-фильтры `is_bot=False` — no-op. Это НЕ означает «ботов нет».

### Доказательство ботового раздува product_view (главный результат)

`record_user_action` (utm_tracking.py) **НЕ содержит бот-фильтра вообще** — только `is_request_excluded` (staff/IP-исключения). `record_product_view` вызывается на КАЖДЫЙ GET карточки (product.py:247) безусловно. Замер БД 05.07.2026:

| Метрика | Значение | Интерпретация |
|---|---|---|
| product_view всего | 36 044 | |
| product_view с site_session=NULL | **34 733 (96,4%)** | SimpleAnalyticsMiddleware ОТКАЗАЛСЯ создавать сессию для этих запросов (бот-UA / не-GET / не-navigate Sec-Fetch-Mode / нет text/html Accept) — а UserAction всё равно записан |
| product_view с utm_session=NULL | 35 988 (99,8%) | |
| product_view от залогиненных | 0 | |
| Уникальных site_session у product_view | 642 | реальных «человеческих» просмотров порядка ~1300 |

Пересчёт воронки на «человеческих» данных: ~1311 product_view (с site_session) → 44 add_to_cart = **CTR ~3,4%**, а не 0,12%. UX-катастрофы нет — есть грязные данные. (Точный пересчёт — после фикса, см. CRO-051.)

### Расхождение двух бот-детектов

| | `tracking.py::is_bot` (BOT_SIGNALS) | `utm_utils.py::is_bot_user_agent` (bot_patterns) |
|---|---|---|
| Использует | SimpleAnalyticsMiddleware, blog | UTMTrackingMiddleware |
| Список | 30+ паттернов (googlebot, petalbot, semrush, ahrefs, lighthouse, headless...) | 15 паттернов, включая слишком широкие: `'google'`, `'facebook'`, `'preview'` |
| Риски | `'bot'` как подстрока ловит устройства Cubot/«robot» в UA | `'google'` ловит легитимные UA c "Google" (напр. Google App WebView `GSA/...` содержит «GSA», но UA приложений Google на Android содержат «wv)... Version/4.0 Chrome/... GoogleApp» → человек будет отброшен из UTM-трекинга) |

Два независимых списка = два разных представления о «боте» в одной системе; UTMSession и SiteSession фильтруются по-разному.

### Задача исполнителю

1. В `record_user_action` добавить тот же фильтр, что в SimpleAnalyticsMiddleware: `is_bot(ua)` + Sec-Fetch-Mode + метод GET + (для аноним. новых сессий) Accept: text/html. Либо жёстче: не писать UserAction, если для session_key нет/не создаётся SiteSession.
2. Единый модуль бот-детекта (один список сигналов) для tracking.py и utm_utils.py; убрать слишком широкие подстроки ('google', 'facebook', 'preview') либо заменить на точные токены ('googlebot', 'facebookexternalhit', 'googleother', 'google-inspectiontool').
3. Поле `SiteSession.is_bot`: либо удалить (мёртвое), либо изменить семантику — создавать сессии ботов С флагом (для отчёта «сколько ботов ходит»), но исключать из бизнес-метрик. Решение зафиксировать.
4. Ретро-чистка: UserAction(action_type='product_view', site_session__isnull=True) — 34,7k строк — агрегировать/удалить по согласованию с владельцем (это 94% таблицы UserAction, см. DB-004).
5. Тест: запрос с UA «Googlebot/2.1» на карточку → UserAction НЕ создаётся; обычный Chrome-UA → создаётся.

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | AN-031 | Подтверждён кодом: миграции UTMSession при логине нет; session['utm_data'] переживает логин — готовый fallback; visitor_id заполнен в 128/1015 |
| 05.07.2026 | AN-013 | Подтверждён: Order без полей клик-ID; CAPI берёт fbc/fbp только из payment_payload (только monobank); fbc всего 15 при fbclid 349 — рецепт восстановления fbc из fbclid описан; gclid 533 умирает в UTMSession |
| 05.07.2026 | AN-035 | **Вскрыт механизм: is_bot мёртв by design (ранний return); 34 733/36 044 (96,4%) product_view без site_session = ботовый раздув; человеческий CTR view→cart ~3,4%, не 0,12%** |
