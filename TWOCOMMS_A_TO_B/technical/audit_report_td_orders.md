# Аудит TD-030/031/032 — статусная модель заказа, НП-синк, COGS (код-слой)

Дата: 07.07.2026. Метод: статический аудит кода (SSH-проверки живой БД — отдельный остаток).
Файлы: `orders/models.py`, `orders/status_management.py`, `orders/nova_poshta_service.py`,
`orders/nova_poshta_middleware.py`, `orders/management/commands/update_tracking_statuses.py`, `finance/*`.

---

## TD-030. Статусная модель заказа

### Факт (код против гипотезы чеклиста)

Гипотеза чеклиста «только done/cancelled» относится к ДАННЫМ в БД, а не к модели.
В коде статусов пять (`orders/models.py:11-17`):

| Код | Значение |
|---|---|
| `new` | В обробці (default) |
| `prep` | Готується до відправлення |
| `ship` | Відправлено |
| `done` | Отримано |
| `cancelled` | Скасовано |

Плюс отдельная ось оплаты `payment_status` (`unpaid / checking / prepaid / paid` + legacy `partial`,
строки 28-35) — т.е. «deposit_paid» из целевой модели фактически покрыт `payment_status='prepaid'`.

### Почему в БД только done/cancelled

НП-синк (`nova_poshta_service.py:436-442`) при получении статуса «доставлено» автоматически
переводит заказ в `done`. Промежуточные `new/prep/ship` — транзитные и живут часы/дни;
на выборке старых заказов видны только терминальные состояния. Это НЕ баг модели, а
следствие отсутствия истории.

### Разрывы против целевой модели TECH-070 (created → deposit_paid → paid → shipped → delivered / refused_rts / cancelled)

1. **P1 — нет истории смен статусов.** Ни `OrderStatusHistory`-модели, ни timestamps
   на каждую смену (grep по `StatusHistory|status_history|OrderStatusLog` — пусто).
   Есть только `created`/`updated` (auto_now) и `shipment_status_updated` (только НП-ось).
   Без истории невозможны: SLA-отчёты (время до отправки), когортная точность AN-014,
   ретроспектива RTS.
2. **P2 — нет `refused_rts`.** Отказ при получении сейчас неотличим от `cancelled`
   (через `cancellation_reason`) либо заказ вечно висит в `ship`.
3. **P2 — `delivered` и `done` склеены.** НП «доставлено в отделение» ≠ «получено клиентом»;
   сервис пишет `done` сразу (строка 438), т.е. факт получения не подтверждается.
4. Позитив: `status_management.py` — единая точка смены статуса с transaction.atomic,
   select_for_update, валидацией причин отмены и идемпотентной выдачей/снятием баллов
   (`POINTS_AWARDING_STATUSES`, `points_awarded`-флаг).

### Рекомендация (НЕ внедрено, по ТЗ — только согласование)

Аддитивная миграция: (а) новая таблица `OrderStatusHistory(order, old_status, new_status,
source[admin/telegram/np_sync/webhook], created_at)`, заполняется в
`status_management.apply_status_change` — единственной точке входа; (б) новые choices
`delivered`, `refused_rts` добавить БЕЗ удаления существующих; маппинг НП-кодов:
9/10 → `delivered`, 102/103 (відмова) → `refused_rts`. Без drop/rename (RISK-07).

---

## TD-031. Нова Пошта API-синк статусов

### Факт: трекинг по ТТН СУЩЕСТВУЕТ и развит

- `nova_poshta_service.py` (954 стр.): `get_tracking_info(ttn)` через официальный
  `TrackingDocument.getStatusDocuments` (строки 108-152), `update_order_tracking_status(order)`,
  `update_all_tracking_statuses()` — массовый обход заказов с ТТН не в `done/cancelled`
  (строки 829-848).
- Management command `orders/management/commands/update_tracking_statuses.py` — для cron.
- **Fallback-механизм без cron**: `nova_poshta_middleware.py` — по HTTP-запросам
  запускает обновление в daemon-потоке с cache-lock (строки 100-155) +
  `close_old_connections()` против «MySQL server has gone away» (учтён wait_timeout=60).
  Есть и Simple-версия (синхронная, раз в N запросов) для хостинга без threading.
- Автоперевод в `done` при доставке + телеграм-уведомление (строки 436-548).

Вывод: TECH-071 в части «авто-simulate shipped/delivered» уже реализован. Остаётся
доработка под новую статусную модель (delivered/refused_rts, см. TD-030).

### Замечания

1. **P2 — RTS не обрабатывается.** Обрабатывается только «доставлено» → `done`;
   коды возврата/відмови НП не маппятся ни во что — заказ зависает.
2. **P2 — daemon-поток на shared-хостинге (Passenger) хрупок**: воркер может быть
   убит при рестарте процесса посреди массового обхода; command + внешний cron надёжнее
   (проверить наличие crontab — остаток SSH).
3. P3 — какой middleware реально включён в `MIDDLEWARE` prod-настроек — проверить
   production_settings (threading vs simple), и не дублируется ли с cron.

---

## TD-032. COGS-снапшот в заказе

### Факт: снапшота себестоимости НЕТ

`OrderItem` (`orders/models.py:338-351`): `title, size, qty, unit_price, line_total,
fit_option_*, is_custom` — цена продажи снапшотится, себестоимость — нет.
У `Product`/`ProductColorVariant` полей себестоимости также нет.

В `finance/` себестоимость существует только в консигнационном контуре:
`models_consignment.py:193` — `unit_cost` на консигнационной позиции
(остаток = `(qty - sold_qty) * unit_cost`). Связи «OrderItem → unit_cost на момент
заказа» нет; маржа по retail-заказам невычислима ретроспективно (цены закупки меняются).

### Рекомендация (TECH-072, аддитивно)

1. `OrderItem.unit_cost_snapshot = DecimalField(null=True)` + `cost_source`
   (consignment/manual/estimate) — NULL для истории, заполняется с даты внедрения.
2. Источник: consignment `unit_cost` по product/variant на момент заказа; fallback —
   справочник закупочных цен (создать в finance) или ручной ввод.
3. Бэкфил истории — опционально по средним закупкам, помечать `cost_source='estimate'`.

---

## Сводка приоритетов

| # | Наход | Приоритет |
|---|---|---|
| 1 | Нет OrderStatusHistory / timestamps смен статусов | P1 |
| 2 | RTS/відмова НП не маппится (заказ зависает в ship) | P2 |
| 3 | delivered и done склеены (нет факта получения) | P2 |
| 4 | Daemon-thread fallback хрупок на Passenger; сверить с cron | P2 |
| 5 | Нет COGS-снапшота в OrderItem → маржа невычислима | P2 |

Остаток на SSH-сессию: распределение статусов в живой БД (подтвердить 36 done + 5 cancelled),
наличие crontab для `update_tracking_statuses`, активный NP-middleware в prod-настройках.
