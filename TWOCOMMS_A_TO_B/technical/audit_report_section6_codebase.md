# ОТЧЁТ АУДИТА — РАЗДЕЛ 6: КОДОВАЯ БАЗА (детальные находки для агента-исполнителя)

**Связан с:** `twocomms_global_audit.md` (раздел 6)
**Правило безопасности:** SSH/MySQL-реквизиты в этом файле НЕ фиксируются. «Боевой сервер» = сервер владельца, доступ только у владельца.
**Дата начала:** 05.07.2026

---

## CB-044. Crontab-инвентаризация (ВЫПОЛНЕНО, 05.07.2026)

Снято с боевого сервера командой `crontab -l` (одна SSH-сессия, read-only).

### Полная таблица cron-задач

| # | Расписание | Команда | Скрипт в репо? | Лог | Комментарий |
|---|---|---|---|---|---|
| 1 | `20 4 * * *` (ежедневно 04:20) | `manage.py trim_analytics` | ДА: `twocomms/storefront/management/commands/trim_analytics.py` | `logs/trim_analytics.log` | чистка аналитики — влияет на retention UserAction (связь с DB-004) |
| 2 | `*/30 * * * *` (каждые 30 мин) | `manage.py recover_checkouts` | ДА: `twocomms/orders/management/commands/recover_checkouts.py` | `logs/recover_checkouts.log` | восстановление брошенных чекаутов — КРИТИЧНЫЙ для денег контур; любые правки checkout.py должны учитывать этот фон |
| 3 | `* * * * *` (каждую минуту) | `manage.py run_instagram_bot --ensure` | ДА: `twocomms/management/management/commands/run_instagram_bot.py` | `tmp/ig_bot_cron.log` | самый частый cron; держит IG-бота живым; лог в tmp/ без ротации |
| 4 | `*/5 * * * *` | `manage.py checker_tick` | ДА: `twocomms/management/management/commands/checker_tick.py` | `tmp/checker_tick.log` | назначение — выяснить при аудите management-app |
| 5 | `*/4 * * * *` | `manage.py poll_ig_deal_payments` | ДА: `twocomms/management/management/commands/poll_ig_deal_payments.py` | `tmp/poll_ig_deal_payments.log` | опрос оплат IG-сделок — деньги; каждые 4 минуты |
| 6 | `30 4 * * *` | `manage.py purge_ig_clients` | ДА: `twocomms/management/management/commands/purge_ig_clients.py` | `tmp/purge_ig_clients.log` | чистка IG-клиентов |
| 7 | `15 3 * * *` | `manage.py generate_bot_fingerprints --limit 15 --sleep 2` | ДА: `twocomms/management/management/commands/generate_bot_fingerprints.py` | `tmp/fp_cron.log` | генерация fingerprints для бота |

### Выводы для исполнителя

1. **Все 7 cron-команд существуют в репозитории** — «тихо падающих» cron-задач, ссылающихся на удалённые скрипты, НЕ обнаружено.
2. **Ни один loose-скрипт из корня репо (fix_*.py, deploy*.sh, crawl_all_pages.py и т.д.) НЕ вызывается кроном** → RISK-01 для CB-003 снят: скрипты корня можно архивировать/удалять без риска сломать cron (после grep по коду).
3. **В cron НЕТ:** (а) бэкапа MySQL (см. TD-020 — P0-находка), (б) генерации Google Merchant feed (см. SEO-008 — фид, вероятно, лежит статикой и устаревает), (в) IndexNow/sitemap-пингов, (г) logrotate для tmp/*.log.
4. Логи cron пишутся в 2 разных места: `logs/` (задачи 1–2) и `tmp/` (задачи 3–7) — двойное соглашение; tmp/ может чиститься хостингом.
5. `run_instagram_bot --ensure` каждую минуту = ~1440 запусков python/сутки на shared-хостинге — оценить стоимость запуска интерпретатора (связь с CB-041/TD-015 по памяти Passenger).

---

## CB-043. Git-состояние сервера (ВЫПОЛНЕНО, 05.07.2026)

Снято в той же SSH-сессии: `git status --short`, `git stash list`, `git log -3`, `git branch`.

### Факты

- Ветка: `main`. Последние коммиты совпадают с origin/main (`0c07a63f feat: balance custom print blog conversion blocks`).
- **Отслеживаемые файлы чистые** — конфликтов при `git pull` не будет (модифицированных tracked-файлов нет).
- **Untracked-файлы на бою (полный список):**
  - `passenger_wsgi.py` (в КОРНЕ репо-каталога, НЕ twocomms/) — точка входа Passenger живёт вне git! В репо есть корневой `passenger_wsgi.py`?? — уточнение: git показывает его как untracked, значит на сервере он есть, а в индексе git его нет. В репозитории закоммичен `twocomms/passenger_wsgi.py`. PassengerAppRoot указывает на `~/TWC/TwoComms_Site/twocomms`, т.е. РАБОЧИЙ wsgi — `twocomms/passenger_wsgi.py` (в git), а корневой untracked — вероятно, мёртвый дубль.
  - Диагностические скрипты «на бою», не в git: `twocomms/_db_audit.py`, `twocomms/_fin_diag.py`, `twocomms/dump_products.py`, `twocomms/profile_q.py`, `twocomms/scripts/audit_translations.py`, `twocomms/scripts/dump_category_uk_content.py`, `twocomms/tmp/{fc.py,h429.py,pc.py,merge_one_off.py,_diag_out.txt}`, `twocomms/Anl/`
  - Логи ротации: `twocomms/django.log.1–5`, `twocomms/stderr.log.1–5` (ротация по 5 файлов существует)
  - `twocomms/tmp/pre_merge_backup_20260518_211954.json` — бэкап какого-то merge 18.05.2026
  - `tmp/restart.txt`, `twocomms/tmp/restart.txt`, `twocomms/tmp/ig_bot.pid`, `twocomms/tmp/feeds/feeds_last_build.flag`
- **10 стэшей** на сервере, включая: `WIP on main: c62e9574 fix(finance)…`, `auto-stash-locale-mo`, `predeploy-*`, `WIP … perf(js) Phase 2.1`, `pre_deploy_parser_20260325`, `codex-predeploy-20260304`. Это следы деплой-процесса «stash → pull → (не всегда pop)». **В стэшах может лежать потерянная работа** — исполнителю: просмотреть `git stash show -p` каждого (read-only), задокументировать содержимое, согласовать с владельцем удаление.

### Выводы для исполнителя

1. `git pull` безопасен (tracked чисто). НО: наличие 10 стэшей говорит, что правки «на бою» практикуются — перед КАЖДЫМ деплоем проверять `git status`.
2. `twocomms/tmp/feeds/feeds_last_build.flag` — фид-генерация имеет какой-то триггер НЕ из cron (вероятно, из запроса или management-команды) — учесть в SEO-008.
3. Серверные диагностические скрипты (_db_audit.py и др.) — кандидаты на перенос в репо `scripts/server-diagnostics/` или удаление; сейчас они «невидимы» для ревью.

---

## CB-012. Какой settings-модуль боевой (ВЫПОЛНЕНО, 05.07.2026)

### Факты (сервер + репо)

- `twocomms/passenger_wsgi.py` (боевая точка входа, подтверждено PassengerAppRoot в `~/public_html/.htaccess`): `os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.production_settings")`.
- **Боевой модуль = `twocomms/twocomms/production_settings.py` (639 строк), который делает `from .settings import *` (settings.py — 1342 строки) и переопределяет.**
- env-файлы на сервере: `twocomms/.env` И `twocomms/.env.production` (оба существуют!). production_settings ищет в порядке: `DJANGO_ENV_FILE` → `.env.production` (BASE_DIR, затем parent) → `.env`. Т.е. **фактически грузится `.env.production`**, а `.env` — вероятный источник путаницы (какие значения в нём — проверить отдельной SSH-сессией, НЕ печатая значений секретов, только ключи).
- Ключевые переопределения production_settings: `DEBUG=False`, `SECRET_KEY` из env, DB через env (`DB_ENGINE`, отдельная `DB_NAME_DTF` — вторая БД для dtf! связь с TD-025 db_routers), Redis-настройки (3 БД: cache/static/fragment), `MediaCacheMiddleware` добавляется в конец цепочки, `DISABLE_ANALYTICS` env-флаг может ВЫКЛЮЧИТЬ UTM/Analytics-мидлвари (проверить, не включён ли на бою — если да, объясняет часть разрывов аналитики!), Telegram/NovaPoshta-ключи из env.
- Passenger Python: `virtualenv/.../3.14/bin/python` (Python 3.14), Django==5.2.11, PyMySQL==1.1.2 (подтверждено pip freeze).

### Риски / задачи для исполнителя

1. **RISK-08 закрыт фактом:** править надо `production_settings.py` (или settings.py, помня что production наследует). Любые правки MIDDLEWARE в settings.py могут быть molча изменены production_settings (DISABLE_ANALYTICS-ветка, MediaCacheMiddleware append).
2. **ОБЯЗАТЕЛЬНАЯ проверка исполнителем (1 SSH-сессия):** `grep -c DISABLE_ANALYTICS .env.production .env` и значение (не печатая прочих строк) — если `DISABLE_ANALYTICS=true`, это прямое объяснение части разрывов UTM-аналитики.
3. Комментарий в шапке production_settings «for PythonAnywhere» устарел (хостинг — CloudLinux/Passenger) — мелкий, но вводит в заблуждение.
4. Двойная точка правды подтверждена: 639 строк переопределений поверх 1342 строк базы. Долгосрочная задача — единый settings.py + env, но НЕ сейчас (анти-задача «не рефакторить заодно»).

---

## CB-040. Пины версий (АУДИТ ВЫПОЛНЕН, 05.07.2026 — сам пин делает исполнитель)

Боевые версии сняты с сервера (`pip freeze`, та же SSH-сессия):

| Пакет | requirements.txt | Боевая версия | Действие исполнителя |
|---|---|---|---|
| openai | без пина | **2.30.0** | запинить `openai==2.30.0` |
| google-auth | без пина | **2.52.0** | запинить `google-auth==2.52.0` |
| google-analytics-data | без пина | **0.22.0** | запинить `google-analytics-data==0.22.0` |
| Django | (сверка) | 5.2.11 | — |
| PyMySQL | (сверка) | 1.1.2 | — |

Фикс = 3 строки в `twocomms/requirements.txt`, отдельный коммит `fix(CB-040): pin openai/google-auth/google-analytics-data`.

---

## CB-024. Тестовое покрытие «денежных» потоков (АУДИТ ВЫПОЛНЕН, 05.07.2026 — написание смок-тестов делает исполнитель)

Метод: полная инвентаризация тест-файлов репозитория (find/wc/grep), чтение содержимого ключевых тестов money-path, сверка с боевым кодом checkout/monobank/cart.

### Корректировка базовой линии чек-листа

Утверждение из шапки аудита «checkout/cart/UTM почти без тестов» **частично неверно** и уточняется фактами:

- Всего тест-файлов: **183** (не 177). Из них `storefront/tests/` — **68 файлов, 18 498 строк** (не «почти все finance/management»).
- Money-path тесты СУЩЕСТВУЮТ:

| Файл | Строк | Что реально покрывает |
|---|---|---|
| `storefront/tests/test_checkout.py` | 497 | COD-заказ гостя (создание Order + очистка корзины), снапшот fit-опции в OrderItem, чекаут авторизованного из профиля, guest online_full → вызов monobank, пустая корзина → redirect, промо-сессии; `order_success` рендер + 404; `confirm_payment` |
| `storefront/tests/test_cart.py` | 265 | add/update/remove/clear AJAX, накопление qty, fit-опции раздельными строками, 404 на несуществующий товар, промокоды (apply/remove) |
| `storefront/tests/test_cart_sync.py` | 354 | persist/hydrate корзины в БД, **merge при логине (session+db qty)**, merge custom-cart, кросс-девайс синхронизация (3 интеграционных теста) |
| `storefront/tests/test_utm_tracking.py` | 82 | `record_order_action('purchase'/'lead')`: связка UserAction↔UTMSession↔SiteSession↔Order, **проставление is_converted + conversion_type** — и для авторизованного, и для гостя через `Order.utm_session` |
| `storefront/tests/test_analytics_tracking.py` | 114 | product_view, search, SW/HEAD-запросы не создают сессий, **`test_monobank_success_status_records_purchase_once` — единственный тест идемпотентности purchase** |
| `storefront/tests/test_nova_poshta_checkout_validation.py` | 565 | валидация НП-полей чекаута; упоминает link_order_to_utm |

### Подтверждённые ДЫРЫ покрытия (критично для денег)

1. **`orders/` app — НОЛЬ тестов.** Непокрыты: `facebook_conversions_service.py` (850 строк CAPI — деньги атрибуции), `tiktok_events_service.py` (308 строк), `nova_poshta_service.py`, `telegram_notifications.py`, `status_management.py`, `recover_checkouts` (cron каждые 30 мин трогает заказы — без единого теста!).
2. **`accounts/` app — НОЛЬ тестов.** Непокрыт `cart_middleware.py` — середина цепочки из 26 middleware, восстановление корзины (CRO-035).
3. **Monobank-вебхук:** проверка подписи `_verify_monobank_signature` (monobank.py:196) — НЕ тестируется; идемпотентность повторного callback покрыта ровно ОДНИМ тестом (`test_monobank_success_status_records_purchase_once`), сценарии failure/pending/expired-статусов и `_apply_monobank_status` (monobank.py:1211) — не покрыты.
4. **COD ↔ UTM интеграция:** unit-механизм `record_order_action` покрыт (test_utm_tracking.py), но НЕТ интеграционного теста «COD-заказ через create_order → Order.utm_session заполнен» — потому что самого вызова в checkout.py НЕТ (CRO-041). Тест из test_utm_tracking.py — это acceptance-заготовка: после фикса CRO-041 нужен e2e-тест на уровне view.
5. **Конкурентность остатков:** снятие остатков при заказе и «последний размер на двоих» — тестов нет (связь DB-009). `transaction.atomic` есть (checkout.py:134, monobank.py:539), но атомарность ≠ защита от гонки остатков без select_for_update (проверить исполнителю).
6. **CI отсутствует:** `.github/workflows/` нет ни в корне, ни в twocomms/ — 183 тест-файла НЕ запускаются автоматически. Никто не знает, сколько из них зелёные. Прогон тестов возможен только вручную (и на сервере — с осторожностью: тестовая БД).
7. **Тесты гоняются на SQLite** (settings.py: DB_ENGINE default sqlite), боевая БД MySQL → расхождения (charset, strict mode, атомарность DDL) тестами не ловятся.

### Спецификация минимального смок-пакета для исполнителя (страховка ПЕРЕД фиксами воронки)

Приоритет написания (каждый пункт = отдельный тест-файл/класс, отдельный коммит):

1. `orders/tests/test_monobank_webhook.py`: (а) повторный success-callback не создаёт второй purchase-UserAction и не дублирует CAPI-вызов (mock), (б) невалидная подпись → 400 и заказ не тронут, (в) failure-статус → payment_status='unpaid', корзина восстановима.
2. `orders/tests/test_facebook_capi.py`: event_id детерминирован (дедуп с Pixel), `_send_request_with_retry` при 2 ретраях шлёт события с ОДНИМ event_id, ошибки сети не роняют чекаут.
3. `storefront/tests/test_checkout_utm_integration.py` (acceptance CRO-041): сессия с utm_data → POST create_order (COD) → Order.utm_session != None, Order.utm_source заполнен, UTMSession.is_converted=True, UserAction purchase/lead создан. СЕЙЧАС этот тест упадёт — он и есть definition-of-done фикса.
4. `accounts/tests/test_cart_middleware.py`: анонимная корзина + login → слияние без дублей (поверх существующих unit-тестов cart_sync — но через полный middleware-стек `self.client`).
5. `storefront/tests/test_stock_concurrency.py`: два конкурентных create_order на последнюю единицу → ровно один успешный заказ (или зафиксировать документально, что остатки не снимаются автоматически — тогда тест не нужен, а находка уходит в DB-009).
6. Прогон всего пакета: `python manage.py test storefront orders accounts --parallel` локально/на staging; зафиксировать в журнале число passed/failed как базовую линию (сейчас неизвестно!).

### Риски

- RISK-03: до появления пунктов 1–3 НЕ трогать checkout.py/monobank.py.
- Тестовый прогон на сервере создаёт test_-БД: убедиться, что у MySQL-пользователя нет прав CREATE DATABASE на бою (если нет — тесты гонять только локально/CI на SQLite/MySQL-контейнере).

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | CB-044 | Аудит выполнен: 7 cron-задач, все скрипты в репо; НЕТ бэкап-cron и feed-cron |
| 05.07.2026 | CB-043 | Аудит выполнен: tracked чисто, 10 стэшей, серверные untracked-скрипты |
| 05.07.2026 | CB-012 | Боевой модуль = twocomms.production_settings; на сервере 2 env-файла; DISABLE_ANALYTICS-флаг требует проверки |
| 05.07.2026 | CB-040 | Боевые версии зафиксированы; пин — исполнителю (3 строки) |
| 05.07.2026 | CB-024 | Аудит покрытия выполнен: money-тесты в storefront есть (18,5k строк), но orders/ и accounts/ — 0 тестов, вебхук-подпись и CAPI не покрыты, CI нет; спецификация смок-пакета из 6 пунктов — исполнителю |
