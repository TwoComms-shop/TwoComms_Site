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
- env-файлы на сервере: `twocomms/.env` И `twocomms/.env.production` (оба существуют!). production_settings ищет в порядке: `DJANGO_ENV_FILE` → `.env.production` (BASE_DIR, затем parent) → `.env`. Т.е. **фактически грузится `.env.production`**, а `.env` — вероятный источн��к пу��аницы (какие значения в нём — проверить отдельной SSH-сессией, НЕ печатая значений секретов, только ключи).
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
4. **COD ↔ UTM интеграция:** unit-механизм `record_order_action` покрыт (test_utm_tracking.py), но НЕТ интеграционного теста «COD-заказ через create_order → Order.utm_session заполнен» — потому что самого вызова в checkout.py НЕ�� (CRO-041). Тест из test_utm_tracking.py — это acceptance-заготовка: после фикса CRO-041 нужен e2e-тест на уровне view.
5. **Конкурентность остатков:** снятие остатков при заказе и «последний размер на двоих» — тестов нет (связь DB-009). `transaction.atomic` есть (checkout.py:134, monobank.py:539), но атомарность ≠ защита ����т гонки остатков без select_for_update (проверить исполнителю).
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

## CB-001. Артефакты и скриншоты в git (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Факты (прямые замеры, git ls-files + du, 07.07.2026)

Всего **145 tracked-файлов** в мусорных директориях (чеклист говорил 132 — фактически больше):

| Директория | Tracked-файлов | Размер tracked (KB) | Содержимое |
|---|---|---|---|
| `tmp/` | 69 | **101 184** | smoke-скриншоты (smoke_home.png 6.2MB!), custom-print-live-artifacts (десяток PNG по 3–4.5MB) |
| `artifacts/` | 29 | 26 960 | lighthouse-JSON, скриншоты проверок |
| `output/` | 34 | 26 232 | verification-скриншоты (custom-print-hero-*.png по 3.5MB) |
| `BrandDNA/` | 7 (+11 c twocomms/static/BrandDNA) | 6 788 | **личное фото владельца `me.JPG` 4.8MB — в ДВУХ копиях**: `BrandDNA/me.JPG` и `twocomms/static/BrandDNA/me.JPG` |
| `opros/` | 2 | 3 880 | win-all.png 2.6MB и др. |
| `newCatalog/` | 4 | 3 748 | референс-PNG каталога |
| `Ideas/` | 61 | 4 452 | брейншторм-заметки (текст+изображения) |

- **Топ жирных файлов:** `tmp/smoke_home.png` 6.2MB; `me.JPG` 4.8MB ×2; ~15 PNG по 3–4.5MB в `tmp/custom-print-live-artifacts/` и `output/verification/`. Итого мусор ≈ **170MB tracked** — основная причина 328MB репо.
- **Runtime-зависимости от этих директорий: НЕТ.** Проверено grep по `*.py/*.html/*.css/*.js`: единственные упоминания `BrandDNA` — комментарии в `storefront/seo_utils.py:166` и `storefront/templatetags/i18n_links.py:56` (реальный файл иконки — `img/lang/ptn.png`, НЕ из BrandDNA/). `me.JPG` не референсится нигде (в т.ч. копия в `twocomms/static/BrandDNA/` — мёртвый вес, попадает в collectstatic!).
- `.gitignore` НЕ содержит `artifacts/`, `output/`, `tmp/` (только `tmp/critical-extraction/`), `opros/`, `newCatalog/` — см. CB-006.

### Задачи для исполнителя (порядок)

1. `git rm -r --cached artifacts/ output/ tmp/ opros/ newCatalog/` + `git rm --cached BrandDNA/me.JPG twocomms/static/BrandDNA/me.JPG` (личное фото — приоритет: приватность).
2. Добавить директории в `.gitignore` (см. CB-006 ниже).
3. `Ideas/` — решение владельца (это заметки, не артефакты; кандидат в `docs/ideas/` или приватное хранилище).
4. НЕ запускать `git filter-repo` без явного согласования (правило чеклиста). Без переписывания истории размер `.git` не уменьшится — но свежие клоны через `--depth 1` станут лёгкими после шага 1.
5. ВНИМАНИЕ: `twocomms/static/BrandDNA/` удалять с проверкой collectstatic на сервере (файл мог попасть в `staticfiles/`).

---

## CB-005. XLSX с закупочными ценами в репо (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Факты

- В git отслеживаются 2 файла: `twocomms/wholesale_prices.xlsx` и `twocomms/Оптові ціни TwoComms v12 UA Final Clean.xlsx` — оптовые (закупочные) цены. Репо приватный сейчас, но это бомба при смене видимости/утечке форка.
- **Runtime они НЕ нужны — подтверждено кодом и live-проверкой:**
  - URL `/pricelist_opt.xlsx` (urls.py:570) обслуживается view `wholesale_prices_xlsx`, который **генерирует XLSX на лету** через openpyxl из БД (реализация в `views.py.backup:6434` — грузится лениво через `_load_legacy_views`, см. важную находку ниже).
  - Live-проверка 07.07.2026: `curl https://twocomms.shop/pricelist_opt.xlsx` → 200, 9161 байт (динамическая генерация, не статик 9KB≠файлы в репо).
  - Tracked xlsx — это, вероятно, старый вывод команды `generate_wholesale_prices` (default output=`wholesale_prices.xlsx`, management/commands/generate_wholesale_prices.py:113) — одноразовый артефакт.
- **ВАЖНАЯ ПОБОЧНАЯ НАХОДКА (для CB-001/исполнителей):** `twocomms/storefront/views.py.backup` (7790 строк) — НЕ мёртвый код! Он загружается в рантайме через `_load_legacy_views()` (`storefront/views/__init__.py:329–354`, SourceFileLoader по пути `views.py.backup`) и обслуживает живые маршруты (`/pricelist_opt.xlsx` и др. `_legacy_view`-роуты из urls.py). В `.gitignore` есть явное исключение `!twocomms/storefront/views.py.backup`. Удаление этого файла СЛОМАЕТ прод. Согласуется с `audit_report_legacy_stubs.md`.

### Задачи для исполнителя

1. `git rm --cached` обоих xlsx + паттерн `*.xlsx` в `.gitignore` (проверить, что нигде в коде нет чтения этих файлов с диска — grep подтвердил: нет).
2. Файлы передать владельцу / в приватное хранилище.
3. НЕ трогать `views.py.backup` в рамках этого пункта.

---

## CB-006. Аудит .gitignore (ВЫПОЛНЕН, 07.07.2026)

### Факты

- `.gitignore` 278 строк, секреты покрыты хорошо: `.env*` (c исключениями example/sample), ключи, `json/` (service-account), майнерские артефакты (следы security-инцидента!), `.deploy_pass`, `.kiro/settings/mcp.json`.
- **Дыры покрытия (tracked-файлы, которые должны игнорироваться):**
  - НЕТ `artifacts/`, `output/`, `tmp/` (только `tmp/critical-extraction/`), `opros/`, `newCatalog/`;
  - НЕТ `*.xlsx` → 2 файла с закупочными ценами tracked (CB-005);
  - `*.bak` есть, но ��Е `*.bak2` → tracked `styles.css.bak2` (445KB);
  - `*.backup` есть, НО с явным исключением `!twocomms/storefront/views.py.backup` — это НЕ ошибка: файл живой (см. CB-005), исключение оставить;
  - `*.log` покрыт (дважды — дубль строк 77 и 184, косметика).
- Опасный широкий паттерн: `lib/`, `lib64/`, `var/`, `target/` — стандартный python-шаблон, реальных коллизий с проектом не найдено.
- Паттерны-обереги от майнера (`**/xmrig`, `**/mn.sh`, `**/authorized_keys*`) — подтверждают прошлый инцидент безопасности на сервере; для исполнителя: не удалять.

### Задачи для исполнителя (один PR вместе с CB-001/CB-005)

```gitignore
# добавить:
artifacts/
output/
tmp/
opros/
newCatalog/
*.xlsx
*.bak2
```
Плюс `git rm --cached` соответствующих файлов (сами файлы на диске остаются).

---

## CB-007. Каталоги-сироты и AI-конфиги (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Факты (tracked-файлы по данным git ls-files, 07.07.2026)

| Каталог | Tracked | Размер | Вердикт-кандидат |
|---|---|---|---|
| `Promt/` | 0 | — | не tracked, только на диске — игнорировать/удалить локально |
| `Ideas/` | 61 | 4.4MB | заметки владельца — решение владельца (docs/ или приватно) |
| `opros/` | 2 | 3.8MB | скриншоты опроса — удалить из git (входит в CB-001) |
| `newCatalog/` | 4 | 3.7MB | референсы редизайна — удалить из git (CB-001) |
| `BrandDNA/` | 11 (вкл. twocomms/static/) | 6.7MB | личное фото me.JPG — удалить из git приоритетно (CB-001) |
| `.claude/` | 2 | 28K | AI-конфиг |
| `.codex/` | 1 | 8K | AI-конфиг |
| `.cursor/` | 2 | 8K | AI-конфиг |
| `.kiro/` | 53 | 984K | самый большой AI-конфиг (steering-доки, specs) |
| `.serena/` | 5 | 28K | AI-конфиг |
| `.zenflow/` | 1 | 4K | AI-конфиг |
| `.superpowers/` | 5 | 24K | AI-конфиг (в .gitignore добавлен, но 5 файлов уже tracked — заигнорены только новые) |

- Код НЕ ссылается ни на один из каталогов (grep по py/html/js/css: только 2 комментария, см. CB-001).
- AI-конфиги суммарно ~1.1MB / 69 файлов — не критично по размеру, но 7 инструментов = когнитивный шум. `.kiro/` выглядит наиболее живым (949KB steering/specs, упоминается в .gitignore как активный: mcp.json исключён из git осознанно).

### Задачи для исполнителя

1. Спросить владельца, какие AI-инструменты реально живы (судя по гигиене .gitignore — Kiro точно жив).
2. Мёртвые AI-конфиги → `git rm -r --cached` + .gitignore.
3. `.superpowers/`: уже в .gitignore — доудалить 5 tracked-файлов из индекса для консистентности.
4. `opros/`, `newCatalog/`, `BrandDNA/` — покрываются PR по CB-001.

---

## CB-002. md-отчёты в корне (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Факты

- Фактически tracked в корне: **175 md-файлов, ~3.9MB** (чеклист говорил 202 — часть уже убрана ранее).
- Крупнейшие кластеры по префиксу: PERFORMANCE_* (23), UTM_* (10), PRODUCT_* (7), META_* (7), VIEWS_* (5), GTM_* (5), DTF_* (5), CART_* (5), TRACKING_* (4), NOVA_* (4), MODAL_* (4).
- **Ссылок из кода нет:** grep всех 175 имён по `twocomms/` (*.py/*.html/*.js) — 0 совпадений. Перенос безопасен.
- `docs/` уже существует — целевая структура `docs/archive/...` готова к использованию.

### Задачи для исполнителя

1. Один PR: `git mv` всех отчётных md в `docs/archive/2025/` и `docs/archive/2026/` (по дате из содержимого/git log).
2. В корне оставить README.md, DEPLOY.md, CHANGELOG.md (проверить их наличие; при отсутствии — создать заглушки не нужно, просто не переносить существующие аналоги).

---

## CB-003. Loose-скрипты в корне (АУДИТ ВЫПОЛНЕН, 07.07.2026) + P0-НАХОДКА

### ⚠️ P0: пароль SSH прода в открытом виде в git

- **`deploy_finance.sh` (tracked, строка 5): `export SSHPASS='<реальный пароль qlknpodo@195.191.24.169>'`** — единственный tracked-файл, где пароль остался НЕзамаскированным (`git grep` по HEAD подтверждает ровно 1 файл).
- Во всех остальных скриптах (13 tracked `.exp`, `deploy_paramiko.py`, `deploy_fixes.sh`, `deploy_optimizations.sh`, `deploy_promo_system.sh`, `deploy_redis.sh`) пароль уже заменён на плейсхолдер `***REMOVED_SSH_PASSWORD***` — прошлая зачистка пропустила один файл.
- Пароль был в истории git многих файлов → считать скомпрометированным.
- **Действия исполнителя (приоритет над всем разделом):**
  1. СМЕНИТЬ пароль qlknpodo на сервере (согласовать с владельцем; лучше перейти на ssh-ключи + отключить PasswordAuthentication).
  2. Замаскировать/удалить строку в `deploy_finance.sh`, коммит.
  3. История git: пароль остаётся в истории — фиксаци�� факта; `git filter-repo` только по явному согласованию (правило чеклиста).

### Факты по скриптам

- Фактически tracked в корне: **65 скриптов** (.py/.sh/.exp/.js; чеклист говорил 49).
- **Cron их НЕ вызывает** — подтверждено инвентаризацией CB-044 (05.07.2026): все 7 cron-задач — management-команды из репо. Риск «сломать cron» снят без новой SSH-сессии.
- **Код `twocomms/` их НЕ вызывает** — grep по представителям (update_feed_now.sh, update_google_merchant_feed.sh, setup_session_cleaner.sh, security_check.py): 0 ссылок.
- Категории:
  - deploy-семейство (~20 шт: deploy*.sh/.exp/.py, run_*.exp, restart.exp) — дубли одного процесса; реально живой процесс деплоя сверить с владельцем;
  - одноразовые fix_*/update_index_v3..v5/restructure_html.py — мёртвые, кандидаты на удаление;
  - потенциально полезные: crawl_all_pages.py, optimize_images.py, security_check.py, setup_*.sh → `scripts/` с README;
  - `postcss.config.js` — НЕ трогать (конфиг сборки), `inspect_purge.js`/`modal_position_debug.js` — разовые отладки.
- SSH из sandbox: сервер сбрасывает соединение (kex reset) — вероятно, IP-фильтр/fail2ban; исполнителю с доступом ничего перепроверять по cron не нужно (см. CB-044).

### Задачи для исполнителя

1. P0-блок выше — первым делом.
2. PR: `scripts/archive/` для deploy-дублей, `scripts/` для полезных, удаление мёртвых fix_*.

---

## CB-014. dtf переопределяет collectstatic (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Что проверено

- `dtf/management/commands/collectstatic.py` (38 строк) — наследует стандартный
  `django.contrib.staticfiles...collectstatic.Command`, перед `super().handle()`
  вызывает `build_dtf_minified_assets(settings.BASE_DIR)` из `dtf/minify_assets.py`.
- `dtf/minify_assets.py` (105 строк) — минифицирует ровно 3 файла через `rcssmin`/`rjsmin`
  (оба пакета запинены в `requirements.txt`: rcssmin==1.1.2, rjsmin==1.2.2):
  - `dtf/static/dtf/css/dtf.css` → `dtf.min.css`
  - `dtf/static/dtf/js/dtf.js` → `dtf.min.js`
  - `dtf/static/dtf/js/components/effects-bundle.js` → `effects-bundle.min.js`
- `settings.py:169` — `"dtf.apps.DtfConfig"` стоит ПЕРЕД `django.contrib.staticfiles`,
  поэтому команда действительно перехватывает ЛЮБОЙ вызов `collectstatic` в проекте
  (включая деплой основного магазина: `deploy.sh`, `deploy.exp`, `deploy_finance.sh` и др. —
  все вызывают просто `collectstatic --noinput`).
- Дублирующая команда `dtf/management/commands/minify_dtf_assets.py` существует отдельно —
  т.е. минификацию МОЖНО дергать явно, без перехвата collectstatic.

### Находки и риски

1. **P2 — hard-fail всего деплоя из-за DTF.** `_run_dtf_minification()` кидает `CommandError`,
   если любой из 3 source-файлов отсутствует (`FileNotFoundError` в `build_dtf_minified_assets`)
   или минификация упала. Это роняет ВЕСЬ collectstatic основного магазина, даже если DTF
   не менялся. При `--dry-run` минификация пропускается — единственный обход.
2. **P3 — парадокс: минифицированные файлы НЕ используются шаблонами.**
   grep по `dtf/templates/`: `base.html:127` подключает `dtf/css/dtf.css` (НЕ .min),
   `base.html:538/546` — `dtf/js/dtf.js` и `effects-bundle.js` (НЕ .min).
   Ни один шаблон не ссылается на `*.min.css`/`*.min.js` (кроме vendored `htmx.min.js`).
   Подмены на уровне storage/middleware тоже нет (проверены settings, production_settings,
   twocomms/*middleware*.py — логики swap на .min нет).
   НО: прод использует `whitenoise.storage.CompressedManifestStaticFilesStorage`
   (production_settings.py:548) — whitenoise сам gzip/brotli-сжимает оригиналы,
   поэтому практическая ценность отдельных .min-файлов ≈ 0.
3. `.min`-файлы tracked в git (`dtf.min.css`, `dtf.min.js`, `effects-bundle.min.js`) —
   генерируемые артефакты в репо, diff-шум при каждом изменении исходников.

### Вывод / задачи для исполнителя

- Переименовать команду в `collectstatic_dtf` (или удалить — есть `minify_dtf_assets`)
  и убрать комментарий-зависимость порядка INSTALLED_APPS; либо, минимум,
  заменить hard-fail на warning, чтобы DTF не мог заблокировать деплой магазина.
- Решить судьбу .min-артефактов: либо шаблоны переводятся на .min (тогда tracked-файлы
  оправданы), либо вся связка minify+override удаляется как мёртвая (whitenoise уже сжимает).
- Деплой других приложений НЕ ломается функционально (super().handle() вызывается всегда),
  риск только в hard-fail сценарии — задокументировано.

---

## CB-011. scripts/ внутри twocomms (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Инвентаризация `twocomms/scripts/` (8 файлов, 4214 строк)

| Скрипт | Строк | Назначение | Вердикт |
|---|---|---|---|
| fill_translations.py | 3706 | Phase 17b: заполнение RU/EN msgstr в locale/*.po (словарь переводов зашит в код) | Фаза НЕ завершена → оставить до завершения i18n |
| wrap_themes_lazy.py | 144 | Phase 17v: одноразовый destructive-transform `_product_themes.py` (обёртка в gettext_lazy) | УЖЕ ПРИМЕНЁН → в архив |
| check_pdp_overlap.py | 130 | разовая отладка вёрстки PDP | в архив/удалить |
| render_social_previews.py | 78 | генерация social-preview картинок | разовый, в архив |
| compile_mo_polib.py | 68 | компиляция .po → .mo через polib (замена msgfmt на shared-хостинге) | ЖИВОЙ инструмент i18n-пайплайна → оставить |
| list_color_pairs.py | 42 | разовый листинг цветов | в архив/удалить |
| list_colors.py | 30 | разовый листинг цветов | в архив/удалить |
| list_untranslated.py | 16 | подсчёт пустых msgstr | живой (мелкий) → оставить рядом с fill_translations |

### Прове��ка «завершена ли Phase 17»

- **НЕ завершена.** Пустые msgstr в locale: ru — 638 из 2450 msgid (26%),
  en — 639 из 2450 (26%), uk — 1331 из 1357 (98%, uk = исходный язык, это норм
  при uk-as-msgid, но .mo для uk в корневом locale/ отсутствует — только dtf/locale/uk).
- `wrap_themes_lazy.py` свою работу СДЕЛАЛ: `_product_themes.py` содержит 205 вхождений
  `_( ... )` — повторный запуск destructive-скрипта опасен (двойная обёртка).
- .mo-файлы tracked в git и свежие (последний коммит locale/ru/django.mo — 07.07.2026),
  т.е. пайплайн fill_translations → compile_mo_polib реально живой и используется.
- Ссылок на скрипты из кода/cron/деплой-скриптов НЕТ (grep по *.py/*.sh/*.md вне scripts/ — 0),
  вызываются только вручную.

### Вывод / задачи для исполнителя

1. НЕ удалять `scripts/` целиком: fill_translations.py + compile_mo_polib.py +
   list_untranslated.py — живой i18n-пайплайн (перевод не закончен: ~26% строк пусто).
2. `wrap_themes_lazy.py` — переместить в `scripts/archive/` с пометкой «applied,
   do not re-run» (destructive, уже применён).
3. `check_pdp_overlap.py`, `list_colors.py`, `list_color_pairs.py`,
   `render_social_previews.py` — кандидаты в archive/удаление (разовые).
4. fill_translations.py на 3706 строк — это в основном данные (словарь TRANSLATIONS);
   при желании словарь можно вынести в JSON/YAML, но это оптимизация, не долг.

---

## CB-013. Дублирующиеся static-директории img/ и images/ (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Фактическая картина (уточнение к формулировке чеклиста)

Директорий не 2, а 3, и они НЕ дубли друг друга — контент не пересекается:

| Директория | Размер | Файлов | Контент |
|---|---|---|---|
| `twocomms_django_theme/static/img/` | 12MB | 67 + подпапки | ОСНОВНАЯ: логотипы, фавиконки, social-preview, noise/vignette, catalog/, configurator/ (3.6MB), logo_fire/ (5.8MB), icons/, lang/, pdp/ |
| `twocomms_django_theme/static/images/` | 6.3MB | 16 | ТОЛЬКО banksy_* (3 картинки × png+webp+3 srcset-размера) + hero_dtf_graphic.png |
| `static/img/` (корень проекта) | 2.6MB | 1 | только `price.png` (для email-шаблона КП) |

Обе theme-директории попадают в один namespace `static/...` через STATICFILES_DIRS
(settings.py:813-816: theme/static + корневой static). Коллизий имён нет.

### Карта использования

- `images/banksy_*.png` — 3 ссылки, все из `pro_brand.html` через тег `{% responsive_image %}`
  (тег сам подставляет webp/srcset — PNG-путь в шаблоне это ключ, а не реальный отдаваемый файл).
- `images/hero_dtf_graphic.png` — **0 ссылок** в коде/шаблонах → мёртвый файл.
- `img/price.png` (корневой static) — 1 ссылка из `management/.../commercial_offer_email.html`.
- `img/social-preview-*.jpg` — og:image (внешние потребители! соцсети кэшируют URL).
- `img/placeholder.jpg` — используется GMC-фидом (внешний потребитель — Google Merchant).

### Находки

1. **P3 — мёртвый вес ~5.5MB в git (не влияет на прод-трафик):**
   - `img/logo_fire/stena.png` (2.7MB) — не используется (в шаблоне только stena2.png);
   - `img/logo_fire/stena2.png` (2.7MB) — используется в pro_brand, но грузится как PNG
     2.7MB без webp-версии — это и вес репо, и вес страницы (P2 для perf pro_brand);
   - `images/hero_dtf_graphic.png` — 0 ссылок.
2. **P3 — configurator/ui/*.png по 450-650KB** (hoodie/tshirt превью) — нет webp-версий.
3. Слияние `images/` → `img/` ВОЗМОЖНО без риска (нет коллизий имён, потребители banksy —
   только pro_brand.html + логика responsive_image), но затрагивает пути → по RISK-14
   сначала убедиться, что banksy-URL не светятся во внешних фидах (проверено: НЕ светятся,
   внешние потребители используют только img/social-preview* и img/placeholder — их НЕ трогать).

### Задачи для исполнителя

1. Удалить мёртвые: `img/logo_fire/stena.png`, `images/hero_dtf_graphic.png` (~2.7MB+).
2. Сгенерировать webp для `stena2.png` и `configurator/ui/*.png`, переключить шаблоны.
3. (Опционально) слить `images/` в `img/banksy/` — только banksy-набор, 3 ссылки в одном
   шаблоне; пути `img/social-preview*`, `img/placeholder.jpg` НЕ менять (внешние кэши).

---

## CB-036. vendor/ 1.1MB (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Инвентаризация

`twocomms_django_theme/static/vendor/` = ТОЛЬКО Font Awesome 6 Free (self-hosted, 1.1MB):

| Файл | Размер | Нужен? |
|---|---|---|
| css/all.min.css | 104KB | да (подключён в base.html:72 с media=print+onload) |
| webfonts/*.woff2 (4 шт) | 308KB | да — реально отдаются браузерам |
| webfonts/*.ttf (4 шт) | 700KB | **НЕТ** — ttf это fallback в @font-face после woff2; woff2 поддержан всеми браузерами с 2016 |

Плюс `dtf/static/dtf/js/vendor/htmx.min.js` (48KB) — используется dtf-шаблонами, ок.

### Проверка дублей с CDN

Дублей НЕТ: base.html грузит с CDN только Bootstrap 5.3.3 (css+js, jsdelivr);
Font Awesome — только self-hosted. Двойной загрузки одной библиотеки не обнаружено.
Продуманная оптимизация уже есть: страницы без FA-иконок (index, catalog, pro_brand)
отключают загрузку через пустой блок `{% block fontawesome_css %}`.

### Задачи для исполнителя

1. **Quick win P3:** удалить 4 `.ttf` из vendor/fontawesome/webfonts (−700KB, 64% размера
   vendor/) и вычистить `url(...ttf) format("truetype")` из all.min.css. Риск ≈ 0.
2. (Опционально, больший выигрыш для perf) заменить FA на инлайн-SVG подмножество
   используемых иконок — но это отдельная задача из PERF-блока, не гигиена репо.

---

## CB-033. Inline-скрипты в base.html (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Инвентаризация

Фактически 16 `<script>`-тегов = 8 external (src, все defer/module — ок) + 8 inline,
из которых 1 закомментирован. Активных inline-блоков — 7:

| # | Строки | Что делает | Вердикт |
|---|---|---|---|
| 1 | 20-45 | CSRF self-heal (чистка дублей csrftoken-cookie) + заполнение meta[csrf-token] | ОСТАВИТЬ inline: должен отработать до любого fetch/XHR |
| 2 | 249-306 | Deferred-загрузчик Ahrefs Analytics (interaction/quiet-window gating, prod only) | МОЖНО вынести в файл: template-переменных внутри нет; но дублирует device-class логику блока #3 (см. находку 1) |
| 3 | 373-417 | Device-class детектор (data-device-class=low/mid/high + effects-lite/perf-lite классы) | ОСТАВИТЬ inline: обязан выполниться до paint, иначе FOUC тяжёлых эффектов |
| 4 | 420-433 | Sync-hints: window.__TC_SYNC_CART/FAVS из `{% if user.is_authenticated %}` | ОСТАВИТЬ inline (крошечный, содержит template-логику) |
| 5 | 917-925 | Стаб window.trackEvent с очередью до загрузки analytics-loader | ОСТАВИТЬ inline (9 строк, должен существовать до любых кликов) |
| 6 | 928-998 | Самописный GTM-загрузчик (GTM-PRLLBF9H hardcoded): interaction-gating + таймауты 35s/30s/25s на home — специально чтобы Lighthouse НЕ увидел GTM в окне аудита | см. находку 2; вынос в файл возможен ({% comment %} внутри — единственный template-элемент) |
| 7 | 1027-1052 | Interaction-gated инжектор analytics-loader.js (fallback 25s) | МОЖНО вынести (template-переменная только src через {% static %} — решается data-атрибутом) |
| 8 | 1384-1394 | SW-регистрация — ЗАКОММЕНТИРОВАНА ({% comment %}) вместе с include partials/analytics.html | МЁРТВЫЙ код в шаблоне — удалить обе закомментированные секции |

### Находки

1. **P3 — дублирование device-class логики.** Блок #2 (Ahrefs) содержит СВОЮ копию
   детектора deviceClass (~20 строк), хотя блок #3 уже пишет `data-device-class` на html.
   Ahrefs-блок сначала читает атрибут, но имеет полный fallback-детектор — мёртвая ветка,
   т.к. блок #3 выполняется раньше (стоит выше в head... фактически блок #2 стоит ВЫШЕ
   блока #3 в файле → fallback реально срабатывает; порядок стоит поменять).
2. **P2 — «Lighthouse-маскировка» аналитики задокументирована в самом коде.** GTM/Ahrefs/
   analytics-loader сознательно гейтятся так, чтобы НЕ загружаться в окне Lighthouse-аудита
   (таймаут 35s на home + interaction-only). Это осознанный трейд-офф (комментарии Phase 22e),
   но: (а) реальные perf-метрики RUM отличаются от лабораторных; (б) сессии без interaction
   короче 25с полностью теряются в аналитике. Решение бизнеса, не баг — задокументировано.
3. Кандидаты на вынос в кэшируемые файлы: блоки #2, #6, #7 (суммарно ~150 строк HTML
   на каждой странице). Выигрыш мал (HTML gzip), риск порядка исполнения — переносить
   аккуратно, сохранив последовательность: device-class → стаб trackEvent → загрузчики.

### Задачи для исполнителя

1. Удалить закоммент. секции (SW-регистрация, partials/analytics.html) — мёртвый шаблонный код.
2. Поднять блок device-class (#3) ВЫШЕ Ahrefs-блока (#2) и удалить fallback-детектор из #2.
3. (Опционально) вынести #2/#6/#7 в static/js/loaders.js с data-атрибутами для конфигурации.
4. GTM-loader связан с CRO-004/AN-002 — судьбу самописного загрузчика решать вместе с ними.

---

## CB-034. analytics-loader.js 56KB (АУДИТ ВЫПОЛНЕН, 07.07.2026)

### Состав файла (1496 строк, 56KB, грузится interaction-gated, v=6)

| Подсистема | Строки (≈) | Что делает |
|---|---|---|
| Event bridge `window.trackEvent` | 156-463 | Единая точка: рассылает события в fbq (Meta), gtag/dataLayer (GA4), ttq (TikTok) с буферизацией до загрузки пикселей |
| TikTok payload builder + identify | 464-630 | Спец-формат contents/value/currency + advanced matching |
| **Собственная реализация SHA-256** | 631-711 | Хэширование email/phone для advanced matching (Meta/TikTok) — 80 строк криптографии вручную |
| User data builder | 712-870 | fbp/fbc cookies, sessionStorage guest-data, валидация email/phone |
| loadGoogleAnalytics (GA4 direct) | 871-934 | Грузит gtag.js с G-109EFTWM05 напрямую, шлёт page_view вручную |
| loadClarity | 935-986 | MS Clarity t7u94cvpqc, interaction-gated |
| loadMetaPixel | 1051-1146 | Pixel 823958313630148 + прогон _fbqBuffer |
| loadTikTokPixel | 1147-1393 | Pixel D43L7DBC77UA61AHLTVG + прогон _ttqBuffer |
| Init/BFCache | 1394-1496 | Порядок запуска, bfcache restore |

Конфигурация приходит из data-атрибутов `<html>` (base.html:11-13, hardcoded в шаблоне
G-109EFTWM05 / t7u94cvpqc / 823958313630148; TikTok — из context processor).

### Находки

1. **Мёртвых веток Celery/ab_testing НЕТ** — grep по celery/ab_test/experiment: 0 вхождений.
   Файл монолитный, но весь код живой.
2. **P2 — риск двойного счёта (связка AN-001 подтверждена со стороны кода):**
   loader грузит GA4 (gtag) НАПРЯМУЮ и шлёт page_view вручную, ПАРАЛЛЕЛЬНО в base.html
   грузится GTM-контейнер GTM-PRLLBF9H. Если внутри GTM-контейнера настроен тег GA4-config
   или Meta/TikTok-теги — события задвоятся (trackEvent шлёт и в dataLayer, и в fbq/ttq
   напрямую). Из кода содержимое GTM-контейнера не видно → исполнителю AN-001 нужно
   выгрузить контейнер и сверить список тегов с прямыми загрузками loader-а.
3. **P3 — legacy-дубль конфигурации:** скрытый `<div id="am">` (base.html:1115-1116)
   дублирует пиксель-IDs через НЕсуществующие context-переменные `{{ ga_id }}`,
   `{{ clarity_id }}`, `{{ meta_pixel_id }}` (context processor отдаёт только TIKTOK_PIXEL_ID)
   → атрибуты пустые. Loader читает #am в 3 местах (563, 769, 988) как источник user-data —
   работает, но конфиг-атрибуты в #am мёртвые.
4. **P3 — самописный SHA-256** (80 строк): работает, но crypto.subtle.digest доступен во
   всех целевых браузерах (HTTPS-only сайт) — можно заменить на 10 строк async-кода.
5. Debug-логи `console.log('[TikTok Pixel] ...')` остались в прод-коде (шумят в консоли).

### Задачи для исполнителя

1. AN-001: выгрузить конфигурацию GTM-PRLLBF9H и устранить дубли тегов (GA4/Meta/TikTok)
   между контейнером и analytics-loader — решить, что остаётся источником истины.
2. Почистить пустые конфиг-атрибуты #am (либо удалить div, перенеся user-data хранение).
3. Заменить самописный SHA-256 на crypto.subtle (минус ~80 строк).
4. Убрать console.log TikTok-отладки из прода.
5. (Опционально) разбить на модули (meta.js/tiktok.js/ga.js/clarity.js) — но при
   interaction-gated загрузке единым файлом выигрыш спорный; не приоритет.

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | CB-044 | Аудит выполнен: 7 cron-задач, все скрипты в репо; НЕТ бэкап-cron и feed-cron |
| 05.07.2026 | CB-043 | Аудит выполнен: tracked чисто, 10 стэшей, серверные untracked-скрипты |
| 05.07.2026 | CB-012 | Боевой модуль = twocomms.production_settings; на сервере 2 env-файла; DISABLE_ANALYTICS-флаг требует проверки |
| 05.07.2026 | CB-040 | Боевые версии зафиксированы; пин — исполнителю (3 строки) |
| 05.07.2026 | CB-024 | Аудит покрытия выполнен: money-тесты в storefront есть (18,5k строк), но orders/ и accounts/ — 0 тестов, вебхук-подпись и CAPI не покрыты, CI нет; спецификация смок-пакета из 6 пунктов — исполнителю |
| 07.07.2026 | CB-001 | Аудит выполнен: 145 tracked-артефактов ≈170MB (tmp/ 101MB!), личное фото me.JPG ×2 копии; runtime-зависимостей нет; план git rm --cached |
| 07.07.2026 | CB-005 | Аудит выполнен: 2 xlsx с закупочными ценами tracked; runtime не нужны (XLSX генерится на лету, live 200 подтверждён); ВАЖНО: views.py.backup — ЖИВОЙ рантайм-код через _load_legacy_views |
| 07.07.2026 | CB-006 | Аудит выполнен: секреты покрыты; дыры — artifacts/, output/, tmp/, opros/, newCatalog/, *.xlsx, *.bak2; исключение !views.py.backup оставить |
| 07.07.2026 | CB-007 | Аудит выполнен: Promt/ не tracked; 7 AI-конфигов (69 файлов, живой похоже только .kiro); opros/newCatalog/BrandDNA → в PR CB-001 |
| 07.07.2026 | CB-002 | Аудит выполнен: 175 md ~3.9MB в корне, ссылок из кода 0, docs/ существует; план git mv → docs/archive |
| 07.07.2026 | CB-003 | Аудит выполнен + ⚠️ P0: deploy_finance.sh содержит НЕзамаскированный SSH-пароль прода (остальные скрипты зачищены ранее) → сменить пароль; 65 скриптов, cron/код их не вызывают (по CB-044) |
| 07.07.2026 | CB-014 | Аудит выполнен: override перехватывает ЛЮБОЙ collectstatic; P2 — hard-fail деплоя при отсутствии dtf-исходников; парадокс — .min-файлы шаблонами НЕ используются (whitenoise и так сжимает); план — переименовать/удалить |
| 07.07.2026 | CB-011 | Аудит выполнен: Phase 17 НЕ завершена (~26% msgstr пусто ru/en) — fill_translations+compile_mo_polib живые; wrap_themes_lazy УЖЕ применён (205 обёрток) — в архив, destructive; 4 разовых скрипта — кандидаты в архив |
| 07.07.2026 | CB-013 | Аудит выполнен: 3 директории (не 2), контент НЕ пересекается; мёртвые stena.png 2.7MB + hero_dtf_graphic.png; stena2.png 2.7MB и configurator/ui без webp; внешние потребители (og:image, GMC placeholder) — только img/, пути не трогать |
| 07.07.2026 | CB-036 | Аудит выполнен: vendor/ = только Font Awesome 6 self-hosted; дублей с CDN НЕТ (CDN только Bootstrap); quick win — удалить 4 .ttf-fallback (−700KB, 64% vendor/) |
| 07.07.2026 | CB-033 | Аудит выполнен: 7 активных inline-блоков (1 закомментирован — удалить); 4 обязаны остаться inline (CSRF, device-class, sync-hints, trackEvent-стаб); дубль device-class детектора в Ahrefs-блоке; «Lighthouse-маскировка» GTM (35s таймаут) — осознанный трейд-офф, задокументирован |
| 07.07.2026 | CB-034 | Аудит выполнен: мёртвых Celery/ab_test веток НЕТ; P2 — GA4 грузится напрямую ПАРАЛЛЕЛЬНО GTM-контейнеру → риск двойного счёта (AN-001); #am div с пустыми конфиг-атрибутами; самописный SHA-256 (80 строк) → crypto.subtle; console.log TikTok в проде |
