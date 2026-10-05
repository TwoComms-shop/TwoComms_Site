# Первый срез Instagram Bot 3.0 · 05.10.2026

Реализован локальный вертикальный срез §14 и P7-1.A плана90: факты входящего сообщения → response plan → безопасный ответ/repair → исходные receipts и незакрытые обязательства → карточка/карта. Это затронутые части P0-1/2, P1-7, P2-1.B, P4-1/2; остальные паспорта не объявляются выполненными.

**Состояние выпуска:** изменения находятся в рабочем дереве. Commit, push, SSH pull, production migration, изменение flags, перезапуск и отправки клиентам не выполнялись. Incident352 не объявляется исправленным в production. P7-1.C —72часа и20подходящих естественных новых ходов после контролируемого выпуска — остаётся открытым.

## Что изменено

- Принятый webhook идемпотентно сохраняет размер, тип вещи, цвет/fit, намерение заказа и допустимую identity до генерации, независимо от output gates. Кириллический `л` нормализуется в L. Исправления, отрицания, альтернативы, вопросы и цитаты отделены от выбора; model hint не отменяет source abstention.
- Точное имя либо собственное полное SENT presentation позволяет связать товар. Частичное/UNKNOWN/чужое presentation не даёт identity; первый товар сохраняет прежний совместимый размер, switch/reset/recipient/repeat изолируют требования. Original per-source episodes сохраняются при replay.
- Captured response plan различает пожелание, применимую configuration, цену/наличие, checkout effects и следующий вопрос. Fresh controls согласованы с executor; unsupported fresh `item` не рекламируется. Source preference не становится stock/order/payment authority. Исторический подтверждённый checkout items path сохраняется.
- Полезный fallback проходит те же truth/scope/CAS guards. SENT — физическая доставка: она не закрывает непокрытый запрос цены, покупки, сервиса или фото. Durable coverage хранится в существующих revision receipts; sources остаются PENDING. Вопрос о действительно необходимом selector переводит ожидание к клиенту только после полной SENT доставки; иначе остаётся owner/debt.
- Optional holding/apology не добавляется автоматически. Continuous wait имеет durable source/receipt suppression через связанные inbound, recovery и restart; SENT/UNKNOWN уже доставленного уведомления препятствуют повтору. Optional delay apology требует важного unresolved payment/service source, проверенного timing/ownership и6неночных часов по versioned Kyiv policy.
- Repair factory и сериализация payload проверяются до atomic reservation. Predictable refusal не сжигает слот и не вращает модели с исходным prompt. Local semantic rejection отличается от provider failures; существующие8HTTP/2scarce/1repair, permission/freshness и project reserves сохранены.
- Карточка, prompt и карта читают одинаковые current source facts. L/тип вещи/намерение видны до SKU, со source ID и явной неопределённостью применения/наличия. Исторический неподтверждённый compatibility snapshot не становится текущим фактом. GET не получает private selection candidate authority.

## Схема и совместимый откат

`management.0218_ig_commerce_source_action_transitions` меняет source FK transition с OneToOne на ForeignKey: исходный факт и validated selection action могут иметь отдельные переходы одного сообщения. Decision/source остаётся уникальным; `(session_id, to_revision)` остаётся уникальным. Native introspection подтверждает это после настоящей миграции.

Intake producer включается только при effective revision rollout; `persistence_only` сам по себе сохраняет legacy delivery. Immutable producer marker удерживает ранее принятый revision source при выключении флага. Existing worker drain обрабатывает только принадлежащие ему preparation heads и expired-lease CLAIMED heads, включая crash до effects и после сохранения proposal. Live lease, unowned observations и уже SENT части не пересылаются. CAS вращает token, сохранённый proposal переиспользуется.

**Операционный откат:** выключить rollout при сохранении совместимого нового кода и схемы, оставить drain существующего owner. Raw возврат старого Git/schema не является проверенным откатом: старый код не понимает новые receipts, а обратная OneToOne миграция несовместима с уже появившимися несколькими переходами одного source. Подготовка релиза должна учитывать0218 до запуска новых producers.

## Проверки и границы доказательств

Shared local runtime: CPython3.14.6/Django6.1. Production read-only baseline: `main`/`0795423014887c619b3992deba865967ae456e87`, Python3.14.7/Django6.1, MariaDB11.4.13/InnoDB/utf8mb4; revision execution effective ON, analysis/typed modes OFF. Данные352 не изменялись и не использовались как fixture; сырые сообщения/ник/provider IDs не сохранялись в evidence.

- **Native основной срез:324/324 PASS,213.045s**, без skips. [Лог](mariadb-main-324.log). Включены реальный ingress/CAS, competing workers, source/action constraints, repair reservation race, permission transitions, receipt finalization и compatible rollout drain. После этого прогона дополнительно исправлены pure quote/topic/source parsing boundaries; их окончательная проверка записывается отдельно.
- **Native финальные регрессии:128/128 PASS,38.504s**, без skips, после всех quote/negation/imperative-price правок. [Лог](mariadb-final-128.log). Проверены source parser/state/product identity, response plan, truth/guard и полный source-union путь до proposal/доставки. Этот набор частично пересекается с324; числа не суммируются как уникальные тесты.
- **Native burst budget:17/17 PASS,14.539s**, без skips. [Лог](mariadb-burst-budget-17.log). В том числе реальная конкуренция переноса source budget с admitted HTTP на одном экономическом mutex; eighth SQLite skip проверен на MariaDB. Все8engine skips итогового SQLite имеют соответствующие native gates.
- **Итоговый SQLite:755tests,747PASS/8engine skips/0failures,131.767s**, после всех правок. [Лог](sqlite-final-755.log).48модулей, включая reply/truth/debt, live delivery, rollover/replay, provider budget, authority, checkout, permission/inbound, source/card/journey. Состав прогонов: [test-modules.json](test-modules.json). SQLite skips касаются MariaDB row locks/NOWAIT/schema и не выдаются за доказательство конкуренции.
- [Native schema](native-schema-final.log): disposable MariaDB11.4.12, InnoDB/utf8mb4, management0218 applied, decision/source unique, transition/source nonunique, session/to_revision unique.
- Browser QA — настоящий авторизованный локальный Django UI с synthetic fixture: desktop1238px,390×844 и320×844, без горизонтального overflow и console errors. После reload карточка и раскрытый подбор показывают L/«Футболка»/намерение/source№1, без выдуманного наличия, заказа или точного denominator. [Desktop](card-and-journey-desktop.jpg), [390px](card-mobile-390.jpg), [320px](card-mobile-320.jpg). Production UI этих изменений ещё не проверен.
- Native runner применяет реальные management/sessions/sites/accounts/product-catalog dependencies и storefront0098. Finance/DTF и другие несвязанные leaves не мигрируются и не объявляются проверенными. Empty storefront0096 predecessor получает HASH endpoint fixture, необходимую assert-contract0097; миграции не фальсифицируются. Только disposable `test_twocomms_ig_*` truncation заменяет DELETE teardown append-only tables; post_migrate восстанавливает content types/capabilities.
- Внешняя сеть закрыта тестовым profile; provider/HTTP receipts — mocks. Реальные provider API calls, customer sends и production writes —0. Отказ сети в раннем Google-indexing signal не объявляется provider call; итоговые прогоны дополнительно отключают indexing.

## Существовавшие отдельно проблемы

`tests_gemini_accounting_shadow`:75/78PASS; три `reconcile_expired_request_graphs` failures воспроизводятся теми же именами/ожиданиями при загрузке HEAD-копий изменённых provider services из временных файлов. [Current78](accounting-baseline-current-78.log), [HEAD3](accounting-baseline-head-3.log). Исправление этого accounting остатка не входит в данный срез; весь accounting suite не объявляется зелёным.

`makemigrations management --check` предлагает прежний drift default `system_prompt`: HEAD model уже отличался от migration0217;0218 этот default не меняет. [Доказательство](prompt-default-migration-baseline.log). Новая посторонняя prompt migration не создавалась.

Два старых permission fixtures получали403 и на HEAD: только в этих fixtures явно выданы `operate_ig_bot` и `view_ig_conversation_pii`. Production authorization не расширена; generic staff/reviewer dominant-deny regressions проходят.

Все указанные native тесты выполнены на отдельной локальной БД. [Production metadata](production-readonly-schema.log) подтверждают исходную совместимость и необходимость0218, но не заменяют post-deploy runtime, browser и natural-traffic acceptance.

[Проверка сохранения исходных правок](dirty-preservation.json):27не относящихся к этому срезу initial diff sections остались byte-identical; отдельная пользовательская правка `attentionElapsedLabel` сохранена. HEAD не менялся, staging не выполнялся. Временная browser вкладка закрыта, локальные Django/MariaDB процессы остановлены; evidence сохранено в репозитории.
