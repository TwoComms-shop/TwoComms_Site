# OPS — боевой сервер TwoComms

> W0-5 (CB-043/CB-044/CB-012). Зафиксировано по инвентаризации 05-07.07.2026.
> Обновляйте этот файл при ЛЮБОМ изменении crontab или деплой-процесса.

## Боевой settings-модуль

- Продакшен: `twocomms.settings` (env-переменные из `.env` в корне проекта на сервере).
- Тесты в песочнице/CI: `test_settings` (sqlite in-memory) / `preview_settings`.
- Ключевые env-инварианты: `MONOBANK_TOKEN`, `MONOBANK_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `FACEBOOK_CAPI_TOKEN`, `TIKTOK_EVENTS_TOKEN` — все берутся из env, НЕ из кода (после W0-2-ротации).

## Crontab (live snapshot 14.07.2026: 2 задачи)

> Важно: инвентаризация 05.07.2026 содержала 7 задач, но повторный production
> `crontab -l` 13–14.07.2026 показал только ротацию логов. F-083 добавил вторую
> задачу ниже. Старые записи нельзя считать действующими и нельзя массово
> восстанавливать без отдельной проверки их команд, логов и альтернативных
> scheduler-процессов.

| # | Расписание | Команда | Лог | Назначение / риск |
|---|---|---|---|---|
| 1 | `10 4 * * *` | `scripts/rotate_twocomms_logs.sh` | `$HOME/log_archives/twocomms/rotate_twocomms_logs.log` | ротация/retention persistent logs |
| 2 | `17 4 * * *` | `manage.py reconcile_purchase_actions --apply` | `logs/reconcile_purchase_actions.log` | F-083: идемпотентно лечит доказуемые пропуски внутренних purchase |

### Чего в cron НЕТ (известные дыры)

1. **Бэкапа MySQL** — P0, см. W0-3 (скрипт `scripts/backup_mysql.sh` в репо; добавить cron `[SERVER]`).
2. Генерации Google Merchant feed (SEO-008) — фид статикой, устаревает.
3. IndexNow/sitemap-пингов.
4. Задач из старого snapshot: `trim_analytics`, `recover_checkouts`,
   `run_instagram_bot --ensure`, `checker_tick`, `poll_ig_deal_payments`,
   `purge_ig_clients`, `generate_bot_fingerprints`. Их отсутствие подтверждено,
   но способ восстановления требует отдельного production-аудита.
5. **`manage.py check_survey_inactivity`** (W3-1/TD-015) — survey-репорты о брошенных опросах. Раньше висела в `CELERY_BEAT_SCHEDULE` (каждые 2 мин), но beat не запущен → не выполнялась никогда. Добавить cron `*/5 * * * *` `[SERVER]`.
6. **`manage.py cleanup_analytics_data`** (W2-10/AN-051) — retention аналитики (UserAction >180д, неконверсионные UTMSession >90д, orphan-события). Добавить cron `20 4 * * 0` (раз в неделю) `[SERVER]`.

### Решение: Celery НЕ возвращаем (W3-1 / TD-015)

Прод-хостинг (shared, Passenger) не запускает Celery-воркер и beat; Redis-брокер
с сервера не резолвится. Все `@shared_task` работают через синхронный шим
(`storefront/tasks.py`), фоновость для Telegram-уведомлений — daemon-поток
(`orders/tasks.py`). `CELERY_BEAT_SCHEDULE` в settings.py — мёртвый конфиг,
периодика выполняется ТОЛЬКО через crontab (см. дыры 5-6 выше). Новую
фоновую работу оформлять как management-команду + cron, НЕ как Celery-таск.

### Инварианты

- Обе текущие cron-команды находятся в репо; loose-скрипты корня кроном не вызываются.
- НИКОГДА не добавляйте cron-задачу без записи в эту таблицу.
- Новые задачи логируйте в `logs/`; `reconcile_purchase_actions` пишет одну короткую итоговую строку в сутки.

### MySQL backup runbook (F-090)

Repository entry point:
`/home/qlknpodo/TWC/TwoComms_Site/scripts/backup_mysql.sh`.

- The script requires every database name explicitly as positional arguments;
  it has no implicit production database fallback.
- Credentials are read only from
  `TWC_MYSQL_DEFAULTS_FILE` (default `$HOME/.my.cnf`, mode `0600` or `0400`).
- `$HOME/db_backups`, `daily/` and `weekly/` are outside the project/web root and
  remain mode `0700`; archives and `last_success` remain mode `0600`.
- A non-blocking `flock` prevents cron/manual overlap. Exit 75 means another
  backup already owns the lock.
- Every configured database is first dumped to a process-scoped temporary
  archive. Size and `gzip -t` must pass for the complete batch before any final
  archive is replaced.
- Daily retention is 14 days; Sunday copies remain 35 days. Successful runs
  atomically update `$HOME/db_backups/last_success` with timestamp and database
  count only.

Target cron (replace the placeholders with the two names read from production
settings; database names are not credentials):

```cron
# TWOCOMMS_DB_BACKUP
45 3 * * * /bin/bash /home/qlknpodo/TWC/TwoComms_Site/scripts/backup_mysql.sh <MAIN_DB> <DTF_DB> >> /home/qlknpodo/db_backups/backup.log 2>&1
```

Before installing cron, run the exact command manually, validate every archive
with `gzip -t`, then restore each archive into a uniquely named temporary
database. Compare table counts and key-table row counts, run Django checks, and
drop only the temporary restore databases in guaranteed cleanup. Install the
marked cron block idempotently without rewriting unrelated jobs.

### Log rotation / PII (W3-6)

REPO-часть:

- `twocomms.log_handlers.PIIRedactionFilter` подключён ко всем logging handlers и маскирует email, украинские телефоны и длинные числовые последовательности до записи в persistent logs.
- `scripts/rotate_twocomms_logs.sh` ротирует большие `*.log` под проектом, gzip-архивы складывает в `$HOME/log_archives/twocomms`, права 700/600, retention 30 дней.

SERVER-часть:

```cron
10 4 * * * /bin/bash /home/qlknpodo/TWC/TwoComms_Site/scripts/rotate_twocomms_logs.sh >> /home/qlknpodo/log_archives/twocomms/rotate_twocomms_logs.log 2>&1
17 4 * * * cd /home/qlknpodo/TWC/TwoComms_Site/twocomms && /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/python manage.py reconcile_purchase_actions --apply >> /home/qlknpodo/TWC/TwoComms_Site/twocomms/logs/reconcile_purchase_actions.log 2>&1
```

## Git-состояние сервера (CB-043, снято 05.07.2026)

- Ветка `main`, tracked-файлы чистые → `git pull` безопасен.
- **10 git-stash** на сервере — судьба каждого решается с владельцем `[OWNER]`; до решения `git stash drop` НЕ выполнять.
- Untracked диаг-скрипты на бою — инвентаризировать перед удалением.

## Смок-тесты перед деплоем (W0-4 / CB-024)

Прогонять локально перед каждым деплоем:

```bash
python manage.py test \
  storefront.tests.test_checkout \
  storefront.tests.test_monobank_webhook \
  storefront.tests.test_cart_sync \
  storefront.tests.test_utm_attribution \
  --settings=test_settings
```

Покрытие «денежного» контура:
- COD-заказ (гость и авторизованный), double-submit, промокоды — `test_checkout`
- Подпись ECDSA, pull-verify, недоплата, **идемпотентность повторного вебхука** — `test_monobank_webhook`
- Слияние корзины при логине, мультидевайс — `test_cart_sync`
- UTM-привязка заказа (fallback-цепочка) — `test_utm_attribution`

## Деплой

- Изменили статику (`static/js/*`, CSS) → bump cache-buster в base.html + `collectstatic`.
- Изменили модели → `makemigrations` + `migrate` (бэкап БД перед migrate — см. W0-3).
- После деплоя: смок-прогон руками — главная, карточка товара, добавление в корзину, шаг чекаута.
