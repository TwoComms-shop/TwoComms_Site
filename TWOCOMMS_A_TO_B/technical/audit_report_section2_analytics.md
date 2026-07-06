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

`record_user_action` (utm_tracking.py) **НЕ содержит бот-фильтра вообще** — только `is_request_excluded` (staff/IP-исключения). `record_product_view` вызывается ��а К��ЖДЫЙ GET карточки (product.py:247) безусловно. Замер БД 05.07.2026:

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

## AN-014. Offline-конверсии delivered (АУДИТ ВЫПОЛНЕН, 06.07.2026) — механизм СУЩЕСТВУЕТ, но не через send_event_for_order_status

**Ключевая коррекция формулировки чек-листа:** статусная модель Order — НЕ «только done/cancelled». В `orders/models.py:10–16` — 5 статусов: `new / prep / ship / done / cancelled` (+ `shipment_status` CharField 100 для текста статуса НП). Цифра «36 done + 5 cancelled» из живой БД — это РАСПРЕДЕЛЕНИЕ данных, а не модель. Отдельного статуса `delivered` нет — `done` = «Отримано» и играет его роль; статуса refused/RTS (отказ на отделении) нет вообще.

**Как offline-конверсия по факту доставки реально работает (полный трейс):**
1. `orders/management/commands/update_tracking_statuses.py` — management-команда, по всем заказам с ТТН (`tracking_number`, исключая cancelled и done+«отримано») дёргает НП API. Запуск предполагается кроном — **наличие крона на сервере не подтверждено** (SSH-батч, ждёт выполнения; в репо crontab-фиксации нет).
2. `orders/nova_poshta_service.py::update_order_tracking_status` (строки 430–479): если `_status_indicates_delivered(...)` и `order.status != 'done'` → `status='done'`, `payment_status='paid'` (для COD это момент «деньги получены»), плюс дедуп-якорь `payment_payload.np_tracking.last_status_code`.
3. Затем `_send_facebook_purchase_event(order)` (строки 481–524): **идемпотентность корректная** — флаг `payment_payload.facebook_events.purchase_sent` проверяется до отправки и ставится после успеха; повторные прогоны крона дублей не создают.
4. `send_event_for_order_status` (facebook_conversions_service.py:812) — **мёртвый публичный API: 0 call-sites** во всём репо (только определение). Чек-лист ссылается на него ошибочно — реальная точка входа п.2–3.

**Найденные разрывы:**
- **P1: offline-конверсия доставки уходит ТОЛЬКО в Meta CAPI.** TikTok (`tiktok_events_service.send_purchase_event`) и GA4 при delivered не вызываются нигде — TikTok узнаёт о purchase только в monobank-потоке (`storefront/views/utils.py:694`). Кросс-платформенное определение purchase расходится (стыкуется с CRO-045).
- **P2: нет offline-события отмены/возврата.** При `cancelled` после отправленного Purchase никакого refund/cancel-события не шлётся ни в одну платформу → ROAS в кабинетах завышен на сумму невыкупов.
- **P2: `payment_status='paid'` для COD проставляется по факту доставки НП**, но событие Purchase уходит с полной суммой заказа без сверки фактического наложенного платежа (частичный выкуп двух-позиционного заказа даст неверную value).
- **P3: `fix_delivered_orders.py`** — вторая команда, разбирающая `shipment_status` строкой (`split(' - ')`) — хрупкий парсинг текстового поля; следы ручного «чинения» рассинхрона доставок.
- Блокер из чек-листа подтверждён частично: модель shipped (`ship`) есть, но переходы `ship→done` автоматизированы только через НП-трекинг; TECH-070 (полная модель `deposit_paid/delivered/refused_rts` + timestamps) остаётся актуальной.

**Остаток (SSH):** подтвердить крон `update_tracking_statuses` в crontab и наличие Purchase-отметок `facebook_events.purchase_sent` в payment_payload реальных done-заказов.

---

## AN-015. test_event_code изоляция (АУДИТ ВЫПОЛНЕН, 06.07.2026) — изоляция есть, но найдена ДЫРА ЗАГРЯЗНЕНИЯ БОЕВОЙ СТАТИСТИКИ

**Инвентаризация механизма (3 слоя):**
1. **Meta CAPI (сервер):** `facebook_conversions_service.py:57` — `FACEBOOK_CAPI_TEST_EVENT_CODE` читается через getattr, но **нигде в settings.py/production_settings.py НЕ определён** (grep = 0) → всегда None, боевые события чистые. ОК.
2. **TikTok CAPI (сервер):** `settings.py:1291` — `TIKTOK_EVENTS_TEST_EVENT_CODE` из env. Если env-переменная останется установленной на проде — ВСЕ серверные TikTok-события пойдут как тестовые (не попадут в боевую статистику). Проверка env на сервере — остаток SSH.
3. **TikTok Pixel (клиент):** `context_processors.py:89–96` → `TIKTOK_TEST_EVENT_CODE` из env → `base.html:13` `data-tiktok-test-event-code` → `analytics-loader.js:1348–1352` `ttq.load(pixel, {test_event_code})`. Тот же риск: забытая env-переменная переводит ВЕСЬ сайт в тест-режим TikTok. Meta Pixel клиентского test-режима не имеет вовсе (изолируется только серверным test_event_code — это штатно для Meta).

**P1-НАХОДКА (обратная сторона задачи): публичная страница `/test-analytics/` загрязняет БОЕВУЮ статистику.**
- `storefront/urls.py:555` → `static_pages.py::test_analytics_events` — **без login_required/staff-проверки, доступна любому**.
- `test_analytics.html:395–421`: через 3 секунды после загрузки **автоматически** стреляет полную воронку PageView→ViewContent→AddToCart→InitiateCheckout→AddPaymentInfo→**Purchase (599 грн)**→Search в Meta Pixel И TikTok Pixel.
- test_event_code применяется ТОЛЬКО к TikTok и ТОЛЬКО если передан `?ttq_test=` или установлен env. **Для Meta Pixel тестового режима на этой странице нет вообще** → каждый заход любого человека/JS-исполняющего бота на `/test-analytics/` = фейковый Purchase 599 грн в боевом Meta Pixel (и в TikTok при пустом env).
- Смягчения: `noindex, follow` в шаблоне есть; в `ROBOTS_INTERNAL_DISALLOW_PATHS` страница НЕ входит (краулится); ссылок на неё в шаблонах не найдено (security through obscurity).
- При 3 purchase за всю историю UserAction даже единичные заходы сюда существенно искажают пиксельную статистику. **Рекомендация: закрыть страницу staff-only (однострочный декоратор) или удалить маршрут.**

**Остаток (ручной шаг владельца/SSH):** `printenv | grep TIKTOK` на сервере; проверить в TikTok Events Manager, не помечены ли боевые события тестовыми.

---

## AN-050. Cookie-consent баннер (АУДИТ ВЫПОЛНЕН, 06.07.2026) — баннера НЕТ, пиксели грузятся без согласия

1. Grep по всем шаблонам и JS (`cookie consent|consent mode|gdpr|cookiebar|cookie-banner`) — **пусто** (подтверждено и в iter4, NEW-401). Баннера согласия не существует ни в каком виде.
2. Пиксели (Meta, TikTok, GTM/GA4) грузятся безусловно для всех посетителей — единственная «задержка» — ленивый interaction-триггер GTM (CRO-004), который является performance-механизмом, а не consent-механизмом.
3. Google Consent Mode v2 не настроен (grep `gtag('consent'` = 0). Для UA-трафика юридический риск низкий (закон «Про захист персональних даних» не требует cookie-баннер в форме GDPR), но: (а) ЕС-посетители обрабатываются без согласия — формальное нарушение GDPR/ePrivacy; (б) с 2024 Google требует Consent Mode v2 для персонализации рекламы по ЕЭЗ-аудиториям — при масштабировании на ЕС реклама деградирует.
4. Решение для владельца: зафиксировано как TECH-077. Минимальный вариант — гео-таргетированный баннер только для не-UA IP + Consent Mode v2 default denied для ЕЭЗ. НЕ блокер для текущ��го UA-рынка.

---

## AN-051. IP и геолокация — законность хранения (АУДИТ ВЫПОЛНЕН, 06.07.2026) — сбор есть, политика НЕ упоминает, retention НЕТ

**Что собирается (код):** `utm_middleware.py:153–192` — в UTMSession пишутся `ip_address` + `country/city/region/timezone` через `get_geolocation(ip)`; `tracking.py` (SiteSession/PageView) также хранит IP и UA. Плюс `UserAction.metadata` может содержать произвольные данные (см. AN-039).

**Проверка политики конфиденциальности (`storefront/support_content.py:1134–1171`, страница `/privacy-policy/` = `_render_support_page('privacy_policy')`):**
1. Текст политики — 2 декоративные секции общих слов («дані потрібні для замовлень», «технічні інструменти аналітики потрібні для оцінки роботи сторінок»).
2. **НЕ упомянуты:** сбор IP-адреса, геолокация по IP, cookie-идентификаторы (`twc_vid`, sessionid), Meta Pixel/TikTok Pixel/GA4 как получатели данных, сроки хранения, права субъекта (доступ/удаление), передача третьим лицам (Nova Poshta, Monobank, Meta, TikTok, Google). Единственное упоминание cookies — в meta_keywords (иронично: только для SEO).
3. **Retention-политика отсутствует:** `trim_analytics.py` чистит только PageView/SiteSession (90 дней); **UTMSession (с IP+гео) и UserAction не чистятся никогда** (подтверждено iter4, NEW-404) → персональные данные накапливаются бессрочно.
4. Вывод: фактическая практика сбора данных существенно шире задокументированной. Риск для UA-рынка умеренный (закон 2297-VI требует уведомление о целях обработки), но текст политики нужно привести в соответствие с реальностью + добавить retention для UTMSession/UserAction (объединяется с NEW-404 и DB-004 в одну задачу «data lifecycle»).

---

## AN-001. Инвентаризация GTM-обвязки GTM-PRLLBF9H (АУДИТ ВЫПОЛНЕН, код-слой, 07.07.2026) — два параллельных конвейера dataLayer + 3 мёртвых артефакта; сам контейнер — ручной шаг владельца

**Ограничение:** доступ к интерфейсу GTM есть только у владельца. Здесь — полная инвентаризация того, ЧТО сайт кладёт в dataLayer (вход контейнера) и как грузится контейнер. Владельцу остаётся сверить теги/триггеры внутри GTM против этой карты.

**Загрузка контейнера (base.html:929–1000):**
1. GTM-PRLLBF9H грузится ЛЕНИВО: по первому user interaction (scroll/click/touch) ИЛИ по таймауту. Таймаут зависит от маршрута и device class: home 25–35s, прочие страницы 4–9s, passive-страницы (about) 12–18s. Подтверждает CRO-004/AN-002: paid-bounce до interaction невидим для всех клиентских тегов.
2. `dataLayer` создаётся в base.html ДО загрузки GTM — события буферизуются корректно, GTM обработает буфер при старте.

**Производители dataLayer — ДВА параллельных конвейера с разными схемами:**

*Конвейер A — GA4-native пуши (правильная ecommerce-схема: `event` + `ecommerce.items[]` + `eventModel` + `event_id` + fbp/fbc):*
| Событие | Источник |
|---|---|
| `view_item` | product-detail.js:1170 (прямой push, не зависит от trackEvent) |
| `select_item` | main.js:1908 (клик по карточке в листинге, с index/item_list_name) |
| `view_item_list` | main.js:1960 (до 50 карточек, guard `__twcViewItemListSent`) |
| `add_to_cart` | main.js:356 `pushAddToCartEvent` (+ снапшот корзины `/cart/items/` для ecomm_prodid) |
| `begin_checkout` | main.js:217 (checkout form start) + checkout-mono.js:369,557 |
| `purchase` | order_success.html:2136 (transaction_id/tax/shipping/coupon; guard sessionStorage + server flag) |
| `login` | auth_login.html:86 (method:password), telegram-verify.js:299 (method:telegram) |
| `share_product` | product-detail.js:733 |
| `pwa_*`/`web_push_*` | modules/pwa-install.js:148, modules/web-push.js:457 |

*Конвейер B — trackEvent-мост (analytics-loader.js:169–458): Meta-style имена (ViewContent, AddToCart, InitiateCheckout, Purchase, AddPaymentInfo, Lead, Search, Contact, CompleteRegistration, AddToWishlist, RemoveFromWishlist, CustomizeProduct, FindLocation) уходят одновременно в fbq, TikTok И в GA-слой:*
- если `win.gtag` уже создан (после загрузки gtag.js) → `gtag('event', 'AddToCart', payload)` — ПЛОСКИЙ payload;
- если gtag ещё нет → `dataLayer.push({event:'AddToCart', eventParameters: payload})` — payload ВЛОЖЕН в `eventParameters`.

**Находки:**
1. **P2 (подтверждение CRO-033 на полном покрытии): каждый воронковый шаг генерирует ДВА dataLayer-события** — `AddToCart`+`add_to_cart`, `ViewContent`+`view_item`, `InitiateCheckout`+`begin_checkout`, `Purchase`+`purchase`. Если в GTM есть триггеры на оба имени (или GA4-тег с «Send all events») — двойной счёт всей воронки в GA4. Проверка триггеров — ручной шаг владельца в GTM UI.
2. **P2: схема конвейера B нестабильна во времени** — одно и то же событие имеет плоские параметры (через gtag) или вложенные в `eventParameters` (через dataLayer) в зависимости от того, успел ли загрузиться gtag.js на момент события. GTM-переменные, настроенные на `eventParameters.value`, потеряют данные gtag-ветки и наоборот. Фикс-кандидат: убрать GA-ветку из trackEvent вовсе (конвейер A полностью покрывает GA4) — это же закрывает находку 1.
3. **P2: gtag.js G-109EFTWM05 грузится НАПРЯМУЮ (analytics-loader.js:871–933, `loadGoogleAnalytics`) параллельно с GTM.** Если внутри GTM-PRLLBF9H тоже есть тег GA4 Configuration с тем же Measurement ID — page_view и все события задваиваются на уровне GA4. Сверка — ручной шаг владельца (или временно выключить один из путей и сравнить Realtime).
4. **P3: `partials/analytics.html` (163 строки) — 100% мёртвый файл**: всё содержимое обёрнуто в `{% comment %}`, внутри placeholder-ID (GA_MEASUREMENT_ID, FACEBOOK_PIXEL_ID, YANDEX_METRICA_ID), но файл по-прежнему `{% include %}`-ится из base.html:1379 на каждом запросе (рендер пустого блока). Кандидат на удаление вместе с include.
5. **P3: Yandex Metrika-ветка мертва**: analytics-loader.js читает `data-ym-id` с `<html>` — атрибут нигде не выставляется → `win.YM_ID=0`, `ym()`-ветка trackEvent недостижима. Мёртвый код ~30 строк.
6. **P3: у анонимов `<div id="am">` (base.html:1115) заполняется data-ga-id/data-clarity-id/data-meta-pixel-id/data-tiktok-pixel-id — эти атрибуты никем не читаются** (loader берёт ID с `<html>`, а `buildAdvancedMatchingMap` читает только em/ph/fn/ct/externalId). Мёртвая ветка шаблона.

**Идентификаторы (hardcode в base.html:11–13, только при `not debug`):** GA4 `G-109EFTWM05`, Clarity `t7u94cvpqc`, Meta Pixel `823958313630148`, TikTok `D43L7DBC77UA61AHLTVG` (+ env-переопределение TIKTOK_PIXEL_ID).

**Остаток (ручной шаг владельца):** экспорт контейнера GTM-PRLLBF9H → сверить теги/триггеры с картой выше; найти мёртвые теги (Yandex? старые имена событий?) и дубли GA4.

---

## AN-003. GA4-события воронки (АУДИТ ВЫПОЛНЕН, код-слой, 07.07.2026) — ядро есть и корректно; add_shipping_info/add_payment_info/payment_type ОТСУТСТВУЮТ; purchase не покрывает COD

**Карта покрытия GA4-воронки (код-слой; live DebugView — ручной шаг владельца):**

| GA4-событие | Статус | Где | items[] | value/currency |
|---|---|---|---|---|
| view_item_list | ЕСТЬ | main.js:1960 | да (до 50) | нет value (норма) |
| select_item | ЕСТЬ | main.js:1908 | да | нет (норма) |
| view_item | ЕСТЬ | product-detail.js:1170 | да | да |
| add_to_cart | ЕСТЬ | main.js:378 | да | да |
| begin_checkout | ЕСТЬ | main.js:217, checkout-mono.js:369/557 | да | да |
| add_shipping_info | **НЕТ** (grep=0) | — | — | — |
| add_payment_info | **НЕТ GA4-native**; только Meta-style `AddPaymentInfo` (checkout-mono.js:312/504) → в GA4 приедет нестандартное имя, в ecommerce-отчёты не попадёт | — | — | — |
| purchase | ЕСТЬ, но **только monobank paid/prepaid** (order_success.html:2136, `shouldSendPurchase` по payment_status) | order_success.html | да | да + tax/shipping/coupon/transaction_id |
| generate_lead | **НЕТ GA4-native**; только Meta-style `Lead` (cart.js:790, custom-print-configurator.js:3224) | — | — | — |
| refund | **НЕТ** (grep=0) | — | — | — |

**Находки:**
1. **P1 (стык с CRO-045): GA4-воронка обрывается на begin_checkout для COD-заказов** — purchase стреляет только при `shouldSendPurchase` (оплаченный monobank-поток). COD (основной поток магазина) в GA4 никогда не даёт purchase → GA4-конверсия/ROAS систематически занижены. Закрывается TECH-066/070 (единое определение purchase + серверное событие доставки).
2. **P2: параметр `payment_type` (cod/prepay) НЕ существует нигде** (grep по static/js + templates = 0) — TECH-007 подтверждён как невыполненный. Добавить в begin_checkout/purchase.
3. **P2: шагов add_shipping_info/add_payment_info в GA4-схеме нет** — при том что данные доступны (выбор НП-отделения = shipping, выбор способа оплаты = payment). Выбор отделения уже трекается как Meta-style `FindLocation` (main.js:1860) — конвертировать/дополнить GA4-парой.
4. **P3: item_id-схема неконсистентна**: view_item_list генерирует синтетический `TC-<pid>-default-S` (main.js:1948), а add_to_cart/purchase шлют реальный offer_id `TC-<pid>-<color>-<size>` → item-scoped отчёты GA4 не сматчат листинговые показы с покупками. Плюс кириллица в offer_id (NEW-406) портит сегментацию.
5. Качество реализации ядра высокое: event_id на каждом событии (задел под дедуп), fbp/fbc в payload, guard от повторной отправки view_item_list/purchase, cart-снапшот для ecomm_prodid, remarketing-параметры ecomm_pagetype/ecomm_totalvalue (задел под Google Ads dynamic remarketing).

**Остаток (ручной шаг владельца):** GA4 DebugView-прогон воронки + проверка, какие имена реально приезжают (add_to_cart vs AddToCart, см. AN-001 находка 1).

---

## AN-010. Meta Pixel 823958313630148 — клиентские события (АУДИТ ВЫПОЛНЕН, код-слой, 07.07.2026) — обвязка образцовая, но 4-й аргумент fbq несёт игнорируемые ключи, а advanced matching есть только у залогиненных

**Механика загрузки (analytics-loader.js:1051–1145):**
1. Pixel ID берётся из `data-meta-pixel-id` на `<html>` (base.html:12, только `not debug`). Загрузка отложенная (см. AN-001) → PageView уходит с задержкой до 25–35s на главной; bounce-визиты теряются (= AN-002).
2. `fbq('init')` с advanced matching из `<div id="am">`; PageView после init; `_fbqLoaded`-флаг + буфер `_fbqBuffer` для событий до загрузки — при блокировщике события копятся в буфере (не теряются в JS, но и не отправляются).
3. `<noscript><img>` fallback присутствует и корректно вынесен в body (base.html:1126).

**События, привязанные к fbq через trackEvent (полный список call-sites):** PageView (init), ViewContent (product-detail.js:1205, main.js:1881, product_detail_new.html:479), AddToCart (main.js:1641, custom-print-configurator.js:3288), InitiateCheckout (main.js:219, checkout-mono.js:381/569), AddPaymentInfo (checkout-mono.js:312/504), Purchase (order_success.html:2213), Lead (cart.js:790, custom-print-configurator.js:3224), Search (main.js:1845/1849), Contact (contacts.html:329/336), CompleteRegistration (auth_register.html:100, telegram-verify.js:290), AddToWishlist/RemoveFromWishlist (favorites.js:100/122), CustomizeProduct (product-detail.js:1132), FindLocation (main.js:1860), RemoveFromCart (ui-fallback.js:202 — мёртвая ветка, см. CRO-033).

**Находки:**
1. **P2: 4-й аргумент `fbq('track', name, data, metaOptions)` содержит `external_id`/`fbp`/`fbc`/`user_data` — fbevents.js поддерживает в options ТОЛЬКО `eventID`.** Остальные ключи браузерный пиксель игнорирует молча: код создаёт ложное впечатление, что per-event user_data уходит в Meta через браузер (реально уходит только через CAPI). Функционального вреда нет (eventID передаётся корректно, fbp/fbc пиксель сам читает из cookie), но при отладке EMQ это дезориентирует. Зачистить или закомментировать.
2. **P2: advanced matching при `init` заполняется ТОЛЬКО для залогиненных** (base.html:1101–1108: em/ph/fn/ct из профиля). Для гостей `#am` пуст, а данные гостя из sessionStorage (`_twc_guest_*`, заполняются в чекауте) уходят лишь в metaOptions.user_data, который пиксель игнорирует (находка 1) → **EMQ гостевых purchase-событий держится только на серверном CAPI + fbp/fbc**. Фикс-кандидат: повторный `fbq('init')` с обновлённым matching после заполнения формы чекаута гостем.
3. **P3: PII (raw email/телефон/имя/город) залогиненного пользователя рендерится в DOM открытым текстом** (div#am). Для Meta это штатный паттерн (пиксель хэширует на клиенте), страницы авторизованных не кэшируются (CRO-032) — утечки через кэш нет; но PII видна любому расширению браузера. Зафиксировано как осознанный трейд-офф.
4. Валидация перед отправкой на месте: email regex, телефон 10–15 цифр, value≥0, currency uppercase, value/currency только для ecommerce-событий — снижает reject rate в Events Manager.
5. Дедуп-задел корректен: `generateEventId()` → `metaOptions.eventID` строкой; серверные CAPI-события должны слать тот же event_id (проверка пары — AN-012).

**Остаток (ручной шаг владельца):** Meta Pixel Helper / Events Manager Test Events — живая сверка, что все события из списка реально стреляют и advanced matching заполняется у залогиненных.

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
| 06.07.2026 | AN-014 | Offline-Purchase при доставке РАБОТАЕТ (НП-трекинг→done→Meta CAPI, идемпотентно), но только Meta; send_event_for_order_status — мёртвый API (0 call-sites); нет refund-событий |
| 06.07.2026 | AN-015 | **P1: публичный /test-analytics/ авто-стреляет фейковый Purchase 599 грн в боевой Meta Pixel** (test-код только для TikTok и только по ?ttq_test=); FB CAPI test-код всегда None (чисто); env TIKTOK_* — остаток SSH |
| 06.07.2026 | AN-050 | Cookie-consent и Consent Mode v2 отсутствуют полностью; пиксели грузятся без согласия; для UA не блокер, для ЕС-трафика — TECH-077 |
| 06.07.2026 | AN-051 | Политика privacy НЕ упоминает IP/гео/пиксели/сроки; UTMSession+UserAction с IP никогда не чистятся (retention нет) — задача «data lifecycle» вместе с NEW-404/DB-004 |
| 07.07.2026 | AN-001 | Код-слой: 2 параллельных конвейера dataLayer (GA4-native + trackEvent Meta-style) = потенциальный двойной счёт всей воронки; gtag.js грузится параллельно с GTM (риск дубля page_view); мёртвые артефакты — partials/analytics.html, YM-ветка, аноним. div#am; остаток — сверка контейнера владельцем |
| 07.07.2026 | AN-003 | Ядро GA4-воронки корректно (view_item_list→…→begin_checkout), НО purchase только monobank (COD невидим GA4 — P1); payment_type/add_shipping_info/add_payment_info отсутствуют (TECH-007 не выполнен); item_id листинга не сматчится с покупками (TC-pid-default-S vs реальный offer) |
| 07.07.2026 | AN-010 | Meta Pixel клиент: полный список call-sites задокументирован; P2 — 4-й аргумент fbq несёт игнорируемые ключи (user_data через браузер НЕ уходит), advanced matching только у залогиненных (гостевой EMQ держится на CAPI); валидация и eventID-дедуп образцовые; остаток — Pixel Helper владельцем |
