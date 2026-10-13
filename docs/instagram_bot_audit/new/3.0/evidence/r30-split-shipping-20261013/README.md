# Engineering37 · раздельные отправления

Статус выпуска фиксируется отдельно в production receipt после реального pull и runtime-проверки. [Контракт и ограничения](CONTRACT.md).

## Что проверяется

- Актуальная подписанная проверка всей корзины, точные источники выбора, два явных варианта с плательщиком, максимумом каждой посылки и общей суммой. UK/RU/EN, целые строки и полное количество, отсутствие скрытой дополнительной платы.
- Реальная SENT-квитанция карточки перед выбором; native SS1 с точным клиентом, сообщением, версией и чистым bundle. Голое «да», чужая/старая кнопка, новые вопросы в том же bundle и UNKNOWN не создают согласие.
- Один оплаченный заказ с полной SourceCart→Proposal→Generation→Payment→OrderItem связью. COD распределяется с сохранением копеек; доставка не превращается в оплату товаров. Автоматическая привязка после winner и ограниченное восстановление, включая старую историю непривязанных выборов.
- Фактическая передача точных товаров и отдельная ТТН каждой посылки. Общий складской пул агрегируется по физической идентичности, конечные резервы и оставшиеся оплаченные обязательства защищены. Повтор не списывает stock дважды; одна ТТН не присваивается двум посылкам.
- Первая полученная посылка не завершает весь заказ. Только carrier evidence всех требуемых посылок открывает один aftercare; неизвестный статус, возврат, отказ, старая/будущая дата и legacy scalar TTN не обходят этот контракт.
- Настоящие CSRF/capabilities/privacy/reset/epochs/config gates; неизменяемые native INSERT/UPDATE/DELETE guards и возобновляемая MariaDB DDL. Source-bound Gemini/card context остаётся в прежних budgets; GET не пишет и не вызывает provider.

## Проверки

**Финальный clean gate: 432/432 native MariaDB PASS, 0 skips, 280.382s; 116 новых causal методов; 19/19 UI-controller PASS, 0 skips.** [Raw native](NATIVE_FINAL_432_PASS.txt), [Node](NODE_PASS.txt), [checks](CHECKS_PASS.txt). После UI-дебага проверена вся затронутая совместимость; independent final review без новых P1/P2.

`NATIVE_338_PASS.txt`: 338/338 PASS, 0 skips, 262.025s. 107 новых causal методов и 231 совместимый сценарий: stock sync, checkout/payment winner, inventory allocation/transfer, size/provenance, fulfillment truth, notifications, lifecycle, postbacks и review reuse.

`NATIVE_CONTEXT_85_PASS.txt`: после двух дополнительных регрессий — 85/85 PASS, 0 skips, 59.521s. Проверены 11 реально истёкших непривязанных выборов перед новым paid order и обычный менеджер с отдельными capabilities при native context limit96 SELECT. Наборы пересекаются: их суммы не означают число уникальных сценариев.

`NODE_PASS.txt`: 19/19 PASS, 0 skips; реальный controller, сериализация canonical p1…pn после добавления/удаления групп, суммы/плательщик, валидация, сохранение draft, UNKNOWN с тем же operation UUID, typed errors и запрет превышения cap.

Браузерный прогон обнаружил два дефекта, которые замороженные unit clocks не выявляли: несовместимые wire-ключи UI и сравнение времени только что созданной строки с началом операции. Исправление прошло native advancing-clock gate55/55 и повторный actual UI POST; будущие старые proof по-прежнему отклоняются. Исторические offer lines/locale сохраняют настоящие названия после оплаты; exact bound choice скрывает повторную привязку, чужие historical candidates не разрешают оформление. Общий статус ship появляется только при полном sealed dispatch coverage; money/loyalty/scalar TTN не переписываются. Старый lifecycle fixture исправлен причинно: business consent получено до отправки, затем подтверждённое получение; запрет позднего opt-in не ослаблен.

Все native тесты выполняются в отдельной disposable MariaDB11.4.12 с network fence, CPython3.14.6/Django6.1. Логи mocked HTTP-ошибок, unknown outcome, CSRF-deny и source-signature rejection — ожидаемые отрицательные сценарии, не реальные отправки. SQLite проверяет только DDL45 guards, не подменяет MariaDB CAS/transactions.

## Что остаётся открытым

WAIT — обсуждение условий, не готовый оплачиваемый backorder. Исполнение требует AVAILABLE всей корзины и ordinary full-stock quote/payment. Следующий приоритет — отправка доступной части при ожидании остальных товаров с настоящим future allocation. Затем автоматическое создание нескольких документов НП; сейчас менеджер регистрирует уже оформленные ТТН после фактической передачи товаров. Внутри одной строки количество между посылками не делится; изменение привязанного плана требует отдельного amendment.

Все 27 полных паспортов остаются частичными. Production fixtures, тестовые ТТН настоящих заказов и проверочные сообщения клиентам не создаются. UGC90 дней/тот же неиспользованный10→15% сохранены.

## Actual UI

Настоящий login/CSRF/API POST: signed manager case→форма двух вариантов70UAH together/140UAH separate→сохранённый Offer. External transports заменены только в disposable sender/payment fixture; настоящий SENT proof, SS1 choice, full quote/finite reservation/paid winner и auto-bound план. Обе реальные формы dispatch сохраняют свои test-only ТТН и70UAH;71UAH при cap70 заблокированы. Первый carrier9/второй7 не дают completed/aftercare, второй9 даёт done и один aftercare event; money/scalar TTN/manifest unchanged. [Partial proof](UI_CARRIER_PARTIAL.json), [complete proof](UI_CARRIER_COMPLETE.json).

320/390/1816px: document width равно viewport, overflow0 и непересекающиеся карточки посылок, application console errors0; source CSS96d442011f6f/JSd29a64b34db6 реально загружены. Скриншоты: [390](UI_PARCELS_390.jpg), [320](UI_PARCELS_320.jpg), [desktop](UI_PARCELS_DESKTOP.jpg). Editor320 отражает короткие подписи плательщиков. Проверочные данные только локальные.
