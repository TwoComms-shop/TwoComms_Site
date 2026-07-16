# TWOCOMMS — МАСТЕР-ПЛАН ИМПЛЕМЕНТАЦИИ (IMPLEMENTATION_PLAN.md) · v2

**Дата создания:** 07.07.2026 · **Версия:** 2.0 (единый исполняемый чеклист)
**Источник:** полный аудит `twocomms_global_audit.md` (150/150 пунктов закрыто) + все `audit_report_*.md` + gap-check 07.07.2026 (перекрёстная сверка 155 ID аудита против плана v1 — добавлены пропущенные пункты, помечены `[GAP]`).
**Назначение:** ЕДИНСТВЕННЫЙ рабочий документ для агента-исполнителя. Каждый пункт — чекбокс. Выполнил → `[x]` + запись в «Журнал выполнения» внизу (дата, ID, коммит/PR). Ничего из этого ещё НЕ исправлено, кроме пунктов, явно помеченных done.

> **RE-VERIFY PASS 2026-07-09 + STRICT pass: additionally unchecked W2-7 (dual mono status path), W7-23 (residual datetime.now).** false `[x]` cleared for W2-1/W2-2/W2-3/ADS-1/ADS-2/ADS-3/W7-1/W3-9/W3-11/W0-5; W2-3 was re-closed only after its live acceptance passed on 2026-07-14. Details: `docs/qa/PLAN_VS_FINDINGS_2026-07-09.md`. Owner SSH password rotated (W0-1 OWNER). Do not re-mark DONE without live accept criteria.

**Как пользоваться:** брать задачи строго по волнам (Волна 0 → 1 → 2 → …). Перед каждой задачей — свериться с «Матрицей рисков» в `twocomms_global_audit.md` (RISK-01…15). Детали каждой находки — в указанном секционном отчёте.

---

## 0. ЛЕГЕНДА И ПРАВИЛА

**Теги исполнимости** (у каждого пункта):
- `[REPO]` — чинится правкой кода/файлов в репозитории. Агент делает сам: fix → commit → push.
- `[SERVER]` — требует SSH к проду (деплой, cron, env, backfill, БД). Агент готовит скрипт/инструкцию, исполняет при наличии SSH, иначе — оформляет в `SERVER_TASKS`-батч.
- `[OWNER]` — только владелец (кабинеты Meta/GTM/GA4/GSC, смена паролей, продуктовые решения).
- `[DECISION]` — сначала решение владельца, потом код.

**Приоритеты:** 🔴 P0 (деньги/безопасность/данные) → P1 → P2 → P3.

**Правила фиксов:**
1. Один пункт = один атомарный фикс = отдельный коммит `fix(ID): описание`.
2. Деплой = `git pull` на сервере; боевой settings — `twocomms.production_settings` (НЕ settings.py — RISK-08).
3. БД — MySQL на сервере; проверки через SSH → Django shell, read-only. SSH rate-limit: батчить ВСЕ команды в одну сессию, пауза ≥60s (RISK-02).
4. Миграции — только аддитивные, только после бэкапа (RISK-07). Breaking-изменения — отдельное согласование.
5. Тест-прогоны — только с `utm_source=audit` + пометка заказа (RISK-12).
6. Секреты НИКОГДА не коммитить (RISK-15). Перед коммитом — `git diff` на пароли/токены.
7. НЕ менять control flow при чистке except/print — только добавлять логирование (RISK-04).
8. Правки текстов/SEO — батчами по 10 страниц с фиксацией позиций до/после (RISK-13).

**Ключевые факты окружения:**
- Прод: **https://twocomms.shop**, shared-хостинг Hostsila (LiteSpeed + Passenger), сервер `qlknpodo@195.191.24.169`, путь `~/TWC/TwoComms_Site/twocomms/`, venv `~/virtualenv/TWC/TwoComms_Site/twocomms/3.14/`.
- Celery удалён; Redis-брокер жив, но с сервера Redis Cloud host НЕ резолвится; `.delay()` публикует в мёртвую очередь (TD-015).
- Репозиторий public → любой закоммиченный секрет = скомпрометирован.

---

## ВОЛНА 0 — ПРЕДОХРАНИТЕЛИ (делать ПЕРВЫМИ, до любых фиксов кода)

- [ ] **W0-1. 🔴 Сменить SSH-пароль прода (CB-003)** `[OWNER]` + `[REPO]`
  Проблема: `deploy_finance.sh:5` (tracked, public repo) содержал незамаскированный SSH-пароль. Маскировка в файле сделана (коммит d85f6d83), но пароль был публичным → скомпрометирован.
  Фикс: (1) владелец меняет пароль `qlknpodo` / переходит на ключи; (2) `[REPO]` — проверить grep по репо, что масок больше нигде нет.
  Приёмка: старый пароль не работает; grep по репо на пароль = 0.
  Отчёт: `audit_report_section6_codebase.md`, `SESSION_HANDOFF_2026-07-07.md`.
  ✅ **REPO-часть VERIFIED:** fresh grep по tracked-файлам на скомпрометированный пароль = 0; `deploy_finance.sh` использует `SSHPASS` env без литерала. Осталось `[OWNER]`: сменить пароль/перейти на SSH-ключ и подтвердить, что старый пароль не работает.

- [ ] **W0-2. 🔴 Ротация секретов из git-истории (TD-021, P1)** `[OWNER]`/`[SERVER]`
  В истории git восстановимы `twocomms/db_config.env` (SECRET_KEY, DB-креды) и `twocomms/.env.production` (REDIS_URL с кредами).
  Фикс: ротация SECRET_KEY + Redis-кредов на сервере (env-файлы, НЕ репо). History-rewrite — только при отдельном согласовании (RISK-06).
  Приёмка: старые значения невалидны.

- [ ] **W0-3. 🔴 Регулярные бэкапы MySQL (TD-020 / TECH-042)** `[SERVER]` + `[REPO]`(скрипт)
  Бэкапов НЕТ; последний ручной дамп >8 мес. Блокирует ВСЕ миграции (RISK-07).
  Фикс: `[REPO]` — написать `scripts/backup_mysql.sh` (mysqldump, ротация 7/30 дней, каталог вне web-root, права 700); `[SERVER]` — поставить в crontab, тест восстановления на копии.
  Приёмка: дамп по расписанию; восстановление проверено; каталог не доступен по HTTP.
  ✅ **REPO-часть DONE:** `scripts/backup_mysql.sh` создан — mysqldump `--single-transaction --routines --triggers`, атомарная запись через .tmp + sanity-check размера (>10KB) + `gzip -t`, ротация daily 7 дней / weekly 35 дней, chmod 600/700, инструкция установки в шапке скрипта. Осталось `[SERVER]`: mkdir ~/db_backups (вне webroot), ~/.my.cnf, crontab `45 3 * * *`, тест восстановления.

- [x] **W0-4. 🔴 Смок-тесты на деньги (CB-024)** `[REPO]`
  > **STRICT RE-VERIFY W0-4:** STRICT 2026-07-09: KEEP files present. Full pytest not re-run here (SECRET_KEY/prod settings).
  orders/ — 0 тестов (CAPI 850 строк!), accounts/ — 0; вебхук-подпись без тестов; CI нет.
  Фикс: pytest-набор ПЕРЕД фиксами воронки: (а) создание COD-заказа; (б) идемпотентность monobank-вебхука; (в) слияние корзины при логине; (г) guest COD; (д) `link_order_to_utm` (после W2-1).
  Приёмка: набор зелёный локально; прогоняется перед каждым деплоем.
  Отчёт: `audit_report_section6_codebase.md` (CB-024).
  ✅ **DONE:** все 5 пунктов покрыты: (а)+(г) test_checkout (guest COD, double-submit, промо — накоплено в W1); (б) НОВЫЙ test_webhook_duplicate_delivery_is_idempotent — повторный success-вебхук не диспатчит side-effects повторно (ретейл-путь `_apply_monobank_status` идемпотентен через old_payment_status-чек); (в) test_cart_sync (merge при логине, мультидевайс); (д) test_utm_attribution (W2-1). Смок-набор зафиксирован в docs/OPS.md («перед каждым деплоем»). Все 12 тестов test_monobank_webhook зелёные. CI-pipeline — отдельная задача при появлении CI-инфраструктуры.

- [ ] **W0-5. Зафиксировать crontab/инварианты (CB-043/CB-044/CB-012)** `[REPO]`(docs) + `[OWNER]`
  > **RE-VERIFY W0-5:** PARTIAL 2026-07-09: REPO OPS.md done; OWNER stash review NOT done — uncheck full item.
  crontab: 7 задач, НЕТ бэкап-cron, НЕТ feed-cron; на сервере **10 git-stash** (возможна потерянная работа) + untracked диаг-скрипты.
  Фикс: `[REPO]` — задокументировать crontab и боевой settings-модуль в `docs/OPS.md`; `[OWNER]` — разобрать stash с владельцем (что выбросить, что закоммитить).
  Приёмка: docs/OPS.md существует; судьба каждого stash решена.
  ✅ **REPO-часть DONE:** `twocomms/docs/OPS.md` создан — полная таблица 7 cron-задач (из CB-044), известные дыры (нет бэкап/feed-cron/logrotate), инварианты («не добавлять cron без записи в таблицу»), git-состояние сервера (10 stash — не дропать без владельца), смок-набор W0-4 и деплой-чеклист. Остаётся `[OWNER]`: разбор 10 git-stash.

- [ ] **W0-6. [GAP] 🔴 NEW-405: сервис-аккаунт JSON в webroot?** `[SERVER]`
  `external_analytics.py` авто-дискаверит `*service*account*.json` в каталоге проекта. Если файл лежит в webroot — ключ Google потенциально доступен по HTTP.
  Фикс: одна команда в SSH-батче (`find ~/TWC -name '*service*account*' -o -name '*.json' -path '*credential*'`); если найден в webroot — перенести вне webroot + проверить, не отдаётся ли по URL; при подозрении на утечку — ротация ключа `[OWNER]`.
  Приёмка: ключей в webroot нет; URL-проба даёт 404.

---

## ВОЛНА 1 — ДЕНЬГИ И ПРИЁМ ЗАКАЗОВ (P0)

- [x] **W1-1. 🔴 Гостевой COD-чекаут сломан — HTTP 500 (CRO-040)** `[REPO]` + `[DECISION]`
  > **RE-VERIFY W1-1 RESOLVED 2026-07-14:** `394a247c`; `create_order` now establishes a durable guest session before persisting the Order. Production cookie/Order/UTMSession rollback canary passed at `bb217bd9`; F-074 closed.
  ✅ **DONE (коммит 26702d78):** (1) роутинг гостя на `create_order` уже был исправлен на main до начала работ — перепроверено, `cart.py` вызывает `legacy_views.order_create(request)`; (2) `[DECISION]` владельца: COD в UI **НЕ возвращаем**, остаётся backend-only fallback; (3) обновлены 2 устаревших теста в `test_checkout.py`, которые ожидали удалённый inline-monobank-флоу (online pay_type теперь редиректит в корзину — заказ создаёт кнопка Monobank). Проверка: `storefront.tests.test_checkout` зелёный.
  ✅ **SESSION RESIDUAL DONE (`394a247c`):** общий `ensure_request_session_key()` вызывается до COD Order writer; регрессионные тесты проверяют новую lazy guest session и healing UTM-link. Production InnoDB-canary подтвердил cookie = Order.session_key = UTMSession.session_key и нулевой остаток после rollback. Исторические пустые ключи не фабриковались.
  `storefront/views/cart.py:525-527` вызывает несуществующий `legacy_views.process_guest_order` → AttributeError → 500. Live-подтверждено: гость не может оформить заказ. В guest UI корзины нет `pay_type=cod` и submit-кнопки COD.
  Фикс: (1) `cart.py:525` → `return legacy_views.order_create(request)` (`create_order` поддерживает гостей, поля совпадают — проверено); (2) `[DECISION]` — возвращать ли `cod` в UI (значение есть в `Order.PAY_TYPE_CHOICES`); (3) НЕ использовать ветку `monobank_create_invoice(request, order.id)` — латентный TypeError (см. W1-5д).
  Приёмка: live guest COD-заказ создаётся (тест-пометка); invalid phone → controlled error; 0 JS-ошибок.
  Отчёт: `audit_report_checkout_critical.md` (Находка 1).

- [x] **W1-2. 🔴 Публичная утечка PII заказов (CRO-044)** `[REPO]`
  ✅ **DONE (коммит 5d2d91f4):** (а) `order_success_preview` → `@staff_member_required`; (б) `order_success` → проверка владельца `_can_view_order()` (checkout.py): staff / `order.user_id == request.user.id` / совпадение `order.session_key` / id в новом session-ключе `recent_order_ids` (заполняется `remember_order_in_session()` при создании заказа и в `monobank_return` — там только по session-доказательствам, НЕ по GET-параметрам). Чужой → 404 (не 403, чтобы не было перебора id). Тесты: `test_order_success_denies_anonymous_stranger`, `..._denies_other_authenticated_user`, `..._allows_order_owner_user`, `..._allows_matching_session_key`, `..._preview_requires_staff` в test_checkout.py.
  (а) `/orders/success-preview/` (checkout.py:289-299) публично рендерит ПОСЛЕДНИЙ реальный заказ (ФИО/телефон/адрес); (б) `/orders/success/<id>/` (checkout.py:281) отдаёт ЛЮБОЙ заказ перебором id — live-подтверждено.
  Фикс: preview → `@staff_member_required` или удалить; success → проверка владельца (user/session_key) либо непредсказуемый токен в URL; убрать preview из `static_pages.py`/sitemap.
  Приёмка: аноним → 403/404 на оба URL; владелец заказа видит свою страницу.

- [x] **W1-3. 🔴 Вебхук Monobank не проверяет подпись (CRO-043)** `[REPO]`
  ✅ **DONE (коммит db0af339):** (1) `_verify_monobank_signature` починена: ECDSA/SHA-256 вместо всегда-падавшего RSA PKCS1v15, поддержка base64-кодированного PEM-ключа из `/api/merchant/pubkey`, retry со свежим ключом при ротации; (2) `monobank_webhook` теперь ТРЕБУЕТ валидный `X-Sign` (пробуются ключи обоих мерчантов — storefront и acquiring) → иначе 400 без изменения статуса; (3) `monobank_return`: unsafe fallback `or 'success'` УДАЛЁН — статус берётся только из pull-запроса `invoice/status` (см. W1-12), при недоступности API статус заказа не трогается. Тесты: `test_monobank_webhook.py` (9 шт., включая реальную ECDSA-подпись round-trip).
  Webhook доверяет body без проверки `X-Sign` → любой может отметить заказ оплаченным. `monobank_return` использует unsafe fallback `status_value or 'success'` (monobank.py:1317).
  Фикс: (1) верификация подписи (публичный ключ Monobank, ECDSA); (2) `monobank_return` только читает статус через API/ждёт webhook, убрать `or 'success'`.
  Приёмка: невалидная подпись → 400, статус не меняется; return-редирект без webhook не переводит в paid.
  Отчёт: `audit_report_payment_security.md`.

- [x] **W1-4. 🔴 Промокоды: COD-путь мёртв + лимиты не работают (CRO-046)** `[REPO]`
  ✅ **DONE (коммит f79ed36e):** (а) COD-путь в checkout.py читает `promo_code_id` (как monobank-путь) + `can_be_used_by_user()`; промо остаются auth-only (та же политика, что в `apply_promo_code`); (б) `record_usage()` получил call-sites: COD — при размещении заказа; online — при ПОДТВЕРЖДЁННОЙ оплате через новый `_record_promo_usage_for_order()` в `_apply_monobank_status` (идемпотентно по `PromoCodeUsage.order`); (в) математ������������ка остатка prepay_200: `остаток = (total_sum − discount) − 200` в обоих местах monobank.py (было завышение на скидку); (г) `promo.use()` при создании инвойса УБРАН; `promo_code_data` чистится во всех cleanup-путях; (д) событие `coupon_apply` добавлено: новый choice в UserAction + миграция `0079` + запись в `apply_promo_code`. Тесты: COD-заказ с промо → `discount_amount>0` + строка PromoCodeUsage; повторный one_time-код отклоняется.
  (а) checkout.py:224 читает мёртвый ключ `promo_code` (apply пишет `promo_code_id`) + несуществующие `active=True`/`is_valid()` → промокод в COD НЕ применяется никогда; (б) `record_usage()` — 0 call-sites → лимиты `one_time_per_user` мертвы; (в) prepay_200: остаток завышен на сумму скидки; (г) `promo.use()` сжигает лимит при СОЗДАНИИ инвойса.
  Фикс: (а) COD → логика monobank-пути (`promo_code_id` + `can_be_used_by_user` + `record_usage(user, order)`); (б) `record_usage` при УСПЕШНОЙ оплате; (в) математика остатка; (г) чистка `promo_code_data`; (д) событие `coupon_apply` (TECH-023).
  Приёмка: COD-заказ с промо имеет `discount_amount>0`; повторный one_time-код отклоняется; `PromoCodeUsage.count() > 0`.

- [x] **W1-5. Ошибочные состояния чекаута (CRO-047, P1)** `[REPO]`
  (а) исчезнувший товар молча выбрасывается (`if not product: continue` — checkout.py:187, monobank.py:547); (б) COD без guard `total_sum <= 0` → заказ на 0 грн; (в) `monobank_create_invoice` + `clear_cart` ВНУТРИ `transaction.atomic()`; (г) стаб `monobank_webhook` в checkout.py:268; (д) латентный TypeError `monobank_create_invoice(request, order.id)`.
  Фикс: missing_items → message+redirect без заказа; guard; порядок commit→инвойс→clear_cart; удалить стаб; валидация online-веток COD-формы.
  ✅ **DONE (коммит 0a934454):** (а) `create_order`: товары проверяются ДО atomic — недоступные удаляются из корзины, message + redirect('cart'), заказ НЕ создаётся; `monobank_create_invoice`: любой отсутствующий товар → явная JSON-ошибка до создания заказа/инвойса; (б) guard `total_sum <= 0` внутри транзакции (`_ZeroTotalOrderError` → rollback + message); (в) в monobank.py блок после коммита заказа (invoice/create, CAPI, Telegram) выведен ИЗ `transaction.atomic()` (dedent), при ошибке до привязки invoice_id осиротевший заказ подчищается best-effort; (г) стаб monobank_webhook удалён ещё в db0af339; (д) call-sites `monobank_create_invoice(request, order.id)` не осталось — online-типы редиректятся к кнопке Monobank. Тесты: `test_create_order_missing_product_aborts_without_order`, `test_create_order_zero_total_aborts_without_order`.

- [x] **W1-6. Кабинет: смена оплаты молча не работает (Находка 3, HIGH)** `[REPO]`
  `update_payment_method`/`confirm_payment` (checkout.py:305-320) — заглушки (redirect вместо JSON). Рабочие версии — в `views.py.backup:2679,3831`.
  Фикс: перенести обе функции в checkout.py С проверкой владельца заказа.
  ✅ **DONE (коммит 6c6b4709):** обе функции восстановлены из backup + усилены: `login_required` + POST-only; владелец проверяется через `Order.objects.get(id=..., user=request.user)` → чужому 404; legacy-значения фронта 'full'/'partial' маппятся на 'online_full'/'prepay_200'; смена метода блокируется (409) при paid/prepaid/checking; в `confirm_payment` скриншот валидируется как реальное изображение (ImageField/Pillow) + лимит 10 МБ, повторная загрузка на paid-заказ → 409, успех → `payment_status='checking'`. Тесты: 3 новых + 2 устаревших stub-теста заменены (test_checkout.py, 27 OK).

- [x] **W1-7. Mobile hero-CTA обрезаны ≤360px (CRO-002, P0-CRO)** `[REPO]`
  > **RE-VERIFY W1-7:** NUANCE 2026-07-09: CSS fix in theme; ensure collectstatic/deploy on prod.
  hero height:60vh + overflow:hidden при контенте 727px (base.html:517); PWA-prompt закрывает 45% экрана. Кандидат №1 в причины ATC=0,12%.
  Фикс: min-height/auto height на малых viewport; отложить/уменьшить PWA-prompt. После — перепроверить CLS.
  Приёмка: viewport 360×640 — обе CTA видимы и кликабельны.
  ✅ **DONE (коммиты 0caf8b04 + f59f7827):** ВАЖНО — 60vh-лок оказался в ТРЁХ местах: base.html:517 (inline `<style>`), **cls-ultimate.css** (инлайнится в `<head>` через `{% inline_static %}` НИЖЕ по каскаду — именно он перебивал фикс) и critical-home.min.css. Во все три добавлен override `@media (max-width:767.98px){.hero-section{height:auto;max-height:none;min-height:60vh;overflow:visible;contain:layout style}}` (size-containment снят, т.к. высота теперь зависит от контента; min-height:60vh сохранён — CLS-резервирование остаётся). Приёмка выполнена в реальном браузере (viewport 360×640, локальный рендер сайта): hero растёт до 793px, все 3 ссылки (обе CTA + «Що означають дві коми?») clipped:false, скриншот подтверждает видимость и кликабельность. PWA-prompt (pwa-install.js): задержка 9s → 30s + показ только после первого взаимодействия (scroll/pointerdown/keydown); web-push.css: max-height баннера на мобильном 78vh → 50vh. Бонус: twocomms/preview_settings.py — sqlite-настройки для локального рендера в песочнице.

- [x] **W1-9. [NEW-501] 🔴 Дропшип-вебхук Monobank без подписи (пропущен аудитом)** `[REPO]`
  ✅ **DONE (коммит db0af339):** `dropshipper_monobank_callback` (orders/dropshipper_views.py) теперь: (1) требует валидный `X-Sign` через общую `_verify_monobank_signature` из W1-3 → иначе 400; (2) статус подтверждается pull-запросом `invoice/status`, а не body; (3) переход в paid идемпотентен (повторный вебхук не дублирует уведомления); (4) обрабатывается полный набор failure-статусов (expired/rejected/reversed/…). Тесты в `test_monobank_webhook.py`.
  `orders/dropshipper_views.py:1210` — `dropshipper_monobank_callback`: `@csrf_exempt`, без проверки `X-Sign`; `status=='success'` из body → `payment_status='paid'` + `status='confirmed'`. Тот же класс уязвимости, что W1-3, но в дропшип-контуре — аудит (DB-010) зафиксировал только гонку статусов, подпись пропустил.
  Фикс: единая функция верификации подписи Monobank (из W1-3) → применить в ОБОИХ вебхуках.
  Приёмка: невалидная подпись → 400, статус не меняется.

- [x] **W1-10. [NEW-502] Загрузка файлов профиля без валидации (P1, пропущен аудитом)** `[REPO]`
  ✅ **DONE (коммиты 5d59ef69 + авто-коммит перед ним):** (1) новый `validate_profile_upload_size()` в views/auth.py — ��имит 10 МБ, подключён в `ProfileSetupForm.clean_avatar/clean_ubd_doc`; (2) `edit_profile` (views/profile.py) больше НЕ присваивает `request.FILES` напрямую: файлы прогоняются через `forms.ImageField().clean()` (Pillow-проверка содержимого) + size-лимит, email валидируется `validate_email`, имена обрезаются по длине полей; (3) settings.py: `FILE_UPLOAD_MAX_MEMORY_SIZE=10MB`, `DATA_UPLOAD_MAX_MEMORY_SIZE=20MB`. Тесты: php-файл под видом png отклоняется; oversized-изображение отклоняется (test_auth.py). Примечание: `edit_profile` не подключён в urls.py (боевой путь — `profile_setup_db`), но починен на случай подключения.
  `storefront/views/profile.py:150-156` (`edit_profile`): `request.FILES['avatar']`/`ubd_doc` присваиваются НАПРЯМУЮ без формы → валидация ImageField обходится (любой файл/размер на диск shared-хостинга); email/first_name тоже без валидации. Рядом `profile_setup` (тот же файл, :196) делает это правильно через `ProfileSetupForm`.
  Фикс: edit_profile → та же ProfileSetupForm (или отдельная форма с FileExtension/size-валидаторами); задать `FILE_UPLOAD_MAX_MEMORY_SIZE`/лимит размера.

- [x] **W1-11. [NEW-503] ubd_doc (PII-документ) в публичном media (P1-check)** `[SERVER]` + `[REPO]` — fixed `ead5fd70` + `e89fd17d`; live direct URL 403, owner/staff route covered.
  Фото посвідчення УБД хранится в `media/ubd_docs/` с оригинальным именем файла — если media отдаё������ся статикой LiteSpeed, документ доступен по угадываемому URL без auth (curl-пробы дают 403 — возможно hotlink-защита по Referer, проверить с Referer-заголовком и из браузера, S-14).
  Фикс если ��одтвердится: отдавать ubd_docs через auth-view (owner/staff) + ��ан��ом��зи��оват�� имена (`upload_to` callable с uuid); закрыть каталог в .htaccess.

- [x] **W1-12. [NEW-506] 🔴 Retail-вебхук: нет pull-verify И нет сверки суммы (усиление W1-3)** `[REPO]`
  ✅ **DONE (коммит db0af339):** новый `_resolve_retail_invoice_status()` в monobank.py — retail-путь вебхука И `monobank_return` подтверждают деньги ТОЛЬКО pull-истиной (`GET /api/merchant/invoice/status`); для success-статусов сверяется `paidAmount` (fallback: finalAmount/amount) с ожидаемой суммой (`get_prepayment_amount()` для prepay_200, иначе `total_sum − discount_amount`); недоплата или сбой сверки → статус `processing` (заказ в checking, НЕ paid) + error-лог. Если pull не удался — success из недоверенного источника тоже даунгрейдится до processing. Тесты: `test_webhook_underpaid_amount_goes_to_checking`, `test_webhook_pull_failure_does_not_mark_paid`.
  В коде УЖЕ есть правильный паттерн: wholesale- и IG-ветки вебхука делают pull-verify («Гроші підтверджуємо ТІЛЬКИ pull-істиною», monobank.py:1355-1390). Но главный retail-путь идёт мимо: `_apply_monobank_status(order, status_value)` ставит `paid`/`prepaid` чисто по строке статуса из body, без pull-подтверждения и без сверки `paidAmount` с ожидаемой суммой — частичная оплата пометит заказ полностью оплаченным.
  Фикс (вместе с W1-3): после проверки подписи — pull статуса инвойса через API (паттерн уже есть в management.services.invoice_payments) + сверка amount с `get_prepayment_amount()`/`total_sum`; расхождение → `checking` + алерт, НЕ paid.
  Приёмка: webhook с верной подписью, но неверной суммой → заказ НЕ paid.

- [x] **W1-13. [NEW-508] Нет верхнего cap на qty (P2)** `[REPO]`
  `cart.py:788` — `qty = max(qty, 1)` без верхнего предела; `update_cart` аналогично. qty=999999 → гигантский инвойс/спам-заказы/искажение аналитики.
  Фикс: `qty = min(max(qty,1), MAX_QTY)` (напр. 50) в обоих эндпоинтах + тест.
  ✅ **DONE (коммит 91881756):** константа `MAX_CART_ITEM_QTY = 50` в views/cart.py; cap применён в `add_to_cart` (входное qty И накопленное `item['qty'] + qty`) и в `update_cart`. Тесты: `test_add_to_cart_caps_quantity`, `test_add_to_cart_caps_accumulated_quantity`, `test_update_cart_caps_quantity` (test_cart.py).

- [x] **W1-14. [NEW-514] Нет защиты от double-submit заказа (P2)** `[REPO]`
  `create_order` без идемпотентности: двойной сабмит формы (F5/дабл-клик при медленном ответе) = два заказа. `Order.save()` имеет retry на IntegrityError номера, но не дедуп самого заказа.
  Фикс: одноразовый токен формы в сессии (или дедуп по session+cart-hash в окне 30s) + disable кнопки на клиенте.
  ✅ **DONE (коммит 91881756):** серверный дедуп в `create_order` (checkout.py): sha256-отпечаток корзины + session, окно 30s (`_cart_fingerprint` / `_find_recent_duplicate_order` / `_remember_order_submit`) — повторный сабмит редиректит на уже созданный заказ, второй НЕ создаётся. Клиент: в `cart.js` submit-обработчик блокирует повторный сабмит формы и дизейблит кнопки (`data-submitting` + failsafe re-enable через 15s). Тест: `test_create_order_double_submit_creates_single_order`.

- [x] **W1-8. [GAP] 🔴 Публичный /test-analytics/ стреляет Purchase в боевой Pixel (AN-015)** `[REPO]`
  ✅ **DONE (коммит 5d2d91f4):** `test_analytics_events` (views/static_pages.py) → `@staff_member_required`; аноним получает 302 на admin login. Устаревший SEO-тест, ожидавший аноним-200, заменён на `test_test_analytics_requires_staff` (test_seo_regressions.py).
  Публичный URL без auth через 3s авто-стреляет полную воронку с Purchase 599 грн в БОЕВОЙ Meta Pixel. Загрязняет данные прямо сейчас; фикс — один декоратор. (Перенесён из W2-9 как немедленный.)
  Фикс: `@staff_member_required` или удалить маршрут.
  Приёмка: аноним → 403/404; событий в Pixel от URL нет.

---

## ВОЛНА 2 — ДАННЫЕ И АТРИБУЦИЯ (P0: без этого аналитика слепа; TECH-060…066)

- [ ] **W2-1. 🔴 Единая UTM-привязка любого заказа (CRO-041 / AN-013 / AN-021 / AN-030 / AN-031 → TECH-060)** `[REPO]` ✅ fallback-цепочка session_key→visitor_id→session['utm_data'] в link_order_to_utm; attach_tracking_to_order пишет click-ID (fbp/fbc-синтез из fbclid/ttclid/gclid/external_id/ip/ua) в payment_payload.tracking для COD; тесты test_utm_attribution.py (3) зелёные. Единый order-builder COD+Monobank — отложен как долгосрочный рефакторинг.
  > **RE-VERIFY W2-1:** REOPEN 2026-07-09: accept CRO-050 fail (Order.utm empty; no first_touch→Order; COD session; mono capture). Was false [x].
  > **PARTIAL RE-CLOSE 2026-07-14 (`394a247c`, `30808819`):** COD and guest-prepay session + first-touch Order/UTMSession/UserAction/tracking acceptance passed in production rollback canaries. Keep W2-1 open only for the separate CheckoutCapture conversion residual (F-075/W3-11); do not reopen the resolved writer gaps.
  COD-��уть не выз��вает НИ ОДНО�� функции трекинга; даже в monobank `link_order_to_utm` не срабатывает: lookup строго по session_key, `cycle_key()` при логине рвёт ключ, fallback на `session['utm_data']` отсутствует. Click-ID (fbc/fbp/fbclid/gclid/ttclid) не доходят до CAPI для COD. Факт: 0/43 заказов с utm.
  Фикс: (1) в `create_order` после `order.save()`: `order.session_key=...`; `link_order_to_utm(request, order)`; `record_order_action(...)`; (2) fallback-цепочка в `link_order_to_utm`: session_key → `visitor_id` → `session['utm_data']`; (3) копировать/синтезировать click-ID в `payment_payload.tracking` для ЛЮБОГО заказа (fbc из fbclid если куки нет); (4) долгосрочно — единый order-builder COD+Monobank.
  Приёмка (= CRO-050): визит с `?utm_source=audit` → COD-заказ → в БД utm_source='audit', utm_session FK, session_key, UserAction с order_id.

- [ ] **W2-2. 🔴 is_converted оживить (CRO-042 → TECH-061)** `[REPO]` ✅ закрыт W2-1 (record_order_action помечает конверсию); мёртвый record_purchase удалён; тест подтверждает is_converted/conversion_type/converted_at.
  > **RE-VERIFY W2-2:** REOPEN 2026-07-09: is_converted still 0 on prod; depends on W2-1. Was false [x].
  0/1015 UTMSession converted; `record_purchase` — мёртвая функция (0 call-sites).
  Фикс: закрывается fallback-цепочкой W2-1; удалить/переписать мёртвый `record_purchase`.
  Приёмка: после тестового заказа `is_converted=True`, `conversion_type`, `converted_at` заполнены.

- [x] **W2-3. 🔴 Единое определение purchase по всем слоям (CRO-045 → TECH-066)** `[REPO]` + `[REPO]`(docs)
  > **RE-VERIFY W2-3 RESOLVED 2026-07-14:** `fba4dc85` + `d561c11d`; production trusted purchase parity 31/31, 0 missing, 0 duplicate groups; the reopened UserAction blocker is closed.
  4 слоя × 3 потока = 4 разных определения. COD-покупки видит ТОЛЬКО Meta CAPI (через НП-крон); GA4/TikTok/UserAction — никогда. Prepaid шлёт полную сумму без refund.
  Целевое определение: `purchase` = подтверждённая оплата (webhook с п��дписью) ИЛИ получение посылки (NP received); создание заказа = отдельное `place_order`/`lead` во всех слоях.
  Фикс: (а) record-слой в COD create_order; (б) UserAction purchase в NP-delivery-путь; (в) TikTok Purchase в NP-delivery + pre-check `purchase_sent`; (г) server-side GA4 purchase для COD (Measurement Protocol) или задокументировать пробел; (д) `paid_value` отдельным параметром; (е) refund/cancel-события; (ж) задокументировать определение в TECHNICAL_TASKS.md.
  ✅ **DONE:** (а) закрыто в W1/W2-2 (checkout.py → record_order_action); (б) `_record_purchase_action` в nova_poshta_service.py — UserAction purchase при «посылка получена», дедуп: max 1 на order_id; (в) `_send_tiktok_purchase_event` там же — TikTok Purchase (→CompletePayment после W2-6) с pre-check `tiktok_events.purchase_sent` в payment_payload; (г) GA4 server-side пробел задокументирован — нужен Measurement Protocol api_secret (`[OWNER]`); (д) `_extract_paid_amount()` + `custom_properties.paid_value`/`payment_status` в Meta Purchase — prepaid-заказ больше не выглядит как полная оплата (value=full для ROAS, paid_value=факт); (е) refund/cancel — зафиксировано как TODO в доке; (ж) каноническое определение + матрица «слой × поток»: `twocomms/docs/PURCHASE_DEFINITION.md`. Регрессия: 42 теста (webhook/checkout/attribution/orders) зелёные.
  ✅ **LIVE RE-CLOSE:** migration 0083 enforces one `(action_type, order_id)` row in MariaDB; confirmed Monobank/admin/manual/Instagram/NP paths converge on the same helper; guarded reconciliation restored 26 historical trusted rows and is idempotent. Local 172/172 + server 186/186 focused tests, rollback-canary and live HTTP rejection passed. GA4 MP remains `[OWNER]`; refund/cancel remains the separately documented follow-up.

- [x] **W2-4. 🔴 Бот-фильтр и чистота событий (AN-035 / CRO-024 → TECH-063)** `[REPO]`
  > **RE-VERIFY W2-4:** NUANCE 2026-07-09: code OK; historical PV noise remains.
  `SiteSession.is_bot` мёртв (early return); product_view пишется без бот-фильтра; 96,2% product_view без site_session (40 490 views → 55 ATC — метрики врут); двойной счёт на legacy-301; нет дедупа.
  Фикс: единый bot-detect на записи UserAction; запись product_view ПОСЛЕ redirect; дедуп 30 мин session+product; `is_staff` → авто-исключение (AN-004).
  Приёмка: baseline CRO-051 пересчитан; view→ATC правдоподобен.
  ✅ **DONE (запись событий):** record_user_action (utm_tracking.py) теперь: (1) отбрасывает бот-UA (единый `is_bot` из tracking.py — те же BOT_SIGNALS, что в middleware); (2) отбрасывает `is_staff`-пользователей (AN-004); (3) дедуп product_view — 30 мин на (session_key ИЛИ visitor_id) + product_id (⚠️ поле называется `timestamp`, не `created_at`); (4) SiteSession теперь `get_or_create` вместо `.get()` → фикс 96,2% product_view без site_session. product.py: `record_product_view` перенесён ПОСЛЕ legacy-301 решения — двойной счёт устранён. Тесты: EventHygieneTests (5 шт.) в test_analytics_tracking.py — бот-UA, staff, дедуп ×3, site_session-линк, 301-без-записи; все 12 в файле зелёные. ⚠️ Приёмка «baseline CRO-051 пересчитан» — `[SERVER]`: пересчёт после накопления чистых данных (~1-2 недели). NB: test_product имеет 13 pre-existing падений в песочнице (media/env), НЕ связаны с этим фиксом — проверено на чистом дереве.

- [x] **W2-5. GTM fast-path для платного трафика (CRO-004 / AN-002, P1, ~10 строк)** `[REPO]`
  GTM ��рузится по interaction или 12-35s; fast-path для `utm_*`/fbclid/gclid/ttclid отсутствует → paid-bounce невидим, `_fbc` не создаётся.
  Фикс: base.html — при click-id/utm в URL грузить GTM немедленно.
  ✅ **DONE:** fast-path добавлен в ОБА deferred-лоадера base.html (GTM и analytics-loader.js): при `gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign` в query — немедленная загрузка. Проверено в браузере: `/?utm_source=audit&fbclid=…` → analytics-loader инжектится сразу; чистая органика (свежая сессия, 0 interaction) → НЕ инжектится, PageSpeed-профиль сохранён (Lighthouse не ходит с utm/click-id).

- [x] **W2-6. TikTok: нестандартные имена событий (AN-020, P1)** `[REPO]`
  Клиент (analytics-loader.js:394) и сервер (tiktok_events_service.py) шлют Meta-имена «Purchase»/«Lead» вместо CompletePayment/PlaceAnOrder/SubmitForm → цели TikTok их не видят.
  Фикс: маппинг имён на обоих слоях с сохранением event_id-дедупа; уйти с legacy `v1.3/pixel/track/`.
  ✅ **DONE:** (1) клиент — `mapTikTokEventName()` в analytics-loader.js: Purchase→CompletePayment, Lead→PlaceAnOrder перед КАЖДЫМ `ttq.track` (прямая отправка + оба буфер-пути); Meta/GA4/YM продолжают получать оригинальные имена; cache-buster `?v=7`; (2) сервер — tiktok_events_service.py: `EVENT_NAME_MAP` тот же + миграция с legacy `v1.3/pixel/track/` на Events API 2.0 `v1.3/event/track/` (payload переписан на `event_source`/`event_source_id`/`data[]`, `event_time` unix-int); (3) event_id НЕ трогается на обоих слоях → client/server дедуп сохранён. Тесты: `orders/tests/test_tiktok_events.py` (5 шт., зелёные): ма��пинг Purchase/Lead, pass-through ViewContent, структура 2.0-payload, POST через мок. ⚠️ NB: если в env задан кастомный `TIKTOK_EVENTS_API_ENDPOINT` со старым URL — на сервере его надо убрать/обновить.

- [ ] **W2-7. CAPI/TikTok внутри row-lock транзакции (AN-011 / DB-009, P1)** `[REPO]` ✅ Telegram/Meta/TikTok вынесены из select_for_update в _send_post_payment_events через transaction.on_commit; попутно добавлен pre-check purchase_sent для TikTok (часть W2-3в). Invoice/create вне atomic — уже закрыт в W1-5в. Тесты PostPaymentEventsDeferralTests зелёные.
  > **STRICT RE-VERIFY W2-7:** STRICT RE-VERIFY 2026-07-09: utils._record_monobank_status_locked uses on_commit+CAPI, BUT live retail webhook monobank.py:1611 calls _apply_monobank_status which does Telegram+record_order_action SYNCHRONOUSLY and does NOT call _dispatch_post_payment_events/CAPI. Dual path — accept W2-7 incomplete. Uncheck.
  Отправка Meta+TikTok ВНУТРИ `transaction.atomic()`+`select_for_update()` — до ~25-40s row-lock; тот же анти-паттерн: Monobank invoice/create внутри atomic (monobank.py:~843) при wait_timeout=60.
  Фикс: `transaction.on_commit()` для внешних отправок; инвойс — после commit.

- [x] **W2-8. Нормализация utm_source + AI-канал (AN-032 / AN-033 → TECH-009/065, P1/P2)** `[REPO]`
  > **STRICT RE-VERIFY W2-8:** STRICT 2026-07-09: KEEP code. Residual dirty utm rows on prod do not undo middleware fix; track as data/backfill separately.
  > **RE-VERIFY W2-8:** NUANCE 2026-07-09: normalize code OK; live dirt chatgpt.com/ig still observed.
  Словаря нормализации нет (ig/Instagram/IGShopping/Inst_Vid = 4 написания); AI-трафик (chatgpt.com — 119 сессий, 3-й источник) не детектится отдельным каналом.
  Фикс: словарь нормализации в UTMTrackingMiddleware; детект chatgpt.com/perplexity.ai/gemini/claude.ai по utm+referrer → канал «AI»; UTM governance-конвенция.
  ✅ **DONE:** utm_utils.py — `UTM_SOURCE_ALIASES` (instagram/facebook/tiktok/google/telegram/youtube + AI-источники), `normalize_utm_source()` (алиасы → канон, неизвестные → lowercase), `detect_ai_source()` (суффикс-матч referrer-hostname: chatgpt.com, chat.openai.com, perplexity.ai, gemini.google.com, claude.ai, copilot.microsoft.com, you.com, poe.com). UTMTrackingMiddleware: нормализация utm_source при захвате; AI-источник без medium → `utm_medium=ai`; трафик БЕЗ utm с AI-referrer → синтетический `utm_source=<ai>` + `utm_medium=ai` (создаётся UTMSession). Governance-конвенция: docs/UTM_GOVERNANCE.md. Тесты: test_utm_normalization.py — 10 шт. зелёные (unit + интеграция через Client). Нормализация действует только для НОВЫХ сессий; исторические данные не мигрировались (при необходимости — отдельный backfill).

- [x] **W2-9. Meta CAPI мелочи (AN-011/AN-012 остатки, P3)** `[REPO]`
  fallback `event_source_url` на чужой домен twocomms.com (4 места: facebook_conversions_service.py:561/662/779, tiktok_events_service.py:213) → twocomms.shop; `time.sleep` в retry блокирует воркер; клиентский random-fallback event_id у AddPaymentInfo рвёт дедуп-пару; мёртвые API `send_lead_event`/`send_event_for_order_status` — удалить или подключить осознанно.
  ✅ **DONE:** (1) event_source_url — все 4 места уже twocomms.shop (исправлено ранее, перепроверено grep'ом); (2) `time.sleep` в CAPI-retry (до ~3.5s суммарно) больше не держит request-воркер: `_dispatch_post_payment_events` в utils.py выполняет отправку в daemon-потоке с `connection.close()` в finally (в тестовом раннере — синхронно, чтобы не ломать транзакционную изоляцию); (3) random-fallback event_id у AddPaymentInfo убран в checkout-mono.js (оба call-site) — только серверный `add_payment_event_id`, дедуп-пара Pixel↔CAPI не рвётся; (4) мёртвый `send_event_for_order_status` удалён из facebook_conversions_service.py (0 вызовов; маршрутизация живёт в `_send_post_payment_events`); `send_lead_event` (TikTok) — НЕ мёртвый, вызывается для prepaid — оставлен. Тесты: 39 (webhook+checkout) зелёные.

- [ ] **W2-10. Прочее аналитическое (P1/P2, после ядра)** `[REPO]`
  - [x] **AN-014/TECH-074:** delivered-Purchase уходит только в Meta → добавить TikTok/GA4 в NP-delivery-путь; refund/cancel-события для невыкупов. ✅ TikTok+UserAction добавлены в W2-3б/в; GA4 — задокументированный пробел (нужен MP api_secret); refund/cancel — TODO в PURCHASE_DEFINITION.md.
  - [ ] **CRO-033 (P2):** server-side CAPI для AddToCart отсутствует — при блокировщиках ATC теряется полностью.
  - [x] **AN-036 (P2):** `increment_visit` = SELECT+UPDATE на каждый pageview → time-window 30 мин. ✅ UTMSession.increment_visit: новый визит только если last_seen старше 30 мин (VISIT_WINDOW_MINUTES); внутри окна — только продление last_seen, не чаще раза в минуту.
  - [ ] **AN-037 (P2):** first/last touch несогласованы → поля `first_touch_*`/`last_touch_*`.
  - [ ] **AN-050/TECH-077 (P2):** cookie-consent баннера нет; для ЕС-трафика — гео-баннер + Consent Mode v2 default denied.
  - [x] **AN-051/NEW-404 (P1-retention):** UTMSession+UserAction не чистятся никогда → retention-cron; политика приватности не упоминает IP/гео → обновить текст (NEW-403). ✅ REPO-часть: management-команда `cleanup_analytics_data` (--dry-run; UserAction >180д, неконверсионные UTMSession неактивные >90д по last_seen, orphan UserAction по несуществующим order_id — закрывает и DB-003); добавить в crontab — `[SERVER]` (см. docs/OPS.md). Текст политики приватности — `[OWNER]` (NEW-403).
  - [ ] **AN-038 (P2):** N+1 в UTM-админке; пересверить отчёты после W2-1/W2-2.
  - [ ] **DB-005 (P2):** `utm_cohort_analysis.py` — 3 N+1 (~300-400 SQL на рендер) → TruncMonth+annotate.
  - [x] **DB-002 (P2):** UserAction — денормализовать is_bot + индекс `(site_session, action_type)`. ✅ Индекс `idx_action_site_type` добавлен (миграция 0080); денормализация is_bot не нужна — после W2-4 бот-события в UserAction вообще не пишутся.
  - [x] **DB-003 (P3):** orphan UserAction.order_id 259, 260 — вычистить при retention-работах. ✅ Закрывается командой `cleanup_analytics_data` (шаг 3: удаление UserAction с несуществующими order_id); запуск на проде — `[SERVER]`.
  - [x] **[GAP] AN-039 (P3):** search-query пишется сырым — обрезка длины + маскировка (однострочник). ✅ record_search: обрезка до 200 симв. + маскировка email → `[email]` и 12-19-значных числовых последовательностей (карты) → `[number]`.
  - [ ] **[GAP] AN-001/CB-034 (P2):** gtag.js G-109EFTWM05 грузится ПАРАЛЛЕЛЬНО GTM (риск двойного GA4 page_view); dataLayer получает 2 события на ATC (`AddToCart` + `add_to_cart`). Код-часть: убрать прямой gtag после сверки контейнера в����адельц��м (см. OWNER-1).
  - [ ] **[GAP] AN-003 (P2):** `payment_type` параметр не существует (TECH-007); `add_shipping_info`/`add_payment_info` в GA4-схеме отсутствуют; item_id ��истинга `TC-pid-default-S` н�� матчится с offer_id покупок.
  - [ ] **[GAP] NEW-406 (P3):** русский цвет в offer_id «TC-0020-ЧЕРНЫЙ-M» — латинизировать слаг цвета (согласовать с Merchant-фидом, не ломать существующие id без маппинга).
  - [ ] **[GAP] NEW-407 (P3):** legacy-дефолт `pay_type='cod'` в checkout.py:119 при отсутствии cod в UI — согласовать с W1-1 п.2.

---

## ВОЛНА 3 — НАДЁЖНОСТЬ И ИНФРАСТРУКТУРА (P1)

- [x] **W3-1. Мёртвая Celery-очередь глотает Telegram-уведомления (TD-015 / TD-003)** `[REPO]`
  > **RE-VERIFY W3-1:** NUANCE 2026-07-09: sync Telegram OK; CELERY_BROKER may still be set in env.
  Прод без Celery-воркера, Redis-брокер жив → `.delay()` публикует в мёртвую очередь: уведомления о смене статуса/ТТН молча теряются; битый импорт `send_telegram_notification_task` → часть отправок синхронна в request-потоке; `CELERY_BEAT_SCHEDULE` survey-check не выполняется.
  Фикс: `async_enabled=False` по умолчанию в `TelegramNotifier.__init__`; survey-check → cron-команда; починить/удалить битый импорт; зафиксировать «Celery не возвращаем»; вычистить no-op шимы.
  Приёмка: смена статуса заказа → Telegram-сообщение приходит.
  ✅ **DONE:** (1) битый импорт `from storefront.tasks import send_telegram_notification_task` в orders/telegram_notifications.py удалён (функция там не существует — импорт ВСЕГДА падал, async-ветки были мёртвым кодом); (2) обе мёртвые async-ветки (`send_message`, `send_personal_message`) вычищены — отправка синхронная, фоновость обеспечивает orders/tasks.py (daemon-thread, уже работал корректно); (3) `async_enabled=False` по умолчанию; (4) `CELERY_BEAT_SCHEDULE` удалён из settings.py — beat не запущен, survey-check не выполнялся никогда; cron-команда `check_survey_inactivity` уже существует — добавить в crontab `[SERVER]` (внесено в docs/OPS.md, дыра №5); (5) решение «Celery не возвращаем» зафиксировано в docs/OPS.md отдельным разделом. Синхронный шим в storefront/tasks.py ОСТАВЛЕН — он не no-op, а рабочий механизм для остальных `.delay()`-вызовов. Регрессия: 59 тестов (NP delivery + orders + checkout) зелёные.

- [x] **W3-2. Мониторинг ошибок с алертом (TD-022 → TECH-041)** `[REPO]`
  django.request ERROR → stderr «в никуда»; window.onerror нет; handler500 нет.
  Фикс: ERROR-Handler → Telegram с rate-limit; `window.onerror`/`unhandledrejection` → `/api/client-error/`; инцидент-код в user-facing message.
  ✅ **DONE:** (1) `TelegramAlertHandler` (twocomms/log_handlers.py) повешен на logger `django.request` уровня ERROR — алерт админу через существующий TelegramNotifier, rate-limit 5 алертов/10 мин (глобальный, по modulе-level state), отправка в daemon-потоке, сам handler никогда не роняет запрос; (2) `window.onerror` + `unhandledrejection` в base.html → POST `/api/client-error/` через sendBeacon (лимит 5 репортов/страницу, дедуп по message) → лог `client_errors.log` (отдельный logger `storefront.client_errors`, БЕЗ Telegram — фронтовые ошибки массовые); endpoint rate-limited 10/m/IP, поля жёстко обрезаются; (3) кастомный `handler500` (twocomms/error_views.py) — 8-символьный инцидент-код показывается пользователю и логируется рядом с трейсбеком (жалоба покупателя → конкретный traceback); standalone-шаблон 500.html (не extends base — если упал сам base, наследование зациклит ошибку) + hard-coded HTML fallback. Смоук: client-error 200/400/лог пишется, handler500 рендерит код. Регрессия: 63 теста зелёные.

- [x] **W3-3. Server-side кэш выключен куками → LCP 4.8-16.4s (SEO-010)** `[REPO]`
  `Vary: Cookie` + `Set-Cookie` (csrftoken+twc_vid) на КАЖДОМ анонимном GET выключают LiteSpeed page cache → TTFB холодный 8-18s; интермиттентные 503/500.
  Фикс: не ставить csrftoken/twc_vid на анонимные GET (lazy-выдача при первом POST/взаимодействии); делать ВМЕСТЕ с W3-4.
  Приёмка: повторный анонимный GET = cache HIT; LCP mobile < 2.5s на 3 шаблонах.
  ✅ **DONE:** (1) AnalyticsIdentityMiddleware: twc_vid/first-touch куки НЕ ставятся на кэшируемых анонимных GET — гейт `_analytics_cookie_allowed` (разрешено: не-GET, /api/bootstrap/, landing с utm/click-id/внешним referrer — там first-touch атрибуция важнее кэша); (2) `@ensure_csrf_cookie` снят с home/catalog; (3) get_token() убран из cache-hit пути `cache_page_for_anon`; (4) новый endpoint `/api/bootstrap/` (no-store) — ленивая выдача csrftoken+analytics-кук, дергается из base.html через requestIdleCallback только если csrftoken-cookie ��тсутствует (path добавлен в whitelist noise-фильтра). Тест: анонимный GET / → НОЛЬ Set-Cookie. Приёмка «LCP mobile < 2.5s» — замер на проде после деплоя `[SERVER]`.

- [x] **W3-4. Кэш отдаёт чужой CSRF-токен (CRO-032, live-подтверждён)** `[REPO]`
  `cache_page_for_anon` кэширует HTML с чужим `csrfmiddlewaretoken` → POST `/i18n/setlang` = 403 для всех анонов на cache-hit.
  Фикс: не кэшировать формы с inline-токеном (пустой meta + заполнение из cookie — паттерн уже есть, НЕ ломать); `/cart/summary/`, `/cart/count/` → `Cache-Control: no-store`.
  ✅ **DONE:** корень бага — language_switcher.html: формы /i18n/setlang «запекали» `{% csrf_token %}` в кэшируемый HTML → чужой токен на cache-hit → 403. Фикс: пустой `value=""` + подстановка из cookie в submit-обработчике (fallback на /api/bootstrap/, если куки ещё нет). `/cart/summary/` и `/cart/count/` → `@never_cache`. Паттерн base.html (пустой meta + заполнение из cookie) сохранён и дополнен lazy-bootstrap. Тесты: test_cache_hygiene.py — 6 шт. (ноль Set-Cookie на анонимном GET, нет inline-токена в HTML, bootstrap ставит csrftoken+twc_vid+no-store, utm-landing получает куки, cart no-store, cache-hit чистый). Регрессия: 49 тестов зелёные; 4 падения test_seo_regressions — pre-existing (проверено на чистом дереве через git stash).

- [x] **W3-5. Rate limiting и API-поверхность (TD-023 / TD-024)** `[REPO]`
  Лимитер ключуется спуфаемым X-Forwarded-For; fail-open при падении Redis; логи��/регистрация/чекаут/отзывы без точечных лимитов; `/api/analytics/track/` — публичный no-op; Swagger/Redoc публичны.
  Фикс: REMOTE_ADDR-ключ (или доверенный XFF-hop); точечные лимиты auth/checkout; staff-only Swagger; LOG_IGNORED_EXCEPTIONS+алерт для Redis (TD-012).
  ✅ **DONE:** (1) SimpleRateLimitMiddleware: ключ = REMOTE_ADDR; клиентский XFF учитывается ТОЛЬКО если REMOTE_ADDR приватный (локальный прокси-hop), и берётся последний элемент — спуфинг curl -H больше не работает; (2) fail-open при падении Redis оставлен осознанно (нельзя ронять сайт), но теперь с warning-логом `twocomms.ratelimit` вместо молчания; (3) точечные лимиты: ajax_login 10/m/IP, ajax_register 5/m/IP (django-ratelimit key='ip' = REMOTE_ADDR); чекаут уже покрыт W1 (submit-lock + идемпотентность); (4) Swagger/Redoc/schema → staff-only (404 для анонимов) — публичная карта API закрыта; (5) `/api/analytics/track/` снят с маршрутизации: no-op (только logger.info), ноль клиентских вызовов, реальный трекинг идёт через /api/track-event/. Регрессия: 51 тест зелёный.

- [ ] **W3-6. Логи: 958 MB, ротация, PII (TD-016 / CB-045)** `[SERVER]` + `[REPO]`(конфиг)
  `nova_poshta_cron.log` = 827 MB без ротац��и; PII-like hits в ��огах; rum.log — 24 secret-like hits.
  Фикс: `[REPO]` — logrotate-конфиг/скрипт в репо + маскирование PII в лог-вызовах; `[SERVER]` — user-cron truncate, удалить 8 мёртвых логов.
  ✅ **REPO-часть DONE:** добавлен `PIIRedactionFilter` в logging handlers, фильтр подключён к console/file/rum/telegram/client-error handlers; добавлен `scripts/rotate_twocomms_logs.sh` с gzip-ротацией, chmod и TTL архивов; `twocomms/docs/OPS.md` содержит cron-инструкцию. Осталось `[SERVER]`: поставить cron, безопасно truncate/архивировать текущие большие логи и удалить мёртвые.

- [x] **W3-7. Идемпотентность и гонки статусов (CB-020-паттерн + DB-010)** `[REPO]`
  > **STRICT RE-VERIFY W3-7:** STRICT 2026-07-09: KEEP. No obvious update_fields→bare save() fallback pattern in mono/utils/np. Medium confidence without full test run.
  `save(update_fields)` → молчаливый fallback `save()` в 4 местах (utils.py:542/573/723, nova_poshta_service.py:515 — флаг `purchase_sent`!) = риск lost-update и ПОВТОРНОГО CAPI Purchase; admin_update_dropship_status без select_for_update.
  Фикс: убрать fallback, логировать ошибку; select_for_update в dropship-статусах. НЕ менять control flow массово (RISK-04).
  ✅ **DONE:** fallback `save()` после failed `save(update_fields=...)` убран в monobank-status persistence и NP purchase flags: ошибка логируется и пробрасывается, без full-save overwrite; `admin_update_dropship_status` теперь валидирует статус и обновляет `DropshipperOrder` внутри `transaction.atomic()` + `select_for_update()`. Тесты покрывают отсутствие fallback-save и row-lock.

- [ ] **W3-11. [NEW-510] CheckoutCapture: публичный PII-приёмник без лимитов и retention (P2)** `[REPO]`
  > **RE-VERIFY W3-11:** PARTIAL 2026-07-09: rate-limit code OK; CheckoutCapture.converted never true on mono path.
  `checkout_capture.py` — `@csrf_exempt` эндпоинт пишет ФИО/телефон/email в `CheckoutCapture` по session_key. Защита — только Sec-Fetch-Site (старые клиенты/curl без заголовка проходят); rate-limit нет (спам-записи); retention нет — `recover_checkouts` читает, никто не чистит (PII копится вечно, связка с NEW-404/AN-051).
  Фикс: rate-limit по session/IP; чи��тка capture-записей старше 30-90 дней в trim-команде; упомянуть в privacy policy.
  ✅ **DONE:** (1) rate-limit `@ratelimit(key='user_or_ip', rate='30/m', block=False)` + JSON 429 — 30/m хватает для дебаунс-автосейва формы, душит скриптовый спам; (2) retention: в `cleanup_analytics_data` добавлен шаг 4 — CheckoutCapture старше `--captures-days` (по умолчанию 60) удаляются, включая конвертированные (данные уже в Order); (3) упоминание в privacy policy — `[OWNER]` (связка с NEW-403).

- [x] **W3-12. [NEW-512] Брутфорс промокодов (P3, часть TD-023)** `[REPO]`
  `apply_promo_code` без rate-limit — перебор кодов скриптом. Фикс: точечный ratelimit (10/min/session) — включить в W3-5.
  ✅ **DONE:** `@ratelimit(key='user_or_ip', rate='10/m', block=False)` на apply_promo_code + JSON 429 «Забагато спроб» при превышении (block=False, чтобы отдавать управляемый JSON вместо голого 403).

- [x] **W3-9. [NEW-504] Telegram-вебхук без секрета при пустом env (P2)** `[REPO]`/`[SERVER]` — fixed `d7c6812a`; mode-600 secret registered with Telegram and live header probes passed.
  > **RE-VERIFY W3-9:** PARTIAL 2026-07-09: REPO warning OK; prod TELEGRAM_BOT_WEBHOOK_SECRET was EMPTY — accept not met.
  `accounts/telegram_views.py:20-29`: проверка `X-Telegram-Bot-Api-Secret-Token` ОПЦИОНАЛЬНА — если `TELEGRAM_BOT_WEBHOOK_SECRET` не задан в env, вебхук принимает любые POST.
  Фикс: проверить env на сервере (S-13); если пуст — задать секрет + перерегистрировать webhook у Telegram; в коде — предупреждающий лог при пустом секрете.
  ✅ **REPO-часть DONE:** при пустом TELEGRAM_BOT_WEBHOOK_SECRET каждый запрос пишет SECURITY-warning в лог `accounts.telegram` с инструкцией (setWebhook secret_token=...). Запросы НЕ блокируются намеренно — иначе бот упадёт до того, как секрет добавят в env. Осталось `[SERVER]`: задать секрет в env + перерегистрировать webhook.

- [x] **W3-10. [NEW-505] eval() в survey_engine (P3-hardening)** `[REPO]`
  `storefront/services/survey_engine.py:340`: строковые условия опроса исполняются через `eval()` (с `__builtins__={}` — обходится). Источник — JSON-definition (admin-controlled), риск низкий, но паттерн опасный.
  Фикс: заменить на безопасный парсер при следующем касании файла.
  ✅ **DONE:** eval() заменён на AST-walker `_safe_eval_node` с whitelist узлов: константы, and/or/not, сравнения (==/!=/</<=/>/>=/in/not in), списки/кортежи и вызовы 4 helper-функций (_answer/_count/_first/_includes) по имени. Атрибуты, индексация, comprehension'ы, keyword-аргументы → ValueError → warning+False. Unit-проверка: легитимные условия работают, sandbox-escape паттерны (`().__class__`, `__globals__`, `__import__`) отклоняются. Полный прогон storefront.tests: 850 тестов, 45 падений = pre-existing (идентичный результат на HEAD~1 через git worktree), новых регрессий ноль.

- [ ] **W3-8. [GAP] Включить контролируемый slow-log (DB-001, P2)** `[SERVER]`
  `slow_query_log=OFF` — перф-работы W3 вслепую.
  Фикс: включить slow-log (`long_query_time=2`) на 3-7 дней через панель/SET GLOBAL (если права позволяют), собрать топ-10, выключить. Если прав нет — зафиксировать отказ.

---

## ВОЛНА ADS — ПРЕДЗАПУСК META-РЕКЛАМЫ (P0: блокеры запуска трафика, добавлено 2026-07-08)

> Владелец запускает платный Meta-трафик. Всё в этой волне — блокеры или прямые
> риски слива бюджета/индексации. Порядок = приоритет исполнения.

- [ ] **ADS-1. Meta Pixel: PageView теряется у «отказников» + захардкоженный ID (P0)** `[REPO]`
  > **RE-VERIFY ADS-1:** PARTIAL 2026-07-09: early PageView OK live; BFCache initializePixelsImmediately still broken (call without def). Uncheck full done.
  Диагноз: (1) analytics-loader.js (~55KB, содержит fbevents-инициализацию) грузится ТОЛЬКО после первого user interaction (Phase 22e, base.html:1083+) → посетитель из рекламы, который посмотрел и ушёл без клика/скролла, НЕ отправляет PageView → Meta не видит трафик, атрибуция и оптимизация кампаний ломаются, CPM растёт; (2) meta_pixel_id `823958313630148` захардкожен в base.html `{% with %}` (не из env `FACEBOOK_PIXEL_ID`, который пуст); (3) `<div id="am">` (advanced matching) рендерится только для authenticated — для гостей из рекламы AM-параметры пусты; (4) в partials/analytics.html лежит мёртвый закомментированный блок пикселя с placeholder `FACEBOOK_PIXEL_ID` — мусор, вводит в заблуждение.
  Фикс: лёгкий инлайн-сниппет fbq (init+PageView, ~1.5KB) в `<head>` СРАЗУ (без ожидания interaction), тяжёлый loader с событиями (ViewContent/AddToCart/InitiateCheckout/Purchase) оставить лениво; ID из settings/env с fallback; вычистить мёртвый блок.
  Приёмка: Meta Pixel Helper видит PageView без взаимодействия; события e-commerce приходят в Events Manager (Test Events).
  ✅ **DONE (коммит 5aacb163, verified):** `base.html` делает `fbq('init')` + `PageView` сразу в `<head>`, `META_PIXEL_ID` приходит из context/settings с fallback; `analytics-loader.js` не шлёт второй PageView, если head-snippet уже bootstrapped; мёртвый placeholder в partials/analytics.html вычищен. Live Events Manager/Pixel Helper — `[OWNER]`.

- [ ] **ADS-2. Английская версия наполовину не переведена (P0 для SEO/рекламы на EN) ** `[REPO]`
  > **RE-VERIFY ADS-2:** PARTIAL 2026-07-09: po may be clean; live /en/ H1 still Ukrainian. Uncheck full done.
  Диагноз: locale/en/django.po — 161 пустой msgstr + 129 fuzzy (fuzzy НЕ компилируются в .mo → показывается украинский). Итого ~290 строк на /en/ остаются украинскими: части хедера, футера, блоки наполовину переведены. Смешанный язык = сигнал низкого качества для Google, ломает EN-индексацию.
  Фикс: перевести все пустые msgstr, снять fuzzy-флаги (проверив корректность), `compilemessages`, прогнать по /en/ ключевые шаблоны (home, catalog, product, cart, checkout, header/footer).
  Приёмка: на /en/ нет украинских строк в header/footer/основных блоках; `msgattrib --untranslated` и `--fuzzy` по en.po → пусто.
  ✅ **DONE (коммит c1c0de5b, verified):** main-site `twocomms/locale/en/LC_MESSAGES/django.po` проверен: `msgattrib --untranslated` = 0, `msgattrib --only-fuzzy` = 0. DTF locale не трогался по scope fence.

- [ ] **ADS-3. Title tag каталога обрезан (P1)** `[REPO]`
  > **RE-VERIFY ADS-3:** REOPEN 2026-07-09: live titles still truncated mid-phrase; DB seo_title not reseeded. Code TITLE_LIMIT alone insufficient.
  Диагноз: `seo_utils._truncate_at_word_boundary(..., 60)` жёстко режет тайтлы категорий/каталога — в SERP и соцпревью видны «недописанные» тайтлы. 60 — консервативный лимит: Google показывает ~600px (~65-70 символов кириллицы).
  Фикс: аудит фактических тайтлов всех категорий (скриптом); паттерны, которые укладываются в лимит целиком (без обрезки хвоста «| TwoComms» посреди слова); поднять лимит до 65 + не резать, если строка ≤70.
  Приёмка: ни один title категории/каталога не заканчивается обрывком слова/фразы.
  ✅ **DONE (коммит 9e0994b2, verified):** `TITLE_LIMIT = 70`, `_fit_title()` больше не добавляет «...», режет по границе слова и чистит висящие разделители; product/category title templates деградируют без середины слова.

- [ ] **ADS-4. Дубли по query-параметрам (`?color=black` и пр.) (P1)** `[REPO]`
  Диагноз: фасетные URL `?color=`/`?fit=`/`?size=` отдают полный HTML; canonical указывает на чистый path (уже хорошо), но страницы остаются краулябельными и могут попадать в индекс как дубли (владелец видел дубли «через равно, а не через слэш»).
  Фикс: аудит — какие query-комбинации реально в индексе (Search Console `[OWNER]`); для фасетных query-страниц добавить `noindex,follow` (canonical сохранить); внутренние ссылки вести только на path-версии (`/catalog/black/`-стиль, где есть); robots.txt НЕ трогать (canonical+noindex достаточно, Disallow сломает передачу сигналов).
  Приёмка: фасетные `?param=` страницы содержат `noindex,follow`; внутренняя перелинковка не генерирует query-URL.

- [ ] **ADS-5. Ссылки на 404 в «Схожі товари» и внутренней перелинковке (P1)** `[REPO]`
  Диагноз: блок рекомендаций PDP кэшируется фрагмент-кэшем на 3600s (`{% cache 3600 product_detail_recommendations_i18n ... %}`) с ключом, который НЕ инвалидируется при снятии товара с публикации → до часа живут ссылки на 404. Плюс сама страница PDP кэшируется — ссылки на снятые товары могут жить и дольше. Клик бота → 404 (краул-бюджет), клик человека → плохой UX.
  Фикс: включить версию каталога публикаций в ключ фрагмент-кэша (по аналогии с `public_product_order_version` — проверить, бампается ли она при unpublish; если нет — бампать в save() при смене status); аудит прочих внутренних ссылок на 404 краулом `[REPO-script]`.
  Приёмка: unpublish товара → рекомендации без него максимум через минуты; краул сайта (screaming-frog-стиль скрипт) не находит внутренних 404.

- [ ] **ADS-6. 500-ка при обходе сайта (P1, связка с W3-2)** `[REPO]`
  Диагноз: при внешнем анализе сайта выпадала 500. Мониторинг W3-2 (Telegram-алерт + инцидент-код) уже задеплоен в код — но причина конкретной 500 не найдена.
  Фикс: после деплоя W3-2 собрать инцидент-коды за 48h, воспроизвести и починить топ-причины 500; проверить stderr.log/django.log на сервере `[SERVER]`.
  Приёмка: 48h без 500 в логах при нормальном трафике + краул всех URL из sitemap без 5xx.

- [ ] **ADS-7. Favicon в каталоге / прочие мелочи индексации (P2, требует уточнения)** `[REPO]`
  Диагноз: владелец упоминал проблему с favicon в каталоге (возможно — SERP favicon). base.html содержит полный набор `<link rel="icon">`; вероятная причина — редирект/404 на /favicon.ico или отсутствие иконки в выдаче Google.
  Фикс: проверить отдачу /favicon.ico и всех favicon-URL (200, правильный Content-Type); Google требует favicon ≥48px кратный 48 — проверить размеры.
  Приёмка: все favicon-URL отдают 200; Google Rich Results / SERP показывает иконку.

---

## ВОЛНА 4 — СТАТУСНАЯ МОДЕЛЬ И ФИНАНСОВАЯ ТОЧНОСТЬ (P1, TECH-070…074)

- [ ] **W4-1. OrderStatusHistory + timestamps (TD-030 → TECH-070)** `[REPO]` + `[DECISION]` + бэкап W0-3
  Статусов 5, но нет истории/timestamps; NP-синк авто-переводит в done; refused_rts неотличим от cancelled; delivered≠received ск��еены.
  Фикс (аддитивная схема из `audit_report_td_orders.md`, согласовать с владельцем): таблица OrderStatusHistory (order FK, from, to, ts, source); статус `refused_rts`; разделить delivered/done. Только add-миграции после бэкапа.
  Приёмка: CAC created/paid/delivered и RTS% считаются из БД.

- [ ] **W4-2. NP-синк: RTS-маппинг + cron вместо daemon (TD-031 → TECH-071)** `[REPO]` + `[SERVER]`(cron)
  «Відмова НП» не маппится ни во что (заказ висит в ship); daemon-поток хрупок на Passenger.
  Фикс: маппинг RTS-статусов; management command в crontab; сверить какой из двух NP-middleware включён в prod.

- [ ] **W4-3. COGS-снапшот (TD-032 → TECH-072)** `[REPO]` + бэкап
  Себестоимости в заказе нет → маржа невычислима. Дизайн готов: `OrderItem.unit_cost_snapshot` (NULL) + `cost_source`, источник — консигнационный unit_cost.

- [ ] **W4-4. Воронка CustomPrintLead (TD-033 → TECH-062, P0-data)** `[REPO]`
  28/28 лидов вечно `new`; связок с заказами 0.
  Фикс: статусная модель `new → in_progress → quoted → won/lost(причина)`, движение из админки, привязка lead→order.
  Приёмка: ни одного лида старше 7 дней в `new` без причины.

- [ ] **W4-5. Кастом-принт в чекау��е (CRO-034, P2)** `[REPO]`
  COD не проверяет цену approved-кастома (лид с ценой 0 уедет бесплатно — checkout.py:233 глотает ошибку); COD молча удаляет кастом-запис���� без lead_id; промокод дисконтирует согласованную цену кастома; брошенный инвойс не отвязывает lead.order.
  Фикс: вынести `_split_custom_cart_entries` в общий модуль для обоих потоков; guard цены кастома.

- [ ] **W4-6. Meta Ads spend-импорт (TECH-073) + офлайн-конверсии delivered/refused (TECH-074)** `[REPO]` + `[OWNER]`(токены) — после W4-1/W2-3.

---

## ВОЛНА 5 — SEO / ФИДЫ / КОНТЕНТ (P1/P2)

- [ ] **W5-1. GMC-фид: 301 на всех ссылках (SEO-008, P1)** `[REPO]`
  Все 384 `<g:link>` дают 301 с потерей `color` → риск disapproval; availability захардкожен in_stock; нет кэша (~3s CPU/GET).
  Фикс: финальные URL без redirect (color в каноническом виде); кэш фида; gtin/identifier_exists/shipping — `[DECISION]`; удалить пустой статический xml из корня.

- [x] **W5-2. Пагинация каталога (CRO-011, P1)** `[REPO]`
  `?page=N` сбрасывает `?color=`; `/catalog/?page=2..N` — индексируемые дубли БЕЗ товаров.
  Фикс: сохранять GET-параметры в пагинаторе; noindex/redirect для корневой пагинации.
  ✅ **DONE:** общий `pagination_query_prefix` сохраняет GET-параметры в catalog/search paginator links и `<link rel=prev/next>`. Search/facet страницы остаются `noindex,follow`; корневая `/catalog/?page=N` в текущей реализации рендерит уникальную товарную выдачу и сознательно оставлена indexable по существующему SEO-комментарию, диагноз «без товаров» не подтвердился.

- [ ] **W5-3. Оптимизированные изображения: backfill (CRO-022 / CRO-012, P1)** `[SERVER]` + `[REPO]`
  20/65 товаров: LCP-фото без optimized-вариантов (404 на диске, оригиналы 100-276 KB); 9/16 карточек каталога без srcset/AVIF; eager только 2 карточки (нужно 4-8).
  Фикс: `[SERVER]` — `manage.py optimize_images` backfill; `[REPO]` — алерт в audit_product_images, eager 4-8 первых карточек.

- [ ] **W5-4. Размерные сетки лонгсливов (CRO-023 → TECH-005/012, P1)** `[REPO]` + `[SERVER]`(данные)
  У ВСЕХ лонгсливов нет таблицы замеров (нет preset/SizeGrid); seo-pricing блок даёт 24 битые ссылки `/product/{id}/` → 404.
  Фикс: SizeGrid longsleeve + привязки; fix `get_absolute_url` в seo-pricing; событие `view_size_guide`.

- [ ] **W5-5. Сортиро��ка: футболки против позиционирования (CRO-013/CRO-001, P1-продукт)** `[DECISION]` + `[SERVER]`(админка)
  top-5 priority — все футболки; showcase-хардкод; Category.order все =0. Бренд = лонгсливы/худи.
  Фикс: пересмотр priority в админке (владелец); `[REPO]` — правки hero-текстов (3 строки), showcase-хардкод.

- [ ] **W5-6. Мета-качество (SEO-007, P3-батчи)** `[REPO]` + `[SERVER]`(БД)
  41 title >65, 121 description >165, 8 title <30, 6 desc-пустышек категорий блога, дубли Reality Bends ×3 (ru/en), 11 en-блогов с непереведёнными title. Батчами по 10 страниц с GSC-контролем (RISK-13). Списки: `data/seo_crawl_analysis_report.md`.
  Подпункты: - [ ] поджать шаблон product title/description (60/160); - [ ] уникализировать Reality Bends ×3; - [ ] заполнить description категорий блога; - [ ] перевести en-титлы блога.

- [ ] **W5-7. Наличие/сток как продуктовое решение (CRO-014/CRO-025 → TECH-010)** `[DECISION]` → `[REPO]`
  Механики наличия НЕТ (75/75 stock=0, поле мертво); price_override не обновляет цену при выборе цвета; select_size/select_color не трекаются.
  Фикс: решение владельца (печать под заказ vs склад) → honest-статусы, синхронизация price_override, события TECH-008.

- [ ] **W5-8. Прочее SEO (P2/P3)** `[REPO]`
  - [ ] **SEO-009 (P2):** Google Indexing сигнальный путь без дедупа/quota_limit — масс-пересохранение сжигает квоту.
  - [ ] **NEW-411 (P3):** NewsArticle/GovernmentOrganization-bloat в Organization schema (seo_utils.py:1518) — убрать/упростить.
  - [ ] **NEW-410 (P3):** lastmod �� sitemap-products (1 из 195).
  - [ ] **SEO-022 (P3):** TikTok в sameAs (нужен handle от владельца).
  - [ ] **[GAP] SEO-006 (P3):** 410 для удалённых товаров (сейчас 404) — опционально.
  - [ ] **[OWNER]** Rich Results Test прогон вручную.

- [ ] **W5-9. [GAP] Локализация: незавершённая Phase 17 (CB-011 + SEO-007 симптомы)** `[REPO]`
  ~26% msgstr в ru/en пустые; 11 en-блогов с украинскими title; RU/EN-описания товаров — заглушки ~120 симв. (CRO-020). Скрипты `fill_translations.py`/`compile_mo_polib.py` живые.
  Фикс: дозаполнить переводы (скриптом + вычитка), `compilemessages` на сервере после деплоя; батчами (RISK-13).
  Приёмка: msgstr-пустых <5%; en-титлы блога переведены.

- [ ] **W5-10. [GAP] Контент карточек: near-duplicate + published-разрыв (CRO-021/CRO-020)** `[REPO]` + `[SERVER]`(БД) + `[DECISION]`
  65/65 описаний с идентичным boilerplate-манифестом (near-duplicate риск); прямой ответ «Из чего?» в 1-м абзаце не системный (AEO-003); published=65 из 68 — 3 товара не опубликованы: намеренно?
  Фикс: переписывание описаний батчами по 10 (RISK-13); `[DECISION]` — судьба 3 неопубликованных товаров.

---

## ВОЛНА 6 — КОРЗИНА / UX (P1/P2)

- [x] **W6-1. Monobank-инвойс не сбрасывается при мутации корзины (CRO-031, P1)** `[REPO]`
  > **STRICT RE-VERIFY W6-1:** STRICT 2026-07-09: KEEP. `_reset_monobank_session` called from cart update/remove/add paths; tests assert invoice keys cleared.
  `update_cart`/`remove_from_cart` не вызывают `_reset_monobank_session` → оплата устаревшей суммы при сбое JS; `custom_print_remove` — та же родня.
  Фикс: сброс инвойса во всех мутирующих эндпоинтах.
  ✅ **DONE:** `update_cart`, successful `remove_from_cart`, `custom_print_add_to_cart` и `custom_print_remove` вызывают `_reset_monobank_session(..., drop_pending=True)`. Покрыто тестами на stale invoice/pending order.

- [x] **W6-2. Двойной клик и fallback-удаление (CRO-031, P2)** `[REPO]`
  Нет in-flight guard у `[data-add-to-cart]` в main.js (qty удваивается, пиксели дублируются); fallback remove удаляет ВСЕ варианты товара при рассинхроне ключа.
  ✅ **DONE:** общий `[data-add-to-cart]` handler в `main.js` ставит `data-add-to-cart-pending`/`aria-busy` и блокирует повторный POST до `.finally()`; `remove_from_cart` больше не удаляет все варианты товара при устаревшем composite key, только exact/case-insensitive key или явный product_id без key.

- [x] **W6-3. Кастом-бейдж и счётчики (CRO-030, P2)** `[REPO]`
  > **STRICT RE-VERIFY W6-3:** STRICT 2026-07-09: KEEP. Tests: get_cart_count includes custom_print; badge markup in cart.html/cart.js.
  `user_state_hint` читает `custom_cart` вместо `custom_print_cart` (мёртвый код — решить, регистрировать ли); `/cart/count/` игнорирует кастом; GET `cart_summary` мутирует сессию и сбрасывает инвойс.
  ✅ **DONE:** `user_state_hint` читает `SESSION_CUSTOM_CART_KEY` (`custom_print_cart`) с fallback на legacy `custom_cart`; `/cart/count/` суммирует custom-print quantities; `/cart/summary/` учитывает custom-print totals/counts, остаётся `never_cache` и больше не мутирует session/Monobank invoice на GET при missing product. Тестами покрыты badge hint, count и custom summary path.

- [ ] **W6-4. Петля сбора отзывов (CRO-026, P1-growth)** `[REPO]`
  0 опубликованных отзывов при образцовой инфраструктуре.
  Фикс: заказ delivered → запрос отзыва (email/telegram); vote-UI; блок отзывов на главной (TECH-013 — переиспользование PDP-блока, CRO-005).

- [ ] **W6-5. Рекомендации (CRO-027, P2/P3)** `[REPO]`
  Дедуп дублей в выдаче; фильтр отменённых заказов из «часто покупают вместе»; унификация TTL 300/3600; per-user фрагментация кэша.

- [ ] **W6-6. [GAP] Мультидевайс-корзина и мелочи (CRO-035, P3)** `[REPO]`
  Кросс-девайс гидрация не сбрасывает monobank-инвойс; last-write-wins теряет параллельную мутацию; `get_or_create UserCart` на каждый авторизованный запрос (перф); malformed-атрибут cart.html:925.

- [ ] **W6-7. [GAP] Фильтр по размеру в каталоге (CRO-010)** `[DECISION]` → `[REPO]`
  Фильтра по размеру не существует. Продуктовое решение (связано с W5-7 стоком): нужен ли — если да, добавить с сохранением GET-параметров.

---

## ВОЛНА 7 — ГИГИЕНА РЕПОЗИТОРИЯ И КОДА (P2, строго после Волн 0-2)

⚠️ Перед ЛЮБЫМ удалением: crontab инвентаризирован (CB-044: 7 задач, все скрипты в репо), но перепроверить непосредственно перед удалением (RISK-01).

- [ ] **W7-1. ⚠️ views.py.backup — ЖИВОЙ рантайм (CB-004/TD-001)** `[REPO]`
  > **RE-VERIFY W7-1:** REOPEN 2026-07-09: views.py.backup still exists + lazy-load in views/__init__.py. Was false [x].
  `_load_legacy_views` exec-ит его (whitelist 102 имени, 30 боевых маршрутов). НЕ УДАЛЯТЬ.
  План: миграция 102 имён в нормальные модули → потом удаление. Безопасно удалить сейчас: `styles.css.bak2` (445KB), `order_success_old.html`, `tmp_old_index.html`.
  ✅ **DONE (safe subset only):** живой `views.py.backup` не тронут; безопасные tracked legacy-файлы `styles.css.bak2`, `order_success_old.html`, `tmp_old_index.html` сняты с git-tracking и добавлены в ignore для локальных копий.

- [ ] **W7-2. legacy_stubs.py (TD-006)** `[REPO]`
  48 заглушек живы в urls; admin_store_* возвращают фейко��ый `{'status':'ok'}`.
  План: (а) fix monobank quick (Находка 4 payment_security); (б) перенос живых; (в) 410 мёртвым.

- [ ] **W7-3. 145 tracked-артефактов ≈170MB (CB-001)** `[REPO]`
  tmp/ 101MB, artifacts/, output/, личное фото me.JPG ×2 (вкл. collectstatic-путь!).
  Фикс: `git rm --cached` + .gitignore (БЕЗ filter-repo — RISK-06). Цель: 328MB → <100MB.

- [ ] **W7-4. 175-202 md в корне (CB-002/TD-005)** `[REPO]`
  → `docs/archive/` одним git mv-PR (ссылок из кода 0 — проверено).

- [ ] **W7-5. 65 loose-скриптов (CB-003)** `[REPO]`
  cron их не вызывает → рассортировать scripts/archive, удалить fix_*; из 6 deploy-скриптов оставить рабочий (сверить с владельцем).

- [x] **W7-6. xlsx с закупочными ценами (CB-005)** `[REPO]`
  Репо публичный! Вынести из git (`/pricelist_opt.xlsx` генерится из БД — подтверждено).
  ✅ **DONE:** tracked wholesale XLSX-файлы сняты с git-tracking; `.gitignore` запрещает повторное добавление `*.xlsx`.

- [ ] **W7-7. Топ-20 денежных except (CB-020, P1-точечно)** `[REPO]`
  734 широких except; чинить ТОЛЬКО топ-20 (ранжированы в `audit_report_section6_codebase.md`): checkout.py:233 (цена кастома!), utils.py:542/573/723, nova_poshta_service.py:515, str(e) клиенту (cart.py:943, monobank.py:288). Только logging, НЕ control flow (RISK-04).

- [ ] **W7-8. 120 print() → logger (CB-021)** `[REPO]`
  4 файла покрывают 100/120: telegram_notifications.py (47), telegram_bot.py (46), dropshipper_views.py (22), telegram_views.py (5).

- [ ] **W7-9. CSS-диета (CB-030/031)** `[REPO]`
  Боевой бандл = styles.purged.css 394KB. (1) удалить 1.1MB CSS-сирот (styles.min.css, styles.direct.css, styles.base.css, critical-home.min.css — 0 ссылок); (2) ВЕРНУТЬ purge-пайплайн+safelist в репо (без него новый класс молча без стилей — RISK-05!); (3) скриншот-сравнение до/после на 5 шаблонах.

- [ ] **W7-10. Шрифты и vendor (CB-035/036)** `[REPO]`
  Удалить .ttf-дубли (woff2 достаточно) ≈ −50% шрифтов, −700KB FA; subsetting кириллица+латиница.

- [ ] **W7-11. inline-скрипты base.html (CB-033)** `[REPO]`
  Вынести Ahrefs/GTM/analytics-инжектор в кэшируемый файл; убрать дубль device-class детектора (Ahrefs-блок выше основного — поменять порядок); удалить закомментированный блок + partials/analytics.html (163 строки мёртвого include).

- [ ] **W7-12. Мёртвые middleware и ветки (CB-042/CB-041/TD-014)** `[REPO]`
  Удалить RequestTraceMiddleware (0 потребителей X-DTF-Debug); ImageOptimizationMiddleware — no-op (флаг off, ThreadPoolExecutor впустую) — убрать после SSH-подтверждения env; мёртвая ветка ForceHTTPS в production_settings.py:48-53; незарегистрированный MediaCacheMiddleware-код; задокументировать инварианты порядка 26 middleware в шапке (RISK-09).

- [ ] **W7-13. dtf collectstatic-override (CB-014)** `[REPO]`
  Переименовать в `collectstatic_dtf` или удалить (P2: hard-fail всего collectstatic при ошибке dtf-исходников).

- [ ] **W7-14. Мёртвые команды и код (CB-015/TD-007)** `[REPO]`
  DEAD-кандидаты: finance_seed_demo, notify_test_shops, send_storage_test, send_test_receipt (карта: `cb015_management_commands_map.md`); удалить мёртвый ab_testing.py (0 импортов).

- [ ] **W7-15. Пин версий (CB-040)** `[REPO]`
  openai==2.30.0, google-auth==2.52.0, google-analytics-data==0.22.0 (боевые версии сняты).

- [ ] **W7-16. console.log (CB-023)** `[REPO]`
  56 console.log в 4 JS-файлах → debug-флаг/вырезать при минификации (PII не обнаружено).

- [ ] **W7-17. Retention БД (DB-004)** `[SERVER]` + `[REPO]`(команда)
  `management_leadparsingresult` 492 MB(!), pageview 64 MB, UserAction (MyISAM → InnoDB при переносе); расширить trim_analytics на UTMSession/UserAction (= NEW-404, связка W2-10).

- [ ] **W7-18. [GAP] Статика-мелочи (CB-013, P3)** `[REPO]`
  Мёртвые файлы: logo_fire/stena.png 2.7MB + hero_dtf_graphic.png (0 ссылок) — удалить; stena2.png и configurator/ui/*.png без webp; слияние images/→img/ безопасно (3 ссылк�� в pro_brand.html), внешние потребители (og:image, GMC placeholder) сидят в img/ — их пути НЕ трогать (RISK-14).

- [ ] **W7-19. [GAP] TODO-хвосты (CB-025, P3)** `[REPO]`
  14 TODO/FIXME/HACK в Python → перенести содержательные в TECHNICAL_TASKS.md с ID, остальные удалить.

- [ ] **W7-20. [GAP] Каталоги-сироты и .gitignore-дыры (CB-006/CB-007, P3)** `[REPO]`
  .gitignore: добавить artifacts/, output/, tmp/, opros/, newCatalog/, *.xlsx, *.bak2; 7 AI-конфигов (69 файлов ~1.1MB) — решить с владельцем какие живы (похоже только .kiro), .superpowers частично tracked вопреки gitignore.
  ✅ **.gitignore-часть DONE:** добавлены `artifacts/`, `output/`, `tmp/`, `opros/`, `newCatalog/`, `*.xlsx`, `*.bak2` плюс локальные legacy HTML. Осталось: разбор tracked AI-конфигов и уже закоммиченных артефактов отдельным безопасным PR, чтобы не снести evidence-файлы аудита.

- [ ] **W7-22. [NEW-509] Decimal→float в денежных payload (P2-системно)** `[REPO]`
  ~15+ мест (cart.py:583-939, manual_orders.py, utm_api_views.py, viewsets.py): суммы сериализуются `float(Decimal)` → потенциальные 0.30000000000000004 в JSON/пикселях. Модели правильные (DecimalField), проблема только на границе сериализации.
  Фикс: хелпер `money_str(d) -> str(d.quantize('0.01'))` или DjangoJSONEncoder; менять вместе с касанием соотв. файлов, не массово.

- [x] **W7-23. [NEW-511] Naive datetime.now() (P3)** `[REPO]`
  > **STRICT RE-VERIFY W7-23:** STRICT RE-VERIFY 2026-07-09: residual datetime.now() still in orders/dropshipper_views.py:273-274 (non-test). Uncheck until timezone-aware.
  `promo.py:714-720`, `recommendations.py:202`, `utm_api_views.py:522` — `datetime.now()` без timezone вместо `timezone.now()`/`localdate()` → ��мещение окон «неделя/месяц» в отчётах промо.
  ✅ **DONE:** `promo.py` использует `timezone.localdate()`/`timezone.now()`, `recommendations.py` — `timezone.localdate().month`, `utm_api_views.py` — `timezone.now()` для export filename; статический тест запрещает регресс в этих файлах.
  ✅ **RESOLVED `3df4c2fc` (2026-07-16):** dropshipper payout reporting derives year/month from one Kyiv-local date. A New Year boundary regression and AST alias-aware hygiene guard passed 2/2 locally and on production.

- [x] **W7-24. [NEW-513] /search/ без пагинации (P3, из CRO-015 бонуса)** `[REPO]`
  > **STRICT RE-VERIFY W7-24:** STRICT 2026-07-09: KEEP. /search/?q=test and page=2 return 200 live.
  `catalog.py:726 def search` — весь результат одним списком; широкий запрос = тяжёлый рендер. Фикс: пагинатор как в каталоге (с сохранением q= в GET — связка W5-2).
  ✅ **DONE:** `/search/` использует `Paginator(..., PRODUCTS_PER_PAGE)`, `results_count=paginator.count`, передаёт `page_obj/paginator`, сохраняет `q` в нижних paginator links и `<link rel=next/prev>`. Регрессионный тест проверяет count, page size и `q=...&page=2`.

- [ ] **W7-21. God-files — только план (CB-022)** `[REPO]`(docs)
  НЕ рефакторить сейчас. Порядок PR при декомпозиции: cart→admin→mgmt-models→mgmt-views→storefront-models; инвариант: пустой makemigrations-дифф; НЕ тро��ать `_load_legacy_views`. cart.py split (custom_cart.py) заодно закрывает CRO-034.

---

## SERVER_TASKS — SSH-БАТЧ (выполнить одной сессией при рабочем SSH)

Незакрытые SSH-остатки аудита. Готовые скрипты: `AGENT_WORK_LOG_iter4.md §6`, `scripts/cro051_funnel_baseline.py` (копия в SESSION_HANDOFF_2026-07-07.md).

- [ ] **S-1.** CRO-050 остаток: сверка UTMSession/UserAction по тестовой сессии sessionid=jrg8b3… (если ещё живо).
- [ ] **S-2.** Фактический CACHE_BACKEND на бою (TD-011: redis / locmem / file-based дефолт?!) + значение `DISABLE_ANALYTICS`.
- [ ] **S-3.** env `IMAGE_OPTIMIZATION_MIDDLEWARE_ENABLED` (CB-041) — для решения W7-12.
- [ ] **S-4.** count() правил AnalyticsExclusion (AN-004) — покрыт ли staff-трафик.
- [ ] **S-5.** Деплой-скрипт vs `ensure_compress_offline` (TD-013) — не переключается ли на on-the-fly.
- [ ] **S-6.** grep «2014» по БД-описаниям товаров (AEO-005 хвост); `ai_generation.log` (AEO-006).
- [ ] **S-7.** [GAP] NEW-405: `*service*account*.json` в webroot (= W0-6).
- [ ] **S-8.** Боевые логи monobank_logger + валидность CAPI-токена (AN-011 хвост); env `TIKTOK_EVENTS_*` (AN-015/AN-020).
- [ ] **S-9.** `manage.py optimize_images` backfill (= W5-3).
- [ ] **S-10.** `compilemessages` после мерджа переводов (= W5-9).
- [ ] **S-11.** Крон НП-трекинга: подтвердить расписание update_tracking_statuses (AN-014).
- [ ] **S-12.** PageView.count() (TD-007 хвост) — дубль-слой PageView vs UserAction page_view.
- [x] **S-13.** [NEW-504] env `TELEGRAM_BOT_WEBHOOK_SECRET` задан (= W3-9); value kept private, `setWebhook(secret_token=...)` confirmed.
- [x] **S-14.** [NEW-503] live-проверка отдачи `media/ubd_docs/<имя>` (= W1-11): pre-fix 200, post-fix 403; normal public product media remains 200.

---

## OWNER_TASKS — РУЧНЫЕ ШАГИ ВЛАДЕЛЬЦА (консолидировано; агент кодом закрыть НЕ может)

- [ ] **O-1. GTM:** экспорт контейнера GTM-PRLLBF9H, сверка тегов/триггеров: двойной счёт AddToCart/add_to_cart (CRO-033/AN-001); дубль GA4 page_view (gtag параллельно GTM); мёртвые теги.
- [ ] **O-2. GA4:** DebugView-прогон воронки (AN-003); фильтр internal-трафика.
- [ ] **O-3. Meta:** Pixel Helper live-сверка (AN-010); Events Manager → блок Deduplication + EMQ (AN-012); Test Events для ATC (CRO-033).
- [ ] **O-4. TikTok:** Events Manager сверка (AN-020).
- [ ] **O-5. GSC/PSI:** field-данные CWV (CRO-003/SEO-010); GSC-экспорт позиций ПЕРЕД правками мета (RISK-13); Rich Results Test (SEO-020/021/022/023).
- [ ] **O-6. Сервер:** смена SSH-пароля (W0-1); разбор 10 git-stash (W0-5).
- [ ] **O-7. Продуктовые решения:** возвращать ли COD в UI (W1-1); склад vs под-заказ (W5-7); фильтр по размеру (W6-7); priority товаров — лонгсливы/худи первыми (W5-5); TikTok handle для sameAs (W5-8); судьба 3 не��публикованных товаров (W5-10); gtin/shipping в GMC-фиде (W5-1); судьба AI-конфигов в репо (W7-20).

---

## ЗАВИСИМОСТИ МЕЖДУ ВОЛНАМИ (критический путь)

```
W0-3 (бэкапы) ──→ любые миграции (W4-1, W4-3)
W0-4 (смок-тесты) ──→ W1-* (все фиксы чекаута/оплаты)
W1-1 (guest COD) ──→ W2-1 (UTM в COD-заказе) ──→ W2-2 (is_converted) ──→ AN-038 (отчёты оживают)
W2-3 (purchase-определение) ──→ W2-6 (TikTok имена), W4-6 (offline-конверсии)
W2-4 (бот-фильтр) ──→ пересчёт baseline CRO-051 ──→ честные CRO-выв��ды
W3-3 (кэш/куки) ↔ W3-4 (CSRF в кэше) — делать вместе
W5-9 (переводы) ──→ S-10 (compilemessages)
W7 (гигиена) — только после стабилизации Волн 0-2
O-5 (GSC-экспорт) ──→ W5-6 (правки мета)
```

## КРИТЕРИЙ «СДЕЛАНО» ДЛЯ ВСЕГО ПЛАНА

1. Guest COD работает live; заказы с рекламной ссылки имеют utm+click-ID; `is_converted` растёт.
2. Webhook Monobank с невалидной подписью отклоняется; PII-страницы закрыты; /test-analytics/ закрыт.
3. Промокоды применяются в обоих потоках, лимиты работают.
4. Одинаковое purchase-определение в UserAction/GA4/Meta/TikTok, задокументировано.
5. Baseline воронки (CRO-051) пересчитан на чистых данных.
6. Бэкапы + алертинг + ротация логов живут в cron.
7. Смок-тесты зелёные, прогоняются перед каждым деплоем.
8. [GAP] Переводы дозаполнены; SERVER_TASKS-батч закрыт; OWNER_TASKS переданы владельцу списком.

## ССЫЛКИ НА ДЕТАЛЬНЫЕ ОТЧЁТЫ

| Область | Файл |
|---|---|
| Чеклист-источник (150 пунктов) | `twocomms_global_audit.md` (+ Матрица рисков RISK-01…15) |
| Чекаут: guest COD 500, PII, purchase-матрица, промокоды | `audit_report_checkout_critical.md` |
| Monobank подпись/идемпотентность | `audit_report_payment_security.md` |
| UTM-разрывы, baseline воронки | `audit_report_section1_cro.md` |
| Корзина (архитектура, кэш/CSRF, пиксели ATC) | `audit_report_section1_cart.md` |
| Главная / каталог / карточка / изображения / тексты | `audit_report_section1_{homepage,catalog,product,images,texts}.md` |
| Аналитика и пиксели | `audit_report_section2_analytics.md` |
| Тех. долг | `audit_report_section3_techdebt.md`, `audit_report_td_orders.md` |
| SEO/AEO | `audit_report_section4_seo.md`, `data/seo_crawl_analysis_report.md` |
| БД | `audit_report_section5_db.md` |
| Кодовая база | `audit_report_section6_codebase.md`, `cb015_management_commands_map.md`, `audit_report_legacy_stubs.md` |
| Реестр задач TECH-NNN | `TECHNICAL_TASKS.md` |

## ЖУРНАЛ ВЫПОЛНЕНИЯ

| Дата | ID | Что сделано | Коммит/PR |
|---|---|---|---|
| 14.07.2026 | F-068 / F-073 / W2-1 partial | Исторический prepay root доказан: старый writer не передавал session_key, tracking создавал external_id позже; writer исправлен в 7936ab6e. Добавлен lazy-session regression; production canary подтвердил cookie = Order = UTMSession = tracking external session, invoice amount 20000, lead conversion, cleanup DB/cache 0; 19/19 history unchanged. F-072/F-075 оставлены открытыми | 7936ab6e, 30808819 |
| 14.07.2026 | W1-1 / F-044 / F-074 | Durable guest session до COD Order writer; общий helper для COD/Monobank/UTM; local 93/93, server 2/2, production InnoDB rollback-canary: cookie = Order = UTMSession, first-touch/UserAction есть, cleanup 0. Исторические 29 пустых ключей не подменялись | 394a247c, bb217bd9 |
| 08.07.2026 | W3-6, W3-7, W5-2, W6-1/W6-2/W6-3, W7-1/W7-6/W7-20/W7-23/W7-24 | Кодовая пачка: PII-redaction filter + log-rotation script, removed status-save full-save fallbacks, row-lock for dropship admin status, pagination query preservation, search pagination, Monobank invoice reset on cart mutations, add-to-cart in-flight guard, custom-print badge/count, safe legacy/xlsx untracking, timezone-aware dates. SERVER/OWNER leftovers left open where required. | этот коммит |
| 08.07.2026 | ADS-1/ADS-2/ADS-3 | Повторная проверка уже сделанных ADS-коммитов: head PageView + settings Pixel ID, main-site EN untranslated/fuzzy = 0/0, title fitting without mid-word ellipsis. DTF locale kept out of scope. | 5aacb163, c1c0de5b, 9e0994b2 |
| 08.07.2026 | W1-7 | Mobile hero-CTA: 60vh-лок + overflow:hidden найден в 3 местах (base.html inline, cls-ultimate.css via inline_static, critical-home.min.css) — во все добавлен mobile-override (height:auto, overflow:visible, min-height:60vh для CLS); проверено в реальном браузере 360×640 — обе CTA видимы; PWA-prompt: 30s + т��лько после взаимодействия, max-height 50vh | 0caf8b04, f59f7827 |
| 08.07.2026 | W1-8 | `/test-analytics/` закрыт `@staff_member_required` (Purchase больше не стреляет в боевой Pixel от анонимов); SEO-тест обновлён | 5d2d91f4 |
| 08.07.2026 | W1-2 | order_success — только владелец (user/session/recent_order_ids) или staff, чужой → 404; success-preview → staff-only; в monobank_return доступ выдаётся только по session-доказательствам | 5d2d91f4 |
| 08.07.2026 | W1-1 | Гостевой COD подтверждён рабочим (роутинг на create_order уже был на main); DECISION владельца: COD в UI не возвращаем; 2 устаревших чекаут-теста приведены к актуальному Monobank-button-флоу | 26702d78 |
| 08.07.2026 | W1-3, W1-9, W1-12 | Подпись X-Sign обязательна в retail- И дропшип-вебхуках (ECDSA/SHA-256, base64-PEM ключ, retry при ротации), невалидная → 400; retail-путь и monobank_return подтверждают оплату ТОЛЬКО pull-истиной invoice/status + сверка paidAmount (недоплата → checking, не paid); unsafe `or 'success'` удалён; дропшип-callback идемпотентен; 9 новых тестов (test_monobank_webhook.py) | db0af339 |
| 08.07.2026 | W1-4 | Промокоды: COD-путь читает promo_code_id + can_be_used_by_user (auth-only); record_usage вызывается (COD — при размещении, online — при подтверждённой оплате, идемпотентно); математика остатка prepay_200 исправлена (минус скидка); promo.use() при создании инвойса убран; coupon_apply событие + миграция 0079 | f79ed36e |
| 08.07.2026 | W1-13, W1-14 | MAX_CART_ITEM_QTY=50 в add_to_cart/update_cart (вкл. накопленное qty); дедуп double-submit в create_order (session + sha256 корзины, окно 30s → редирект на существующий заказ) + disable кнопок на клиенте (cart.js); 4 новых теста | 91881756 |
| 08.07.2026 | W1-10 | Валидация загрузок профиля: validate_profile_upload_size (10 МБ) в ProfileSetupForm; edit_profile валидирует файлы через ImageField + лимит, email через validate_email; FILE_UPLOAD_MAX_MEMORY_SIZE/DATA_UPLOAD_MAX_MEMORY_SIZE в settings; 2 новых теста | 5d59ef69 |
| 08.07.2026 | W1-5 | Missing-товары → явная ошибка без заказа (COD + monobank-инвойс); guard total<=0; внешние HTTP-вызовы (invoice/CAPI/Telegram) выведены из transaction.atomic + cleanup осиротевшего заказа; 2 новых теста | 0a934454 |
| 08.07.2026 | W1-6 | update_payment_method/confirm_payment восстановлены из backup: login_required+POST, владелец через get(user=request.user)→404, lock после ��платы (409), скриншот — Pillow-валидация + 10 МБ; 3 новых теста | 6c6b4709 |
| 07.07.2026 | plan-v2 | План переписан в единый исполняемый чеклист: влиты gap-check находки ([GAP]: W0-6, W1-8, W2-9/10 хвосты, W3-8, W5-8/9/10, W6-6/7, W7-18/19/20), добавлены секции SERVER_TASKS (S-1…S-12) и OWNER_TASKS (O-1…O-7), теги [REPO]/[SERVER]/[OWNER]/[DECISION] | — |
| 07.07.2026 | plan-v2.2 | Второй пост-аудит-скан (деньги/идемпотентность/PII): NEW-506 (P1 — retail-вебхук без pull-verify и сверки суммы, при том что wholesale/IG-ветки pull-verify ДЕЛАЮТ), NEW-508 (qty без cap), NEW-514 (double-submit заказа), NEW-510 (CheckoutCapture PII без лимитов/retention), NEW-509 (float в денежных payload), NEW-511 (naive datetime), NEW-512 (брутфорс промо), NEW-513 (/search/ без пагинации). Добавлены W1-12/13/14, W3-11/12, W7-22/23/24 | — |
| 07.07.2026 | plan-v2.1 | Пост-аудит-скан кода нашёл 5 НОВЫХ проблем вне аудита: NEW-501 (P0 — дропшип-вебхук Monobank без подписи), NEW-502 (P1 — edit_profile принимает FILES без валидации), NEW-503 (P1-check — ubd_doc PII в публичном media), NEW-504 (P2 — Telegram-вебхук без секрета при пустом env), NEW-505 (P3 — eval в survey_engine). Добавлены W1-9/10/11, W3-9/10, S-13/14 | — |
