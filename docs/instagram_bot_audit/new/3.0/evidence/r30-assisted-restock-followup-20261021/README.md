# 41C · opt-in после ответа менеджера для кастомных и смешанных товаров

✅ DEPLOYED · 21.10.2026 · код `0c178f61e533c4a5480a344092d41314bf12efd1`.

## Граница блока

Только полный текущий согласованный список **ALL-WAIT**, содержащий хотя бы
одну кастомную позицию: кастомные товары или смесь каталожных и кастомных.
READY/WAIT, оплаченные, оформляемые и отменённые части намерения сюда не входят.
Для них сохраняется отдельный сценарий выбора отправлений и согласования стоимости.
Полный Engineering41 и 27 паспортов остаются частичными.

## Что работает

- Реальный assisted request → подписанный draft/CAS approval менеджера → собственный typed manager-update task. Старые approvals не backfill-ятся. Повтор той же операции не создаёт второй task.
- Используется прежняя native fulfillment-очередь, без фиктивного сообщения клиента, каталожного review или RevisionDeliveryEffect. Собственный SENT/MID и immutable task-квитанция предшествуют согласию.
- Приглашение перечисляет все точные позиции один раз и содержит одну положительную кнопку. UK/RU/EN фиксируются по собственному источнику клиента. Повторный равный fit убран из отображения; количества, получатели и `40,5` сохраняются.
- Только собственное RS1-нажатие после квитанции создаёт согласие. Настоящий executor отправляет ACK и обрабатывает отмену; Gemini не заменяет обработчик согласия. Новая версия ACK использует `source-choice.v1`; прежний41B и исторические41A сохранены.
- Перед HTTP заново проверяются права менеджера, source/namespace/epochs, текущие request/decision, TTL, коммерческий владелец, вся корзина и конфигурация, текст и кнопка. Изменение количества, получателя, товара или цвета требует новой проверки.
- UNKNOWN и отсутствие MID не повторяются. Историческая квитанция восстанавливается после новой корзины или истечения текущего решения без повторной отправки; повреждённый исходный источник отклоняется.
- Отдельное свежее физическое ALL-подтверждение менеджера требуется для уведомления: все позиции, достаточное количество, допустимое основание. Начальный WAIT/дата поставки не становятся наличием; уведомление не резервирует товар и не создаёт счёт/заказ.

## Исправленные зависимости

Ожидающий typed WAIT больше не блокирует даже несвязанный точный список наличия:
coordinator проверяет его собственную историческую цепочку и не трактует её как
каталожное availability-сообщение. Повреждённая цепочка остаётся fail-closed.
Уже принятое/завершённое точное соглашение подавляет устаревший WAIT; учитываются
все строки, количества, получатели, конфигурация и исходный baseline. Изменение
каталога под тем же PK допускает новый проверенный ответ, сохраняя старую историю.

Реальный shared Gemini/admin context сначала превышал24SELECT. Устранены повторные
чтения одного графа: отдельный opaque HistoricalReadCase на одно приглашение,
private issuer, точные fingerprints/HMAC, не более3 собственных источников,
обязательный `close` в `finally`. Глобального кэша нет; отправка и mutable gates
используют независимые свежие проверки. Исторические фотографии проверяются одной
пакетной выборкой с прежними правилами ownership/HMAC/digest/удаления.
Принятое согласие, отмена, две реальные фото и отмена с фото укладываются в24SELECT;
shared96/2400 остаются прежними. Повторное чтение после изменения source не
использует прежний read-case. Read-case не принимается для legacy/effect receipt.

## Приёмка

**273 уникальных Python-сценария и12Node UI PASS,0skips.**
262 входят в native MariaDB gate (34новых +42commerce/mixed/partial +186прежних);
в этот gate включены также явные SQLite-migration cases. Ещё11 отдельных SQLite
media/paid-media тестов проверяют прежние фото и доступ. Повторные focused-прогоны
не прибавлены к общему числу. Native gate:2173.424s, MariaDB11.4.12, полный граф
миграций, reverse/reapply, schema/check,0blocked warnings и cleanup verified.
AST, Django check, migration drift, scoped diff и10 неизменных source hashes PASS.

```sh
.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite restock-followup
(cd twocomms && ../.venv/bin/python manage.py test management.tests_ig_assisted_commerce_media management.tests_ig_assisted_commerce_paid_media --settings=test_settings)
node --test tests/js/management-ig-restock-waiting-list.test.cjs
```

Все DB/HTTP-fixtures локальные; native HTTP factory не выдаётся за реальную Meta-доставку.

## Production и продолжение

✅ Scoped commit → GitHub main → canonical SSH git pull → Django check и
WSGI/supervisor reload. SHA main/supervisor/child совпал; main/workers healthy,
maintenanceOFF, обе feature flags включены. Все10 source hashes совпали.
7READONLYSELECT,0provider attempts именно проверяющего процесса;81catalog rows
и stock digest не изменились. Productionfixtures0, ручных клиентских отправок0.
Новых миграций, markup/CSS/JS/assets нет; существующий UI списка и native кнопка
сохранены. Локально Python3.14.6/Django6.1, сервер Python3.14.8/Django6.1.

[Runtime evidence](VERIFICATION.json), [source hashes](SOURCE_SHA256.json).

**Далее OPEN:** единый multipart invitation/ACK/revoke/notification lifecycle и
доказанное ALL-delegation между purpose. Граница950байт сохранена: длинный список
не обрезается ради согласия, частичная подписка не создаётся. Реальная визуальная
приёмка свежей карточки в Direct остаётся OPEN; source/HTTP factory/health её не
подменяют.90дней сохранены, но не расширяют окно Instagram; native future
capability остаётся unverified. Natural cohorts/P5/P7 сохраняются по приоритету.
