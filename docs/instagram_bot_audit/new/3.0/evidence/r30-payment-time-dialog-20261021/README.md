# 33A · Уточнение согласованного времени напоминания об оплате

✅ Код `96ab41140`, интегрированный production `0439cd5d3505080561083df3b01f6dba841fbd97`. Canonical main push → SSH pull --ff-only → management0243 → check → restart/owned supervisor reload. Собственный bounded maintenance lease снят; runtime receipt приведён в JSON.

После «напомните об оплате» без времени бот задаёт понятный вопрос на UK/RU/EN. Ответ только временем допускается до истечения запроса уточнения (не более30минут после его создания) и только после доказанной SENT-доставки этого вопроса, пока не изменились точная покупка, счёт, исходные сроки и разрешения. Вмешавшееся другое сообщение, чужой источник, новая корзина/счёт, оплата, пауза/reset/privacy, UNKNOWN или отсутствующий MID не создают согласие. Относительное время считается от provider clock; quiet hours/DST не угадываются. После времени всё равно нужна одна положительная кнопка существующего PR1; до нажатия задачи нет. Старые прямые PR1 работают без нового поля provenance.

Pending state подписан и append-only, это запрос времени, не согласие. Карточка и Gemini видят один source-backed awaiting_time без назначенной даты/задачи; manifest epoch0 разрешён только этому состоянию с двумя разными проверенными источниками. Реальная оплата во время последнего чтения receipt снимает actionable state через свежий purchase/payment fence. Read budget96 и prompt2400 сохранены; guard отправки не заменялся лёгким reader. EN pure reminder больше не маскируется под новый checkout; mixed/обычный checkout сохраняют прежнее поведение. Fresh native color provenance сохраняется одним реальным чтением, не доверяет private prepared graph без проверки.

**106уникальных Python сценариев PASS0skips:**85native workflow до финального read-only fence +21native final context/cross-gate; финальные91SQLite повторяют coverage и не суммируются. AST evidence подтверждает, что после85native изменились только reader functions, action paths не менялись. Оба disposable MariaDB gates проходят полный реальный forward graph, schema/check, review/storefront reverse/reapply и verified cleanup. Изолированный SQLite0243 apply/unapply/reapply сохраняет прежние guards. Settlement fixtures удерживают отдельную thank-you delivery, не требуют внешней сети.

```sh
/Users/zainllw0w/TwoComms/site/.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite payment-time-dialog
/Users/zainllw0w/TwoComms/site/.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite payment-time-context
```

Production0243: одна новая InnoDB-таблица/11колонок, unique request source, client/expiry index и3INSERT/UPDATE/DELETE guards. Только read-only runtime/schema/stock verification, provider attempts0 в процессе проверки;81stockrows/digest неизменны. Новых Direct-тестов и synthetic production records не создавали. Геометрия/assets не менялись; новый pending hint следует прежней геометрии. Authenticated production после reload:320/390px — page, drawer, context, messages и reminder row без горизонтального overflow; reminder245/315px. Проверено реально существующее unknown state, viewport восстановлен. Новый live pending не фабриковался. Полный natural Direct visual gate остаётся OPEN; историческая browser приёмка33 не подменяет его.90дней и Meta23h не расширены.

Следующий приоритет:41E совместимость длинных951–1000символов уже доставленных native restock соглашений; затем версионированный полный multipart. Простое снижение лимита producer не исправляет прежние ACK/notification. Полные27паспортов и natural cohorts остаются частичными.
