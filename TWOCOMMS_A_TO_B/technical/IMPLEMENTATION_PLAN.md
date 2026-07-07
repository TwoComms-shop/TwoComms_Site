# TWOCOMMS — МАСТЕР-ПЛАН ИМПЛЕМЕНТАЦИИ (IMPLEMENTATION_PLAN.md)

**Дата создания:** 07.07.2026
**Источник:** полный аудит по чеклисту `twocomms_global_audit.md` (150/150 пунктов закрыто) + все секционные отчёты `audit_report_*.md` в этой папке.
**Назначение:** единый рабочий документ для агентов-исполнителей. Здесь СВЕДЕНЫ все подтверждённые находки, ранжированы по приоритету, с конкретными файлами/строками, шагами фикса, рисками и критериями приёмки. Ничего из этого ещё НЕ исправлено.
**Как пользоваться:** брать задачи строго по волнам (Волна 0 → 1 → 2 → …). Перед каждой задачей — свериться с разделом «Матрица рисков» в `twocomms_global_audit.md` (RISK-01…15). Детали каждой находки — в указанном секционном отчёте.

---

## 0. КЛЮЧЕВЫЕ ФАКТЫ ОКРУЖЕНИЯ (прочитать обязательно)

- Прод: **https://twocomms.shop**, shared-хостинг Hostsila (LiteSpeed + Passenger), сервер `qlknpodo@195.191.24.169`, проект `~/TWC/TwoComms_Site/twocomms/`. Деплой = `git pull` на сервере.
- Боевой settings-модуль: **`twocomms.production_settings`** (НЕ settings.py — RISK-08). На сервере есть `.env` И `.env.production`.
- БД: **MySQL на сервере** (не локальная SQLite). Все проверки данных — через SSH → Django shell, read-only. **SSH rate-limit подтверждён** (kex reset при серии подключений) — батчить ВСЕ команды в одну сессию, пауза ≥60s (RISK-02).
- Celery удалён; прод работает без воркеров, но Redis-брокер жив → `.delay()` «успешно» публикует в мёртвую очередь (детали TD-015).
- Тестовые прогоны — только с `utm_source=audit` и пометкой заказа (RISK-12).
- Секреты НИКОГДА не коммитить (RISK-15). Перед коммитом — `git diff` на пароли/токены.
- Правило фиксов: один пункт = один атомарный фикс = отдельный коммит `fix(ID): описание`. Миграции — только аддитивные, только после бэкапа (RISK-07).

---

## ВОЛНА 0 — ПРЕДОХРАНИТЕЛИ (делать ПЕРВЫМИ, до любых фиксов кода)

Без этих пунктов любой следующий шаг — игра в рулетку.

### W0-1. 🔴 Сменить SSH-пароль прода (CB-003, P0-security)
- **Проблема:** `deploy_finance.sh` (tracked в git, строка 5) содержит НЕзамаскированный SSH-пароль прода в `export SSHPASS='...'`. Репозиторий public → пароль скомпрометирован.
- **Фикс:** (1) владелец меняет пароль `qlknpodo` / переходит на ключи; (2) замаскировать строку в файле (`***REMOVED***`); (3) коммит. Историю git НЕ переписывать без отдельного согласования (RISK-06).
- **Приёмка:** старый пароль не работает; grep по репо на пароль = 0.
- Отчёт: `audit_report_section6_codebase.md` (CB-003), `SESSION_HANDOFF_2026-07-07.md`.

### W0-2. 🔴 Ротация секретов из git-истории (TD-021, P1)
- **Проблема:** в истории git лежат удалённые `twocomms/db_config.env` (SECRET_KEY, DB_USER/DB_NAME/HOST) и `twocomms/.env.production` (REDIS_URL с кредами) — восстановимы через `git show`.
- **Фикс:** ротация SECRET_KEY + Redis-кредов на сервере (env-файлы, НЕ репо). History-rewrite — только при расшаривании репо.
- **Приёмка:** старые значения невалидны.
- Отчёт: `audit_report_section3_techdebt.md` (TD-021).

### W0-3. 🔴 Регулярные бэкапы MySQL (TD-020 / TECH-042, P0)
- **Проблема:** регулярных бэкапов НЕТ; последний ручной дамп >8 мес. Блокирует ВСЕ миграции (RISK-07).
- **Фикс:** cron-дамп (mysqldump) ежедневно, ротация 7/30 дней, каталог вне web-root; тест восстановления на копии.
- **Приёмка:** дамп появляется по расписанию; восстановление проверено; каталог не доступен по HTTP.

### W0-4. 🔴 Смок-тесты на деньги (CB-024, P0)
- **Проблема:** orders/ — 0 тестов (CAPI 850 строк!), accounts/ — 0; вебхук-подпись monobank без тестов; CI отсутствует; тесты на SQLite vs боевой MySQL.
- **Фикс (минимальный набор ПЕРЕД фиксами воронки):** тест создания COD-заказа; идемпотентность monobank-вебхука; слияние корзины при логине; `link_order_to_utm` (после W1-фиксов); guest COD.
- **Приёмка:** pytest-набор зелёный локально; запускается перед каждым деплоем.
- Отчёт: `audit_report_section6_codebase.md` (CB-024).

### W0-5. Зафиксировать crontab/настройки как инварианты (CB-043/CB-044/CB-012 — уже проаудированы)
- crontab: 7 задач, НЕТ бэкап-cron, НЕТ feed-cron; на сервере 10 stash (возможна потерянная работа) + untracked диаг-скрипты. Действие: разобрать stash с владельцем, задокументировать crontab в репо (`docs/OPS.md`).

---

## ВОЛНА 1 — ДЕНЬГИ И ПРИЁМ ЗАКАЗОВ (P0, чинить сразу после Волны 0)

### W1-1. 🔴 Гостевой COD-чекаут полностью сломан — HTTP 500 (CRO-040, Находка 1)
- **Проблема:** `storefront/views/cart.py:525-527` вызывает `legacy_views.process_guest_order(request)` — функция НЕ существует в пакете `views/` (осталась только в `views.py.backup`, который для этого имени не загружается) → `AttributeError` → 500. Live-подтверждено: гость физически не может оформить заказ. Дополнительно: в guest UI корзины вообще НЕТ `pay_type=cod` (только online_full/prepay_200) и нет submit-кнопки COD.
- **Влияние:** потеря всех гостевых COD-заказов — вероятная главная причина низкой конверсии.
- **Фикс:**
  1. Минимально: `cart.py:525` → `return legacy_views.order_create(request)` (`create_order` уже поддерживает гостей, поля формы совпадают — проверено).
  2. Продуктовое решение с владельцем: возвращать ли настоящий `cod` в UI (значение есть в `Order.PAY_TYPE_CHOICES`, но не выводится).
  3. НЕ использовать ветку `monobank_create_invoice(request, order.id)` — сигнатура принимает только `request` → латентный TypeError (см. W1-5).
- **Приёмка:** live guest COD-заказ создаётся (тестовая пометка), invalid phone → controlled error, NP tokens работают, 0 JS-ошибок.
- Отчёт: `audit_report_checkout_critical.md` (Находка 1 + Дополнение CRO-040).

### W1-2. 🔴 Публичная утечка PII заказов (CRO-044, Находка 2)
- **Проблема:** (а) `/orders/success-preview/` (checkout.py:289-299, urls.py:350) публично рендерит ПОСЛЕДНИЙ реальный заказ: ФИО, телефон, адрес; (б) `/orders/success/<id>/` (checkout.py:281) отдаёт ЛЮБОЙ заказ перебором id без проверки владельца — live-подтверждено `/orders/success/1/` → 200 + purchase-payload анониму.
- **Фикс:** preview → `@staff_member_required` или удалить; success → проверка владельца (user или session_key) либо непредсказуемый токен в URL; убрать preview из `static_pages.py`/sitemap.
- **Приёмка:** аноним получает 403/404 на оба URL; владелец заказа видит свою страницу.
- Отчёт: `audit_report_checkout_critical.md` (Находка 2, Дополнение CRO-044).

### W1-3. 🔴 Вебхук Monobank не проверяет подпись — подделка оплаты (CRO-043, Находка 9)
- **Проблема:** основной webhook доверяет body без проверки `X-Sign` → любой может отметить заказ оплаченным. Плюс `monobank_return` использует unsafe fallback `status_value or 'success'` (monobank.py:1317) — purchase-цепочка (UserAction+Telegram+квитанция) может стартовать из браузерного редиректа без криптографического подтверждения.
- **Фикс:** (1) верификация подписи webhook (публичный ключ Monobank, ECDSA) — приоритет №1; (2) `monobank_return` НЕ должен применять статус сам — только читать актуальный статус через API/ждать webhook; убрать `or 'success'`.
- **Приёмка:** запрос с невалидной подписью → 400, статус заказа не меняется; return-редирект без webhook не переводит заказ в paid.
- Отчёт: `audit_report_payment_security.md`.

### W1-4. 🔴 Промокоды: COD-путь мёртв + лимиты не работают (CRO-046)
- **Проблема:** (а) `checkout.py:224` читает мёртвый ключ сессии `promo_code` (apply пишет `promo_code_id`), обращается к несуществующим `active=True`/`is_valid()` → промокод в COD НЕ применяется НИКОГДА: клиент видит скидку в корзине, а платит полную сумму; (б) `record_usage()` (пишущий `PromoCodeUsage`) имеет 0 call-sites → `one_time_per_user`/групповые лимиты не работают; (в) P1: при prepay_200 остаток в описании инвойса завышен на сумму скидки (`total_sum + discount_amount` двойной счёт; `get_remaining_amount` игнорирует discount); (г) P1: `promo.use()` сжигает лимит при СОЗДАНИИ инвойса, а не при оплате.
- **Фикс:** (а) в COD заменить мёртвый блок логикой monobank-пути (`promo_code_id` + `can_be_used_by_user` + `record_usage(user, order)`); (б) `record_usage` при УСПЕШНОЙ оплате (webhook), не при создании инвойса; (в) починить математику остатка; (г) чистить `promo_code_data` при финализации и в COD; (д) событие `coupon_apply` (TECH-023).
- **Приёмка:** COD-заказ с промо имеет `discount_amount>0` и корректную сумму; повторное применение one_time-кода отклоняется; `PromoCodeUsage.count() > 0` после тестовой оплаты.
- Отчёт: `audit_report_checkout_critical.md` (CRO-046).

### W1-5. Ошибочные состояния чекаута (CRO-047, P1)
- **Проблемы:** (а) исчезнувший товар молча выбрасывается из заказа (`if not product: continue` — checkout.py:187, monobank.py:547); (б) COD без guard `total_sum <= 0` → возможен заказ на 0 грн; (в) `monobank_create_invoice` + `clear_cart` ВНУТРИ `transaction.atomic()` → внешний API в транзакции; при сбое инвойса корзина теряется без заказа (cached_db-сессии не откатываются); (г) стаб `monobank_webhook` в checkout.py:268 (возвращает ok на всё) — удалить; (д) латентный TypeError `monobank_create_invoice(request, order.id)` (checkout.py:~254).
- **Фикс:** missing_items → message + redirect без создания заказа; guard total_sum; порядок: commit заказа → инвойс → clear_cart при успехе; удалить стаб; починить/валидировать ветку online-типов в COD-форме.
- Отчёт: `audit_report_checkout_critical.md` (CRO-047), `audit_report_section1_cro.md` (CRO-041 находка №2).

### W1-6. Кабинет: смена способа оплаты молча не работает (Находка 3, HIGH)
- **Проблема:** `update_payment_method`/`confirm_payment` в checkout.py:305-320 — заглушки (redirect вместо JSON), а `my_orders.html` ждёт JSON → фичи мертвы. Рабочие версии — в `views.py.backup` (строки 2679, 3831).
- **Фикс:** перенести обе функции в `checkout.py` С ПРОВЕРКОЙ владельца заказа; убрать заглушки.
- Отчёт: `audit_report_checkout_critical.md` (Находка 3).

### W1-7. Mobile hero-CTA обрезаны ≤360px (CRO-002, P0-CRO)
- **Проблема:** на mobile 360px кнопки hero («Дивитись каталог», «Створити свій принт») обрезаны `overflow:hidden` (hero height:60vh при контенте 727px, base.html:517); PWA-prompt закрывает 45% первого экрана. Кандидат №1 в причины ATC=0,12%.
- **Фикс:** min-height/auto height для hero на малых viewport; отложить/уменьшить PWA-prompt. После фикса перепроверить CLS (держать min-height).
- **Приёмка:** viewport 360×640 — обе CTA видимы и кликабельны.
- Отчёт: `audit_report_section1_homepage.md`.

---

## ВОЛНА 2 — ДАННЫЕ И АТРИБУЦИЯ (P0: без этого вся аналитика слепа; TECH-060…066)

### W2-1. 🔴 Единая UTM-привязка любого заказа (CRO-041 / AN-013 / AN-021 → TECH-060)
- **Проблема:** COD-путь (`checkout.py::create_order`) не вызывает НИ ОДНОЙ функции трекинга (grep = 0): нет `link_order_to_utm`, `record_order_action`, `session_key` не пишется. И ДАЖЕ в monobank-потоке `link_order_to_utm` фактически не срабатывает: 0/41 заказов с utm (lookup строго по session_key, UTMSession создаётся только при визите с utm, `cycle_key()` при логине рвёт ключ, fallback на `session['utm_data']` отсутствует). Click-ID (fbc/fbp/fbclid/gclid/ttclid) не доходят до CAPI для COD — `payment_payload.tracking` пишет только monobank.py:972.
- **Фикс:**
  1. В `create_order` после `order.save()`: `order.session_key = ...`; `link_order_to_utm(request, order)`; `record_order_action(...)`.
  2. Расширить `link_order_to_utm` fallback-цепочкой: UTMSession по session_key → по `visitor_id` → напрямую `session['utm_data']` + platform_data в поля Order.
  3. Копировать/синтезировать click-ID в `payment_payload.tracking` для ЛЮБОГО заказа (fbc из fbclid если куки нет).
  4. Долгосрочно: единый order-builder для COD+Monobank (устраняет всю дупликацию Волны 1).
- **Приёмка (acceptance = CRO-050):** визит с `?utm_source=audit` → COD-заказ → в БД utm_source='audit', utm_session FK, session_key, UserAction с order_id.
- Отчёты: `audit_report_section1_cro.md` (CRO-041), `audit_report_section2_analytics.md` (AN-013, AN-021, AN-031).

### W2-2. 🔴 is_converted оживить (CRO-042 → TECH-061)
- **Проблема:** 0/1015 UTMSession converted. Следствие W2-1 (fallback-цепочка) — `mark_as_converted` не находит сессию. `record_purchase` — мёртвая функция (0 call-sites).
- **Фикс:** автоматически закрывается W2-1 п.2; удалить/переписать мёртвый `record_purchase`.
- **Приёмка:** после тестового заказа `UTMSession.is_converted=True`, `conversion_type`, `converted_at` заполнены.

### W2-3. 🔴 Единое определение purchase по всем слоям (CRO-045 → TECH-066)
- **Проблема:** 4 слоя × 3 потока = 4 разных определения покупки. COD-покупки видит ТОЛЬКО Meta CAPI (через НП-крон); GA4/TikTok/UserAction — никогда. Внутренний `lead` = создание инвойса ДО оплаты. Prepaid шлёт полную сумму без refund-событий → ROAS завышен на невыкупы.
- **Целевое определение (зафиксировано аудитом):** `purchase` = подтверждённая оплата (webhook с проверенной подписью) ИЛИ получение посылки (NP received); создание заказа — отдельное событие `place_order`/`lead` во всех слоях одинаково.
- **Фикс:** (а) record-слой в COD create_order; (б) UserAction purchase в NP-delivery-путь; (в) TikTok Purchase в NP-delivery-путь + pre-check `purchase_sent` в paid-ветке utils.py; (г) server-side GA4 purchase для COD (Measurement Protocol) или честно задокументировать пробел; (д) `paid_value` отдельным параметром; (е) refund/cancel-события для невыкупов (связка AN-014).
- Отчёт: `audit_report_checkout_critical.md` (CRO-045, матрица).

### W2-4. 🔴 Бот-фильтр и чистота событий (AN-035 / CRO-024 → TECH-063)
- **Проблема:** `SiteSession.is_bot` мёртв by design (early return); `record_user_action` пишет product_view без бот-фильтра; 96,2% product_view без site_session → 40 490 product_view при 55 ATC — метрики врут. Плюс двойной счёт product_view на legacy-301-URL (запись ДО redirect) и отсутствие дедупа повторных просмотров.
- **Фикс:** единый bot-detect на записи UserAction; запись product_view ПОСЛЕ redirect; дедуп 30 мин по session+product; `is_staff` → авто-исключение (AN-004).
- **Приёмка:** новый baseline CRO-051 (см. `audit_report_section1_cro.md`) пересчитан после фикса; ratio view→ATC становится правдоподобным.
- Отчёты: `audit_report_section2_analytics.md` (AN-035), `audit_report_section1_product.md` (CRO-024).

### W2-5. GTM fast-path для платного трафика (CRO-004 / AN-002, P1, ~10 строк)
- **Проблема:** GTM грузится по interaction или таймауту 12-35s; fast-path для `utm_*`/fbclid/gclid/ttclid ОТСУТСТВУЕТ → paid-bounce невидим для Meta/TikTok/GA4, `_fbc` не создаётся.
- **Фикс:** в base.html: если в URL есть click-id/utm → грузить GTM немедленно.
- Отчёт: `audit_report_section1_homepage.md` (CRO-004).

### W2-6. TikTok: нестандартные имена событий (AN-020, P1)
- **Проблема:** и клиент (`analytics-loader.js:394`), и сервер (`tiktok_events_service.py`) шлют Meta-имена «Purchase»/«Lead» вместо словаря TikTok (CompletePayment/PlaceAnOrder/SubmitForm) → цели оптимизации и воронковые отчёты TikTok их НЕ видят.
- **Фикс:** маппинг имён на обоих слоях с сохранением event_id-дедупа; заодно уйти с legacy endpoint `v1.3/pixel/track/`.
- Отчёт: `audit_report_section2_analytics.md` (AN-020).

### W2-7. CAPI/TikTok отправка внутри row-lock транзакции (AN-011 / DB-009, P1)
- **Проблема:** блок отправки Meta+TikTok живёт ВНУТРИ `transaction.atomic()` + `select_for_update()` — до ~25-40s удержания row-lock при retry/timeout. Тот же анти-паттерн: HTTP-вызов Monobank invoice/create внутри atomic (monobank.py:~843) при wait_timeout=60.
- **Фикс:** `transaction.on_commit()` для внешних отправок; инвойс — после commit.
- Отчёты: `audit_report_section2_analytics.md` (AN-011), `audit_report_section5_db.md` (DB-009).

### W2-8. Нормализация utm_source + AI-канал (AN-032 / AN-033 → TECH-009/065, P1/P2)
- **Проблема:** словаря нормализации нет — ig/Instagram/IGShopping/Inst_Vid = 4 написания одного канала; сырые ad id как source. AI-трафик (chatgpt.com — 119 сессий, 3-й источник!) не детектится отдельным каналом.
- **Фикс:** словарь нормализации при записи в UTMTrackingMiddleware; детект chatgpt.com/perplexity.ai/gemini/claude.ai по utm+referrer → канал «AI»; UTM governance-конвенция.

### W2-9. Прочее аналитическое (P1/P2, после ядра)
- **AN-014/TECH-074:** delivered-Purchase уходит только в Meta; нет refund/cancel → добавить TikTok/GA4 в NP-delivery-путь, refund-события.
- **AN-015 (P1):** публичный `/test-analytics/` без auth авто-стреляет Purchase 599 грн в БОЕВОЙ Meta Pixel через 3s → staff-only или удалить.
- **AN-001/CB-034 (P2):** gtag.js G-109EFTWM05 грузится ПАРАЛЛЕЛЬНО GTM → выгрузить контейнер, сверить теги, убрать дубль; dataLayer получает 2 события на ATC (AddToCart + add_to_cart) — проверить триггеры GTM.
- **CRO-033 (P2):** server-side CAPI для AddToCart отсутствует полностью — при блокировщиках ATC теряется.
- **AN-036 (P2):** `increment_visit` = SELECT+UPDATE на каждый pageview → time-window 30 мин.
- **AN-037 (P2):** несогласованность first/last touch → поля first_touch_*/last_touch_*.
- **AN-050/TECH-077 (P2):** cookie-consent баннера нет вообще; для ЕС-трафика — гео-баннер + Consent Mode v2 default denied.
- **AN-051/NEW-404 (P1-retention):** UTMSession+UserAction НЕ чистятся никогда (trim_analytics только PageView/SiteSession); политика приватности не упоминает IP/гео → retention-cron + текст политики.
- **AN-038 (P2):** N+1 в UTM-админке (~сотни запросов на вкладку) + системные нули до W2-1/W2-2.
- **DB-005 (P2):** `utm_cohort_analysis.py` — 3 классических N+1 (~300-400 SQL на рендер) → TruncMonth+annotate.
- **DB-002 (P2):** UserAction без is_bot-индекса; нет `(site_session, action_type)` под воронку.
- **DB-003:** orphan UserAction.order_id 259, 260 без Order — вычистить при retention-работах.

---

## ВОЛНА 3 — НАДЁЖНОСТЬ И ИНФРАСТРУКТУРА (P1)

### W3-1. Мёртвая Celery-очередь глотает Telegram-уведомления (TD-015 / TD-003, P1)
- **Проблема:** прод без Celery-воркера, но Redis-брокер жив → `.delay()` успешно публикует в мёртвую очередь: Telegram-уведомления о смене статуса заказа/ТТН молча теряются (`orders/signals.py::_safe_queue_notification` — sync-fallback только при исключении, которого нет); `CELERY_BEAT_SCHEDULE` survey-check никогда не выполняется; очередь `celery` в Redis растёт. Плюс битый импорт `send_telegram_notification_task` из storefront.tasks → часть отправок синхронна в request-потоке.
- **Уточнение сервера:** Redis Cloud host не резолвится с сервера — рост очереди может не подтверждаться, но уведомления всё равно теряются/синхронны.
- **Фикс:** `async_enabled=False` по умолчанию в `TelegramNotifier.__init__` (одна строка); survey-check → cron-команда; починить/удалить битый импорт; зафиксировать решение «Celery не возвращаем»; вычистить no-op шимы.
- **Приёмка:** смена статуса заказа → Telegram-сообщение приходит.
- Отчёт: `audit_report_section3_techdebt.md` (TD-015, TD-003), `NEXT_AGENT_PROMPT.md` §4.4.

### W3-2. Мониторинг ошибок с алертом (TD-022 → TECH-041, P1)
- **Проблема:** алертинга НЕТ: django.request ERROR → stderr.log «в никуда»; window.onerror нет; handler500 нет. Телеграм-обвязка есть, но не подключена к ошибкам.
- **Фикс:** ERROR-Handler → Telegram с rate-limit; `window.onerror`/`unhandledrejection` → `/api/client-error/`; короткий инцидент-код в user-facing message (CRO-047 п.7).

### W3-3. Server-side кэш выключен куками → LCP 4.8-16.4s (SEO-010, P1)
- **Проблема:** `Vary: Cookie` + `Set-Cookie` (csrftoken+twc_vid) на КАЖДОМ анонимном GET полностью выключают LiteSpeed page cache → каждый визит = полный Django-рендер; TTFB холодный 8-18s; интермиттентные 503/500 (Passenger overload). CWV НЕ зелёные: LCP каталог 16.4s.
- **Фикс:** не ставить csrftoken/twc_vid на анонимные GET (lazy-выдача при первом POST/взаимодействии) или настроить cache-vary ignore; связан с CRO-032-багом ниже.
- **Приёмка:** повторный анонимный GET = cache HIT; LCP mobile < 2.5s на 3 шаблонах.
- Отчёт: `audit_report_section4_seo.md` (SEO-010).

### W3-4. Кэш отдаёт чужой CSRF-токен (CRO-032, P1, live-подтверждён)
- **Проблема:** самописный `cache_page_for_anon` кэширует HTML с чужим `csrfmiddlewaretoken` → POST `/i18n/setlang` (language switcher) = 403 для всех анонов на cache-hit (TTL 600s).
- **Фикс:** не кэшировать формы с inline-токеном (пустой meta + заполнение из cookie — паттерн уже есть в проекте, НЕ ломать); `/cart/summary/`, `/cart/count/` → `Cache-Control: no-store`.
- Отчёт: `audit_report_section1_cart.md` (CRO-032).

### W3-5. Rate limiting и API-поверхность (TD-023 / TD-024, P1/P2)
- **Проблемы:** глобальный лимитер ключуется спуфаемым X-Forwarded-For; fail-open при падении Redis (совместно с TD-012 — молчаливое отключение); логин/регистрация/чекаут/отзывы без точечных лимитов; `/api/analytics/track/` — публичный no-op; Swagger/Redoc публичны.
- **Фикс:** REMOTE_ADDR-ключ (или доверенный XFF-hop), точечные лимиты на auth/checkout, staff-only для Swagger, LOG_IGNORED_EXCEPTIONS+алерт для Redis.

### W3-6. Логи: 958 MB, ротация, PII (TD-016 / CB-045, P1)
- **Проблема:** `logs/` = 958 MB, `nova_poshta_cron.log` = 827 MB без ротации; PII-like hits в django/stderr/recover/NP-логах; rum.log — 24 secret-like hits.
- **Фикс:** logrotate (user-level cron с truncate), маскирование PII в лог-вызовах, удалить мёртвые логи (celery.log и 8 других).

### W3-7. Идемпотентность и гонки статусов (CB-020-паттерн + DB-010, P1/P2)
- **Проблема:** системный паттерн `save(update_fields)` → молчаливый fallback `save()` в 4 местах (utils.py:542/573/723, nova_poshta_service.py:515 — флаг `purchase_sent`!) = риск lost-update и ПОВТОРНОГО CAPI Purchase; admin_update_dropship_status без select_for_update (гонка с вебхуком).
- **Фикс:** убрать fallback, логировать ошибку; select_for_update в dropship-статусах. НЕ менять control flow массово (RISK-04).

---

## ВОЛНА 4 — СТАТУСНАЯ МОДЕЛЬ И ФИНАНСОВАЯ ТОЧНОСТЬ (P1, TECH-070…074)

### W4-1. OrderStatusHistory + timestamps (TD-030 → TECH-070)
- **Проблема:** статусов 5 (new/prep/ship/done/cancelled), но нет истории/timestamps смен; NP-синк авто-переводит в done → транзитные статусы невидимы; refused_rts неотличим от cancelled; delivered≠received склеены.
- **Фикс (аддитивная схема из `audit_report_td_orders.md` — согласовать с владельцем, RISK-07):** таблица OrderStatusHistory (order FK, from, to, ts, source); статус `refused_rts`; разделить delivered/done. Только add-миграции после бэкапа.
- **Приёмка:** CAC created/paid/delivered и RTS% считаются из БД.

### W4-2. NP-синк: RTS-маппинг + cron вместо daemon (TD-031 → TECH-071)
- **Проблема:** трекинг развит, но «відмова НП» не маппится ни во что (заказ зависает в ship); daemon-поток хрупок на Passenger.
- **Фикс:** маппинг RTS-статусов НП; management command в crontab; сверить какой из двух NP-middleware включён в prod.

### W4-3. COGS-снапшот (TD-032 → TECH-072)
- **Проблема:** OrderItem снапшотит только цену продажи; себестоимости нет → маржа невычислима. Дизайн готов в `audit_report_td_orders.md`: `OrderItem.unit_cost_snapshot` (NULL) + `cost_source`, источник — консигнационный unit_cost.

### W4-4. Воронка CustomPrintLead (TD-033 → TECH-062, P0-data)
- **Проблема:** 28/28 лидов вечно `new`; связки с заказами 0.
- **Фикс:** статусная модель `new → in_progress → quoted → won/lost(причина)`, движение из админки, привязка lead→order.
- **Приёмка:** ни одного лида старше 7 дней в `new` без причины.

### W4-5. Кастом-принт в чекауте (CRO-034, P2)
- **Проблемы:** COD не проверяет цену approved-кастома (лид с ценой 0 уедет бесплатно — checkout.py:233 глотает ошибку, подтверждено CB-020); COD молча удаляет из сессии кастом-записи без lead_id; промокод дисконтирует согласованную цену кастома; брошенный инвойс не отвязывает lead.order.
- **Фикс:** вынести `_split_custom_cart_entries` в общий модуль для обоих потоков; guard цены кастома.

### W4-6. Meta Ads spend-импорт (TECH-073) + офлайн-конверсии delivered/refused (TECH-074) — после W4-1/W2-3.

---

## ВОЛНА 5 — SEO / ФИДЫ (P1/P2)

### W5-1. GMC-фид: 301 на всех ссылках (SEO-008, P1)
- **Проблема:** все 384 `<g:link>` дают 301 с потерей `color` → риск disapproval «mismatched landing page»; availability захардкожен in_stock; нет кэша ответа (~3s CPU/GET).
- **Фикс:** генерировать финальные URL без redirect (с color-параметром в каноническом виде); кэш фида; gtin/identifier_exists/shipping — по решению владельца; удалить пустой статический xml из корня.

### W5-2. Пагинация каталога (CRO-011, P1)
- **Проблемы:** `?page=N` сбрасывает `?color=`; `/catalog/?page=2..N` — индексируемые дубли БЕЗ товаров (корень не рендерит грид).
- **Фикс:** сохранять GET-параметры в пагинаторе; noindex/redirect для корневой пагинации.

### W5-3. Оптимизированные изображения: backfill (CRO-022 / CRO-012, P1)
- **Проблема:** у 20/65 товаров LCP-фото без optimized-вариантов (404 на диске, оригиналы 100-276 KB) — старый bind-баг, код уже починен, backfill не выполнен; 9/16 карточек каталога без srcset/AVIF.
- **Фикс:** `manage.py optimize_images` на сервере + алерт в audit_product_images; eager 4-8 первых карточек каталога.

### W5-4. Размерные сетки лонгсливов (CRO-023 → TECH-005/012, P1)
- **Проблема:** у ВСЕХ лонгсливов нет таблицы замеров (нет preset/SizeGrid); seo-pricing блок даёт 24 битые ссылки `/product/{id}/` → 404.
- **Фикс:** SizeGrid longsleeve + привязки; fix `get_absolute_url` в seo-pricing; событие `view_size_guide`.

### W5-5. Сортировка: футболки против позиционирования (CRO-013/CRO-001, P1-продукт)
- **Проблема:** top-5 priority — все футболки; showcase-хардкод; Category.order все =0. Бренд = лонгсливы/худи.
- **Фикс:** пересмотр priority в админке + порядок категорий; правки текстов hero (3 строки копирайта).

### W5-6. Мета-качество (SEO-007, P3-батчи)
- 41 title >65, 121 description >165, 8 title <30, 6 desc-пустышек категорий блога, дубли Reality Bends ×3 (ru/en), 11 en-блогов с непереведёнными title. Батчами по 10 страниц с GSC-контролем (RISK-13). Полные списки: `data/seo_crawl_analysis_report.md`.
- Прочее P2/P3: Google Indexing без дедупа/квоты при масс-пересохранении (SEO-009); NewsArticle/GovernmentOrganization-bloat в Organization schema (NEW-411); lastmod в sitemap-products (NEW-410); TikTok в sameAs; Rich Results Test вручную.

### W5-7. Наличие/сток как продуктовое решение (CRO-014/CRO-025 → TECH-010)
- **Проблема:** механики наличия НЕТ как класса (75/75 вариантов stock=0, поле мертво); price_override не обновляет цену при выборе цвета; select_size/select_color не трекаются.
- **Фикс:** сначала продуктовое решение владельца (печать под заказ vs склад), потом: honest-статусы, синхронизация price_override, события TECH-008.

---

## ВОЛНА 6 — КОРЗИНА / UX (P2)

- **CRO-031 (P1):** `update_cart`/`remove_from_cart` не сбрасывают Monobank-инвойс → оплата устаревшей суммы при сбое JS. + In-flight guard двойного клика в main.js; fallback remove удаляет все варианты товара.
- **CRO-030 (P2):** `user_state_hint` читает `custom_cart` вместо `custom_print_cart` (мёртвый код — сначала решить, регистрировать ли); `/cart/count/` игнорирует кастом; GET `cart_summary` мутирует сессию и сбрасывает инвойс.
- **CRO-026 (P1-growth):** отзывов 0 опубликованных при образцовой инфраструктуре — построить петлю сбора (заказ delivered → запрос отзыва), vote-UI; блок отзывов на главной (TECH-013) — переиспользование готового.
- **CRO-027 (P2/P3):** рекомендации — дедуп дублей, фильтр отменённых заказов из «часто покупают вместе», унификация TTL.
- **CRO-021-побочно (P2):** 65/65 описаний с идентичным boilerplate-манифестом (near-duplicate) — переписывание батчами (RISK-13), заодно прямой ответ «Из чего?» в 1-м абзаце (AEO-003).
- **NEW-406/407 (P3):** русский цвет в offer_id «TC-0020-ЧЕРНЫЙ-M»; legacy-дефолт pay_type='cod' в checkout.py:119.

---

## ВОЛНА 7 — ГИГИЕНА РЕПОЗИТОРИЯ И КОДА (P2, строго после Волн 0-2)

⚠️ Перед ЛЮБЫМ удалением: crontab уже инвентаризирован (CB-044: 7 задач, все скрипты в репо), но перепроверить непосредственно перед удалением (RISK-01).

- **CB-004/TD-001 — ⚠️ КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ:** `storefront/views.py.backup` — ЖИВОЙ рантайм-код (`_load_legacy_views` exec-ит его, 30 боевых маршрутов, `/pricelist_opt.xlsx` и др.). НЕ УДАЛЯТЬ. План: миграция 102 имён из whitelist в нормальные модули, потом удаление. Безопасно удалить сейчас: styles.css.bak2 (445KB), order_success_old.html, tmp_old_index.html.
- **TD-006:** legacy_stubs.py — 48 заглушек живы в urls; admin_store_* возвращают фейковый `{'status':'ok'}`. План: fix monobank quick (Находка 4 payment_security), перенос живых, 410 мёртвым.
- **CB-001:** 145 tracked-артефактов ≈170MB (tmp/ 101MB, artifacts/, output/, личное фото me.JPG ×2 вкл. collectstatic-путь) → `git rm --cached` + .gitignore (без filter-repo, RISK-06). Цель: репо 328MB → <100MB.
- **CB-002/TD-005:** 175-202 md в корне → `docs/archive/` одним git mv-PR (ссылок из кода 0 — проверено).
- **CB-003:** 65 loose-скриптов; cron их не вызывает → рассортировать scripts/archive, удалить fix_*.
- **CB-005:** xlsx с закупочными ценами — вынести из git (репо публичный!).
- **CB-020 (P1-точечно):** 734 широких except; чинить ТОЛЬКО топ-20 денежных мест (ранжированы в `audit_report_section6_codebase.md`): checkout.py:233 (цена кастома!), utils.py:542/573/723, nova_poshta_service.py:515, str(e) клиенту (cart.py:943, monobank.py:288). Только добавлять logging, НЕ менять control flow (RISK-04).
- **CB-021:** 120 print() → logger; 4 файла покрывают 100/120 (telegram_notifications.py 47, telegram_bot.py 46, dropshipper_views.py 22, telegram_views.py 5).
- **CB-030/031 CSS-диета:** боевой бандл = styles.purged.css 394KB; удалить 1.1MB CSS-сирот (styles.min.css, styles.direct.css, styles.base.css, critical-home.min.css — 0 ссылок, регрессия невозможна); ВЕРНУТЬ purge-пайплайн+safelist в репо (без него новый класс в шаблоне молча без стилей — RISK-05!).
- **CB-035/036 шрифты/vendor:** удалить .ttf-дубли (woff2 достаточно) ≈ −50% веса шрифтов −700KB FA; subsetting кириллица+латиница.
- **CB-033:** inline-скрипты base.html — вынести Ahrefs/GTM/analytics-инжектор в кэшируемый файл; убрать дубль device-class детектора; удалить закомментированный блок + partials/analytics.html (163 строки мёртвого include).
- **CB-042:** удалить RequestTraceMiddleware (подтверждённый мертвец) и ImageOptimizationMiddleware (no-op, CB-041: флаг выключен, ThreadPoolExecutor впустую); мёртвая ветка ForceHTTPS в production_settings.py:48-53; задокументировать инварианты порядка 26 middleware в шапке (RISK-09).
- **CB-014:** dtf collectstatic-override → переименовать в collectstatic_dtf или удалить (P2: hard-fail всего collectstatic).
- **CB-015:** DEAD-кандидаты команд: finance_seed_demo, notify_test_shops, send_storage_test, send_test_receipt (карта 93 команд: `cb015_management_commands_map.md`).
- **CB-040:** запинить openai==2.30.0, google-auth==2.52.0, google-analytics-data==0.22.0.
- **CB-022:** god-files НЕ рефакторить сейчас; порядок PR при декомпозиции: cart→admin→mgmt-models→mgmt-views→storefront-models; инвариант: пустой makemigrations-дифф; НЕ трогать `_load_legacy_views`.
- **TD-007:** удалить мёртвый ab_testing.py (0 импортов); TD-014: удалить незарегистрированный MediaCacheMiddleware-код.
- **DB-004:** retention для `management_leadparsingresult` (492 MB!), pageview (64 MB), UserAction (MyISAM! — рассмотреть InnoDB при переносе).
- **CB-023:** 56 console.log → debug-флаг/вырезать при минификации.

---

## ЗАВИСИМОСТИ МЕЖДУ ВОЛНАМИ (критический путь)

```
W0-3 (бэкапы) ──→ любые миграции (W4-1, W4-3)
W0-4 (смок-тесты) ──→ W1-* (все фиксы чекаута/оплаты)
W1-1 (guest COD) ──→ W2-1 (UTM в COD-заказе) ──→ W2-2 (is_converted) ──→ AN-038 (отчёты оживают)
W2-3 (purchase-определение) ──→ W2-6 (TikTok имена), W4-6 (offline-конверсии)
W2-4 (бот-фильтр) ──→ пересчёт baseline CRO-051 ──→ честные CRO-выводы
W3-3 (кэш/куки) ↔ W3-4 (CSRF в кэше) — делать вместе
W7 (гигиена) — только после стабилизации Волн 0-2
```

## КРИТЕРИЙ «СДЕЛАНО» ДЛЯ ВСЕГО ПЛАНА

1. Guest COD работает live; заказы с рекламной ссылки имеют utm+click-ID; `is_converted` растёт.
2. Webhook Monobank с невалидной подписью отклоняется; PII-страницы закрыты.
3. Промокоды применяются в обоих потоках, лимиты работают.
4. Одинаковое purchase-определение в UserAction/GA4/Meta/TikTok, задокументировано в TECHNICAL_TASKS.md.
5. Baseline воронки (CRO-051) пересчитан на чистых данных и сохранён как новая базовая линия.
6. Бэкапы + алертинг + ротация логов живут в cron.
7. Смок-тесты зелёные, прогоняются перед каждым деплоем.

## ССЫЛКИ НА ДЕТАЛЬНЫЕ ОТЧЁТЫ

| Область | Файл |
|---|---|
| Чеклист-источник (150 пунктов, все закрыты аудитом) | `twocomms_global_audit.md` |
| Чекаут: guest COD 500, PII, purchase-матрица, промокоды | `audit_report_checkout_critical.md` |
| Monobank подпись/идемпотентность | `audit_report_payment_security.md` |
| UTM-разрывы CRO-041/042, baseline воронки | `audit_report_section1_cro.md` |
| Корзина (архитектура, кэш/CSRF, пиксели ATC) | `audit_report_section1_cart.md` |
| Главная / каталог / карточка / изображения / тексты | `audit_report_section1_{homepage,catalog,product,images,texts}.md` |
| Аналитика и пиксели (GTM/GA4/Meta/TikTok/UTM/consent) | `audit_report_section2_analytics.md` |
| Тех. долг (Celery, бэкапы, логи, rate-limit, статусы) | `audit_report_section3_techdebt.md`, `audit_report_td_orders.md` |
| SEO/AEO (CWV, фид, краул 489 URL, schema) | `audit_report_section4_seo.md`, `data/seo_crawl_analysis_report.md` |
| БД (индексы, N+1, retention, транзакции) | `audit_report_section5_db.md` |
| Кодовая база (гигиена, except, CSS, middleware, команды) | `audit_report_section6_codebase.md`, `cb015_management_commands_map.md`, `audit_report_legacy_stubs.md` |
| Реестр задач TECH-NNN | `TECHNICAL_TASKS.md` |
