# AGENT WORK LOG — итерация 3 (продолжение глобального аудита)

**Назначение файла:** живой лог агента-аудитора для передачи контекста следующему ИИ-агенту, если лимиты закончатся посреди работы. Здесь фиксируется: что сделано, что увидено, какие выводы, что осталось.

**Дата начала:** 06.07.2026
**Задача (от владельца):** выполнять невыполненные пункты `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md` (на старте: 104 не сделано `[ ]`, 44 сделано `[x]`). НИЧЕГО не менять в коде — только анализ и детальные отчёты в md-файлы этой папки. После каждого пункта: дополнить md-отчёт раздела → поставить `[x]` в чек-листе → commit + push в main.

---

## СРЕДА И ДОСТУПЫ (проверено 06.07.2026)

1. **Git:** ветка v0 `v0/viyivo9988-6838-5863b323`, HEAD == origin/main (`1dc95dea`). Fetch main настроен (`git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`). Пуш в main: `git push origin HEAD:main`.
2. **SSH к серверу:** `sshpass` установлен (`sudo dnf install sshpass`). Команда: `sshpass -p '<пароль у владельца>' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 "bash -lc '...'"`. ⚠️ Сервер агрессивно сбрасывает соединения (`kex_exchange_identification: reset`) — БАТЧИТЬ все команды в 1 сессию, между попытками пауза 60+ секунд. Первая попытка 06.07 — reset, ретраить.
3. **БД:** боевая MySQL только на сервере; доступ через `source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && cd /home/qlknpodo/TWC/TwoComms_Site/twocomms && python manage.py shell`. Только read-only.
4. **Секреты НЕ коммитить** (правило 3 чек-листа). Пароль SSH есть в постановке задачи владельца — в файлы репо не записывать.

## ПОРЯДОК РАБОТЫ (принятый план)

Порядок: сначала пункты, выполнимые статически по репо (TD-00x, CB-xxx, SEO-код, AN-код), параллельно ретраи SSH для БД-пунктов (CRO-051, DB-xxx, AN-032/033/039, TD-016 и т.д.). Пункты, требующие доступов владельца (GTM-интерфейс AN-001, Meta Events Manager AN-010/012, GA4 DebugView AN-003), закрываются частично кодовым анализом + пометка «остаток — ручной шаг владельца».

CRO-050 (сквозной тест-прогон с тестовым заказом): создание заказа = запись в боевую БД + Telegram-уведомление владельцу. Решение: прогнать воронку до чекаута включительно БЕЗ сабмита заказа + доказать разрыв трассировкой кода; сабмит тестового заказа — только по явному согласованию владельца (правило 4 чек-листа).

## ЖУРНАЛ ДЕЙСТВИЙ (обновляется постоянно)

- [06.07 // setup] Прочитан чек-лист (421 строка), подсчитано 104 невыполненных пункта. Существующие отчёты в папке: audit_report_section1_* (homepage/catalog/product/cart/images/texts/tracking/cro), audit_report_section2_analytics.md, audit_report_section3_techdebt.md, audit_report_section6_codebase.md, audit_report_checkout_critical.md, audit_report_legacy_stubs.md, audit_report_payment_security.md. Правило: ДОПОЛНЯТЬ существующие файлы разделов, не плодить новые.
- [06.07 // setup] Установлен sshpass; git main зафетчен; HEAD==main; первая SSH-попытка — connection reset (rate-limit), нужен ретрай с паузой.

## СТАТУС ПУНКТОВ ИТЕРАЦИИ 3

(сюда записывается каждый закрытый пункт: ID → краткий вывод → файл отчёта)

- **TD-001 ✅** — views.py.backup НЕЛЬЗЯ удалять: исполняется в рантайме (`_load_legacy_views`, whitelist 102 имени, 30 маршрутов `_legacy_view`). Отчёт: audit_report_section3_techdebt.md.
- **TD-002 ✅** — order_success_old.html: 0 ссылок, удалить безопасно.
- **TD-003 ✅** — НАЙДЕН P2-БАГ: `orders/telegram_notifications.py:18` импортирует `send_telegram_notification_task` из storefront.tasks, где символа НЕТ → всегда None → send_admin_message/send_personal_message всегда синхронные (requests.post в потоке запроса). Плюс сигнатуры несовместимы с orders/tasks.py-версией. ai_signals через shim = синхронная AI-генерация при сохранении товара в админке.
- **TD-004 ✅** — инвентаризация мусора twocomms/: Ideas/ ~150 md, Promt/ с PDF, _audit/ 24 файла, 2 xlsx с оптовыми ценами (P2 — публичный репо), tmp/feeds/feeds_dirty.flag — РАБОЧИЙ (не удалять, gitignore).
- **TD-005 ✅** — 202 md в корне подтверждено; план git mv → docs/archive.
- **TD-006 ✅** — 48 stubs, admin_store_* дают фейковый ok; объединить с TD-001; monobank quick CRITICAL уже в audit_report_legacy_stubs.md.
- **TD-007 ✅** — карта аналитики: tracking.py (PageView/SiteSession, 2 middleware), utm_tracking (UserAction/UTMSession), ai_signals — активны; **ab_testing.py мёртв (0 импортов)**. Остаток: PageView.count() по SSH.

- **AN-004 ✅** — серверный слой исключений (AnalyticsExclusion, 5 типов правил) покрывает все 3 писателя (tracking.py:161, utm_middleware.py:67, record_user_action:51), но is_staff НЕ авто-исключается и клиентские пиксели не покрыты. Отчёт: audit_report_section2_analytics.md.
- **AN-021 ✅** — ttclid доезжает до TikTok CAPI только через `order.payment_payload['tracking']`, который пишется ТОЛЬКО в monobank.py:972 (tracking_context строится в строках 871–966: fbp/fbc/ttclid cookies + IP + UA). COD (`checkout.py create_order`) tracking не пишет. `UTMSession.ttclid` сервисами не читается.
- **AN-030 ✅** — link_order_to_utm (utm_tracking.py:330) линкует строго по UTMSession(session_key); session['utm_data'] переживает логин, но не используется как fallback. Вызовы: checkout.py:179, monobank.py:584.
- **AN-036 ✅** — increment_visit в ОБЕИХ ветках (else-ветка utm_middleware.py:117–128 и update-ветка ~:215) = SELECT+UPDATE каждый pageview; visit_count = pageviews.
- **AN-037 ✅** — session-слой last touch (перезапись :103), UTMSession first touch (get_or_create defaults); заказы фактически first-touch.
- **AN-039 ✅** (код-слой) — record_search (utm_tracking.py:226) пишет сырой query; фикс: truncate+маска. Остаток: SSH-выборка 181 записи.

### Незакрытые хвосты для SSH-сеанса (батчить в 1 подключение!)
1. `PageView.objects.count()` (TD-007), `UserAction` by type (CRO-051 базовая линия).
2. `AnalyticsExclusion.objects.count()` + список правил (AN-004 остаток).
3. Выборка UserAction search-запросов на PII (AN-039 остаток): `UserAction.objects.filter(action_type='search').values_list('metadata', flat=True)[:50]`.
4. Удаление celery.log НЕ делать (мы read-only) — только зафиксировано в отчёте.
5. SSH дважды сброшен 06.07 (kex reset) — пауза 45s не помогла; пробовать паузы 3–5 мин, все запросы в одном heredoc.

## ЧТО ДЕЛАТЬ СЛЕДУЮЩЕМУ АГЕНТУ

1. Открыть этот файл + `twocomms_global_audit.md`, найти первый `[ ]`.
2. Проверить раздел «СТАТУС ПУНКТОВ» — не начат ли пункт наполовину.
3. Продолжать по тому же циклу: анализ → дополнить md раздела → `[x]` → commit+push в main.
