# SESSION HANDOFF — 2026-07-07 (агент v0, ветка audit-checklist-analysis)

Документ для следующего агента. Здесь зафиксировано, на чём остановилась текущая сессия
работы по чеклисту `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`.

---

## 1. Общий статус

- Рабочая ветка: `audit-checklist-analysis` (смерджена в `main` в конце этой сессии).
- В чеклисте остаётся **54 незакрытых пункта** (`- [ ]`), см. `grep -c '^- \[ \]' twocomms_global_audit.md`.
- В этой сессии шла работа над блоком аналитики: **CRO-051** (baseline воронки),
  **AN-011 / AN-012** (аудит Facebook CAPI и TikTok Events API), плюс обзор UTM-аналитики
  админки (utm_analytics.py / utm_api_views.py).
- НИ ОДИН из этих пунктов ещё НЕ отмечен как выполненный в чеклисте — работа не завершена.

## 2. Блокер: SSH к продакшн-серверу

- Сервер: `qlknpodo@195.191.24.169` (пароль тот же, что использовался ранее в проекте).
- Путь проекта на сервере: `/home/qlknpodo/TWC/TwoComms_Site/twocomms`
- Virtualenv: `source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate`
- **Проблема:** в конце сессии SSH стабильно отваливается с
  `kex_exchange_identification: read: Connection reset by peer`.
  Похоже на fail2ban / rate-limit после нескольких подряд подключений.
  Ранее в этой же сессии подключение работало. Рекомендация: подождать 15–30 минут
  и пробовать снова, НЕ делать частых повторных попыток.

## 3. CRO-051 — baseline воронки (НЕ ЗАВЕРШЕНО)

Подготовлен готовый measurement-скрипт для `python manage.py shell` на сервере.
Он лежал в сандбоксе как `/tmp/cro051.py` (сандбокс эфемерный), поэтому полная копия — ниже.
Скрипт считает:

1. Тоталы UserAction по action_type.
2. Воронку: page_view → product_view → add_to_cart → initiate_checkout → lead → purchase.
3. Характеристику product_view (доля без site_session/utm_session/user, дедуп по парам session+product).
4. Оценку ботов по User-Agent привязанных SiteSession (+ поле is_bot).
5. Помесячную воронку (8 окон по 30 дней).
6. Воронку по уникальным сессиям (distinct site_session_id).
7. Топ-10 товаров по просмотрам.
8. Остаток CRO-050: проверка UTM-сессий с utm_source='audit' и связанных действий.

Как запустить (когда SSH снова заработает):

```bash
sshpass -p '<PASSWORD>' ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 \
  "bash -lc 'source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && cd /home/qlknpodo/TWC/TwoComms_Site/twocomms && python manage.py shell'" \
  < TWOCOMMS_A_TO_B/technical/scripts/cro051_funnel_baseline.py
```

Вывод — JSON между маркерами `===CRO051_JSON_START===` / `===CRO051_JSON_END===`.
Результат нужно вписать в отчёт и закрыть CRO-051 (и хвост CRO-050) в чеклисте.

Скрипт сохранён в репо: `TWOCOMMS_A_TO_B/technical/scripts/cro051_funnel_baseline.py`.

## 4. AN-011 — Facebook Conversions API (код изучен, отчёт не записан)

Изученные файлы:
- `twocomms/orders/facebook_conversions_service.py` — сервис CAPI: retry с backoff,
  test_event_code, hashing PII. Смотреть `_send_request_with_retry`, настройки
  `FACEBOOK_CONVERSIONS_API_TOKEN` / `FACEBOOK_PIXEL_ID` в settings/production_settings.
- `twocomms/orders/models.py` (~строки 234–250) — генераторы event_id
  (`get_purchase_event_id` и т.п.) для дедупликации pixel+CAPI.
- `twocomms/storefront/views/utils.py` (~строки 600–732) — серверные вызовы отправки событий.
- `twocomms/orders/nova_poshta_service.py` (~строки 480–525) — отправка purchase при
  подтверждении через НП.
- `twocomms_django_theme/templates/pages/order_success.html` (~строки 2090–2160) —
  клиентский fbq с event_id и гейтинг отправки purchase (shouldSendPurchase / isPaid).

Осталось: свести находки в отчётный раздел (по образцу `audit_report_section2_analytics*.md`),
зафиксировать риски/несоответствия, отметить AN-011 в чеклисте.

## 5. AN-012 — TikTok Events API (код изучен, отчёт не записан)

Изученные файлы:
- `twocomms/orders/tiktok_events_service.py` (весь файл, ~308 строк) — серверные события.
- `twocomms_django_theme/static/js/analytics-loader.js` — клиентский ttq:
  `buildTikTokPayload`, маппинг событий (CompletePayment / PlaceAnOrder), event_id.
- `twocomms_django_theme/templates/base.html` — загрузка пикселя TikTok
  (pixel id вида `D43L7DBC77UA61AHLTVG`, проверить источник — hardcode vs settings).
- `order_success.html` — есть ли клиентский CompletePayment и совпадает ли event_id
  с серверным для дедупликации.

Осталось: то же — оформить выводы в отчёт и закрыть пункт.

## 6. UTM-аналитика админки (обзор начат)

Изученные файлы:
- `storefront/utm_analytics.py` (смотрели ~строки 68–191, 303–365 — funnel_stats и агрегации).
- `storefront/utm_api_views.py` (~строки 20–66 + список def/декораторов) — проверка
  auth-декораторов (is_staff) на API и CSV-экспорте.
- `storefront/views/admin.py` (~строки 2790–3060) — admin-панель/дашборд UTM.
- `storefront/management/commands/send_utm_report.py` — команда отчёта.
- Роутинг: `storefront/urls.py` (пути `utm*`), шаблон `admin_panel.html`.

Осталось: довести до конца проверку авторизации всех UTM endpoint'ов и качество запросов
(N+1, отсутствие фильтра ботов в funnel_stats), записать в отчёт.

## 7. Рекомендуемый порядок для следующего агента

1. Дождаться доступности SSH, прогнать `scripts/cro051_funnel_baseline.py`, записать JSON-результат.
2. Оформить разделы отчёта по AN-011 / AN-012 на основе файлов из п.4–5.
3. Завершить обзор UTM-аналитики (п.6).
4. Проставить `[x]` в `twocomms_global_audit.md` только по фактически закрытым пунктам.
5. Продолжать по оставшимся 54 незакрытым пунктам чеклиста.
