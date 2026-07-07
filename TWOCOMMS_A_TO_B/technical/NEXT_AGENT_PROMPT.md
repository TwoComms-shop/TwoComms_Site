# ПРОМПТ ДЛЯ СЛЕДУЮЩЕГО АГЕНТА — Глобальный аудит TwoComms

> Дата передачи: 07.07.2026 (вечерняя сессия). Предыдущая сессия: v0-агент, ветка `v0/bokok41916-6794-ad79d65d` (смержена в main, PR #55).
> Прочитай этот файл ЦЕЛИКОМ перед началом работы. Здесь весь контекст, все грабли и точный план действий. Более ранний контекст — в `SESSION_HANDOFF_2026-07-07.md`.

---

## 1. ЧТО ЭТО ЗА ПРОЕКТ И ЗАДАЧА

- **Проект:** TwoComms — Django e-commerce магазин стритвир-одежды (Украина). Продакшн: **https://twocomms.shop**
- **Хостинг:** Hostsila shared hosting, LiteSpeed + Passenger (Python WSGI). Сервер: `qlknpodo@195.191.24.169`, проект живёт в `~/TWC/TwoComms_Site/twocomms/`
- **Репо:** `zainllw0w/TwoComms_Site`, основная ветка `main`. Django-код в `twocomms/`, аудит-документы в `TWOCOMMS_A_TO_B/technical/`
- **Главная задача:** пройти глобальный чек-лист **`TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`** (~150 пунктов). Каждый пункт: проверка → результат в соответствующий `audit_report_section*.md` → отметить `- [x]` в чек-листе с выводом и датой.
- **Владелец просит:** после КАЖДОГО закрытого шага — коммит и пуш В СВОЮ ВЕТКУ + мерж/пуш в `main` (владелец хочет, чтобы main всегда был актуален). Прямой `git push origin main` из песочницы работает через worktree-приём (см. §6).

## 2. ТЕКУЩИЙ СТАТУС ЧЕК-ЛИСТА

**Осталось 12 незакрытых пунктов** (в этой сессии закрыты CB-015 и CB-041; SEO-006/SEO-007 в работе, краулер бежит):

| ID | Строка (~) | Суть | Что нужно |
|---|---|---|---|
| CRO-051 | 126 | Базовая линия конверсии из живой БД UserAction | SSH + Django shell |
| TD-015 | 193 | passenger_wsgi + лимиты воркеров/памяти | SSH (code-часть частично см. §4.4) |
| TD-016 | 194 | Логи на сервере, ротация, секреты | SSH |
| SEO-006 | 223 | Битые внутренние ссылки, 410 для удалённых товаров | 🔄 Краулер работает, добить (см. §4.1) |
| SEO-007 | 224 | Уникальность/длина meta всех товаров | 🔄 Краулер работает, добить (см. §4.1) |
| AEO-001 | 238 | Какие страницы цитирует ChatGPT | SSH + Django shell |
| DB-001 | 249 | Медленные запросы MySQL | SSH |
| DB-003 | 251 | Целостность Order ↔ UTMSession ↔ User | SSH + Django shell |
| DB-004 | 252 | Рост UserAction, SHOW TABLE STATUS | SSH + Django shell |
| DB-006 | 254 | Charset utf8mb4 везде | SSH + Django shell |
| DB-007 | 255 | makemigrations --check + showmigrations | SSH |
| CB-045 | 310 | Размер логов, logrotate, PII в логах | SSH |

**10 из 12 требуют SSH. SSH из песочницы НЕ РАБОТАЕТ** — подтверждено повторно в этой сессии (см. §3).

## 3. SSH — ПОДТВЕРЖДЁННЫЙ БЛОКЕР (не трать время)

В ЭТОЙ сессии снова проверено:
- `sshpass -p '<пароль>' ssh qlknpodo@195.191.24.169` порт 22 — таймаут (3 попытки с паузами).
- Порты 2222, 22022, 2200 — тоже закрыты/фильтруются (проверено `/dev/tcp` и ssh).
- `curl https://twocomms.shop/` при этом работает отлично → сеть жива, фильтруется именно SSH.
- Пароль владелец давал в чате: `Trs5m4t1zxcvqwer!twc` (НЕ коммить его в файлы других форматов; здесь он для преемственности сессий по просьбе владельца).
- **Вывод: не повторяй SSH-попытки больше 1 раза.** Сразу проси владельца запустить готовые скрипты `scripts/server_shell_batch.sh` и `scripts/server_audit_batch.py` на сервере (через cPanel Terminal или локальный ssh) и вставить вывод в чат. Скрипты готовы и покрывают ВСЕ 10 SSH-пунктов.

## 4. ЧТО СДЕЛАНО В ЭТОЙ СЕССИИ (07.07.2026, вечер)

### 4.1 SEO-006 / SEO-007 — медленный краулер РАБОТАЕТ, почти закончил ✅🔄
- **Написан новый комбинированный краулер `scripts/seo_combined_slow_crawl.py`** — ключевые свойства:
  - 1 запрос / 2.5 сек, retry с паузой 12с на 503/000 — **IP-бана НЕТ за всю сессию** (урок прошлой сессии учтён);
  - **resumable**: результаты пишутся построчно (JSONL) в `TWOCOMMS_A_TO_B/technical/data/seo_crawl_results.jsonl`; при перезапуске скрипт читает файл и продолжает с места остановки — просто запусти `python3 TWOCOMMS_A_TO_B/technical/scripts/seo_combined_slow_crawl.py` в фоне;
  - фазы: (1) sitemap collection (489 URL), (2) обход всех sitemap-URL со сбором status/title/description/canonical/robots/og/h1/внутренних ссылок, (3) проверка внутренних ссылок, которых нет в sitemap, (4) тесты 404 (несуществующие URL) и 410 для удалённых товаров. В конце пишет `{"type":"done"}`.
- **Анализатор `scripts/seo_crawl_analyze.py`** — читает JSONL, печатает готовый отчёт: не-200 статусы, битые ссылки, редиректы, дубли title/description, длины, отсутствие canonical/H1/OG, noindex. Запуск: `python3 TWOCOMMS_A_TO_B/technical/scripts/seo_crawl_analyze.py`.
- **Состояние на момент передачи: 431/489 страниц фазы 2 пройдено, ВСЕ 200 (снапшот в git).** Осталось ~58 URL фазы 2 + фазы 3–4. Если краулер умер вместе с песочницей — просто перезапусти той же командой, он продолжит с места остановки. После появления `{"type":"done"}` в JSONL: запусти `python3 TWOCOMMS_A_TO_B/technical/scripts/seo_crawl_analyze.py` и перенеси финальные цифры в SEO-006/SEO-007.
- **Промежуточные находки (уже в чек-листе, строки SEO-006/SEO-007):**
  - 0 битых ссылок, 0 редиректов, 0 страниц без title/description/canonical/H1/OG, 0 дублей title, 0 noindex — база очень чистая;
  - 1 интермиттентный 500 на `/blog/` (при повторе 200; паттерн Passenger-overload, коррелирует с SEO-010);
  - 7 title < 30 симв. (/ru/delivery/, /en/blog/ и др.), 6 title > 65 (/en/, /pro-brand/*), 12 description > 165 (/custom-print/, /wholesale/, /cooperation/ во всех локалях) — P3.
- **Что осталось для закрытия:** дождаться `done` → запустить анализатор → финальные цифры в `audit_report_section4_seo.md` (новый раздел в стиле SEO-010) → `- [x]` в чек-листе. Для 410-теста удалённых товаров краулер шлёт запросы на выдуманные slugs; если нужны реальные снятые товары — спроси владельца.

### 4.2 CB-041 — ЗАКРЫТ ✅ (без SSH!)
- Live-проба: запрос `/media/category_icons/*.png` с `Accept: image/webp` → ответ `content-type: image/png`, БЕЗ заголовка `X-Image-Cache` (middleware ставит его на каждый обработанный запрос) → **флаг `IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED` на бою выключен, middleware — no-op**.
- `cache-control: public, max-age=2592000` на media отдаёт `MediaCacheMiddleware` (production_settings.py:56–57), не image-middleware.
- Код-факты: ThreadPoolExecutor(2) создаётся в `__init__` даже при enabled=False (лишние треды, P3); `IMAGE_OPTIMIZATION_ALLOW_ON_DEMAND` default=False. Вердикт в чек-листе (строка ~306). SSH-остаток минимален: подтвердить env на сервере.

### 4.3 CB-015 — ЗАКРЫТ ✅ (code-часть, без SSH!)
- Полная карта **93 management-команд** с вердиктами: **`cb015_management_commands_map.md`** (в этой же папке).
- Итог: 4 RUNTIME (не трогать), 27 TESTED (в т.ч. `parser_recovery_dry_run` — покрыт тестом, гипотеза «мёртвый» опровергнута), ~16 CRON?-кандидатов, ~42 OPS.
- DEAD?-кандидаты: `finance_seed_demo`, `notify_test_shops`, `send_storage_test`, `send_test_receipt` + **теневой оверрайд `dtf/collectstatic`** (перекрывает стандартную django-команду — подозрительно, изучи `twocomms/dtf/management/commands/collectstatic.py`).
- SSH-остаток: `crontab -l` для вердикта по CRON?-группе.

### 4.4 TD-015 — code-часть ЗАВЕРШЕНА ✅ (SSH-остаток), найден P1-риск
- **ГЛАВНАЯ НАХОДКА (P1): прод работает БЕЗ Celery-воркера, но Redis-брокер ЖИВ (он же кэш).** Прямое доказательство в коде: комментарий `storefront/signals.py:110` — «Production runs without Celery, so attempting .delay() only adds a failed-RPC round-trip». Следствие: `.delay()` НЕ падает (успешно публикует в Redis-очередь `celery`), поэтому:
  - `orders/signals.py::_safe_queue_notification` — sync-fallback срабатывает ТОЛЬКО при исключении из `.delay()`, которого нет → **Telegram-уведомления о смене статуса заказа / добавлении ТТН молча теряются**;
  - `telegram_notifications.py:122` — default `async_enabled=True` → админ-сообщения без reply_markup тоже уходят в мёртвую очередь (строки 169–173);
  - `CELERY_BEAT_SCHEDULE` `survey-inactivity-check` (settings.py:961, каждые 120с) никогда не выполняется — beat не запущен;
  - очередь `celery` в Redis растёт бесконечно → расход памяти Redis.
- Кто уже защитился явно (`async_enabled=False`): `dtf/telegram.py:29`, `custom_print_notifications.py:59`, `reviews/signals.py:111`. Image-оптимизация переведена на `transaction.on_commit` inline (signals.py:107–138) — правильный паттерн.
- `CELERY_TASK_ALWAYS_EAGER` — ЗАКОММЕНТИРОВАН (settings.py:959), т.е. eager-режима нет.
- Синхронные долгие операции в запросах: AI-генерация в `views/admin.py` (~2059) — OpenAI-вызов синхронно, но admin-only; feed-генерация через dirty-флаг + cron (OK).
- **Рекомендуемый фикс** (согласуй с владельцем): `async_enabled=False` по умолчанию в `TelegramNotifier.__init__` — одно слово в одной строке; survey-check перевести на cron-команду.
- SSH-остаток: `redis-cli llen celery` (подтвердить накопление тасков), passenger-status/лимиты памяти. Также в `telegram_notifications.py` куча emoji-`print()` → мусор в Passenger-лог (пересекается с TD-016/CB-045).

## 5. ПЛАН ДЛЯ ТЕБЯ (по приоритету)

1. **Проверь краулер:** `pgrep -f seo_combined_slow_crawl` и `wc -l TWOCOMMS_A_TO_B/technical/data/seo_crawl_results.jsonl`. Если не бежит и в файле нет `"type": "done"` — перезапусти в фоне (resumable). Когда `done` → анализатор → закрой SEO-006 + SEO-007 (отчёт в `audit_report_section4_seo.md`, отметки в чек-листе, коммит + main).
2. **Сразу скажи владельцу про SSH:** «SSH из песочницы недоступен (порт фильтруется), запустите на сервере `scripts/server_shell_batch.sh` и `python manage.py shell < scripts/server_audit_batch.py`, пришлите вывод». Это закроет разом 10 пунктов. Пока ждёшь ответа — работай по п.3.
3. **Code-часть TD-015 уже сделана (§4.4, P1-риск с мёртвой Celery-очередью задокументирован в чек-листе).** Предложи владельцу фикс `async_enabled=False`. Периодически коммить снапшот `data/seo_crawl_results.jsonl`.
4. Когда владелец пришлёт вывод скриптов → распиши результаты по секционным отчётам → закрой CRO-051, TD-015/016, AEO-001, DB-001/003/004/006/007, CB-045 + SSH-остатки CB-015/CB-041.
5. После каждого шага: **коммит в свою ветку + пуш в main** (см. §6).

## 6. ВАЖНЫЕ ГРАБЛИ И СОГЛАШЕНИЯ

- **Пуш в main из песочницы:** `git push origin HEAD:main` из текущей ветки может отклоняться из-за расхождений; рабочий приём:
  ```
  git fetch origin main
  git worktree add /tmp/main-merge FETCH_HEAD
  cd /tmp/main-merge && git merge <твоя-ветка> --no-edit && git push origin HEAD:main
  cd - && git worktree remove /tmp/main-merge --force
  ```
  Владелец ЯВНО разрешил и просит обновлять main после каждого шага. Также он периодически мержит PR сам — перед работой всегда `git fetch origin main && git merge FETCH_HEAD`.
- **`git checkout -B x origin/main` может падать** («not a valid object name») — используй `FETCH_HEAD` после `git fetch origin main`.
- **Rate limit сайта:** 1 запрос / 2.5с, никаких параллельных краулов. Бан = код 000 на всё. В этой сессии бана НЕ было.
- **Интермиттентные 500/503 — НЕ битые ссылки**, это перегрузка Passenger (задокументировано в SEO-010). Перепроверяй повтором через 5–10 сек.
- **Чек-лист — единственный источник правды.** Формат: `- [x] **ID.** ✅ Аудит ДАТА: вывод... (оригинальный текст пункта сохраняй)`. Для «в работе» используй `🔄 В РАБОТЕ ДАТА: ...` не снимая `[ ]`.
- Отчёты **на русском**, стиль — `audit_report_section4_seo.md` (раздел SEO-010 — образец).
- Коммиты: добавляй trailer `Co-authored-by: v0 <it+v0agent@vercel.com>`.
- Песочница: Amazon Linux, python3 есть, `sshpass` через `sudo dnf install -y sshpass` (уже ставился), `agent-browser` доступен. Фоновые процессы переживают между сообщениями, но НЕ переживают пересоздание песочницы — поэтому краулер сделан resumable, а данные в git.

## 7. КЛЮЧЕВЫЕ ФАЙЛЫ (быстрая карта)

```
TWOCOMMS_A_TO_B/technical/
├── twocomms_global_audit.md            ← ГЛАВНЫЙ ЧЕК-ЛИСТ (12 пунктов осталось)
├── audit_report_section4_seo.md        ← отчёт SEO (образец стиля — SEO-010)
├── audit_report_section*.md            ← остальные секционные отчёты
├── cb015_management_commands_map.md    ← карта 93 команд (новое, эта сессия)
├── SESSION_HANDOFF_2026-07-07.md       ← контекст ранних сессий
├── NEXT_AGENT_PROMPT.md                ← этот файл
├── data/
│   └── seo_crawl_results.jsonl         ← JSONL краула (resumable-стейт, в git!)
└── scripts/
    ├── seo_combined_slow_crawl.py      ← ГЛАВНЫЙ краулер SEO-006/007 (resumable)
    ├── seo_crawl_analyze.py            ← анализатор JSONL → готовый отчёт
    ├── server_audit_batch.py           ← Django shell батч для сервера (10 SSH-пунктов)
    ├── server_shell_batch.sh           ← bash батч для сервера
    ├── seo006_link_crawl.py            ← старый краулер (не использовать, без rate-limit)
    └── seo007_meta_audit.py            ← старый аудит мета (не использовать)

twocomms/                               ← Django-прое��т
├── passenger_wsgi.py
├── twocomms/production_settings.py     ← настройки прода (MediaCacheMiddleware:56)
├── twocomms/settings.py                ← IMAGE_OPTIMIZATION флаги: 849–850
├── twocomms/image_middleware.py        ← CB-041 (закрыт)
└── storefront/models.py                ← UserAction/UTMSession (~строки 1670–2130)
```

Удачи. Первым делом: проверь краулер (§5 п.1) и сообщи владельцу про SSH (§5 п.2).
