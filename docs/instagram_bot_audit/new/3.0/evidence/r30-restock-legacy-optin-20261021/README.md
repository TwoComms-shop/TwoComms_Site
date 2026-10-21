# Engineering41A — сквозная проверка прежнего opt-in

Статус: ✅ DEPLOYED21.10.2026 (Europe/Kyiv). Код `f9f3b0004f6d0fdb80fad542689a55c54c8a9cad`.

## Найденная причина

На baseline `8a317ba148bdaed292f2773b48d4f8c0ad1e83c8` реальный
`execute_claimed_revision` для ALL-списка каталоговых товаров при нулевой
складской fit-authority блокировал первоначальное приглашение с
`response_plan_line_claim_unverified`, до provider HTTP. Отдельная строка
`fit: classic` выглядела как утверждение о конфигурации в наличии.
Это условный дефект, а не доказательство поломки всех одиночных приглашений.

После введения новой версии регрессия также выявила старое буквальное
восстановление ACK в single-item read model: отмена проходила, но карта
показывала unverified. Теперь live handler и reader используют одну конечную
политику распознавания текста, без дополнительных SELECT.

## Контракт исправления

- Новые invite/ACK явно относят параметры к выбору клиента. Каждая строка
  сохраняет собственный контекст; пунктуация не переносит authority между
  товарами. Дробная запятая `40,5` сохраняется. Повторный равный fit удалён
  только из отображения; подписанный target не меняется.
- UK/RU/EN, одна положительная кнопка приглашения. Отдельная возможность
  отмены остаётся в подтверждении. Верстка и статические assets не меняются.
- Новые приглашения и ответы подписывают `source-choice.v1`. Уже отправленная
  unversioned invitation может получить новый версионированный ACK.
- Unversioned Answer допускает только два точных известных текста ACK;
  versioned Answer — только свой текст. Требуется настоящий SENT effect,
  правильный source/revision/recipient, digests и единственность отправки.
  Explicit null/unknown version отклоняются. Старый ACK-proof не расширен.
- Исторические envelope/HMAC и доставленные ACK не переписываются. Повторная
  штатная обработка того же принятого source без ACK может завершить отправку
  новым текстом; существующий SENT сохраняет свой payload. Массового replay
  исторических клиентов нет.
- Полное название одиночного товара не обрезается. Invite, ACK и notification
  проверяются на внутреннюю границу 1000 UTF-8 байт до создания согласия;
  непоместившийся список не превращается в частичную подписку.
- Ни общие truth-guards, ни факты наличия, ни складские остатки не изменены.
  Согласие не означает резерв/обещание даты и не обходит физическую проверку
  менеджера. Бизнес-срок остаётся 90 дней, окно принятия — 23 часа; capability
  будущих сообщений Instagram остаётся unverified.

## Приёмка

24 целевых SQLite-теста PASS, включая настоящий revision/outbox/HTTP factory
для single/ALL × UK/RU/EN, старую literal SENT invitation → новый ACK → revoke,
старый доставленный ACK и восстановление незавершённого ACK без изменения
Answer/HMAC/числа подписок. Проверяется отсутствие Gemini и внешнего provider I/O и
изменений инвентаря. Ручных production-отправок и production-fixtures нет.

✅ Финальная регрессия:186 уникальных тестов,0skips,1323.219s на MariaDB11.4.12.
Включены16 новых регрессий и все170 прежних кейсов.24SQLite-проверки входят
в186, не добавляются к общему числу. Полный граф миграций, reverse/reapply,
check/schema и cleanup PASS; предупреждений0. AST, Django check, scoped diff
и неизменность всех7source hashes подтверждены.

Воспроизводимый gate:

```sh
.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite restock-followup
```

Он использует собственную временную MariaDB и полный реальный граф миграций,
запрещает внешнюю сеть и проверяет reverse/reapply/schema/cleanup.

## Production и продолжение

✅ Canonical scoped commit → GitHub main → SSH git pull, штатный3.14 venv,
Django check, WSGI/supervisor reload и READ ONLY-проверка. Основной процесс,
supervisor и child подтвердили SHA кода; main/workers healthy, maintenanceOFF.
Серверный pure contract подтвердил UK/RU/EN, отказ от выдуманного наличия,
дробную запятую, границу сообщения и90дней/23часа.7READONLYSELECT,0provider
attempts;81catalog rows и stock digest не изменились. Новых миграций/assets нет.
Локально CPython3.14.6/Django6.1; сервер CPython3.14.8/Django6.1.

[Runtime evidence](VERIFICATION.json), [проверенные source hashes](SOURCE_SHA256.json).

Следующий приоритет —41C: genuine typed manager-update для assisted/custom
ALL-WAIT; далее multipart lifecycle. Полный Engineering41 и27паспортов
остаются частичными. Визуальная приёмка свежей реальной карточки в Direct
остаётся OPEN; server/source/HTTP-factory не подменяют её.
