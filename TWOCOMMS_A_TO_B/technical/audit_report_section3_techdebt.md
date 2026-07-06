# ОТЧЁТ АУДИТА — РАЗДЕЛ 3: ТЕХНИЧЕСКОЕ СОСТОЯНИЕ И ТЕХ. ДОЛГ

**Связан с:** `twocomms_global_audit.md` (раздел 3)
**Правило безопасности:** реквизиты доступа в этом файле НЕ фиксируются.
**Дата начала:** 05.07.2026

---

## TD-020. Бэкапы MySQL (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **P0-НАХОДКА: РЕГУЛЯРНЫХ БЭКАПОВ НЕТ**

### Факты (боевой сервер, read-only)

1. **В crontab пользователя НЕТ ни одной задачи mysqldump/бэкапа** (полный список cron — см. `audit_report_section6_codebase.md`, CB-044).
2. Найденные на диске дампы:
   - `~/qlknpodo_MySQL_DB_24_10.sql` — ручной полный дамп от **24 октября** (на момент аудита ему >8 месяцев). Лежит в домашней директории, НЕ в web-доступной папке (public_html) — утечки через URL нет.
   - `~/backups/parser_tables_schema_20260324_212516.sql` — **0 байт** (пустой, неудачный дамп) и `..._212543.sql` — 8.2KB (только схема parser-таблиц, от 24.03.2026).
   - `~/.trash/db.sqlite3.bak_20250903_221105` — мусор от старой SQLite-эпохи.
3. Панель хостинга (cPanel/Hostsila) может делать свои бэкапы аккаунта — **у аудитора нет доступа к панели, проверить может только владелец**. До подтверждения владельцем считать, что бэкапов НЕТ.
4. Диск сервера: 76% занято (1.1T/1.6T на разделе) — место для дампов есть.

### Оценка риска

- Боевая БД содержит: 68 товаров, 41 заказ, 1015 UTM-сессий, 36 859 UserAction, 28 CustomPrintLead, финансовые данные (finance app), IG-сделки (poll_ig_deal_payments — работающие оплаты). Потеря = невосстановимая потеря заказов/финансов.
- RISK-07 матрицы: любая миграция на бою (TD-030, CRO-041 добавит поля в Order) без бэкапа = критический риск. **Все P1-фиксы со схемой БД ЗАБЛОКИРОВАНЫ до внедрения бэкапов.**

### Задача исполнителю (конкретно)

1. Создать management-команду или shell-скрипт `backup_mysql.sh`: `mysqldump --single-transaction --routines` всех БД проекта (основная + DTF-БД, см. `DB_NAME_DTF` в production_settings) → gzip → `~/backups/` с датой в имени; ротация 14 дней.
2. Добавить в crontab: ежедневно в 03:00 (до trim_analytics 04:20). Креды mysqldump — из `.my.cnf` (chmod 600) или env, НЕ в теле скрипта.
3. Тест восстановления на копии (TECH-042): развернуть дамп в тестовую БД, `manage.py check` + выборочный count(*) по ключевым таблицам.
4. Убедиться, что `~/backups/` не попадает в web-root и в git.
5. После внедрения — удалить/архивировать устаревший `qlknpodo_MySQL_DB_24_10.sql` (согласовать с владельцем).

---

## TD-016. Логи на сервере (АУДИТ ВЫПОЛНЕН ЧАСТИЧНО, 05.07.2026)

### Факты

| Лог | Размер | Последняя запись | Вывод |
|---|---|---|---|
| `twocomms/stderr.log` | 4.5MB | активен (сегодня) | + ротация .1–.5 существует (untracked-файлы stderr.log.1–5) |
| `twocomms/django.log` | 4.2MB | активен (сегодня) | + ротация .1–.5 |
| `twocomms/rum.log` | 932KB | активен (05.07 02:03) | RUM-метрики пишутся — есть живой real-user-monitoring контур |
| `twocomms/image_optimization.log` | 8.7KB | 24.10.2025 | мёртв ~9 мес → ImageOptimizationMiddleware, вероятно, выключен (связь с CB-041) |
| `twocomms/ai_generation.log` | 6.6KB | 10.09.2025 | AI-генерация не запускалась ~10 мес (связь с AEO-006) |
| `twocomms/celery.log` | 127B | 19.11.2025 | мёртв, Celery удалён — удалить файл (TD-003) |
| `twocomms/cls_fix.log`, `debug.log`, `django_debug.log`, `server.log` | по ~205B–776B | сен 2025 | мёртвые одноразовые логи — удалить |
| `twocomms/cron.log` | 0B | сен 2025 | пуст — удалить |
| cron-логи в `logs/` и `tmp/` | не замерены | — | отдельная SSH-сессия: размеры tmp/ig_bot_cron.log (пишется каждую минуту!) |

### Выводы / задачи исполнителю

1. Ротация основных логов (django/stderr) есть (5 поколений), диск не съеден. Настройку ротации (LOGGING в settings vs logrotate) — уточнить в коде: grep `RotatingFileHandler` в settings.py/production_settings.py.
2. **Не проверено (следующая SSH-сессия):** размер `tmp/ig_bot_cron.log` — пишется 1440 раз/сутки без видимой ротации; содержимое логов на секреты/PII.
3. Мёртвые логи (celery, cls_fix, debug, django_debug, server, cron, image_optimization, ai_generation — если владельцем подтверждено, что контуры мертвы) — удалить одной чисткой.

---

## TD-033. Воронка CustomPrintLead мертва (АУДИТ ВЫПОЛНЕН, 05.07.2026) — **ПОДТВЕРЖДЁН: механизм смены статуса ЕСТЬ, но не используется; модель статусов беднее, чем нужно**

### Факты (код + живая БД 05.07.2026)

1. **Статусная модель уже существует**, но примитивная: `CustomPrintLeadStatus` (storefront/models.py:582) = `new → in_progress → closed`. Стадий `quoted / won / lost` из TECH-062 — НЕТ; `closed` не различает «выиграли» и «слили».
2. **БД: 28/28 лидов в статусе `new`** — ни один вручную не переведён. При этом эндпоинт смены статуса СУЩЕСТВУЕТ: `admin_custom_print_lead_status` (storefront/views/admin.py:1327, POST, staff-only) и счётчики new/in_progress/closed выводятся в staff-панели (admin.py:874–876). Вывод: интерфейс есть, но им не пользуются (не видна ценность/не встроено в процесс) ИЛИ он не заметен в UI панели — требуется подтверждение владельцем.
3. **Параллельная ось — `moderation_status`** (draft → awaiting_review → approved/rejected) РАБОТАЕТ: БД: 24 draft / 2 awaiting_review / 2 approved. Это модерация макета для оплаты, не воронка продаж. Две оси статусов живут независимо, «в работе/закрыта» никак не синхронизируется с «approved/оплачен».
4. **0/28 лидов привязаны к заказу** (`lead.order` NULL у всех) — ни один кастом не дошёл до оплаты, либо связка не проставляется. Учитывая 2 approved-лида — вероятно, реально не дошли до оплаты (approve был недавно) — уточнить у владельца.
5. Таймст��мпов смены статуса нет (только created_at/updated_at/reviewed_at) → время цикла воронки неизмеримо.
6. UserAction-события кастом-воронки пишутся хорошо: custom_print_step_enter 244, step_complete 240, start 66, add_to_cart 3, safe_exit 18 → верх воронки живой, 66 стартов → 28 лидов → 0 продаж. Потенциал: конверсия лид→продажа сейчас 0%.

### Задача исполнителю (TECH-062)

1. Расширить `CustomPrintLeadStatus`: `new → in_progress → quoted → won / lost` (+ `closed` мигрировать в won/lost по решению владельца). Миграция enum — только после TD-020 (бэкапы, RISK-07).
2. Автопереходы: `moderation_status=approved` → status=`quoted`; `lead.order` проставлен и заказ оплачен → status=`won`. Ручной перевод в `lost` с причиной (текстовое поле lost_reason).
3. Таймстемпы переходов (status_changed_at или отдельная таблица истории) — для замера времени цикла.
4. UI: сделать смену статуса очевидной в карточке лида staff-панели (сейчас эндпоинт есть — проверить, есть ли кнопка); отчёт «лиды → won %» на вкладке аналитики.
5. Процесс: 28 висящих лидов разобрать вручную владельцем после внедрения статусов (контакты клиентов ещё тёплые?).

---

## TD-001. storefront/views.py.backup (АУДИТ ВЫПОЛНЕН, 06.07.2026) — **⚠️ КРИТИЧЕСКАЯ КОРРЕКЦИЯ ЗАДАЧИ: ФАЙЛ НЕЛЬЗЯ ПРОСТО УДАЛИТЬ**

**Формулировка чек-листа («удалить из репо, убедиться что ничего не импортирует») ОПАСНА и опровергнута кодом.**

Факты:
1. Файл: `twocomms/storefront/views.py.backup`, **7790 строк / 329 KB**, отслеживается git.
2. Классический `import` его действительно нигде не делает, НО он загружается в рантайме как текст:
   - `storefront/views/__init__.py:329` — `_load_legacy_views()`: читает `Path(...)/views.py.backup` (строка 338), exec-ит и переносит функции в `globals()` пакета views.
   - `storefront/views/__init__.py:292` — `_LEGACY_VIEW_NAMES`: белый список **102 имён** вьюх, которые берутся ИЗ backup-файла.
   - `storefront/urls.py:11–19` — `_legacy_view(name)` вызывает `_load_legacy_views(force=True)` при первом запросе; **30 маршрутов** в urls.py привязаны через `_legacy_view(...)`.
3. То есть **боевые URL (wholesale, admin_product_edit и др.) сегодня обслуживаются кодом из файла с расширением `.backup`**. Удаление файла = мгновенные 500/заглушки на этих маршрутах.
4. Связь с `audit_report_legacy_stubs.md`: там уже доказано, что механизм трёхслойный (реальные из backup / заглушки типа А / заглушки типа Б) и что `monobank_create_checkout` сломан из-за отсутствия в whitelist.

**Правильная задача для исполнителя (вместо «git rm»):**
1. Инвентаризация 102 имён `_LEGACY_VIEW_NAMES` → какие реально вызываются (30 `_legacy_view` маршрутов + package-level exports).
2. Перенести живые функции из `views.py.backup` в модули пакета `storefront/views/` (по доменам: wholesale, admin_*, monobank quick и т.д.).
3. Удалить `_load_legacy_views`/`_LEGACY_VIEW_NAMES`/`_legacy_view` и только потом `git rm views.py.backup`.
4. Обязательные тесты после переноса: все 30 маршрутов отвечают не заглушкой (смок-скрипт по URL-ам из urls.py).
Приоритет: P1 (пока файл жив — рефакторинг любых views рискует рассинхронизацией двух копий кода).

---

## TD-002. order_success_old.html (АУДИТ ВЫПОЛНЕН, 06.07.2026) — мёртвый шаблон, удалить безопасно

1. Файл: `twocomms_django_theme/templates/pages/order_success_old.html`, 16 424 байт, в git.
2. Grep по всему репо (`*.py`, `*.html`): **0 ссылок** (ни `render(...)`, ни `template_name`, ни `{% include %}`, ни `{% extends %}`).
3. Актуальный шаблон — `pages/order_success.html` (используется checkout-потоком).
**Вывод:** безопасно `git rm`. Приоритет P3 (гигиена). Единственный шаг проверки перед удалением: `python manage.py check` + смок заказа → страница успеха.

---

## TD-003. Celery-шимы и следы (АУДИТ ВЫПОЛНЕН, 06.07.2026) — **НАЙДЕН РЕАЛЬНЫЙ БАГ: битый импорт → все прямые Telegram-отправки синхронные**

Архитектура после удаления Celery (подтверждена кодом):
- `twocomms/__init__.py:16–20` — защищённый `try: from .celery import app` (файла celery.py нет → тихо пропускается). Комментарий в коде фиксирует решение «Celery удалён, хостинг без воркеров».
- `storefront/tasks.py:93–118` — shim `shared_task`: `.delay()`/`.apply_async()` = **синхронный inline-вызов**. Задачи: feeds, indexnow, optimize_image_field_task, AI-контент, survey-отчёты.
- `orders/tasks.py` — СВОЯ реализация: `send_telegram_notification_task(order_id, notification_type)` запускает **daemon-поток** (`Thread`, `close_old_connections` до/после) → HTTP-ответ не ждёт Telegram. Используется `orders/signals.py:27`. Это корректный путь.

**БАГ (P2, подтверждён трассировкой):**
- `orders/telegram_notifications.py:17–20`: `try: from storefront.tasks import send_telegram_notification_task / except ImportError: = None`.
- В `storefront/tasks.py` символа `send_telegram_notification_task` **НЕТ** (проверено grep по всем `def`). Импорт всегда падает → переменная всегда `None`.
- Следствие: ветки `if self.async_enabled and send_telegram_notification_task:` (строки 169, 367) **мертвы навсегда**; `send_admin_message()` и `send_personal_message()` всегда идут по fallback — **синхронный `requests.post` к api.telegram.org в потоке запроса**.
- Кто страдает (вызовы вне фоновых потоков): `accounts/signals.py:61` (регистрация), `dtf/telegram.py` (6 вызовов — лиды DTF), `orders/dropshipper_views.py:1541,1608`, `orders/nova_poshta_service.py:593,641,705,752`, `reviews/signals.py:133`. При таймауте Telegram (до 10s+) эти запросы висят.
- Дополнительно: сигнатуры не совпадают — вызовы передают `(message, chat_id=..., parse_mode=...)`, а `orders/tasks.py`-версия ждёт `(order_id, notification_type)`. Т.е. даже «починка» импорта на orders.tasks сломает всё. Нужен отдельный лёгкий async-sender (message, chat_id) с daemon-потоком по образцу orders/tasks.py.
- `celery.log` на сервере: 127 байт, мёртв (зафиксировано в TD-016) — удалить при ближайшем SSH-сеансе.
- Асинхронность, на которую рассчитывал код: feeds_queue (комментарий «apply_async(countdown=300)» → заменено на cron+dirty-flag `tmp/feeds/feeds_dirty.flag`), web_push не найден, AI-генерация — sync inline (медленный admin-save, известно).

**Решение зафиксировать в TECHNICAL_TASKS:** «Celery не возвращаем; фоновость = daemon-Thread / cron». Новая задача: TG-ASYNC-FIX (описание выше).

---

## TD-004. Мусор в корне twocomms/ (АУДИТ ВЫПОЛНЕН, 06.07.2026) — полная инвентаризация

Git-tracked файлы/каталоги в `twocomms/`, не являющиеся кодом приложения:

| Объект | Размер/состав | Вердикт |
|---|---|---|
| `494cb80b2da94b4395dbbed566ab540d.txt` | 32 B | верификационный файл (похоже на IndexNow/поисковую верификацию) — ПРОВЕРИТЬ, отдаётся ли по URL, прежде чем удалять |
| `replacement.txt` | 859 B | разовый рабочий файл — удалить |
| `pricelist.html` | 2 KB | статический прайс — проверить, не отдаёт ли его вьюха `pricelist_page` (legacy_stubs → рендерит `pages/pricelist.html` из templates, НЕ этот файл) — удалить после проверки |
| `wholesale_prices.xlsx` (7.7 KB) + `Оптові ціни....xlsx` (7.6 KB) | **закупочные/оптовые цены в публичном репо** | P2: вынести из git (см. CB-005), почистить историю при необходимости |
| `analyze_views_migration.py`, `check_views_simple.py`, `check_views_structure.py` | 4.8+7.6+7.6 KB | разовые скрипты анализа рефакторинга views → `scripts/` или удалить |
| `_audit/` | 24 файла (probe*.py/sh, raw_results.json, urls_all.txt…) | артефакты старого аудита — удалить из git |
| `_audit_seo.md` (54 KB), `_seo_audit.sh`, `_seo_check.py` | SEO-аудит артефакты | → docs/archive или удалить |
| `Promt/` | 7 файлов, включая **PDF** | промпты для ИИ — вне репо/в docs |
| `Ideas/` | **~150 md-файлов** (management_analytics, itr3, ids2…) | огромный архив ИИ-брейнштормов — → `docs/ideas/` или отдельный репо; раздувает клон |
| `tmp/feeds/feeds_dirty.flag` | runtime-флаг feeds-очереди | **НЕ удалять слепо**: это рабочий механизм cron-feeds; правильно — добавить `tmp/` в .gitignore, файл убрать из индекса (`git rm --cached`) |
| `PROMO_ADMIN_IMPROVEMENTS.md` (11 KB), `docs/` | докуменация | → общий docs/ |

Отдельно: `google_merchant_feed.xml` (378 B в корне twocomms/) — проверить в SEO-008, не устаревший ли дубль генерируемого фида.

---

## TD-005. 202 md-отчёта в корне репозитория (АУДИТ ВЫПОЛНЕН, 06.07.2026)

1. Подтверждено: `ls *.md | wc -l` = **202** в корне репо + 53 `.txt/.sh/.py` loose-файлов (пересечение с CB-002/CB-003).
2. Характер: журналы разовых фиксов (`CART_DISPLAY_BUG_FIX.md`, `AUTH_FIX_SUMMARY.md`, `META_PIXEL_*` и т.п.) — история работ, не документация.
3. Реально активные документы, которые должны остаться в корне: `README*`, `TWOCOMMS_A_TO_B/` (папка планов), `AGENTS.md`-подобные если есть.
4. **План для исполнителя (1 PR, только `git mv`):** создать `docs/archive/2025-2026-fix-reports/`, перенести все 202 файла; grep перед переносом на ссылки на эти файлы из кода/шаблонов (ожидается 0); README-индекс в архиве не нужен (история в git). Риск: нулевой; приоритет P3, но эффект на читаемость репо высокий.

---

## TD-006. legacy_stubs.py (АУДИТ ВЫПОЛНЕН, 06.07.2026; базируется на audit_report_legacy_stubs.md)

1. `storefront/views/legacy_stubs.py` — 222 строки, ~48 заглушек. Все живы в urls (напрямую `views.X` или как fallback при отсутствии в `_LEGACY_VIEW_NAMES`).
2. Категории заглушек: admin_offline_stores/склад-магазины (рендерят `admin/stub.html` или пустой JSON), print_proposal-админка, pricelist/wholesale (частично перекрыты whitelist-ом → реальные из backup), `monobank_create_checkout` (**CRITICAL, см. audit_report_legacy_stubs.md Находка 4**).
3. Ответ на вопрос чек-листа «вернуть 410 для мёртвых URL или удалить»:
   - Заглушки, возвращающие фейковый успех (`JsonResponse({'status':'ok'})` для admin_store_*) — **хуже 410**: админ думает, что действие выполнено. Для мёртвых admin-фич → 410 или убрать маршруты из urls.py.
   - `monobank_create_checkout` — не удалять, а ЧИНИТЬ (перенести реальную реализацию, см. TD-001 план).
   - Публичные URL (pricelist/wholesale) отдаются реальными функциями из backup — трогать только в рамках TD-001-переноса.
4. Итог: TD-006 сливается с TD-001 в одну задачу «ликвидация legacy-слоя views» с 3 подзадачами: (а) починить monobank quick, (б) перенос живых, (в) 410/удаление мёртвых admin-заглушек.

---

## TD-007. Дублирующиеся системы аналитики (АУДИТ ВЫПОЛНЕН, 06.07.2026) — карта «кто что пишет»

| Модуль | Строк | Статус | Что пишет / куда |
|---|---|---|---|
| `storefront/tracking.py` | 223 | **АКТИВЕН** (2 middleware в settings.py:222,224) | `AnalyticsIdentityMiddleware` — first-party cookies (`_tc_id`); `SimpleAnalyticsMiddleware` — `PageView.objects.create` (models.py:1714) + `SiteSession`. Экспортирует `is_bot` (используется views/blog.py:22) |
| `storefront/utm_tracking.py` | 389 | **АКТИВЕН** (через utm_middleware + вызовы record_*) | `UserAction.objects.create` (строки 97, 307) — 36 859 строк в БД (базовая линия); UTMSession — 1015 |
| `storefront/ab_testing.py` | 194 | **МЁРТВ** | grep по репо: импортов **0** (только сам файл); в БД ничего не пишет из живого кода → кандидат на удаление без DB-проверки |
| `storefront/ai_signals.py` | 75 | **АКТИВЕН** | подключается в apps.py:17; на post_save Product/Category дергает `generate_ai_content_for_*_task.delay()` → из-за shim-а TD-003 это СИНХРОННАЯ AI-генерация в потоке админского сохранения |
| `storefront/utm_middleware.py` | — | **АКТИВЕН** (settings.py:223) | UTMSession get_or_create + increment_visit (см. AN-036) |

Пересечение/дубль: `SimpleAnalyticsMiddleware` (PageView/SiteSession) и `utm_tracking.record_page_view` (UserAction page_view) фиксируют пересекающиеся события в РАЗНЫЕ таблицы — два источника правды о трафике; отчёты админки читают UserAction, PageView живёт отдельно. Задача-кандидат: выбрать один канонический слой (UserAction) и вывести PageView из записи либо задокументировать назначение обоих.
Остаточный шаг (SSH, при доступности): `PageView.objects.count()` для оценки объёма таблицы — сервер дважды сбросил SSH 06.07 (rate-limit), выполнить при следующем сеансе. На классификацию «жив/мёртв» не влияет (middleware зарегистрирован → пишет).

---

## ⚠️ МЕТА-НАХОДКА (06.07.2026): прод живёт на production_settings.py, НЕ на settings.py

`passenger_wsgi.py` ставит `DJANGO_SETTINGS_MODULE=twocomms.production_settings`; `manage.py` тоже дефолтит на него. **Все аудиты настроек обязаны проверять production_settings.py (639 строк) как перекрывающий слой** — часть выводов по settings.py относится только к dev. Это касается CACHES, DATABASES, логирования и пр.

---

## TD-010. Узкие места рендера (АУДИТ ВЫПОЛНЕН, 06.07.2026, код-слой)

1. Context processors: 8 кастомных на КАЖДЫЙ запрос. `orders_processing_count` — образцовый (только staff, кэш 60s во fragments). `analytics_settings`, `site_urls`, `web_push_settings`, `user_state_hint` — лёгкие (env/URL-сборка). `management_shell_context`/`finance_shell_context` — проверить отдельно при аудите этих модулей. Криминала нет.
2. Cached template loader включён в прод-ветке (settings.py:994–997) — правильно.
3. Реальные узкие места рендера — не в процессорах, а в тяжёлых вьюхах каталога (см. PERF-раздел) и GZip на динамике (middleware:205).

## TD-011. Redis-кэш: покрытие и инвалидация (АУДИТ ВЫПОЛНЕН, 06.07.2026)

1. **Прод-конфиг — трёхвариантный** (production_settings.py:~380–530, выбор через env `CACHE_BACKEND`): redis (3 алиаса default/staticfiles/fragments, разные DB) | locmem | **file-based fallback** (`/home/qlknpodo/tmp/django_cache`, дефолт если env не задан!). Какой реально активен — знает только env на сервере (SSH-хвост: `echo $CACHE_BACKEND` + `python -c "from django.conf import settings; print(settings.CACHES['default']['BACKEND'])"`).
2. Использование: ~20 мест `cache.set/get_or_set` + fragment-кэш в шаблонах dtf + recommendations.py (alias fragments) + orders_processing_count.
3. Инвалидация: `cache_signals.py` (86 строк) — Category/Product/цвета/AnalyticsExclusion по post_save/post_delete. Осмысленно.
4. Замечание: settings.py прод-ветка определяет ТОЛЬКО 'default' (без fragments), но она мертва — прод на production_settings. Код везде деградирует аккуратно (dtf/cache_utils fallback на default, context processor в try/except).

## TD-012. REDIS_IGNORE_EXCEPTIONS маскирует падения (АУДИТ ВЫПОЛНЕН, 06.07.2026)

1. Подтверждено: `IGNORE_EXCEPTIONS: true` по дефолту (settings.py:875) + `COMMON_REDIS_OPTIONS` в production_settings. При падении Redis сайт живёт, но: кэш-промахи на каждый запрос (тихая деградация скорости) и **rate limiting отключается молча** (SimpleRateLimitMiddleware: `except: pass` = fail-open).
2. Мониторинга события «Redis умер» нет (логгер django_redis пишет warning, но логи никто не смотрит — TD-022).
3. Рекомендация: оставить IGNORE_EXCEPTIONS=true (доступность > кэш), но добавить counter/alert на исключения (django_redis выставляет `DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS=True` + logger handler).

## TD-013. Статика: compressor + whitenoise (АУДИТ ВЫПОЛНЕН, 06.07.2026)

1. django-compressor: `COMPRESS_OFFLINE=not DEBUG` с защитным `ensure_compress_offline()` (settings.py:1024–1076): если manifest CACHE/manifest.json отсутствует/пустой/нечитаем — offline тихо выключается (предупреждение в лог). Значит **если на деплое забыли `compress`, сайт не падает, а молча компрессит на лету** (CPU-цена на shared-хостинге).
2. WhiteNoise: `CompressedManifestStaticFilesStorage`, MAX_AGE 180 дней, кастомный `is_immutable_static_url`. Конфиг здоровый.
3. Риск-процесс: порядок деплоя должен быть `collectstatic → compress → restart`; проверить деплой-скрипт на сервере (SSH-хвост).

## TD-014. Медиа-кэш мидлвари (АУДИТ ВЫПОЛНЕН, 06.07.2026) — одна полумёртвая, одна мёртвая

1. `ImageOptimizationMiddleware` — ЗАРЕГИСТРИРОВАН (settings.py:209), но `IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED` default=False → **пустой pass-through на каждый запрос** (плюс создаёт ThreadPoolExecutor(2) на воркер даже выключенным). Если env-флаг на сервере не установлен — это мёртвый груз в цепочке; убрать из MIDDLEWARE или включить осознанно (SSH-хвост: проверить env).
2. `MediaCacheMiddleware` (media_cache_middleware.py, 42 строки) — **НЕ зарегистрирован нигде = мёртвый код**, удалить. Его функцию (Cache-Control на /media/) на проде выполняет LiteSpeed/htaccess (проверить при SSH).
3. WebP-конверсия: middleware кэширует в `media/optimized_cache/` — на диске сервера может лежать устаревший кэш; замерить размер при SSH-сеансе.

## TD-023. Rate limiting (АУДИТ ВЫПОЛНЕН, 06.07.2026) — глобальный есть, точечного нет, XFF-спуфинг

1. Глобальный: `SimpleRateLimitMiddleware` (middleware.py:~320) — 100 req/min/IP, окно 1 мин через cache. Дыры: (а) **IP берётся из первого значения X-Forwarded-For — подделывается клиентом**, если LiteSpeed не перезаписывает заголовок (проверить конфиг при SSH); (б) fail-open при недоступном кэше (связка с TD-012); (в) dtf.* GET полностью исключён.
2. Точечный django-ratelimit: ТОЛЬКО 2 эндпоинта lookup в cart.py (:1752, :1789) и оба с `block=False` (не блокирует, только помечает). **Логин, регистрация, чекаут, отзывы, /api/ — без индивидуальных лимитов** (login brute-force ограничен только глобальной soтней в минуту).
3. Рекомендация: ratelimit на accounts login/register (5/min), checkout POST (10/min), review POST; и `key=` на основе REMOTE_ADDR, не XFF.

## TD-024. DRF API поверхность (АУДИТ ВЫПОЛНЕН, 06.07.2026)

Карта viewsets.py (707 строк) + api_urls.py:
| ViewSet | Права | Оценка |
|---|---|---|
| Category/Product (ReadOnly) | AllowAny | ок для публичного каталога |
| AdminProductBuilder | IsAuthenticated+IsAdminUser | ок |
| Cart | IsAuthenticatedOrReadOnly | проверить объектную привязку к владельцу при детальном аудите |
| Analytics `/api/analytics/track/` | AllowAny | **МЁРТВЫЙ эндпоинт: тело = TODO, событие НИКУДА не сохраняется** — публичный no-op; удалить или реализовать |
| Communication | AllowAny | проверить, что за операции (спам-вектор) |
Плюс: Swagger/Redoc публично на /api/docs/, /api/redoc/ (:78–84) — раскрытие поверхности; закрыть за staff.

## TD-025. db_routers.py (АУДИТ ВЫПОЛНЕН, 06.07.2026) — обоснован, оставить

`DtfDatabaseRouter` — опциональная изоляция app `dtf` в DATABASES['dtf'] (алиас добавляется в settings.py:636/655 только при env). auth/contenttypes остаются в default (общий логин домена и dtf-поддомена). Код корректный (allow_migrate правильно ограждает обе стороны). Действий не требуется; задокументировано.

---

## TD-021. Секреты не в репозитории (АУДИТ ВЫПОЛНЕН, 06.07.2026) — **P1: db_config.env с DB_PASSWORD и SECRET_KEY лежит в ИСТОРИИ git**

1. Текущее состояние (HEAD): чисто. SECRET_KEY только из env (settings.py:103–109, с fail-fast в проде); хардкодов токенов по паттернам (telegram bot-token, api_key='...') не найдено; `.env*` в .gitignore (кроме example/sample); `.env.example` — единственный env-файл в git.
2. **История: `twocomms/db_config.env` был закоммичен (минимум 3 коммита: f7e276fc, 179908f9, 3d1abb44) и содержит реальные DB_ENGINE/DB_NAME/DB_USER/DB_PASSWORD/DB_HOST + SECRET_KEY + DEBUG.** Файл потом удалили из индекса, но `git show <hash>:twocomms/db_config.env` отдаёт содержимое любому, у кого есть доступ к репо (репо на GitHub).
3. **Действия для исполнителя (P1):**
   - Немедленно: сменить DB_PASSWORD у MySQL-пользователя и SECRET_KEY в проде (смена SECRET_KEY инвалидирует сессии/подписи — сделать в окно с меньшим трафиком; учесть password-reset токены).
   - Затем: вычистить файл из истории (`git filter-repo --path twocomms/db_config.env --invert-paths`) + force-push + попросить GitHub Support очистить кэши PR. Либо принять риск, если репо приватный и доступ ограничен, но ротация секретов обязательна в любом случае.
   - Проверить остальные найденные в истории паттерны при ротации (полный скан `gitleaks`/`trufflehog` — 1 команда, рекомендуется).

---

## TD-030. Статусная модель Order (АУДИТ ВЫПОЛНЕН, 06.07.2026, код-слой) — «ship» некому ставить

1. Розничный `Order.status`: 5 значений — new/prep/ship/done/cancelled (models.py:11–17).
2. Кто ставит что (полный grep по присваиваниям):
   - `prep` — только `order_actions.py:489` (админ-действие);
   - `done` — НП-синк при доставке (`nova_poshta_service.py:438`) + management-команда fix_delivered_orders;
   - `ship` — **НИКТО в коде не ставит автоматически** (только вручную через админку, если ставят). НП-синк знает про отправку (shipment_status обновляется), но промежуточный статус ship не проставляет → заказы прыгают new→done. Это и есть механика «в БД только new/done/cancelled» из наблюдений пред. итераций.
3. Контраст: `DropshipperOrder` имеет БОГАТУЮ модель — 12 статусов (draft…received/refused, models.py:545–558) и НП-синк её реально двигает (models.py:734–775: awaiting_shipment/confirmed/delivered_awaiting_pickup/received/refused).
4. Рекомендация: либо (а) научить НП-синк ставить розничному заказу `ship` при статусах отправки (маленькая правка в _apply_tracking_update — код уже различает статусы), либо (б) осознанно принять 3-статусную модель и убрать prep/ship из UI. Вариант (а) дешёвый и даёт честную воронку статусов.

---

## TD-031. Нова Пошта API-синк статусов (АУДИТ ВЫПОЛНЕН, 06.07.2026) — архитектура здоровая, двухслойная

1. Основной путь: management-команда `update_tracking_statuses` (cron) → `update_all_tracking_statuses()` → `TrackingDocument.getStatusDocuments` (rate-limit учтён, `_check_rate_limit`).
2. Резервный путь: `NovaPoshtaFallbackMiddleware` (зарегистрирован, settings:225) — если cron молчит >15 мин (multiplier 3), обновление запускается daemon-потоком из обычного запроса, с cache-lock от дублей. Продумано.
3. Применение статуса: `_apply_tracking_update` — атомарно с `select_for_update`, антиспам-якорь по StatusCode в `payment_payload['np_tracking']`, отдельные ветки уведомлений (админ/клиент/FB Purchase offline-событие :481).
4. Слабые места: (а) розничный Order получает только `done` (см. TD-030); (б) `_send_facebook_purchase_event` при доставке — проверить дедупликацию с онлайн-Purchase (пересечение с AN-014); (в) работает ли cron фактически — SSH-хвост (`crontab -l`).

---

## TD-032. COGS-снапшот в заказе (АУДИТ ВЫПОЛНЕН, 06.07.2026) — подтверждено: у розничных заказов себестоимости НЕТ

1. `OrderItem` (models.py:338+): title/size/qty/unit_price/line_total — **ни одного поля себестоимости**. Product тоже продаётся без cost-поля в снапшоте.
2. Где COGS есть: (а) офлайн-магазины — `StoreProduct/StoreOrderItem/StoreSale` (storefront/models.py:1742,1807,1848) с cost_price и расчётом маржи; (б) дропшип — `DropshipperOrder.profit` = selling − drop_price (models.py:671).
3. Следствие: посчитать валовую прибыль по РОЗНИЧНЫМ заказам невозможно ретроспективно; любые будущие отчёты прибыли будут врать, если брать текущую закупку задним числом (цены закупки меняются — см. wholesale_prices.xlsx в TD-004).
4. Рекомендация (для TECHNICAL_TASKS): добавить `cost_price_snapshot` в OrderItem (nullable), заполнять при создании заказа из карточки закупочной цены; источник закупочных цен формализовать (сейчас — xlsx в репо, что само по себе антипаттерн, см. CB-005).

---

## Журнал раздела

| Дата | ID | Статус |
|---|---|---|
| 05.07.2026 | TD-020 | **P0: регулярных бэкапов MySQL нет; последний ручной дамп >8 мес. Блокирует все миграции.** |
| 05.07.2026 | TD-016 | Частично: ротация django/stderr есть; 8 мёртвых логов; ig_bot_cron.log требует замера |
| 05.07.2026 | TD-033 | Подтверждён: 28/28 new; эндпоинт смены статуса существует, но не используется; параллельная ось moderation_status работает (24/2/2); 0 лидов привязано к заказам |
| 06.07.2026 | TD-001 | **Коррекция: backup-файл исполняется в рантайме (102 имени, 30 маршрутов) — нужен перенос, не git rm** |
| 06.07.2026 | TD-002 | Мёртвый шаблон, 0 ссылок — удалить безопасно |
| 06.07.2026 | TD-003 | **P2-баг: битый импорт TG-таска → синхронные отправки в request-потоке; сигнатуры несовместимы** |
| 06.07.2026 | TD-004 | Инвентаризация мусора twocomms/ завершена, вердикты по каждому объекту |
| 06.07.2026 | TD-005 | 202 md подтверждено; план 1-PR git mv в docs/archive |
| 06.07.2026 | TD-006 | 48 заглушек; фейковые 'ok' в admin_store_*; объединить с TD-001 |
| 06.07.2026 | TD-007 | ab_testing.py мёртв (0 импортов); карта записи построена; PageView.count() — при SSH |
| 06.07.2026 | META | **Прод на production_settings.py (passenger_wsgi) — аудиты настроек обязаны смотреть оба файла** |
| 06.07.2026 | TD-010 | Context processors здоровые; cached loader включён; узкие места не здесь |
| 06.07.2026 | TD-011 | Трёхвариантный CACHE_BACKEND (redis/locmem/**file-based дефолт**); реальный бекенд — SSH-хвост |
| 06.07.2026 | TD-012 | Подтверждён fail-open: Redis умер → rate limiting молча отключается; алертов нет |
| 06.07.2026 | TD-013 | ensure_compress_offline тихо выключает offline при отсутствии manifest → CPU на лету; проверить деплой-скрипт по SSH |
| 06.07.2026 | TD-014 | ImageOptimization зарегистрирован но default-выключен (pass-through); **MediaCacheMiddleware мёртв — удалить** |
| 06.07.2026 | TD-023 | **XFF-спуфинг ключа лимита; логин/чекаут без точечных лимитов; block=False на обоих ratelimit** |
| 06.07.2026 | TD-024 | **`/api/analytics/track/` — публичный no-op (TODO в теле)**; Swagger публичен; карта прав построена |
| 06.07.2026 | TD-025 | DtfDatabaseRouter обоснован, корректен, действий не требуется |
