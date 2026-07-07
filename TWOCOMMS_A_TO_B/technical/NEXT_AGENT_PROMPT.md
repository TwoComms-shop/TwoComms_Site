# ПРОМПТ ДЛЯ СЛЕДУЮЩЕГО АГЕНТА — продолжение глобального аудита TwoComms

**Обновлено:** 07.07.2026. Предыдущий агент закрыл CB-020, CB-022, CB-042, CB-030, CB-031, CB-041 (все — статический аудит кода). Этот файл — полная передача контекста. Прочитай его целиком ПЕРЕД любой работой — он сэкономит тебе тысячи токенов разведки.

---

## 1. ЧТО ТЫ ДЕЛАЕШЬ (роль)

Ты — агент-аудитор. Ты идёшь по чеклисту `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md` (439 строк, ~150 пунктов), выполняешь НЕ-выполненные пункты `- [ ]` и для каждого:

1. Проводишь аудит (код-анализ / SSH-замер на бою / live-краулинг сайта).
2. Пишешь подробную секцию с находками в соответствующий `audit_report_sectionN_*.md` (см. список ниже) — формат смотри по уже существующим секциям (заголовок `## ID. Название (АУДИТ ВЫПОЛНЕН, дата)`, таблицы находок с приоритетами P0–P3, блок «Выводы для исполнителя»).
3. Добавляешь строку в таблицу «Журнал раздела» в конце того же отчёта.
4. В чеклисте меняешь `- [ ]` на `- [x]`, добавляя `✅ Аудит <дата>: <конденсированные находки> → <имя отчёта>` ПЕРЕД исходным текстом пункта (исходный текст «Где/Что» сохраняешь — так сделаны все 100+ уже закрытых пунктов, смотри примеры в файле).
5. Коммитишь с сообщением вида `audit: close <ID> (<суть>)` + trailer `Co-authored-by: v0 <it+v0agent@vercel.com>`, пушишь в текущую ветку чата.

**ВАЖНО: ты ТОЛЬКО аудируешь. НИЧЕГО не чинишь, не рефакторишь, не удаляешь в коде** (правило №6 чеклиста + анти-задачи TECHNICAL_TASKS.md). Все фиксы — задачи для будущих исполнителей, ты их только формулируешь.

---

## 2. СТРУКТУРА ФАЙЛОВ (где что лежит)

Рабочая директория: `/vercel/share/v0-project` (корень репо zainllw0w/TwoComms_Site).

- **Django-проект:** `twocomms/` (внутри: `twocomms/settings.py`, `storefront/`, `orders/`, `management/`, `accounts/`, `finance/`, `reviews/`, `productcolors/`, `dtf/` и т.д.)
- **Чеклист:** `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- **Отчёты** (в той же папке `TWOCOMMS_A_TO_B/technical/`):
  - `audit_report_section1_homepage.md`, `..._catalog.md`, `..._product.md`, `..._cart.md`, `..._checkout.md`, `..._texts.md`, `..._images.md` — раздел 1 (CRO)
  - `audit_report_section2_seo.md` — SEO/AEO
  - `audit_report_section3_analytics.md` — аналитика (AN-*)
  - `audit_report_section4_db.md` — БД (DB-*)
  - `audit_report_section5_techdebt.md` — техдолг (TD-*)
  - `audit_report_section6_codebase.md` — кодовая база (CB-*) ← сюда писал предыдущий агент
  - `audit_report_section7_risks.md` — риски (RISK-*)
- **Handoff прошлой сессии:** `SESSION_HANDOFF_2026-07-07.md` (та же папка) — там реквизиты SSH/MySQL и накопленные SSH-остатки. НЕ копируй секреты в новые файлы!

---

## 3. ОСТАВШИЕСЯ НЕВЫПОЛНЕННЫЕ ПУНКТЫ (актуально на момент передачи)

Проверь актуальность: `grep -n '^- \[ \]' TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`

### Группа А — выполнимы БЕЗ SSH (делай первыми):

- **CB-015** (строка ~281): мёртвые management-команды. Метод: `ls twocomms/*/management/commands/`, затем grep каждой команды по репо (cron-доки: `TWOCOMMS_A_TO_B/technical/audit_report_section5_techdebt.md` секция CB-044 уже содержит расшифровку боевого crontab — НЕ ходи по SSH заново, там 20 cron-задач перечислены). Таблица «команда → где вызывается → вердикт». Сверка с crontab częściowo уже есть.
- **SEO-007** (~224): мета-титлы/дескрипшены. Код: `twocomms/storefront/seo_utils.py` (осторожно: 24 широких except), `services/product_seo_autofill.py`. Данные из БД можно снять live-краулингом (curl по sitemap-URL, парс `<title>`/`<meta name=description>`) — SSH не обязателен. Sitemap: https://twocomms.shop/sitemap.xml
- **SEO-008** (~225): merchant feed. Файл `twocomms/google_merchant_feed.xml` в репо (статический!), код `storefront/feeds.py`, `services/marketplace_feeds.py`. Сверь даты git-коммита фида vs автогенерация; жив ли URL https://twocomms.shop/google-merchant-feed.xml (curl).
- **SEO-009** (~226): IndexNow. Код: `twocomms/storefront/services/indexnow.py`, `services/google_indexing.py`. Найди, откуда дергаются (сигналы? save-хуки?), есть ли анти-спам-дедуп.
- **SEO-021** (~232): Review schema. Быстрый: по CRO-026 уже известно — отзывов 0, разметка выводится только из approved (порог=1). Осталось зафиксировать «выполнено при появлении отзывов» + проверить корректность шаблона разметки в `product_detail.html`.
- **SEO-006** (~223, частично): битые внутренние ссылки — live-краулинг по sitemap (curl батчами, следи за rate-limit), 410 для удалённых товаров — код `storefront/views/product.py` (что отдаёт unpublished/archived). GSC-часть — «владельцу», так и пиши.
- **SEO-010** (~227): CWV каталог+PDP. Используй agent-browser CLI (`agent-browser vitals <url> --json`) на https://twocomms.shop/catalog/ и любой PDP. Базовая линия home уже в CRO-003 (LCP 1.78s).
- **TD-015** (~193, код-часть): `twocomms/passenger_wsgi.py` — читай код; найди синхронные тяжёлые операции в запросах (AI-генерация: grep `openai` в views; feed-генерация). Лимиты хостинга — SSH-остаток.

### Группа Б — ТРЕБУЮТ SSH (сервер сбрасывает частые коннекты! батч всё в 1 сессию):

- **CRO-051** (~126): baseline воронки из UserAction (read-only ORM-запросы).
- **TD-016 + CB-045** (~194, ~310): логи `~/TWC/TwoComms_Site/twocomms/*.log` — размеры, ротация, секреты. Это ОДИН SSH-заход на оба пункта.
- **DB-001/003/004/006/007** (~249–255): slow log, целостность Order↔UTMSession, SHOW TABLE STATUS UserAction, charset, showmigrations.
- **AEO-001** (~238): landing_page сессий utm_source=chatgpt.com (ORM-запрос).
- **SSH-остатки прошлых аудитов** (собери из журналов отчётов): env-флаги `IMAGE_OPTIMIZATION_*` + размер `media/optimized_cache/` (CB-041), `DISABLE_ANALYTICS` на бою (CB-042/CB-012), untracked-скрипты на сервере со ссылками на CSS-сирот (CB-031 п.1), кол-во процессов Passenger (TD-015).

**SSH-реквизиты — в `SESSION_HANDOFF_2026-07-07.md`.** Подключение: `sshpass -p '<пароль из handoff>' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169`. Django shell на бою: `source ~/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && cd ~/TWC/TwoComms_Site/twocomms && python manage.py shell`. **ВАЖНО: сервер режет частые коннекты (kex_exchange_identification: reset) — готовь ОДИН большой heredoc-скрипт на сессию, а не 10 мелких ssh-вызовов. sshpass может отсутствовать в VM — ставь `sudo dnf install -y sshpass`. В прошлой сессии SSH временами вообще не проходил — если 2 попытки подряд падают, откладывай группу Б и делай группу А.**

---

## 4. КЛЮЧЕВЫЕ ЗНАНИЯ О КОДОВОЙ БАЗЕ (сэкономят тебе разведку)

- **Боевой CSS = `styles.purged.css` (394KB), НЕ styles.css.** Подключения в `twocomms/twocomms_django_theme/templates/base.html:825–841`. Минификация django-compressor ВКЛЮЧЕНА (settings.py:1049–1055, manifest-страж :1024).
- **MIDDLEWARE:** 26 слоёв в `twocomms/twocomms/settings.py:199` + 27-й MediaCache в `production_settings.py:56`. production_settings наследует settings и правит поверх.
- **`storefront/views/__init__.py:329`** грузит `views.py.backup` (7790 строк) через `_load_legacy_views()` — это ЖИВОЙ рантайм, не мёртвый файл.
- **Celery удалён**, работают no-op шимы. Крон-задачи — обычный crontab (уже расшифрован в CB-044, section5).
- **Платёжные потоки:** COD-чекаут `storefront/views/checkout.py`, Monobank `storefront/views/monobank.py` (1400+ строк, create_invoice + webhook), общие хелперы webhook в `storefront/views/utils.py`. Известный P1-паттерн: `save(update_fields) except → save()` (см. CB-020 в section6).
- **Трекинг:** UserAction/UTMSession в `storefront/models.py`; серверные события `record_user_action` БЕЗ бот-фильтра (CRO-024); server-side CAPI для add_to_cart отсутствует (CRO-033).
- **Известные глобальные факты** — таблицы в начале чеклиста (строки 21–68): 68 товаров, 41 заказ, 0 заказов с UTM, 697→734 широких except, репо 328MB и т.д. Сверяйся с ними, не перемеряй без нужды.
- **Grep/Glob-инструменты v0 не видят node_modules**, а Bash не имеет доступа к `user_read_only_context/` — для этих путей используй Read.

## 5. ПРОЦЕДУРНЫЕ НЮАНСЫ (грабли, на которые уже наступали)

1. **Edit требует свежего Read файла** — перед каждым Edit большого файла читай нужный диапазон (offset/limit), иначе получишь ошибку «has not been read yet».
2. **Чеклист и отчёты — большие файлы**: читай их Grep-ом по ID пункта + Read с offset, НЕ целиком (экономия токенов).
3. **Вывод Bash-команд в этом окружении часто «омитится»** — если результат нужен в контексте, пиши его в файл и читай Read-ом, либо делай `| head -N` для компактности.
4. **Коммиты**: файлы автокоммитятся системой в ветку чата; git push может требовать подтверждения пользователя. Пуш — только в ветку чата (`v0/...`), НЕ в main напрямую.
5. **Секреты**: НИКОГДА не вставляй пароль SSH/MySQL в отчёты, коммиты, этот файл. Он есть только в SESSION_HANDOFF.
6. **Числа за пределами кода не выдумывай** — если пункт требует live-данных, а доступа нет, честно фиксируй «SSH-остаток» в отчёте и в пункте чеклиста (так уже сделано в CB-041/CB-042).
7. **Формат галочки в чеклисте**: `- [x] **ID. Название.** ✅ Аудит <дата>: <находки с приоритетами, самое важное жирным> → <файл отчёта>` затем исходный текст «Где:... Что:...» без изменений.
8. **Приоритеты находок:** P0 = ломает деньги/доступность прямо сейчас; P1 = теряет деньги/данные в реальных сценариях; P2 = риск/деградация; P3 = гигиена. Каждой находке — приоритет.

## 6. ПОРЯДОК РАБОТЫ (рекомендация)

1. `grep -n '^- \[ \]'` по чеклисту — актуальный список.
2. Сделай группу А (без SSH) по одному пункту за раз: аудит → секция в отчёт → журнал → галочка → коммит.
3. Попробуй SSH (1 тестовый коннект). Работает → батч группы Б одним-двумя заходами. Не работает → зафиксируй в этом файле и оставь группу Б следующему.
4. Когда токены на исходе — ОБНОВИ ЭТОТ ФАЙЛ: вычеркни сделанное, допиши новые нюансы/грабли, закоммить.

## 7. СТАТУС SSH НА МОМЕНТ ПЕРЕДАЧИ

07.07.2026: sshpass в VM отсутствовал, установка через dnf прошла; тестовый коннект к 195.191.24.169 в этой сессии стабильного результата не дал (вывод команд омитился, повторные попытки не делались ради экономии токенов). Считай состояние SSH неизвестным — начни с одного тестового `echo SSH_OK`.
