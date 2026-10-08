# R30 · Отзыв после повторной покупки · 09.10.2026

## Проблема и граница реализации

После использования lifetime UGC-награды общий invitation guard отменял всё delivered-review событие, включая обычную просьбу об отзыве. Это подтверждённый контракт кода, а не заявленный массовый production incident. Новый полученный заказ заслуживает отдельного честного feedback, но не обещания второй награды.

При создании события существующий outbox сохраняет неизменяемый вариант: обычный отзыв при доказанном предыдущем использовании награды либо комбинированный отзыв с условным UGC-предложением. Во втором варианте eligibility обязательно проверяется при отправке; неизвестная eligibility не даёт send authority и может восстановиться до dispatch без переписывания snapshot. Существующий идемпотентный ключ владельца и generation сохраняется: изменение варианта не создаёт дополнительное событие.

Обычный отзыв не просит сторис и не обещает скидку. Дискриминатор должен совпадать с точным сохранённым локализованным текстом; один флаг не разрешает отправить старый текст со скидкой. Существующие события без metadata остаются прежними комбинированными приглашениями. Их snapshots, terminal states, provider markers и receipts не переписываются и не воспроизводятся.

Оба владельца отправки сохраняют exact order/assignment/carrier truth, service/debt/privacy/pause/window guards и повторную проверку перед provider request. Receipt до позднего запрета остаётся AMBIGUOUS без replay. Никаких grant/consent/lifetime-slot writes, backfill, проверочных Gemini/Meta calls или ручных сообщений клиентам в этом блоке.

## Порядок работ

Субагенты: aftercare_path — services implementation; client_ui_test_maintenance — отдельные regression tests; aftercare_review — независимая проверка. Root владеет test DB, Git, production и журналом. Checkpoint_contract отдельно проверил impact typed512: это не лимит активной narrative memory; следующие checkpoint trust/schema gates остаются открыты.

## Приёмка

Implementation и independent code/test review приняты. Focused native MariaDB47/47PASS/1.947s; **общий452/452nativePASS/21.467s без пропусков**, ✅ production deployed **`cdb5e13201969f0320855af909bd86ab8819e4e6`**, PID3695379, main/supervisor/child sameSHA healthy/maintenanceOFF;26READONLYqueries/providerprobes0. [PROD](PRODUCTION_RELEASE_2026-10-09.md). Оба набора пересекаются. Shared CPython3.14.6/Django6.1, disposable MariaDB11.4 с настоящими моделями/миграциями/guards; production не использовалась как test fixture.

47 новых проверок охватывают оба outbox owners: positive consumption/retained key, отсутствие повторной награды/slot writes, полную квитанцию/once-only delivery, separate repeat order, старый snapshot без mode, неизвестную eligibility и её восстановление до dispatch, поддельный mode/metadata/final_text, service/debt до и после claim, partial receipt→AMBIGUOUS/no replay, окно/пауза/erasure/unassigned/carrier guards, captured номер и UK/RU/EN copy.

Первый общий449run:436PASS/8FAIL/5ERROR/22.889s. Все13сбоев относились к новым fixtures: запрещённый immutable UPDATE, неполная returned receipt и finite window code. Исправлены test construction/transport contract; модельные/физические guards не ослаблялись. Malformed/legacy rows создаются при первом INSERT; late adversarial observation использует отдельный read seam с настоящим validator и проверкой неизменности DB payload. Дополнительный canonical final_text regression использует разрешённую первую запись + actual dispatch без provider I/O. Пересекающиеся focused/general runs не суммируются.

## Ограничения и следующий gate

Изменение применяется при создании новых событий. Старые комбинированные/terminal/ambiguous события не превращаются в plain review и не отправляются повторно. Ручная коммуникация через native Direct требует отдельной доказуемой order binding; provider echo не становится квитанцией старого события или согласием на маркетинг. Награда остаётся lifetime once-only, промокод —90дней после выдачи.

P5-1 immutable checkpoint/revision513/source+key retirement/performance остаётся OPEN. Typed512 не ограничивает активную narrative memory; typed consumers не включаются этим блоком. Следующий checkpoint должен заранее определить archived-prefix trust, monotonic revisions/≤512segment, подпись exact endpoint/all slot coordinates/prefix commitment и privacy/source/key fences. Нельзя снимать depth guard или добавлять compressed-prefix authority ради зелёной отметки.

Rollback: scoped revert→push main→canonical SSH pull/check/restart/single supervisor ensure. Schema/static changes отсутствуют; существующие payloads/receipts не редактировать.
