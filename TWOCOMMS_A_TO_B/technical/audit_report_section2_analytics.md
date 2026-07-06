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

### Доп. находка из альтернативной analytics-ветки: fbclid без utm_* может теряться полностью

`utm_middleware.py::process_request` собирает `platform_data` из `fbclid/gclid/ttclid` и cookies, но сохранение в `request.session['platform_data']` и `UTMSession` завязано на ветку `if has_utm`. Типичный рекламный клик может прийти с `fbclid`, но без вручную добавленных `utm_*`; в таком случае click-ID не попадает в UTMSession и не доходит до CAPI. Fallback-кандидат: сохранять `platform_data` при наличии любого click-ID даже без UTM, а при создании заказа читать также first-touch cookie `twc_ft`, если она содержит click-ID.

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

## AN-004. Исключение internal/staff-трафика (АУДИТ ВЫПОЛНЕН, 06.07.2026) — код-слой ЕСТЬ и хороший, но staff НЕ исключается автоматически

Механизм (`storefront/analytics_exclusions.py`, 200+ строк):
1. Модель `AnalyticsExclusion` (models.py:2591) — 5 типов правил: IP, user_id, visitor cookie (twc_vid), UA-substring, path-startswith. Редактируется из админки (`admin.py:443` + CRUD UI `views/admin_analytics_extras.py`), кэш-снапшот с инвалидацией по сигналу (`cache_signals.py:83`).
2. `is_request_excluded()` вызывается во ВСЕХ трёх писателях: `tracking.py:161` (PageView/SiteSession), `utm_middleware.py:67` (UTMSession), `utm_tracking.py record_user_action:51` (UserAction). Покрытие записи полное.
3. **Дыры:**
   - `snapshot.is_empty → return False`: если в таблице нет ни одного правила, staff-трафик пишется. **`user.is_staff` НЕ проверяется автоматически** — нужно вручную завести правило на каждого сотрудника. Правило-однострочник `if user.is_staff: return True` закрыло бы класс проблем.
   - Клиентский слой (GA4/Pixel/TikTok в base.html) исключениями НЕ управляется — пиксели стреляют и для staff (нужен GA4-internal-traffic фильтр или отключение fbq/gtag при is_staff-флаге в шаблоне).
   - Остаточный вопрос владельцу: заведены ли фактические правила в проде (SSH: `AnalyticsExclusion.objects.count()`).

---

## AN-021. ttclid сквозной путь (АУДИТ ВЫПОЛНЕН, 06.07.2026) — разрыв ПОДТВЕРЖДЁН, работает только для monobank-заказов

Полная трасса:
1. Захват: `utm_middleware.py PLATFORM_PARAMS` → `session['platform_data']['ttclid']` + `UTMSession.ttclid` (создаётся только при первом визите С UTM).
2. Доставка в событие: `orders/tiktok_events_service.py:115–119` читает ttclid ТОЛЬКО из `order.payment_payload['tracking']`.
3. `payment_payload['tracking']` пишется в ОДНОМ месте: `views/monobank.py:972` (tracking_context: fbp/fbc/ttclid из cookies + client_ip + UA, строки 871–966).
4. **COD-заказы (`checkout.py create_order`) payment_payload с tracking НЕ получают** → TikTok CAPI для наложки уходит без ttclid → атрибуция only-monobank. То же самое для fbc/fbp в Meta CAPI (перекликается с AN-013).
5. `UTMSession.ttclid` при этом лежит в БД и связан с заказом через `order.utm_session` — сервисы событий его НЕ читают. Фикс-минимум: fallback `order.utm_session.ttclid` в tiktok_events_service + аналогично fbc/fbp в facebook_conversions_service; фикс-правильный: собирать tracking_context в `create_order` тоже.

---

## AN-030. UTM переживает воронку (АУДИТ ВЫПОЛНЕН, 06.07.2026) — два хранилища с разной живучестью

1. Слой session: `session['utm_data']` живёт всю Django-сессию, переживает логин (Django копирует session-данные при cycle_key). НО `link_order_to_utm` (utm_tracking.py:330) его НЕ читает — линкует заказ строго через `UTMSession.objects.get(session_key=...)`.
2. Слой UTMSession: ключ = session_key → **рвётся при логине** (AN-031, подтверждено: нет обработчика user_logged_in для перепривязки; текущие обработчики — только корзина и survey-промокоды, storefront/signals.py:382).
3. Вызовы link_order_to_utm: checkout.py:179 (COD) и monobank.py:584 — оба пути покрыты, это хорошо.
4. Итог: UTM доживает до заказа только если пользователь НЕ логинился между визитом и заказом. Фикс: в link_order_to_utm добавить fallback на `session['utm_data']` (данные уже под рукой) и/или перепривязку UTMSession по user_logged_in. Дубли AN-031 — фиксы объединить в одну задачу.

---

## AN-036. increment_visit на каждый запрос (АУДИТ ВЫПОЛНЕН, 06.07.2026) — подтверждено, двойная цена

1. Ветка else (utm_middleware.py:117–128): для визита БЕЗ UTM в URL и БЕЗ utm_data в сессии — `UTMSession.objects.get(session_key)` + `increment_visit()` = **SELECT + UPDATE на каждый pageview** такого посетителя.
2. Ветка обновления (строки ~215): при визите С UTM у существующей сессии — тоже increment_visit на каждый запрос.
3. Семантика сломана: `visit_count` = счётчик **pageview**, а не визитов (нет time-window дедупликации). Отчёты, читающие visit_count как «визиты», завышены на порядок.
4. Фикс: инкрементить только раз в N минут (хранить last_seen, инкремент если `now - last_seen > 30min`) — заодно уберёт UPDATE-на-каждый-запрос.

---

## AN-037. Атрибуционная модель (АУДИТ ВЫПОЛНЕН, 06.07.2026) — НЕСОГЛАСОВАННОСТЬ first vs last touch

Два хранилища ведут себя ПО-РАЗНОМУ при приходе нового UTM в той же сессии:
1. `session['utm_data']` — **ПЕРЕЗАПИСЫВАЕТСЯ** новым UTM (utm_middleware.py:103) = last touch.
2. `UTMSession` — `get_or_create` по session_key: новые UTM-параметры **ИГНОРИРУЮТСЯ** (defaults применяются только при create; в update-ветке обновляются лишь visitor_id/ip/landing_page) = first touch.
3. Заказ линкуется к UTMSession → фактическая модель атрибуции заказов = **first touch внутри сессии**, при том что session-слой хранит last touch. Никто это решение не принимал осознанно — оно следствие get_or_create.
4. Решение для владельца: выбрать модель явно. Рекомендация: хранить first_touch_* и last_touch_* поля в UTMSession (метаданные обоих касаний), заказ атрибутировать по last non-direct.

---

## AN-039. record_search и PII (АУДИТ ВЫПОЛНЕН, 06.07.2026) — сырой query в metadata, риск низкий, но не нулевой

1. `utm_tracking.py:226` — `record_search` пишет `metadata={'query': query}` в UserAction БЕЗ обрезки/маскировки. Если пользователь введёт в поиск телефон/e-mail — они лягут в БД навсегда.
2. Смягчение: единственный вызов — из поиска каталога; вероятность PII в товарном поиске низкая. Приоритет P3.
3. Фикс-однострочник: truncate до 100 симв. + regex-маска e-mail/телефонов перед записью; ретенция metadata — в рамках общей политики (AN-051/DB-очистка).

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | AN-031 | Подтверждён кодом: миграции UTMSession при логине нет; session['utm_data'] переживает логин — готовый fallback; visitor_id заполнен в 128/1015 |
| 05.07.2026 | AN-013 | Подтверждён: Order без полей клик-ID; CAPI берёт fbc/fbp только из payment_payload (только monobank); fbc всего 15 при fbclid 349 — рецепт восстановления fbc из fbclid описан; gclid 533 умирает в UTMSession |
| 05.07.2026 | AN-035 | **Вскрыт механизм: is_bot мёртв by design (ранний return); 34 733/36 044 (96,4%) product_view без site_session = ботовый раздув; человеческий CTR view→cart ~3,4%, не 0,12%** |
| 06.07.2026 | AN-004 | Серверный слой исключений полный (3 писателя), но is_staff не исключается автоматически; клиентские пиксели не покрыты; count() правил — при SSH |
| 06.07.2026 | AN-021 | **Разрыв подтверждён: ttclid доезжает до TikTok CAPI только у monobank-заказов (payment_payload.tracking пишет один monobank.py:972); COD — без клик-ID** |
| 06.07.2026 | AN-030 | UTM доживает до заказа только без логина между визитом и заказом; link_order_to_utm не использует session['utm_data'] fallback |
| 06.07.2026 | AN-036 | Подтверждено: SELECT+UPDATE на каждый pageview; visit_count = pageview-счётчик, не визиты |
| 06.07.2026 | AN-037 | **Несогласованность: session-слой = last touch, UTMSession/заказы = first touch (следствие get_or_create, не решение)** |
| 06.07.2026 | AN-039 | Сырой query в metadata без маскировки; риск низкий (товарный поиск), фикс-однострочник описан |
