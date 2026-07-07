# ПРОМПТ ДЛЯ SSH-АГЕНТА — Сбор серверных данных для глобального аудита TwoComms

> Дата: 07.07.2026. Автор: v0-агент (песочница без SSH — IP забанен файрволом хостинга на kex-этапе).
> Ты работаешь в окружении, ГДЕ SSH ДОСТУПЕН. Твоя задача — собрать данные с продакшн-сервера и вернуть их (вывод в чат владельцу И/ИЛИ коммит файлов с выводом в репозиторий). Анализировать и закрывать пункты чек-листа будет другой агент (или ты, если владелец попросит).
> Прочитай этот файл ЦЕЛИКОМ. Здесь всё: доступы, команды, правила безопасности, точный список того, что собрать, и формат возврата.

---

## 1. КОНТЕКСТ ПРОЕКТА (минимум, чтобы ты понимал что делаешь)

- **TwoComms** — Django e-commerce магазин стритвир-одежды (Украина). Прод: **https://twocomms.shop**
- **Хостинг:** Hostsila **shared hosting**, LiteSpeed + Passenger (Python WSGI). Это НЕ VPS — прав root нет, ресурсы ограничены, соседи по серверу есть.
- **Репозиторий:** `zainllw0w/TwoComms_Site`, ветка `main`. Django-код в `twocomms/`, аудит-документы в `TWOCOMMS_A_TO_B/technical/`.
- **Идёт глобальный аудит** по чек-листу `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md` (~150 пунктов). Закрыто всё, кроме **10 пунктов, которым нужны данные с сервера**. Всё, что можно было сделать без SSH (код-анализ, live-краул 489 страниц, curl-пробы), уже сделано.
- Полный контекст предыдущих сессий: `TWOCOMMS_A_TO_B/technical/NEXT_AGENT_PROMPT.md` и `SESSION_HANDOFF_2026-07-07.md`.

## 2. ДОСТУП К СЕРВЕРУ

```
Хост:     195.191.24.169
Порт:     22
Юзер:     qlknpodo
Пароль:   Trs5m4t1zxcvqwer!twc
Проект:   /home/qlknpodo/TWC/TwoComms_Site/twocomms/        (manage.py здесь)
Venv:     /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate
```

Подключение:
```bash
sshpass -p 'Trs5m4t1zxcvqwer!twc' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169
```

### ⚠️ КРИТИЧЕСКИЕ ПРАВИЛА (нарушение = бан IP и срыв аудита)

1. **На сервере агрессивный fail2ban/файрвол.** Прошлые песочницы получали перманентный бан IP за: (а) параллельный HTTP-краул сайта, (б) повторные неудачные SSH-попытки. **Если первое SSH-подключение не удалось — НЕ долби повторами.** Максимум 2 попытки с паузой 2–3 минуты, затем сообщи владельцу.
2. **Минимизируй число SSH-сессий.** Идеально — ВСЁ в 1–2 сессиях. Скрипты для этого спроектированы batch-ово.
3. **ТОЛЬКО ЧТЕНИЕ.** Ничего не менять, не удалять, не перезапускать на сервере. Все команды в скриптах read-only (SELECT/SHOW/ls/grep/ps). НЕ запускай migrate, НЕ трогай .env, НЕ рестартуй Passenger.
4. **Не печатай секреты.** Скрипты уже спроектированы так, что grep по логам выводит только СЧЁТЧИКИ совпадений, не сами строки. Не отклоняйся от этого. Содержимое `.env` не выводить никогда.
5. Это shared hosting — не запускай ничего тяжёлого (долгие table scan'ы по многомиллионным таблицам уже ограничены в скриптах LIMIT'ами и count'ами).

## 3. ЧТО ИМЕННО ЗАПУСТИТЬ (два готовых скрипта, оба в репо)

Скрипты лежат в `TWOCOMMS_A_TO_B/technical/scripts/`. Склонируй репо или скопируй файлы себе.

### Шаг 1 — bash-батч (системная часть: TD-015, TD-016, CB-015, CB-041, CB-045, DB-007)

```bash
sshpass -p 'Trs5m4t1zxcvqwer!twc' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 'bash -s' \
  < TWOCOMMS_A_TO_B/technical/scripts/server_shell_batch.sh \
  > server_shell_batch_output.txt 2>&1
```

Что он собирает (секции `===SECTION name===` в выводе):
- `crontab` — полный crontab -l (нужен для CB-015: какие management-команды реально в cron'е, и TD-015: есть ли cron-замена Celery beat)
- `passenger_processes` — ps по passenger/python/lsphp с RSS-памятью (TD-015: сколько воркеров, сколько памяти жрут)
- `memory_limits` — ulimit -a, free -m (TD-015)
- `htaccess_passenger` — passenger/lsapi директивы из .htaccess (TD-015: лимиты воркеров/инстансов)
- `logs_listing` — все .log файлы проекта с размерами (TD-016, CB-045)
- `logrotate` — есть ли ротация (TD-016, CB-045)
- `log_secret_scan_counts_only` — СЧЁТЧИКИ совпадений паттернов секретов/PII в логах, без самих строк (TD-016, CB-045)
- `env_check` — существование/права .env-файлов + наличие флага IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED (CB-041; сам файл НЕ выводится, только grep-count имени переменной)
- `redis_celery_queue` — `redis-cli llen celery` (TD-015: подтверждение P1-находки — очередь мёртвой Celery растёт)
- `migrations` — `python manage.py makemigrations --check --dry-run` + `showmigrations | grep '\[ \]'` (DB-007)

### Шаг 2 — Django shell батч (данные из БД: CRO-051, AEO-001, DB-001, DB-003, DB-004, DB-006)

```bash
sshpass -p 'Trs5m4t1zxcvqwer!twc' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 \
  'source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && cd /home/qlknpodo/TWC/TwoComms_Site/twocomms && python manage.py shell' \
  < TWOCOMMS_A_TO_B/technical/scripts/server_audit_batch.py \
  > server_audit_batch_output.txt 2>&1
```

Вывод — JSON между маркерами `===AUDIT_JSON_START===` / `===AUDIT_JSON_END===`. Что внутри:
- `action_totals` / `funnel_raw` — счётчики UserAction по типам: page_view → product_view → add_to_cart → initiate_checkout → lead → purchase (CRO-051: базовая линия конверсии)
- `product_view` — качество данных: сколько без session/user, distinct products (CRO-051)
- `ai_referrals` — визиты с referer'ами ChatGPT/Perplexity/Gemini/Copilot + какие страницы цитируются (AEO-001)
- `slow_queries` — SHOW VARIABLES по slow_query_log, performance_schema top-запросы, если доступны (DB-001)
- `order_utm_integrity` — Order ↔ UTMSession ↔ User: orphan'ы, NULL-связи, проценты покрытия (DB-003)
- `table_status` — SHOW TABLE STATUS: строки/размер/engine ключевых таблиц, рост UserAction (DB-004)
- `charsets` — charset/collation БД, таблиц и колонок — всё ли utf8mb4 (DB-006)

### Если что-то падает

- `manage.py shell` может ругаться на настройки — проверь, что активирован venv И что `DJANGO_SETTINGS_MODULE` не нужен явно (обычно manage.py сам подхватывает). Если нужен: `DJANGO_SETTINGS_MODULE=twocomms.production_settings python manage.py shell`.
- Если какая-то секция bash-скрипта падает (нет прав на /etc/logrotate.d и т.п.) — это ОК, скрипт с `set -u` но без `set -e`, он продолжит. Частичный вывод тоже ценен.
- Если redis-cli нет в PATH — попробуй `~/redis-cli` или пропусти секцию, отметь это.
- Python на сервере — 3.14 (по пути venv). Скрипт batch_py совместим.

## 4. ЧТО ВЕРНУТЬ (формат результата)

**Вариант А (предпочтительный): закоммить в репозиторий** `zainllw0w/TwoComms_Site`, ветка любая + PR в main (или прямо в main, владелец разрешает):

```
TWOCOMMS_A_TO_B/technical/data/server_shell_batch_output.txt
TWOCOMMS_A_TO_B/technical/data/server_audit_batch_output.txt
```

⚠️ Перед коммитом ПРОВЕРЬ, что в выводе нет секретов (паролей, ключей, содержимого .env). Скрипты спроектированы безопасно, но перепроверь глазами секции `env_check` и `log_secret_scan_counts_only` — там должны быть только счётчики и имена файлов. Если что-то просочилось — замажь `[REDACTED]`.

**Вариант Б:** вставь оба вывода целиком в чат владельцу — он передаст их аналитическому агенту.

**В коммит-сообщение добавь trailer:** `Co-authored-by: v0 <it+v0agent@vercel.com>`

## 5. КАКИЕ ПУНКТЫ АУДИТА ЭТО ЗАКРОЕТ (для понимания важности)

| ID | Что проверяется | Какой скрипт даёт данные |
|---|---|---|
| CRO-051 | Базовая линия конверсии воронки из живой БД UserAction | batch_py: funnel_raw |
| TD-015 | Passenger: воркеры/память/лимиты + подтверждение P1-риска «мёртвая Celery-очередь в Redis» | shell: passenger_processes, htaccess, redis; batch_py |
| TD-016 | Логи на сервере: размер, ротация, секреты в логах | shell: logs_listing, logrotate, log_secret_scan |
| AEO-001 | Какие страницы сайта цитируют ChatGPT/AI-поисковики (referer-анализ) | batch_py: ai_referrals |
| DB-001 | Медленные запросы MySQL | batch_py: slow_queries |
| DB-003 | Целостность Order ↔ UTMSession ↔ User | batch_py: order_utm_integrity |
| DB-004 | Рост UserAction, SHOW TABLE STATUS | batch_py: table_status |
| DB-006 | Charset utf8mb4 везде | batch_py: charsets |
| DB-007 | makemigrations --check + showmigrations | shell: migrations |
| CB-045 | Размер логов, logrotate, PII в логах | shell: logs_listing, log_secret_scan |
| CB-015 (остаток) | crontab -l → вердикт по 16 CRON?-кандидатам management-команд | shell: crontab |
| CB-041 (остаток) | Подтвердить env-флаг IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED на бою | shell: env_check |

## 6. ВАЖНЫЙ КОНТЕКСТ ДЛЯ ИНТЕРПРЕТАЦИИ (если будешь анализировать сам)

- **P1-находка (уже задокументирована, нужно только подтверждение):** прод работает БЕЗ Celery-воркера, но Redis-брокер жив. `.delay()` успешно кладёт таски в очередь `celery`, которую никто не читает → Telegram-уведомления о смене статуса заказа теряются молча, очередь растёт бесконечно. Подтверждение = `redis-cli llen celery` со значением >> 0.
- **Ожидания по данным:** таблица UserAction — крупнейшая (вероятно, миллионы строк). SiteSession/UTMSession — тоже большие. Если count'ы выполняются долго — это само по себе результат для DB-004.
- Сервер иногда отдаёт интермиттентные 500/503 (Passenger cold-start/overload) — если Django shell подвиснет на старте на 10–20 сек, это нормально.
- Сайт двуязычный uk/ru + en; часовой пояс Киев.
- Все результаты пойдут в отчёты `audit_report_section*.md` на русском языке.

## 7. ЧЕК-ЛИСТ ТВОИХ ДЕЙСТВИЙ

1. [ ] Склонировать `zainllw0w/TwoComms_Site` (main) или получить файлы скриптов.
2. [ ] Проверить SSH-доступ ОДНИМ подключением: `ssh ... "echo OK && hostname"`.
3. [ ] Запустить `server_shell_batch.sh` (шаг 1) → сохранить вывод.
4. [ ] Запустить `server_audit_batch.py` через manage.py shell (шаг 2) → сохранить вывод.
5. [ ] Проверить выводы на секреты, замазать при необходимости.
6. [ ] Закоммитить оба файла в `TWOCOMMS_A_TO_B/technical/data/` + пуш (или вставить в чат).
7. [ ] Сообщить владельцу: «данные собраны, лежат там-то» — дальше их разберёт аналитический агент и закроет 10 пунктов чек-листа.

Удачи. Главное: read-only, минимум SSH-сессий, не долбить при неудаче.
