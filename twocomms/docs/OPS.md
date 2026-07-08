# OPS — боевой сервер TwoComms

> W0-5 (CB-043/CB-044/CB-012). Зафиксировано по инвентаризации 05-07.07.2026.
> Обновляйте этот файл при ЛЮБОМ изменении crontab или деплой-процесса.

## Боевой settings-модуль

- Продакшен: `twocomms.settings` (env-переменные из `.env` в корне проекта на сервере).
- Тесты в песочнице/CI: `test_settings` (sqlite in-memory) / `preview_settings`.
- Ключевые env-инварианты: `MONOBANK_TOKEN`, `MONOBANK_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `FACEBOOK_CAPI_TOKEN`, `TIKTOK_EVENTS_TOKEN` — все берутся из env, НЕ из кода (после W0-2-ротации).

## Crontab (7 задач, снято `crontab -l` 05.07.2026)

| # | Расписание | Команда | Лог | Назначение / риск |
|---|---|---|---|---|
| 1 | `20 4 * * *` | `manage.py trim_analytics` | `logs/trim_analytics.log` | retention UserAction (DB-004) |
| 2 | `*/30 * * * *` | `manage.py recover_checkouts` | `logs/recover_checkouts.log` | **ДЕНЬГИ**: добор брошенных чекаутов; правки checkout.py учитывают этот фон |
| 3 | `* * * * *` | `manage.py run_instagram_bot --ensure` | `tmp/ig_bot_cron.log` | держит IG-бота живым; лог без ротации |
| 4 | `*/5 * * * *` | `manage.py checker_tick` | `tmp/checker_tick.log` | тики management-приложения |
| 5 | `*/4 * * * *` | `manage.py poll_ig_deal_payments` | `tmp/poll_ig_deal_payments.log` | **ДЕНЬГИ**: опрос оплат IG-сделок |
| 6 | `30 4 * * *` | `manage.py purge_ig_clients` | `tmp/purge_ig_clients.log` | чистка IG-клиентов |
| 7 | `15 3 * * *` | `manage.py generate_bot_fingerprints --limit 15 --sleep 2` | `tmp/fp_cron.log` | fingerprints для бота |

### Чего в cron НЕТ (известные дыры)

1. **Бэкапа MySQL** — P0, см. W0-3 (скрипт `scripts/backup_mysql.sh` в репо; добавить cron `[SERVER]`).
2. Генерации Google Merchant feed (SEO-008) — фид статикой, устаревает.
3. IndexNow/sitemap-пингов.
4. logrotate для `tmp/*.log` (W3-6) — REPO-часть готова: `scripts/rotate_twocomms_logs.sh`; добавить cron `[SERVER]`.
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

- Все 7 cron-команд — management-команды из репо; loose-скрипты корня кроном НЕ вызываются (CB-044) → их можно архивировать без риска для cron.
- НИКОГДА не добавляйте cron-задачу без записи в эту таблицу.
- Логи в `tmp/` ротируются только после установки cron для `scripts/rotate_twocomms_logs.sh`; новые задачи логируйте в `logs/` и добавляйте в этот rotation-путь.

### Log rotation / PII (W3-6)

REPO-часть:

- `twocomms.log_handlers.PIIRedactionFilter` подключён ко всем logging handlers и маскирует email, украинские телефоны и длинные числовые последовательности до записи в persistent logs.
- `scripts/rotate_twocomms_logs.sh` ротирует большие `*.log` под проектом, gzip-архивы складывает в `$HOME/log_archives/twocomms`, права 700/600, retention 30 дней.

SERVER-часть:

```cron
10 4 * * * /bin/bash /home/qlknpodo/TWC/TwoComms_Site/scripts/rotate_twocomms_logs.sh >> /home/qlknpodo/log_archives/twocomms/rotate_twocomms_logs.log 2>&1
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
