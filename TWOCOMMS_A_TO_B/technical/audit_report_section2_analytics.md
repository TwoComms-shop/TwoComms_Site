# Аудит — Раздел 2: Аналитика / UTM / CAPI

> Файл дополняется по мере прохождения пунктов чеклиста `twocomms_global_audit.md`.
> Аудитор ничего не менял в коде — только фиксация фактов для агента-исполнителя.

---

## AN-013. fbc/fbp/fbclid не доходят до CAPI для COD — ПОДТВЕРЖДЕНО (05.07.2026)

### Статус: P0 (низкий match quality всех COD-событий Facebook/TikTok CAPI)

### Полная цепочка данных (код, main c4d81dda)

1. **Захват**: `storefront/utm_middleware.py` пишет `fbclid/gclid/ttclid` из GET и `_fbc/_fbp` из cookies в `request.session['platform_data']` и в `UTMSession` (поля есть в модели). Захват работает **только при наличии utm_* в URL** — если пользователь пришёл с `?fbclid=...` но без utm_*, то `has_utm=False` и `platform_data` НЕ сохраняется в сессию и UTMSession не создаётся (fbclid теряется!). Это отдельный дефект: см. блок «Нюанс А» ниже.
2. **Модель Order**: `orders/models.py` — есть только `utm_source/medium/campaign/content/term` (строки 118–122) и `session_key` (58). Полей `fbclid/gclid/ttclid/fbc/fbp` **НЕТ**.
3. **CAPI-отправка**: `orders/facebook_conversions_service.py:371–381` — `fbp`/`fbc` берутся ТОЛЬКО из `order.payment_payload['tracking']`. То же в `orders/tiktok_events_service.py:115`.
4. **Кто пишет `payment_payload['tracking']`**: только `storefront/views/monobank.py:972` (`'tracking': tracking_context`), причём контекст приходит с клиента (`body.get('tracking', {})`, monobank.py:385).
5. **COD-поток** (`checkout.py::create_order`): `payment_payload` не заполняется вообще → CAPI-события COD-заказов уходят **без fbc/fbp**, external_id падает в fallback `f"session:{order.session_key}"` (facebook_conversions_service.py:388), но `order.session_key` у COD пуст (см. CRO-041) → фактически и этот fallback пуст.

### Нюанс А (найден при аудите): fbclid без utm_* теряется полностью

`utm_middleware.py::process_request`: `platform_data` собирается всегда, но сохраняется в сессию и в UTMSession только внутри `if has_utm:`. Типичный трафик из Facebook Ads имеет `fbclid` ВСЕГДА, а utm-разметку — только если её вручную добавили в ссылку объявления. Все клики без utm_* → fbclid не сохранён нигде (кроме first-touch cookie `twc_ft`, см. `tracking.py:53–71` — там fbclid попадает в snapshot, но из него никто не читает fbclid для CAPI).

### Нюанс Б: `_fbc` cookie может отсутствовать

Meta Pixel создаёт `_fbc` только при загрузке пикселя после клика; если пиксель блокируется (adblock/ITP), единственный источник — `fbclid` из URL, из которого `fbc` можно синтезировать (`fb.1.<timestamp>.<fbclid>`). В коде синтез не реализован.

### Рекомендация исполнителю (расширение TECH-060)

1. Добавить в Order поля `fbclid, gclid, ttclid, fbc, fbp` (или единый JSON `click_ids`), миграция.
2. При создании ЛЮБОГО заказа копировать из `request.session['platform_data']` + fallback из `twc_ft` cookie + синтез fbc из fbclid.
3. В `utm_middleware.py` сохранять `platform_data` в сессию/UTMSession и при `has_utm=False`, если есть хоть один клик-ID.
4. В `facebook_conversions_service.py`/`tiktok_events_service.py` fallback: `payment_payload.tracking` → поля Order → UTMSession по `order.utm_session`.

---

## AN-030 / AN-031. КРИТИЧНО: смена session_key при логине рвёт UTM-связку — ПОДТВЕРЖДЕНО ЛОГИЧЕСКИ (05.07.2026)

### Статус: P0. Механизм разрыва доказан по коду; live-репродукция — при следующем SSH-окне.

### Механизм

1. `django.contrib.auth.login()` вызывает `request.session.cycle_key()` — **session_key меняется**, данные сессии (включая `utm_data`/`platform_data`) переносятся в новую запись.
2. `UTMSession.session_key` — остаётся старым (никакой миграции ключа в коде нет: grep по `cycle_key` в проекте — 0 совпадений вне django).
3. После логина: `utm_middleware` видит `utm_data` в сессии → идёт по ветке `elif 'utm_data' in request.session` → только читает, **не** создаёт/не перевязывает UTMSession под новый ключ.
4. `link_order_to_utm` (utm_tracking.py:349) ищет `UTMSession.objects.get(session_key=request.session.session_key)` — по НОВОМУ ключу → `DoesNotExist` → заказ не привязан, даже в Monobank-потоке.
5. `record_lead`/`record_purchase`/`record_user_action` — тот же lookup, тот же разрыв.

### Точки логина, после которых рвётся связка (все вызывают auth.login → cycle_key)

| Файл:строка | Контекст |
|---|---|
| `storefront/views/auth.py:272` | обычный логин |
| `storefront/views/auth.py:357` | регистрация + автологин |
| `accounts/ajax_auth_views.py:33` | AJAX-логин |
| `accounts/ajax_auth_views.py:111` | AJAX-регистрация |
| `accounts/telegram_verify_views.py:437` | Telegram-верификация |
| social-auth pipeline (`social_django`) | Google OAuth |

### Дополнительный разрыв: НЕ-логин смена ключа

`SESSION_COOKIE_AGE` — уточнить на сервере (TODO при SSH-окне). Если сессия истекает до заказа — та же потеря.

### Особый случай: повторный вход с UTM после логина

Если пользователь после логина снова придёт по ссылке с utm_* → `has_utm=True` → `get_or_create` создаст **вторую** UTMSession под новым ключом → дубль атрибуции (сессии 1015 шт. могут содержать такие дубли одного visitor_id — исполнителю: сверка `COUNT(*) GROUP BY visitor_id HAVING COUNT>1`).

### Рекомендация исполнителю

Вариант 1 (минимальный): signal-хендлер на `user_logged_in` → `UTMSession.objects.filter(session_key=old_key).update(session_key=new_key)` (старый ключ нужно снять ДО login(), например в мидлвари/обёртке).
Вариант 2 (правильный): искать UTMSession по `visitor_id` (cookie `twc_vid` живёт 365 дней, `tracking.py:30–32`) с fallback на session_key; поле `visitor_id` в UTMSession уже есть.

---

## AN-035. Бот-фильтр «мёртв» — ПОДТВЕРЖДЕНО, корневая причина НЕ в детекте (05.07.2026)

### Статус: P1. `is_bot=True = 0 из 2899` — это НЕ сломанный детект, а архитектурная особенность + реальная дыра в другом месте.

### Корневая причина «0 ботов в SiteSession»

`storefront/tracking.py::SimpleAnalyticsMiddleware.process_request:153–159`: при `is_bot(ua)==True` мидлварь делает `return None` **до** создания SiteSession. Т.е. боты не записываются с флагом `is_bot=True` — они вообще не записываются. Строки 181/193 (`bot = is_bot(ua)`; `'is_bot': bot`) — **мёртвый код**: до них доходят только не-боты, `bot` всегда False. Отсюда честный ноль. Детект как таковой работает.

### Реальная дыра № 1: UserAction не фильтрует ботов

`utm_tracking.py::record_user_action` проверяет только `is_request_excluded(request)` — **нет** проверки `is_bot_user_agent`. Вьюхи каталога/товара вызывают `record_product_view` для любых UA → боты (не отфильтрованные `is_analytics_noise_path`) пишут UserAction c `utm_session=None, site_session=None`. Это правдоподобное объяснение аномалии «36k product_view». Верификация при SSH-окне: `UserAction.objects.filter(action_type='product_view', site_session__isnull=True, utm_session__isnull=True).count()` + выборка UA невозможна (UA в UserAction не хранится — ещё один пробел: нечем ретроспективно отделить ботов).

### Реальная дыра № 2: два разных детекта с разной полнотой

- `tracking.py::is_bot` — BOT_SIGNALS (широкий, включает lighthouse/pagespeed/playwright).
- `utm_utils.py::is_bot_user_agent` — 12 паттернов, включая опасно широкие `'google'`, `'facebook'` (UA in-app браузера Facebook `FB_IAB/FB4A` не содержит 'facebook' → ок, но UA `GoogleOther`, `Google-InspectionTool` — боты и ловятся; а вот легитимный UA приложения «Google» app (`GSA/...`) НЕ содержит 'google'... содержит `GSA` — не ловится, ок; риск ложноположительных минимален, но несогласованность списков = разные популяции в UTMSession и SiteSession).

Итог: UTMTrackingMiddleware и SimpleAnalyticsMiddleware фильтруют по РАЗНЫМ спискам → «сессии» в двух таблицах несопоставимы.

### Реальная дыра № 3: referrer-спам не фильтруется нигде (чёрного списка доменов нет — grep `referrer.*spam|blacklist` = 0).

### Рекомендация исполнителю (TECH-063)

1. Решить политику: либо писать ботов с `is_bot=True` (тогда убрать ранний return и оставить флаг), либо не писать вовсе (тогда удалить мёртвые `bot`-ветки и поле оставить для ручной разметки). Первое лучше для диагностики.
2. Единый модуль bot-детекта для обоих мидлварей + `record_user_action`.
3. Добавить UA (усечённый) в metadata UserAction для будущей ретро-фильтрации.
4. Тест: запрос с UA `Googlebot/2.1` не должен создавать UserAction/UTMSession; с обычным UA — должен.

---

## AN-036. `increment_visit` на каждый запрос — ПОДТВЕРЖДЕНО, с уточнением популяции (05.07.2026)

### Статус: P1 (нагрузка) + P2 (искажение метрики)

### Точная семантика (utm_middleware.py:110–127)

Ветка `else` (нет utm в URL И нет `utm_data` в сессии): на **каждый** такой запрос — `UTMSession.objects.get(session_key=...)` (SELECT) и, при находке, `increment_visit()` (UPDATE). Важное уточнение, найденное при аудите:

- Для посетителей, у которых `utm_data` ЕСТЬ в сессии (нормальный UTM-визитёр), выполняется ветка `elif` — БЕЗ SELECT и БЕЗ increment. Т.е. у «живых» UTM-сессий visit_count почти не растёт.
- Ветка else реально срабатывает для: (а) визитёров вообще без UTM — SELECT впустую на каждый pageview (UTMSession не существует, DoesNotExist каждый раз); (б) визитёров, у которых Django-сессия потеряла `utm_data`, но UTMSession-строка жива — **это ровно пост-логин состояние из AN-031 НЕ покрывает** (utm_data переживает cycle_key)… фактический случай (б): истёкшая/новая Django-сессия при живом session_key-совпадении — маловероятно. Значит доминирует случай (а): **бесполезный SELECT на каждый pageview каждого не-UTM посетителя**.
- Второй канал инкремента: `_create_or_update_utm_session` при повторном заходе с utm в URL — легитимный.

### Следствия

1. Нагрузка: ~1 лишний SELECT × каждый pageview всего не-UTM трафика (большинство).
2. `visit_count` семантически = «pageviews при потерянной utm_data», не визиты. Верификация распределения — при SSH-окне: `UTMSession.objects.aggregate(Max('visit_count'), Avg('visit_count'))`.

### Рекомендация исполнителю

1. Убрать SELECT из ветки else (или кэшировать факт «UTMSession нет» флагом в сессии: `request.session['no_utm_session']=True`).
2. Переопределить visit_count как «визиты» (инкремент максимум раз в 30 минут — хранить last_seen и сравнивать) либо переименовать в pageview_count.

---

## Статус верификации по живой БД

SSH-доступ 05.07.2026 после первой успешной сессии начал сбрасываться на этапе kex (`Connection reset by peer`) — предположительно anti-bruteforce/лимит хостинга (Hostsila). 3 попытки с бэкоффом до 60 с — неуспешно. Все числовые утверждения из чеклиста (0/41 заказов с utm, 1015/0 converted, 2899/0 ботов, 36k product_view, 181 search) сняты в предыдущую SSH-сессию и считаются актуальными. Подготовленный скрипт повторной сверки (Django shell) сохранён в тексте задачи; выполнить при следующем SSH-окне и дополнить этот файл фактическими цифрами:
- Orders: total / by pay_type / with utm_source / with utm_session / with session_key / payment_payload.tracking by pay_type
- UTMSession: total / converted / visit_count max+avg / top-5
- SiteSession + PageView: total / is_bot
- UserAction: by action_type / «сироты» без обеих сессий / lead+purchase counts
- SESSION_COOKIE_AGE
