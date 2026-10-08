# R30 · Читаемая память и запрос отзыва · 09.10.2026

## Блок A · Карточка памяти

Реализована отдельная конечная manager projection над одним проверенным frozen memory read. API больше не возвращает машинный prompt в поле `memory`; UI получает только `memory_view`: точные цитаты, даты по Киеву, различие времени сообщения и сохранения, темы-подсказки, причины пропусков и ожидающие новые сообщения. Ссылки ведут только на подтверждённые USER-сообщения этого клиента в текущих reset/erasure/time/PK границах. MANAGER/MODEL и подтверждённые human receipts учитываются в watermark тем же producer admission.

Пустая подтверждённая память сохраняет объяснение пропусков; неизвестная/устаревшая provenance не превращается в факты. Legacy-текст показан отдельно только после канонической проверки. Генерация и Gemini prompt не переписывались, provider probes — 0.

Проверки: 10 Python presentation/source-boundary регрессий на shared CPython3.14.6/Django6.1; 11 чистых Node-тестов. Реальный браузерный стенд проверен на 320/390px: page width равен viewport, длинная непрерывная цитата переносится; HTML-подобная строка остаётся текстом. Независимый review выявил и закрыл empty-coverage edge.

Статус: ✅ deployed `b23583f30`; авторизованный production desktop/390/320px, реальные client351/352 read и healthy runtime подтверждены в PRODUCTION_RELEASE_2026-10-09.md. Полный P5-1 checkpoint/513 recovery и natural-live cohort остаются OPEN.

## Блок B · После получения заказа

✅ deployed `c34567b15`: единый локализованный запрос честного отзыва + необязательная сторис с @twocomms; после проверки — одноразовые 10% на следующий заказ, **90 дней с выдачи**. Автоматические владельцы сохраняют immutable snapshots, once-only keys, паузу, response window и реальные provider receipts. Перед отправкой и на provider boundary проверяются service case, незакрытая ответная задолженность, lifetime grant и неизвестная eligibility.

Действующий клиент351 / заказ334 / assignment9v1 проверен по production: получение подтверждено перевозчиком. Legacy event16 не отправлен, `manager_review`, standard response window closed, квитанций нет; он не возобновлялся и не помечался SENT. По явному поручению владельца разовый запрос отзыва отправлен через официальный Instagram Direct; backend сохранил manager echo3330. Это отдельная ручная коммуникация, а не API receipt события16. После echo клиент возвращён боту штатным resume. Промокод ещё не выдавался, согласие на последующий маркетинг не создавалось.

Privacy: исходная переписка, screenshot с платёжными/контактными данными, MID и секреты остаются вне Git. Этот файл не является инструкцией отправлять другим клиентам или повторять ручное сообщение.


Finalaftercare202nativePASS/16new regressions; active+retained-key consumedANY, bounded2conflict→unknown, bothownerspartiallateblock→AMBIGUOUS/noreplay. Four existing MariaDB fixture numbers shortened<=20, old meaningful quality/fit assertions unchanged. Main/supervisor/child/PID3387891healthy/maintenanceOFF, READONLY14queries/providerprobes0. P7oldUItests repair отдельным блоком.


## Блок C · P7: восстановление клиентских UI/API gates · CODE/TEST принят, production pending

Исходная7c97 и первая текущая серия184 имели одинаковые73сбоя (27FAIL/46ERROR), новых0. Фикстуры staff заменены реальными Django capability permissions без superuser/mock bypass; staff без прав и Meta Reviewer с PII разрешением остаются denied. Истинные current payment tests теперь создают review/deal episodes, точную order binding, append-only manager decision или provider projection и проверяют IDs/amount/source/fulfillment authority. Отсутствующий provider proof явно остаётся неавторитетным. Media whitelist расширен только конечными безопасными metadata; неподтверждённые URL/receipt authority не возвращаются. Authorization queries проверяются отдельно от≤2business count-only queries.

Четыре новые регрессии используют реальный source→enqueue→claim→publish→detailAPI: own manager boundary, complete delta/head immutability, чужой namespace и запрет fallback на сырой head. Оба Gemini entry points и Meta entry point fail-on-call/assert_not_called. Это проверки без provider I/O.

Native190/190PASS/15.394s; общий затронутый набор404/404PASS/22.815s +11NodePASS до финального conservative-legacy-hold уточнения. Последний reviewer нашёл, что старый empty legacy slot был обозначен как already_rewarded: блок выдачи сохраняется, причина и текст разделяются на unverified identity и доказанное consumption. Финальная повторная проверка после ремонта: **405/405 native PASS/22.894s**, без пропусков; **11/11 Node PASS**. Independent final reviews без блокеров. Empty legacy hold не перебинден, награда не создавалась, consumption не изменён; выдача/приглашение blocked как unverified identity/unknown. Зелёный release C пока не заявлен до production acceptance.

Следующий аудит (OPEN): attribution-only materialized episode может не иметь review/deal anchors, хотя legacy order history имеет manager decision. Перед изменением ownership нужны bounded production evidence и historical/current contract; фикстуры currentpositive используют настоящий review-owned workflow, ни одного production ownership repair не выполнялось.
