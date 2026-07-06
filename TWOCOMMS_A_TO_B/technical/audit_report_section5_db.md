# Аудит. Раздел 5 — База данных MySQL и целостность

Журнал код-слойного аудита пунктов DB-002, DB-005, DB-008, DB-009, DB-010.
Дата: 07.07.2026. Агент: v0 (сессия продолжения глобального аудита).
Серверные пункты (DB-001, DB-003, DB-004, DB-006, DB-007) требуют SSH — на момент
аудита SSH к 195.191.24.169 сбрасывается (`Connection reset by peer`, вероятно fail2ban);
их остатки перечислены в конце файла.

---

## DB-002. Индексы под аналитические запросы (код-слой) — ЗАКРЫТО 07.07.2026

### Что проверено

`storefront/models.py`: Meta.indexes и db_index у `UserAction` (строки 2036–2123),
`UTMSession` (1901–2033), `SiteSession` (1672–1713), `PageView` (1716–1732).
Сопоставлено с реальными фильтрами отчётных запросов в
`storefront/services/admin_analytics.py` (строки 461–585) и `storefront/utm_analytics.py`.

### Текущее покрытие (подтверждено)

| Модель | Индексы |
|---|---|
| UserAction | `(action_type, -timestamp)` idx_action_type_time; `(utm_session, action_type)` idx_action_utm_type; `(product_id, -timestamp)` idx_action_product; `(order_id)` idx_action_order; db_index на action_type, product_id, order_id, timestamp; FK-индексы utm_session/site_session автоматически |
| UTMSession | `(utm_source, utm_medium, utm_campaign)`; `(-first_seen)`; `(is_converted, -converted_at)`; `(country, city)`; `(device_type, os_name)`; `(is_returning_visitor, -first_seen)`; `(user_registered, -first_seen)` + ~20 одиночных db_index=True почти на каждом поле |
| SiteSession | session_key unique; `(is_bot, -last_seen)` idx_session_bot_seen; db_index на visitor_id, is_bot, first_seen, last_seen |
| PageView | `(is_bot, -when)` idx_pageview_bot_when; db_index на when, is_bot |

### Находки

1. **P2 — у UserAction нет собственного флага is_bot и нет индекса под бот-фильтр.**
   Основной бот-фильтр отчётов — `admin_analytics.py:551`:
   `qs.filter(Q(site_session__is_bot=False) | Q(site_session__isnull=True))` — это JOIN
   UserAction→SiteSession на каждый отчётный запрос по 36k+ строк. OR с isnull
   ломает использование индекса JOIN-таблицы (MySQL не может index_merge через OR по
   разным таблицам) → фактически full scan UserAction + lookup в SiteSession.
   Рекомендация исполнителю: либо денормализовать `is_bot` в UserAction
   (заполнять при записи, составной индекс `(is_bot, action_type, timestamp)`),
   либо переписать OR через UNION/два запроса.
2. **P2 — нет составного индекса `(site_session, action_type)`.** Воронка по
   уникальным сессиям (distinct site_session_id по action_type) использует
   FK-индекс site_session_id + отдельный action_type — покрывающего индекса нет.
3. **P3 — индексная избыточность UTMSession.** Почти каждое поле имеет db_index=True
   (utm_content, utm_term, fbclid, gclid, ttclid, ip_address, country, city,
   device_type, os_name, browser_name, visit_count, is_first_visit, ... — 20+ индексов)
   плюс 7 составных. На shared-MySQL это write-amplification на каждый INSERT/UPDATE
   UTMSession и раздувание InnoDB. Рекомендация: снять db_index с полей, по которым
   нет WHERE в отчётах (visit_count, is_first_visit, browser_name, os_name одиночный,
   ip_address — проверить фактические запросы), оставить составные.
4. **P3 — session_key покрыт**: UTMSession.session_key unique+db_index,
   SiteSession.session_key unique — требование чеклиста «session_key везде indexed» выполнено.
5. `Order.utm_source` (orders/models.py:118) имеет db_index — прямые срезы по заказам
   не требуют JOIN к UTMSession. OK.

### Остаток (SSH)

EXPLAIN ключевых отчётных запросов на живой MySQL (после DB-001) — подтвердить
гипотезу п.1 планом запроса.

---

## DB-005. N+1 в админ-отчётах — ЗАКРЫТО 07.07.2026

### Что проверено

`storefront/services/admin_analytics.py` (1703 строки), `storefront/utm_cohort_analysis.py` (474 строки).

### Находки

1. **admin_analytics.py — в целом ХОРОШО.** Все базовые queryset'ы строятся с
   `select_related`/`prefetch_related`/`annotate` (строки 461, 496, 516, 535, 561, 575, 581):
   SiteSession с utm_data, Order с utm_session+items+product, UserAction с
   site_session+utm_session, Exists-подзапрос для human pageview (468–475). N+1 в
   основной сборке дашборда не обнаружен.
2. **P2 — utm_cohort_analysis.py: классический N+1 в трёх местах:**
   - `cohort_retention` (строки 102–186): вложенный цикл `for cohort_key in cohorts:
     for period in range(max_periods):` — внутри КАЖДОЙ итерации 2–3 отдельных запроса
     (`UTMSession...count()` стр.130–134, `Order.objects.filter(...)` стр.141, 154–158, 165).
     При 12 когортах × 12 периодов это ~300–400 запросов на один рендер отчёта.
     Фикс: одна агрегация с `TruncMonth`+`annotate`+GROUP BY.
   - `repeat_purchase` (строки 329–360): `for customer in customers_data:` — по КАЖДОМУ
     покупателю отдельный `orders_qs.filter(...)` (стр.341) + python-цикл по заказам.
     При росте базы это линейный N+1. Фикс: annotate(Count/Min/Max) + window functions
     или одна выборка с сортировкой.
   - `campaign_variants` (строки 407–425): `for variant in variants_data:` — по каждому
     utm_content отдельный `Order.objects.filter(...)` (стр.415). Фикс: values(utm_content)
     + annotate.
   - Также стр.87: `for session in sessions_qs.iterator()` — построчный python-проход
     по всем UTMSession для раскладки в когорты; заменяется на TruncMonth-агрегацию.
3. **P3** — `repeat_purchase` считает `total_customers = customers_data.count()` (стр.329)
   и затем `sum(1 for item in customers_data ...)` (стр.330) — двойная материализация
   одного queryset.

Вердикт: N+1 сконцентрирован в utm_cohort_analysis.py; admin_analytics.py чистый.
Проверка «< 3s» — серверная (после DB-001).

---

## DB-008. PyMySQL vs mysqlclient — ЗАКРЫТО 07.07.2026

### Факты

- `requirements.txt:11-12`: `# База данных - MySQL (альтернатива mysqlclient)` → `PyMySQL==1.1.2`.
- `twocomms/settings.py:16,21` и `twocomms/production_settings.py:58,61`:
  `pymysql.install_as_MySQLdb()` — активен и в dev, и в prod.
- ENGINE везде `django.db.backends.mysql` через shim PyMySQL.
- mysqlclient в requirements отсутствует; ни одного упоминания попытки сборки C-драйвера.

### Вердикт

Выбор PyMySQL — **осознанный компромисс под shared-хостинг Hostsila** (нет прав/toolchain
для сборки mysqlclient; pure-python ставится без компилятора). Это задокументировано
только косвенно (комментарий «альтернатива mysqlclient»). PyMySQL медленнее на
CPU-bound парсинге результатов (~2–4x на больших выборках), что усугубляет тяжёлые
отчётные запросы из DB-005.

Рекомендация исполнителю (P3): зафиксировать выбор явным комментарием в requirements.txt
и settings; при переезде с shared-хостинга — заменить на mysqlclient (изменения: удалить
pymysql+install_as_MySQLdb, добавить mysqlclient — API совместим). Отдельно отмечено:
в settings/production_settings уже есть продуманный фикс CONN_MAX_AGE под
`wait_timeout=60` shared-хоста (комментарии 2026-06-04) — при смене драйвера не потерять.

---

## DB-009. Транзакционность заказа — ЗАКРЫТО 07.07.2026

### Что проверено

`storefront/views/checkout.py::create_order` (строки 39–290),
`storefront/views/monobank.py` (atomic-блок со строки 539 до ~строки 900+),
`orders/models.py`, `orders/status_management.py`, `storefront/views/utils.py`.

### Находки

1. **Атомарность создания Order+OrderItem — ЕСТЬ.** Оба пути (COD-чекаут
   checkout.py:149 и Monobank monobank.py:539) оборачивают создание Order,
   bulk_create(OrderItem), привязку custom-print leads и пересчёт total_sum в
   `transaction.atomic()`. OK.
2. **«Снятие остатков» НЕ СУЩЕСТВУЕТ как механика.** У Product/ProductColorVariant
   нет полей остатков (grep по storefront/models.py: quantity есть только у
   StoreProduct/оффлайн-магазинов и опт-моделей). Ни checkout.py, ни monobank.py
   не декрементируют никаких остатков. Следствие: сценарий чеклиста «конкурентный
   заказ последнего размера уводит остаток в минус» неприменим — овersell не
   контролируется вообще (модель «всё всегда в наличии» / производство под заказ).
   **P2-решение для владельца:** если физические остатки существуют — нужна модель
   stock + `select_for_update` при снятии; если под заказ — зафиксировать это
   в доках как осознанную модель.
3. **P1 — внешний HTTP-вызов Monobank ВНУТРИ transaction.atomic().**
   `monobank.py`: блок atomic открывается на строке 539, а
   `_monobank_api_request('POST', '/api/merchant/invoice/create', ...)` выполняется
   на строке ~843 — всё ещё внутри atomic (отступ подтверждён). DB-транзакция и
   соединение держатся открытыми на время сетевого вызова к Monobank (таймауты/ретраи).
   На shared-MySQL с `wait_timeout=60` это прямой риск «MySQL server has gone away»
   внутри транзакции и подвисших блокировок. Тот же анти-паттерн, что AN-011
   (Meta/TikTok внутри atomic в orders/monobank utils.py). Фикс: закрыть транзакцию
   после создания Order/OrderItems, invoice-вызов делать вне её; при ошибке —
   компенсирующее удаление/пометка заказа (сейчас `order.delete()` на стр. ~848 и
   ~861 выполняется внутри той же транзакции — при переносе учесть).
4. **P2 — PromoCode: usage не инкрементируется.** checkout.py:246 — комментарий
   `# Increment usage? (Maybe later)`; промокод применяется без учёта лимитов
   использования и без `select_for_update` — один код можно применять безгранично
   и конкурентно.
5. **P3 — аналитические записи внутри транзакции.** `record_initiate_checkout` /
   `record_order_action` (checkout.py:270–277, monobank.py:566) пишут UserAction
   внутри atomic — при откате теряются вместе с заказом (это скорее корректно),
   но удлиняют транзакцию.
6. Cross-ref: IDOR `/orders/success/<id>/` и `/orders/success-preview/` (PII любого
   заказа без проверки владельца) уже зафиксированы в
   `audit_report_checkout_critical.md` (Находка 2) — здесь не дублируются.
7. Позитив: `orders/status_management.py:154` и `orders/nova_poshta_service.py:396`
   используют `select_for_update()` при смене статуса заказа — конкурентные смены
   статуса защищены.

---

## DB-010. Дропшип/опт-контуры — ЗАКРЫТО 07.07.2026

### Что проверено

`orders/models.py` (DropshipperOrder ~576–930, DropshipperStats, DropshipperPayout),
`orders/dropshipper_views.py` (1300+ строк), `orders/wholesale_signals.py`.

### Находки

1. **Разметка дропшип-заказов — КОРРЕКТНА по архитектуре.** DropshipperOrder —
   ОТДЕЛЬНАЯ модель (не Order), со своими OrderItem-аналогами, статусами, индексами
   (dropship_ord_created/status/payment). Retail-аналитика (admin_analytics,
   utm_analytics) работает только с Order → дропшип НЕ загрязняет retail-воронку. OK.
2. **Целостность выплат/статистики.** `DropshipperStats.get_or_create` при апдейте
   заказа (models.py:719); `DropshipperOrder.calculate_dropshipper_payment()`
   (676–691) — детерминированный расчёт от pay_type. Пересчёт статистики идёт
   агрегацией по заказам дропшиппера (models.py:884). Wholesale: сигналы
   `orders/wholesale_signals.py` (pre_save/post_save на WholesaleInvoice) начисляют
   комиссию менеджеру при оплате инвойса — идемпотентность обеспечена проверкой
   смены payment-статуса в pre_save-трекере.
3. **P2 — dropshipper_views.py:1063 и 1302:** admin-эндпоинт
   `admin_update_dropship_status` защищён `@login_required` + ручной `is_staff`
   (строки 1293–1298) — OK; но заказ берётся `get_object_or_404(DropshipperOrder,
   id=order_id)` без select_for_update при смене статуса/payment_status — гонка
   между админом и monobank-вебхуком дропшипа (стр. 1227) возможна (перезапись
   payment_status). Рекомендация: select_for_update по образцу
   orders/status_management.py.
4. **P3 — dropshipper-эндпоинты пользователя** корректно скоупятся
   `dropshipper=request.user` (241, 244, 332, 881, 1063, 1269) — IDOR между
   дропшипперами не обнаружен.
5. Мимоходом (для DB-009/аналитики): `DropshipperOrder.order_source` — свободный текст,
   `Order.sale_source` тоже (models.py:43 комментарий) — унификация значений на
   усмотрение владельца (не блокер).

---

## Остатки раздела 5 (требуют SSH / живой БД)

- DB-001: slow query log / включён ли; топ-10 медленных.
- DB-002 (хвост): EXPLAIN отчётных запросов.
- DB-003: доля заказов с utm_session; осиротевшие UserAction.order_id.
- DB-004: SHOW TABLE STATUS по UserAction; политика retention.
- DB-006: SHOW CREATE TABLE — utf8mb4.
- DB-007: makemigrations --check + showmigrations на сервере.
