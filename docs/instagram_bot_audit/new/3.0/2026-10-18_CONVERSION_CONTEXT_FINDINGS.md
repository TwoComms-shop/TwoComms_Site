# Аудит конверсии, контекста и знаний Instagram-бота — 18.10.2026

Исторический срез аудита18.10: дополнен после синхронизации и ответов владельца Q01–Q29, включая удержание всех200грн. **19.10 владелец отдельно поручил реализовать находки и выполнить commit/push/deploy. Текущее состояние реализации, проверки и production приведены в [release ledger](2026-10-19_IMPLEMENTATION_RELEASE.md).** Формулировки и ссылки ниже сохраняют первоначальные доказательства; они не означают, что прежний дефект остался после релиза. План90 в другом окне этим документом не перезаписывается.

## Границы и источники

Первичный аудит выполнен чтением кода, прежних решений, плана и production health. Затем по прямому запросу владельца основная локальная папка обновлена до действующего релиза с проверяемым сохранением старых файлов вне Git. Production, его данные и план 3.0 не изменялись; клиентам ничего не отправлялось. После синхронизации выполнены профильные тесты и отдельные audit probes на disposable SQLite. Редактируются этот отчёт и связанный Brief.

На первом этапе primary HEAD был 95ff5a90e098b60014cbdbb1346e06a232e038f9 и отставал на 84 коммита. Поэтому первоначальные production-находки проверялись в отдельном clean worktree. **Baseline после синхронизации18.10, до новой реализации: /Users/zainllw0w/TwoComms/site main = origin/main = production HEAD a4818a1b7c621b9f70af818271f94a5b110f2d9a**, Git tree 5ac454a9dc3f77d6feb908fd7c01216b0c104e7e. Тогда все5356 trackedфайлов проверены по содержимому, несовпадений не было. Финальный новый SHA проверяется отдельно в releaseledger. Старые ownerdecisions остаются источником; архивный patch целиком не накладывается.

Production health на 18.10.2026 13:19:49 UTC сообщил одинаковый SHA supervisor/child, `status=ok`, нулевые dangerous backlog, overdue revision execution, pending replies/notifications/analysis/recovery. Это проверка состояния очереди в один момент, не доказательство качества всех диалогов и не воспроизведение новой клиентской ошибки.

## Как читать результаты

- **CODE** — проверен существующий путь в коде релиза a4818a1b7; указан воспроизводимый сценарий. Это не утверждение, что конкретный клиент уже столкнулся с ним.
- **LIVE** — отдельное текущее read-only наблюдение production; время и предел проверки указаны.
- **LOCAL PROBE** — воспроизведён вызываемый контракт на локальных fixtures с тем же кодом; это не частота production-инцидентов. Некоторые probes фиксируют существующее неправильное поведение, поэтому зелёный результат такого probe подтверждает находку, а не исправление.
- **OWNER** — прямое сегодняшнее пояснение владельца или принятое решение Dxxx. Это источник для формализации, но не автоматическая публикация в Gemini.
- **HYPOTHESIS** — обоснованная гипотеза о поведении покупателя; эффект на продажи ещё предстоит измерить.
- **PLAN REMAINDER** — уже признанная незавершённая часть плана. Её нужно дополнить проверкой, а не выдавать за новую независимую функцию.

Приоритет P0 ниже означает блокирующее условие безопасной интеграции. P1 — вероятный существенный ущерб при достижении указанной ветки, особенно перед рекламным запуском. P2 — следующий полезный слой качества/диагностики. Это приоритет аудита, он не меняет номера P0/P1 задач в плане.

Десять независимых направлений проверки: реклама; knowledge/prompt; память; товар/посадка/медиа; задержки; commerce truth; продажный диалог; карта/метрики; прежние решения; соответствие checkout действующему релизу. Основной агент свёл пересечения, перепроверил ключевые функции и сделал production-проверки. Полный suite всего проекта и клиентские отправки не выполнялись. После синхронизации пройдены профильные наборы и отдельные probes; точный состав и ограничения приведены в разделе повторной проверки.

### Что подтверждено на сервере дополнительно

18.10.2026 **13:26:45 UTC** выполнено чтение production MariaDB в сессии TRANSACTION READ ONLY. Активна BotPolicyPublication **id=2, version=2**, создана 07.09.2026, **20 инструкций**, hash **504d49cd0a7b9fc638e73e42cf5432339025400a1f82b881d97afa7cc303a9c8**. У всех двадцати отсутствует required_for_scenarios; reviewed_source пуст. Это устанавливает состояние declarations, а не отсутствие любого полезного текста в инструкциях. См. KB-05. Запрос не использовал provider API, клиентов или платежи.

18.10.2026 **13:28:19 UTC** прямой HTTP-запрос к публичному FAQ подтвердил ряд спорных текстов, включая пять стадий QC, 30+ стирок, wholesale 15–35% и обобщение кроя. Это проверка полученного HTML, без интерактивного браузерного тестирования checkout. См. KB-06.

## Повторная проверка после синхронизации · 18.10, после 15:37 UTC

**Паритет исходников закрыт.** Primary main, origin/main, production checkout, supervisor и child имеют a4818a1b7; tracked tree5ac454a9dc3f77d6feb908fd7c01216b0c104e7e.5356 локальных файлов проверены по Git blob содержимому. Remote tracked diff пуст. Исторические feature worktrees сохраняют свои ветки: они не являются основной runtime/base и не должны принудительно переписываться.

**Данные/окружение отдельно.** Production не разворачивалась заново, клиентская БД не копировалась и не использовалась как fixture. Local3.14.6/Django6.1; production3.14.8/Django6.1. Локальный pin сохранён по AGENTS; это явно обозначенный REL-02, а не молчаливое обещание identical environment. Реальный graph и238 applied management migrations совпадают, leaf0236.91 local dependencies compatible, Django check без замечаний.

**Fresh production readiness 15:40:16 UTC:** status=ok/running, dangerous_backlog=0, execution_overdue=0, reply_pending=0, recovery_unresolved=0.17 inbound rows обозначены preserved_revision_debt_evidence, не runnable queue; historical analysis_failed=4. Снимок содержит5 manual/16 manager-owned revisions и0 attention: это сохранённая ownership, а не подтверждение качества всех диалогов. Active publication15:41:40 остаётсяv2/20 modules/0required declarations с прежним hash, подтверждая KB-05.

### Запущенные проверки и доказательства

| Набор | Результат | Evidence в recovery snapshot |
|---|---|---|
| Referral, required policy, actual cart amendment/paid communication |102 PASS|[primary-regression.log](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/primary-regression.log)|
| Timeline producer и focus +5 external probes |46 PASS,0skips|[memory results](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/probes-memory/tests-final.log), [structured outputs](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/probes-memory/probe-results.json)|
| Fit/focus/chart/coverage +5 external probes |52 PASS|[fit results](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/probes-fit/results.log)|
| Real worker/recovery/outbox,3 new+6 controls |9 PASS,0skips|[latency evidence](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/probes-latency/EVIDENCE.md)|
| Public checkout service: positive/negated/historical manager amount |3 PASS current-behaviour probes|[checkout results](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/probes-checkout/results.log)|

Это пять наборов, часть базовых focus controls пересекается; их не объявляем уникальными customer journeys. Внешние probes созданы вне репозитория, используют synthetic fixtures/disposable in-memory SQLite, provider/Meta output mocks. Network blockers для fit/latency показали0attempts; остальные probes также не выполняли provider HTTP. Temporary fixture ошибки ранних исследовательских прогонов исправлены только во внешних probes; в финальных наборах failures нет. В source/tests приложения изменений не внесено.

**Что стало точнее:** MEM-01 только incremental existing-head stall,2001 символ не всегда скрывает reader; MEM-02 только media fallback без canonical choice; FIT-01 реально неверный information subject; FIT-02 доказан лишний fit choice на однофасонной футболке; FIT-04 plan/execution расходятся; SALE-01 known95/5 блокируется ещё inquiry guard; LAT-01/03 ответственность сохраняется, но полезный ответ уходит в manual/partial; LAT-02 дополнен candidate-unaware scheduling; SALE-04 принятие отрицательной/исторической цены подтверждено service probe. Это materially более точные выводы, чем первоначальная статическая проверка.

**Предел:** не запускались новая customer-facing Instagram отправка, реальные Gemini generation/probes, полная commerce оплата и MariaDB concurrency tests. Локальная воспроизводимость не доказывает частоту, p95,10% conversion или новый rollout. Технические исправления ещё предстоят в другом окне.

## Ответы владельца: что изменилось в контракте · 18.10.2026

Подробный source-bound реестр сохранён в [Brief с ответами Q01–Q29](2026-10-18_OWNER_BRIEF_GRILL_ME.md). Ответы — approved business input для следующей реализации, **не** утверждение, что facts, сайт или shipping policy уже обновлены. Plan90 не редактируется в этом окне.

### Полученные факты и ограничения

| Тема | Новый ответ | Следствие для реализации |
|---|---|---|
| Футболки |Турецкий кулир пенье,95% хлопок/5% эластан; regular≈190GSM, oversize≈220GSM|Сохранить приблизительность; exact current base scope; старый range210–220 сверить с актуальной версией, не новым догадочным batch|
| Худи |90% хлопок/10% полиэстер, трёхнитка пенье≈320GSM, слегка oversize/unisex|Заменить runtime unknown для подтверждённых current hoodies; не распространить на лонгслив/будущий zip; происхождение ткани худи не дано|
| Крой |Только oversize; regular ещё есть в UI и будет выключен|Это новая known catalogue mismatch, отключение не выполнено; проверить retail и Custom отдельно|
| Сетка |Владелец пришлёт изображение hoodie chart|Числа/подписи/допуски не угадывать; одно versioned image+numeric guide для сайта/бота|
| Blank media |Есть front/back в Custom Prints; реальные hoodie photos всеoversize|Агент находит assets; render конструктора маркируется как visualization, не реальное фото конкретной вещи|
| Уход/DTF |30°C; владелец заменяет30+ на более100 стирок при уходе|Сохранить100 как owner-stated claim; не назвать lab test/вечной гарантией; head model произнесена неуверенно|
| Доказательства |Первые принты сохранились; есть новостное stretch-video; детали фото/видео по просьбе|Связать actual assets; растяжение не доказывает100 стирок; не выдумывать18-месячный тест|
| Принты |Покровск2версии/сова, Харьков-костёр2022, Kharkiv forever, ранний енот, дрон с версиями|Связать exact design/version, не выводить историю по похожей картинке; неизвестный смысл — к основателю|
|200+COD |Предметное исключение при fit/gift/quality-trust, full default|Source-bound offer/acceptance; не заставлять каждого клиента идти к менеджеру за штатной формулировкой|
|200 при отказе |Удержать все200, даже если перевозка150|Не обещать вернуть50/«ничего не теряете»; merchant cost и customer-retained amount раздельны|
| Бесплатная доставка |≥3000 после скидок **и full payment**;200+COD исключён|Новая policy version и actual mode в snapshot/ACK/Attempt/TTN payer; SALE-07|
| Военные условия |Бригадные/custom под подразделение черезadmin; обычный каталог — общие бонусы|Сужение прежнегоD037, без автоматического запросаУБД для retail|
| Loyalty/promo |100points→10%;UGC10%;QR/vote/партнёрские программы, совместимость и срок по каждому праву|D033 арифметика остаётся; points reward и multi-promo ещё не закрыты кодом; SALE-08|
| Bulk/drop |Индивидуально возможны10–20%; лучше созвон/удобный чат|Не публичный универсальный диапазон цены; убрать автоматическое15–35% promise|
| Manager |Владелец получает Telegram с конкретным вопросом и подключается|Один owned case и событийное ожидание; не generic «посмотрите» и не фиктивныйSLA|
| Общение |Explicit предпочтения сохраняются всё время|MEM-03 business unknown закрыт; текущая correction выше истории, без speculative profile|
| Успех |Confirmed200 либо full payment|Paid conversion отдельно от fully settled/revenue; один Order/lineage не становится двумя покупками|

### Что ответ не разрешил автоматически

Q15 описывает **сроки кодов**, но не решает, должен ли бот предлагать ручную уступку5/10 за price objection. Нельзя записать новую отменуD021 или разрешение скидки всем. Существующие approvals/entitlements сохраняются до явного изменения.

Q09 не содержит названий наград основателя: ответ ушёл в скидки. Новых биографических claims не добавлять. Q24 подтверждает текущие **сетки**, не вечную актуальность тела/мерок любого клиента. Срок/style implementation делегированы без нового общего интервью.

Один отказ с200 решён. Перенос денег между несколькими test-size посылками и финальной покупкой ещё не имеет однозначной формулы; не создавать её из оборота «вычесть доставку». Брак/ошибка магазина не покрыты правилом удержания обычного отказа.

### Изменённые прежние решения

- D014/D044: новые current hoodie specs и wear/resource owner facts получены; runtime resolver ещё прежний.
- D037: retail military concession сужен до brigade/custom route; стандартный retail пользуется общими программами.
- D041: full default сохранён,200 eligibility конкретизирован, test-size legs требуют отдельной привязки; удержание обычного отказа теперь точно200.
- D043: добавлен full-only criterion бесплатной доставки. Старое нетто≥3000 реализовано корректно для прежнего контракта, но недостаточно для нового.
- D033/D051: последовательный stacking/nullable program expiry сохраняются; «у каждого кода свой срок» не означает отменуUGC90дней или expiry всех баллов.
- D021: новое решение об auto rescue не получено; не подменять owner statement рекомендацией аудита.
- D012: обычный14-дневный service route; дополнительная автоматическая замена принта через полгода не утверждена.

## Главные выводы для корректировки плана

1. **Единая исходная база восстановлена.** REL-01 закрыт: primary main совпадает с production/origin, старые локальные версии сохранены отдельно. Новую реализацию продолжать из проверенного текущего HEAD, не накладывая архивный patch целиком.
2. **Рекламная identity уже доходит, креатив — не полностью.** Нужны единые source-bound resolver, compatible merge и поддерживаемое заполнение multi-product creative. Простая передача account-level последней рекламы проблему усугубит.
3. **Футболочный случай существенно исправлен; готовность худи из этого не следует.** У худи иная ось конфигурации; состав/плотность/unisex теперь получены от владельца, но ещё не опубликованы. Числовая сетка ожидается; смена категории может наследовать старую тему.
4. **Есть конечные лимиты, после которых память/фокус не восстанавливаются сами.** Ограничить вычисления правильно; сделать накопление истории причиной постоянного отказа неправильно.
5. **Корректный текст не всегда считается ответом.** Материал/уход/смысл не имеют такого же semantic coverage, как уже реализованные fit/audience. Отсюда возможен ложный незавершённый долг.
6. **Скорость требует проверки всего пути.** Последовательная очередь, глобальный provider incident и недостаточный send reserve способны задержать обычное продолжение продажи.
7. **Редактирование brand.md не означает изменение знаний.** На сервере также остаются undeclared required scenarios. Нужны честная публикация, scoped знания и доказательство попадания в фактический запрос.
8. **Website current price не означает, что любой текст сайта — подтверждённая policy.** Найдены противоречия FAQ сегодняшним пояснениям и ранее принятым правилам.
9. **10% сейчас нельзя честно измерить существующим делением показателей.** Нужны один cohort/denominator и paid authority; созданный заказ не является оплатой.

## Что уже сделано и должно сохраниться

Это не новый список production-багов:

- Фото-консультация по конкретной футболке, сохранение обсуждаемого принта, classic/oversize, scoped unisex, подписи «пример кроя с другим принтом» и native chart уже реализованы. История проверки релиза: [17.10 focused fit report](/Users/zainllw0w/TwoComms/site/docs/instagram_bot_audit/new/2026-10-17_FOCUSED_PRODUCT_FIT_CONSULTATION.md:158).
- Исправлены прежние проблемы own echo, manager receipt/history timestamps, ложный chart-URL guard и часть starvation при фоновых задачах. Старую последовательность «Звична → ничего» нельзя объявлять заново доказанным текущим инцидентом.
- Карточка уже знает source-bound product/type/size/fit/colour/quantity/purchase intent; карта различает actual/inferred/possible. Response debt и attention snapshot существуют. Новая аккуратная фраза при отсутствии обязательного action уже заменяет старое необоснованное «всё сделано».
- Native checkout уже сопоставляет фактически оплаченный Order и позиции с paid snapshot, пересчитывает доставку после изменения корзины и требует fresh review при особых согласованных условиях.
- Прежний порог3000 после скидок, goods-only total и разграничение merchant_free / carrier_recipient реализованы в orders/services/shipping_policy.py:46–82. Пересчёт и acknowledgment при потере бесплатной доставки существуют в ig_checkout_cart_amendments.py:276–305. Новое owner-условие full-only пока отсутствует; см.SALE-07.
- Есть approved facts, immutable publication, selected/omitted module IDs, compiled request manifest, actual winning model, delivery receipts и guard/authority gates. Создавать рядом ещё одну KB, денежную policy или «единственный новый паспорт клиента» не требуется.
- Память уже имеет датированные собственные цитаты клиента, provenance, source clocks, CAS, reset/erasure и quota gates; история не даёт stock/payment authority. Недостающая durable size/recipient/fit memory прямо признана в плане.

## Находки: базовая интеграция

### REL-01 · Закрыто · Основной checkout приведён к production без потери прежних файлов

**GIT + LOCAL HASH + LIVE.** Ранее primary отставал на 84 коммита/735 путей и содержал смешанный dirty overlay. 18.10 в 15:37 UTC выполнен fast-forward main на a4818a1b7 после отдельного byte-for-byte snapshot. GitHub и production имеют тот же commit и tree; remote tracked diff пуст. 15:41 UTC проверены 5356 local tracked blobs / 354772953 байта — raw content mismatches=0, index tree совпадает.

**Почему релиз не был виден в основной папке.** Он уже находился в GitHub и отдельном local worktree; изменения не существовали только на сервере. Linked worktrees разделяют Git objects, но сохраняют собственные HEAD/ветки и файлы: release из другой ветки не обновляет основной checkout автоматически. Primary main оставался старым, а его dirty overlay частично воспроизводил новые файлы. Это расхождение checkout, а не потеря84 опубликованных коммитов.

**Сохранение данных.** 68 modified tracked +196 incoming untracked collisions были скопированы и SHA-256 проверены до замены. Ещё один unique local test, отсутствующий в релизе, убран из активного test-discovery namespace с сохранением. Всего265 файлов в [recovery snapshot](/Users/zainllw0w/TwoComms/local-sync-backups/20261018T153739Z/README.md); manifest, старый binary patch и исходные версии позволяют сравнить/восстановить конкретный путь. Из264 основных файлов202 уже совпадали с target,62 отличались. Архивный patch нельзя применять целиком поверх нового main.

Не использовались reset, stash или blanket clean/restore. Restore выполнялся только по заранее сохранённому explicit manifest; удалялись только проверенные backed-up collisions. Неизменяемые локальные материалы, .env, .venv, базы и media не переносились с production и не очищались. Оба audit-документа сохранились и теперь дополнены.

**Оставшийся gate.** Основная кодовая база синхронизирована, но это не доказывает идентичность окружения/данных. Результаты tests, migration graph и production metadata приведены ниже; изменения поведения из остальных findings ещё не реализованы.

### REL-02 · P2 · Source parity не равна идентичности среды исполнения

**LOCAL + LIVE metadata.** Локально CPython3.14.6 / Django6.1, как требует AGENTS.md; на сервере под тем же configured virtualenv — CPython3.14.8 / Django6.1 (read-only 15:41:40 UTC). Новый bug из различия patch-версии не доказан. Локальные тесты используют SQLite, production — MariaDB; служебные данные/секреты/настройки публикации не должны копироваться целиком ради похожести.

**Контракт.** Явно фиксировать source SHA, Python/Django и DB backend в evidence; runtime pin обновлять отдельным согласованным изменением проектной спецификации, без подмены обязательного local3.14.6. Dependency metadata локально проверены:91 packages compatible. Не объявлять прохождение SQLite-тестов доказательством SQL-lock/JSON поведения production MariaDB.

**Миграции.** Реальный graph загружен с MIGRATION_MODULES={}, а не disabled test profile:238 management migrations, leaf0236_checkout_amendment_resolution, graph consistent/acyclic. На production read-only перечислены те же238 applied migrations; пропущенных нет,0227–0236 на месте. Helper _resumable_schema.py не является миграцией. Новой миграции или owner business-вопроса из этого не требуется.

## Находки: рекламный вход

### AD-01 · P1 · Содержимое креатива теряется между webhook и sealed request

**CODE.** [ig_turn_revisions.py:136](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_turn_revisions.py:136) сохраняет ref/ad_id/source/type и digest, исключая ads_context_data.post_id/ad_title/photo_url/video_url. ig_turn_capture.py:494–502 сериализует именно этот referral. Полный payload отдельно остаётся на клиенте (instagram_bot.py:17682–17710); media extraction :17405–17423 обрабатывает attachments/story, а не рекламный креатив.

**Сценарий и ущерб.** Клиент из рекламы трёх вещей пишет «та, що справа». Креатив мог присутствовать на ingress, но модель получает identity без самой сцены. Повторный запрос фото увеличивает усилие; угадывание даёт чужую вещь. Частота такого случая на production не измерялась.

**Контракт улучшения.** Bounded quoted ad context с source MID, ad/ref/post и явной доступностью media; изображение проходит существующие URL/ownership/budget gates. Креатив и mapping дают candidates, не выбранный SKU, цену или наличие. Делать вместе с AD-05.

**План / приёмка.** Дополнить P0-1/P2-5/P4-1: nested referral → capture → actual dispatch payload; expired/missing URL, только post_id, injected title, multi-product и reset. Тест persistence на клиенте этот gate не заменяет. Brief Q13–Q14.

### AD-02 · P1 · Два resolver по-разному считают mapping разрешённым

**CODE.** [ig_turn_capture.py:277](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_turn_capture.py:277): собственная выборка объявляет одну active-строку resolved, без published product/usable theme checks. ig_ad_referral.py:54–69 эти проверки делает. ig_turn_intelligence.py:371–375,440 признаёт первый результат exact и снимает ambiguous_ad_referral.

**Сценарий.** Активная реклама снятого с публикации товара либо пустой mapping → «ціна?». Контекст может быть exact, хотя typed resolver говорит unavailable. Это расхождение не доказывает обход stock/payment guards.

**Контракт / приёмка.** Один bounded resolver и одинаковые status/reasons/candidates для captured intelligence, карты и дальнейших consumers. Проверить unpublished, theme-only, empty theme, duplicates, конфликт ad/ref, DB unavailable. Нельзя fallback к последней рекламе аккаунта. Уже относится к P0-1/P0-2/P4-1; нового бизнес-решения не нужно.

### AD-03 · P1 · Обычный редактор не задаёт точные товары; несколько товаров не представлены

**CODE.** ig_bot_models.py:4796–4806 имеет один nullable Product FK, тему и landing_note. [bot.html:1672](/Users/zainllw0w/TwoComms/site/twocomms/management/templates/management/bot.html:1672) и :4851 редактируют ad ID/title/theme; bot_views.py:8725–8728,8924–8930 не возвращает/не сохраняет product. Для landing_note prompt consumer не найден.

**Сценарий.** Владелец создаёт запись обычной панелью, ожидая точного товара. Получается тема. Рекламу трёх вещей нельзя задать как три проверенных кандидата; один FK может создать ложную определённость.

**Контракт.** Поддерживаемая authoring-цепочка single_product / product_set / theme / unknown; bounded product candidates и различимый порядок/позиции, reviewed creative reference. Свободный CTA остаётся quoted evidence. Даже один кандидат не выбирает товар за клиента.

**План / приёмка.** Конкретизация P4-1/P0-1: создать single/multi через UI → reload → capture → actual request; «друга» и «обидві» не теряют позиции. Brief Q13–Q14. Новый отдельный каталог рекламных цен не создавать.

### AD-04 · P1 · Несколько рекламных касаний текущего burst сжимаются в одно

**CODE.** [ig_turn_capture.py:280](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_turn_capture.py:280) выбирает refs[-1]; :297–301 разрешает только последний mapping. ig_turn_intelligence.py:783–784 передаёт одну structured связку. Scalar referrals отдельных сообщений остаются, поэтому полной потери первой identity не утверждаем. Account-level _touch_history (instagram_bot.py:17692–17704) не заменяет message/time scope.

**Сценарий.** Реклама A «для мене», реклама B «на подарунок», затем «який розмір?» одним burst. Last touch не означает одну вещь/одного получателя.

**Контракт / приёмка.** Ordered source-bound touches, distinct candidates/line/recipient scopes; разные targets создают ambiguity либо отдельные позиции. Проверить A→B того же/разного товара, «не A, а B», «обидві», разные получатели и reset. Attribution не становится customer selection. Дополнение P0-1/P0-2/P4-1/P7-1.B; технически не зависит от нового Brief.

### AD-05 · P1 · Merge может соединить identity одной рекламы и креатив другой

**CODE.** [instagram_bot.py:17115](/Users/zainllw0w/TwoComms/site/twocomms/management/services/instagram_bot.py:17115) дополняет nested referral без проверки совместимости ad/ref/post. tests_ig_referral_context.py:16–45 закрепляет смешение message-ad с title event-campaign/video postback-ad.

**Сценарий.** Nested message — футболка A, transitional postback — худи B. Получается A с картинкой B. Передача такого креатива Gemini после AD-01 усилит ошибку распознавания.

**Контракт / приёмка.** Nested identity основная; complementary merge только при совместимых identities. Конфликтующие evidence сохраняются раздельно с reason; missing identity не доказывает совместимость. Same-ID обогащается, different-ID creative не смешивается. Проверять provenance изображения. Дополнение P0-1/P2-5; исправить и прежнее неверное тестовое ожидание.

### AD-06 · P2 · Mapping не имеет версии, привязанной к креативу

**CODE.** ig_bot_models.py:4796–4806 хранит active/created/updated, но не creative/post revision/review validity. ig_turn_capture.py:297–301 фиксирует pk/product/theme; ig_journey_ad_entry.py:49–63 заново разрешает текущую запись. Manifest :441 честно не обещает реконструкцию полного payload.

**Сценарий.** Пока старое сообщение ждёт обработки, под тем же ad/ref меняют ролик или товары. Старый вход связывается с новым mapping; объяснить прежний ответ трудно.

**Контракт / приёмка.** Reviewed ad-context revision с creative/post evidence, candidates, проверившим лицом/временем; revision/digest в captured request. Unknown freshness остаётся unknown, произвольный TTL не придумывается. Проверить mapping A→B до generation и после dispatch; история показывает использованную версию или честную невосстановимость. Дополнение P0-3/P2-8/P4-1. Brief Q13–Q14 и Q29 об attribution.

### AD-07 · P1 · Standalone referral существующего диалога отбрасывается до attribution

**CODE + OFFICIAL DOC черезContext7.** ig_webhook_inbox.py:90–95 возвращает ignored_nonmessage, если нет message/postback. В официальном ig.me описан отдельный messaging_referral при открытии уже существующего диалога; в нём может быть event.referral без новой реплики.

**Сценарий.** Сайт/объявление ведёт по ig.me с opaque ref; старый клиент открывает Direct, attribution-only event отвергнут. Предложенный UTM→ig.me bridge без исправления ingress не работает для этой формы события.

**Контракт / приёмка.** Отдельный immutable attribution event с namespace/sender/owner/event key/provider time/ref evidence, signature/dedup/ownership. Не фабриковать customer message/intent, покупку, автоматический reply или разрешённое окно только по клику. Message, postback и standalone варианты тестировать раздельно для фактически подключённой Meta integration/API version. Не менять subscription по неточному названию из примера без проверки текущего поля.

[Meta: ig.me](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/ig-me). ДополнениеP0-1/P0-2/P2-5 и proposed acquisition bridge; бизнес-решениеQ13 делегировано, доступы у аккаунта не проверялись.


## Находки: скорость и продолжение разговора

### LAT-01 · P1 · Очередь способна превратить обычное продолжение покупки в лишний manager debt

**CODE + LOCAL PROBE actual worker.** ig_revision_live.process_pending_revisions:2575–2628 заранее выбирает due IDs и последовательно подготавливает/исполняет их. Controlled первый model response занимает34с в пределах allowance35с, подтверждённый Meta send11с меньше default timeout12с. Второй клиент из той же выборки уже пересёк общий45с deadline. claim_revision_preparation (ig_turn_revisions.py:425–480) всё же переводит его в PREPARING с истёкшим lease; prepare_revision отвергает preparation_claim_stale до capture/provider. Следующий проход фиксирует preparation_expired manager task.

**Что опровергнуто.** Source не удалён и не помечен answered; он остаётся pending. Это не вечный COLLECTING. Debt/reconciliation сохраняет ответственность, но валидный вопрос требует ручного исхода вместо быстрого ответа. schedule_revision_recovery:685 не принимает PREPARING/COLLECTING; причина recovery_not_expired в этой ветке не доказывает, что время действительно осталось. Production incidence/p95 этим не измерены.

**Контракт / приёмка.** Уменьшить head-of-line blocking, не допускать бессмысленный late prep claim, явно обрабатывать pre-generation expiry в существующем owner/recovery contract. Сохранять per-client order, source/CAS/quotas и no duplicate. Сценарий3–5 клиентов с медленным первым; due→claim и provider-start remaining time, counts preparation_expired по queue depth. Это P0-1/P7/P1-7 correctness, не увеличение одного deadline и не новый вопрос владельцу.

### LAT-02 · P1 · Recovery ждёт другой проект даже при локально eligible независимом кандидате

**CODE + LOCAL PROBE.** inspect_revision_provider_execution (ig_revision_provider_execution.py:265–280) возвращает READY при eligible B, но одновременно next_due_at ожидающего A. schedule_revision_recovery:717–722 без проверки ready выбирает next_due_at от A. В probe A имеет429/retry-after300с, B без skip_reason,7/8HTTP slots остаются; получена задержка около254с после исходного expiry. Затем prepare_outage_recovery:796–800 снова откладывает по любому OPEN chat incident, не сравнивая project/candidates.

**Граница вывода.** B eligible по frozen provider continuation (skip_reason пуст,7HTTP lineage remaining); фактический final project quota admission и удалённое здоровье не проверялись. Incident fingerprint intentionally role/failure_class (ig_provider_incidents.py:102–109); shared notification dedupe сам по себе полезен. Реальный close_stale_incidents:283–321 через5мин без нового failure закрывает aggregate incident, после чего тот же probe spawn-ит recovery. Бесконечная блокировка/универсальное падение всех проектов опровергнуты.30мин — horizon, а не гарантированная длительность incident block.

**Контракт / приёмка.** Ready candidate позволяет рассмотреть запуск с сохранением final quota/deadline admission; cooldown ожидающего A не должен откладывать eligible B. Развести grouped alert и scoped route eligibility. Проверить scheduler delay и execution gate вместе, all exhausted, permission changes, horizon и8HTTP/2scarce/1repair. Сохранить независимые project budgets/reserves, smart routing и0provider I/O health/preflight; не добавлять probes в production. Existing P1-7/P3-9.

### LAT-03 · P1 · Допустимая длительность первой отправки может оставить ответ частичным

**CODE + LOCAL PROBE actual planner/outbox.** ig_revision_live.py:1161 / ig_turn_revisions.py:34 резервируют5с; transport planner допускает до4 частей, HTTP timeout12с. В canonical local probe model34с≤35с породил3 части, первый mocked Meta receipt с MID через11с<12с пересёк общий45с deadline. Per-part CAS корректно остановил хвост: SENT/CANCELLED/CANCELLED, reason revision_deadline_exhausted. Итог delivery_pending, recovery manual reason partial_cancelled_delivery.

**Что опровергнуто.** Replay executor сделал0 дополнительных HTTP/model calls. SENT/UNKNOWN fences и execution-only recovery не повторяют частичную доставку; source/receipt truth сохраняется. Доказано incomplete response/manual action, а не duplicate send или невидимая потеря навсегда. Реальный customer receipt не отправлялся.

**Контракт / приёмка.** Совместно распределить generation, output size/parts и transport-tail budget по фактически планируемому ответу; измерять first useful и complete useful receipts. Не отключать deadline/new-source/window/truth gates, не replay UNKNOWN/SENT, не произвольно урезать HTTP timeout. Проверить late MID, partial continuation ownership и новую входящую между частями. P7/P1-7.

**Уже исправленная соседняя часть.** Daemon background fairness limit=1/soft budgets остаётся; старый batch20/50 не новый production-дефект. Один внешний HTTP способен превысить soft budget — это измеряемое ограничение.

## Находки: память и контекст

### MEM-01 · P1 · Existing head может застрять на incremental backlog; bootstrap и reader имеют другие пределы

**CODE + LOCAL PROBE.** ig_memory_producer.py:663–683 ограничивает incremental delta24 eligible messages; :745–761 — producer source2000 символов. claim_memory_job :299–303 и _finish :425–427 откладывают прежний head/target, без catch-up прогресса. В probe существующий head1 +25 сообщений остался head1 через8 claims с backoff10/20/40/80/160/320/640/900 секунд; девятый process после смены episode также не продвинул head. Reader сообщил timeline_delta_count_budget. Provider HTTP не выполнялся.

**Существенное сужение прежнего вывода.** Bootstrap25 успешно берёт latest24 с complete=false/older_omitted_minimum=1 и затем допускает следующий incremental head2. Нельзя писать «любой backlog>24 ломает память». Источник2001 символ блокирует producer с timeline_delta_char_budget, но reader ещё возвращает historical_as_of и полный2001-character delta: его aggregate limit12000 отличается от producer per-source2000. Не всякое длинное сообщение немедленно скрывает память из Gemini.

**Дополнительная диагностическая неточность.** process_due_memory вернул claimed=0/failed=0/reason=drained, хотя per-client attempts=9/last_reason=timeline_delta_count_budget. Global drained не должно скрывать повторный budget blockage; наличие ошибки UI этим не утверждается.

**Контракт / приёмка.** Bounded resumable catch-up/checkpoints либо owned rebuild/recovery в той же reset/source era; overflow counters отличать от успешно опустевшей очереди. Смена episode не лечит timeline gap автоматически. Сохранять CAS, erasure, source clocks и quotas, не перескакивать correction. Явный reset/rebuild технически возможен, но повторный retry сам не уменьшает gap. Дополнить P0-4/P5-1 eventual progress; нового owner ответа не нужно. Evidence: probes-memory/probe-results.json в snapshot.

### MEM-02 · P2 · Reset-era cap блокирует media-derived focus после13 owner rows

**CODE + LOCAL PROBE.** ig_product_focus.py:168–173 набирает auto_select owner revisions с reset floor и при13 возвращает focus_candidate_scan_capped до проверки доказательств. В native _generate_proposal probe создано13 разных won-image inquiries, последняя _media_identity независимо валидна: owners2…12 дают focused,13 — none/capped. current_product_id=NULL. Смена commercial episode выборку не ограничила.

Даже invalid auto_select rows считаются до proof; отдельный probe на13 rows не вызвал _media_identity ни разу. При этом текущий confirmed canonical product по source_selected_product **обходит** media fallback cap. Полную потерю любого focus утверждать нельзя. Дополнительный later-source cap64 остаётся CODE ограничением, но отдельный64-source probe не запускался.

**Контракт / приёмка.** Ограничивать работу активной доказанной consultation/lineage и bounded latest candidates, а не делать успешную долгую историю причиной пожизненного disqualification до reset. Сохранять ambiguity и canonical selection priority. Проверить13 исторических inquiries, latest photo, foreign namespace/reset/erasure и several positions; не просто поднять cap. P0-1/P0-2/P2-1. Probe доходит до captured context; неправильная реальная отправка не воспроизводилась.

### MEM-03 · P2 · Явная просьба о стиле ответа не имеет столь же устойчивого контракта, как язык

**CODE.** ig_captured_policy_inputs.py:145–152,210–217 и ig_reply_language.py:220–257 фиксируют source-bound язык. Сопоставимого bounded DTO/consumer для «коротко», «без емодзі», «поясніть докладніше» в captured intelligence/state card нет. bot_memory.py:192–208 отбирает восемь цитат, отдавая приоритет другим темам; сохранение такой просьбы не гарантируется.

**Сценарий.** Просьба писать кратко выходит из recent history; Gemini возвращается к общему тону. Это отсутствие проверяемого контракта адаптации, а не утверждение, что модель никогда не подстраивается.

**Контракт / приёмка.** Explicit communication preference с source/scope/supersession; current-turn hints слабые и временные. Последняя собственная просьба выигрывает, manager/quote её не создаёт, необходимая информация остаётся достаточной. Темп — полнота burst и полезное число вопросов, а не предположение «он онлайн/бедный/нерешительный». ig_turn_intent.py:450 уже запрещает такие выводы из молчания. ДополнениеP5-1. Q23 теперь отвечен: explicit постоянная просьба сохраняется до изменения клиентом; временное «сейчас» ограничено source scope. Q24 не устанавливает TTL человеческих мерок. Не повторять владельцу тот же вопрос.

**Отдельный PLAN REMAINDER.** Durable fit/size/recipient ещё не готовая typed-memory система. ig_typed_memory.py:195–250 публикует language/objection/deferred_intent; production callsite read_typed_memory за пределами определения и тестов не найден. Plan90:621–637,230–237 это признаёт. Нужен тот же source-bound reader для prompt/UI/manifest; старая предпочтительная посадка не выбирает новый размер или подарок за клиента. Не создавать второй профиль и не объявлять существующую narrative memory отсутствующей.

## Находки: товар, посадка и изображения

### FIT-01 · P1 · Смена категории может сохранить старую футболочную тему

**CODE.** ig_product_focus.py:141–145,192–201 сбрасывает focus при reset/exact different product/нескольких switch patterns, но не при любом parsed garment-kind change. [ig_fit_consultation.py:146](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_fit_consultation.py:146)–148 использует category старого focus. Словарь ig_commerce_turns.py:34–37 ограничен футболкой/худи; longsleeve/zip отсутствуют и в _OTHER_GARMENT fit parser. В воспроизведённых informational questions garment_type вообще пуст: проблема — невыделенный новый information subject, а не обязательно проигнорированное уже распознанное изменение категории.

**Сценарий.** Обсуждали футболку A → «А жіночі худі у вас є?» либо «А лонгсліви?». Старый товар не обязательно отменён, но текущий информационный вопрос уже о другой категории. Нельзя отвечать футболочной unisex/spec/chart только потому, что A остаётся selected.

**LOCAL PROBE.** Через ProductFocusTests media-winner fixture вопросы «А жіночі худі у вас є?», «А які жіночі лонгсліви є?» и «А жіночі zip худі є?» сохраняют tshirts focus/current_tshirt_audience и дают fallback про unisex-футболки. Это подтверждает неверную применимость fallback, а не реальную отправку клиенту.

**Контракт / приёмка.** Различить retained selection, discussed product и текущий information subject. Категорийный вопрос приостанавливает неприменимые facts, не стирая корзину. Проверить Tshirt→hoodie/longsleeve/zip, возвращение к A, «худі не треба», подарок и multi-line. Дополнение P0-1/P0-2/P4-1. Нужны технические predicates; неизвестные hoodie facts — Brief Q01–Q02.

### FIT-02 · P1 · Консультацию по худи нельзя считать готовой из футболочной реализации

**CODE.** product_catalog migrations 0003:21–33/0006:16–30 задают для худи lining=fleece/no_fleece, а не tshirt fit. ig_revision_catalog_assets.py:158–162 выводит fit code; ig_fit_consultation.py:146–148,196–208 содержит подтверждённый deterministic scope футболок. tests_ig_fit_consultation.py:107–112 специально исключает hoodie scope. При этом fit selector :172–178,223–226 может спрашивать normal/looser без проверки реальных вариантов товара.

**Сценарий.** Реклама худи → «які фасони / є звичайний?»; бот предлагает несуществующую ось либо лишний выбор. У футболки с одним опубликованным фасоном та же ошибка возможна при слишком общем вопросе.

**LOCAL PROBE.** У настоящего configured T-shirt отключён classic: capture_fit_photo_capability возвращает supported_fits=[oversize], но deterministic fallback всё ещё спрашивает «звична чи вільніша». Для худи установлен только неполный contract: тест корректно запрещает распространить tshirt unisex на hoodie; отправка выдуманного regular hoodie не доказана.

**Контракт / приёмка.** Категория → реальные option axes → существующие значения → approved описание. Lining, cut, размер и желаемая свобода посадки — разные понятия. Не выдумывать regular hoodie. Для худи публикуется собственный новый approved unisex fact; футболочный scope не расширяется автоматически. Проверить fleece/no_fleece, единственный крой, ограниченный размерный ряд и unpublished option. P4-1 + P1-1/P1-3; Brief Q01–Q02.

### FIT-03 · P2 · Утверждённый blank/schematic пример кроя не представлен отдельным видом media

**CODE.** ig_revision_catalog_assets.py:108–137 ищет примеры среди published Product той же категории; ig_product_inquiry_presentation.py:89–104 знает product image/variant/cover. Другой принт уже маркируется корректно. Отдельного approved blank/schematic asset с base applicability нет.

**Сценарий.** Клиент просит обе посадки конкретного дизайна; exact classic photo отсутствует. Владелец разрешает показать чистую футболку или схему той же основы. Заводить фиктивный SKU ради иллюстрации нельзя.

**Новый owner input + CODE.** Blank assets уже найдены: storefront/custom_print_config.py:445–476 и static/img/configurator/custom-ref/. Функции называют их renders. Оба hoodie fit ключа ссылаются на одни hoodie-black/hoodie-pink front/back изображения; это не доказательство двух физических кроев. PRODUCT_MATRIX:545–565 при этом ещё предлагает regular/oversize и отдельную premium пенье. Согласовать применимость с owner current-only oversize; не копировать сюда весь ассортимент/цены конструктора как stock truth.

**Контракт / приёмка.** Approved illustration kind + base/cut/version + honest caption; exact design photo / другой принт / blank garment / schematic chart различаются. Схема не доказывает фактический вид принта. Нет approved asset — честное отсутствие и точная сетка, без случайного похожего товара. P4-1/P2-5; Brief Q03–Q04. Content timeline «скоро будут фото» не превращается в обещанную дату.

### FIT-04 · P1 · В одном запросе фото кроя и сетка могут конкурировать

**CODE.** tests_ig_fit_consultation.py:76–82 и ig_response_plan.py:369–385 сохраняют оба намерения. Но [ig_revision_live.py:1624](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_revision_live.py:1624)–1635 имеет chart early return; ig_product_inquiry_presentation.py:132–135 отклоняет запрос при chart phrase.

**Сценарий.** «Покажіть таблицю розмірів та приклади classic крою». Точная фраза проверена: real _catalog_plain_chart_requested=True; _prepare_visible_content вызывает chart factory, fit illustration producer calls=0. Direct prepare_fit_illustration также handled=False. Подписанный chart proof закрывает info:fit/info:size_chart, но оставляет info:fit_presentation и дополнительный fit_option_code obligation этой fixture. Проверены routing и semantic coverage; реальная клиентская доставка не выполнялась.

**Контракт / приёмка.** Один response plan с независимыми допустимыми media obligations, part ownership и итоговой semantic completeness. Combined photo+chart, две сетки/две позиции, missing exact photo, Meta UNKNOWN и новая реплика между частями. Уже отправленную картинку recovery не повторяет. Дополнение существующей response-plan/media приёмки, без нового owner решения.

### FIT-05 · P1 · Картинка сетки не заменяет точные числовые замеры в контексте

**CODE.** size_grid_services.py:184–214 имеет structured rows/notes; для футболок подтверждены отдельные допуски :49–77. ig_catalog_configured_choices.py:131–148 использует guide_data в digest, но не даёт bounded measurements DTO. Native chart producer :454–482 отправляет изображение; bot_catalog.py:158–193 не проецирует числовые замеры.

**Сценарий.** «У моєї зручної кофти півобхват 58 см, яка ваша?» У модели может быть URL сетки без достаточно точной таблицы и определения, что измерено. Высота/вес и визуальная картинка не гарантируют правильный размер.

**LOCAL PROBE.** В реальном _configuration structured width50→58 меняет digest, но numeric width/guide_data/rows не попадают в DTO. ig_catalog_cards.size_grid_for_fit:1333 также возвращает image_url/columns/size labels, не числовые мерки. Не утверждаем, что Gemini никогда не увидит числа из истории/текста; отсутствует проверенный bounded numeric-measurements consumer для консультации.

**Контракт / приёмка.** Exact product/base/cut/version → ограниченные строки измерений, единицы, body-vs-flat-garment semantics, допустимая погрешность и инструкция сравнения. Для hoodie не наследовать tshirt tolerance. По существующим меркам дать обоснованный диапазон/уточнение, без обещания идеальной посадки. Проверить границу двух размеров, личная вещь/подарок, разные категории, missing guide, UK/RU. P4-1/P1-1; Brief Q03.

## Находки: знания, промпты и публикация

### KB-01 · P1 · Владелец получает ложное обещание, что commit brand.md обновит знания

**CODE.** [bot_knowledge/brand.md:3](/Users/zainllw0w/TwoComms/site/twocomms/management/bot_knowledge/brand.md:3)–7 обещает автоматический Git pickup. services/bot_knowledge.py:1–6,57–79 намеренно не читает Markdown в provider payload. ig_brand_source.py:18–31,48–54,175–191 разрешает только tone/size_guidance с точным canonical содержимым; остальные facts остаются у canonical owner, конфликт отклоняется :218–235.

**Сценарий.** Владелец добавил уход/качество/историю принта, уверен, что Gemini это знает. Реальный запрос не изменился; «незнание» выглядит случайным.

**Контракт / приёмка.** Исправить editorial обещание; показывать parsed/reviewed/published/consumed, target owner, version и omissions. Instruction publication и approved fact publication остаются различными. Unpublished edit не меняет prompt; reviewed fact появляется у единственного runtime owner и в actual manifest; conflict/rollback проверяются. Это уже P1-1/P1-2, не новая KB. Brief Q25 определяет операционного владельца.

### KB-02 · P1 · Не хватает scoped owner-maintainable knowledge по основам, темам и принтам

**CODE/OWNER.** approved_public_facts.py:69–95 содержит конкретные tshirt facts/audience, ограниченный hoodie fleece и явную неизвестность composition/GSM. approved_provider_fact_definitions :350–361 получает locale и возвращает все facts языка; bot_knowledge.py:65–79 не выбирает по product/base/topic. Каталог :113–176 содержит конфигурацию и visual fingerprint, не approved print story/spec/care. Fit consumer уже показывает правильный scoped pattern.

**Сценарий.** «Чим ваш худі кращий для мене?», «Чому цей принт про Покровськ?», «Як прати?» — запрос требует разных доказанных тем. Сегодняшний рассказ владельца богат, но не структурирован по товару/партии и ещё не опубликован.

**Уточнение после ответов.** Q01/Q02 теперь дали90/10≈320GSM трёхнитка пенье и unisex слегкаoversize. Называть эти owner facts всё ещё неизвестными неверно; **кодовое** unknown остаётся до обновления reviewed owner/captured applicability. Данных лонгслива и будущих партий нет. ИсторииQ08 тоже уже записаны, но требуют design/version binding.

**Контракт / приёмка.** Модульная editorial база, описанная ниже; runtime через existing reviewed owner/compiler. Stable fact key, product/base/batch/topic/locale, source/review/version, allowed claim и ограничения. Не передавать весь корпус на каждый ход; начать с проверяемого selectors, не внедрять embeddings без измеренной нужды. Scope mismatch/unknown base/future product → честный unknown; irrelevant greeting/service не получает историю всей компании. Дополнить P1-1/P1-3, не переписывать их. Brief Q01–Q10.

### KB-03 · P1 · Mandatory sales block подаёт custom/rescue/collaboration каждому non-service ходу

**CODE.** [instagram_bot.py:1918](/Users/zainllw0w/TwoComms/site/twocomms/management/services/instagram_bot.py:1918)–1946 одновременно содержит rescue5/10, «будь-який DTF-принт», Telegram intake и designer/creator/collaboration. Выбор automation_guardrails :1972–1983 различает только open service case и всё остальное. Captured chain: ig_captured_policy_inputs.py:182–184 → ig_turn_integration.py:130–138 → instagram_bot.py:8961–9002 mandatory dynamic.

**Сценарий.** Обычное «дякую», «ціна?» либо вопрос о ткани получает побочные продажные инструкции. Исправить tags optional playbook недостаточно — нерелевантный текст остаётся mandatory.

**Контракт / приёмка.** Safety/authority boundaries универсальны; topic-specific продажи выбираются existing publication selector по captured own sources. Для custom: «можем рассмотреть», без универсальной feasibility. Actual payload на greeting/thanks/material/price/custom/collaboration/service; релевантность не должна удалять защитные ограничения. Точный residual P1-3, Plan90:279 уже требует greeting без custom. Brief Q16–Q18 только о бизнес-границах, технический routing не ждёт ответов.

### KB-04 · P1 · Guard success ещё не означает проверку всех заявлений о ткани и качестве

**CODE.** bot_knowledge.py:35–41 не делает approved knowledge modules required. ig_policy_compiler.py:240–247,291–306 может опустить optional knowledge, включая unknown-base boundaries. ReplyTruthContext (ig_reply_truth.py:149–184) уже богат timing/availability/payment/shipping authorities, но не имеет общей fabric/origin/quality/print-guarantee applicability. Правила guarantee :539–540,919–924,1209–1210 относятся к срокам. Fit/audience scope проверяется отдельно, поэтому его наличие не закрывает все product claims.

**Сценарий.** Такие заявления не имеют общей предметной проверки fabric/origin/quality applicability и могут остаться неподтверждёнными; конкретный ответ может быть отклонён другими guards. Это CODE coverage gap, не воспроизведённый live bypass.

**Контракт / приёмка.** Минимальная обязательная граница unknown claims; нужный утверждённый material доступен prompt и fallback. Если вводится typed claim validation, один captured scope/version для обоих; не пытаться доказать всё широкой regex. Проверить selected/foreign/unknown base, omitted knowledge, quoted/negated claims и неподтверждённое превосходство. P1-1/P1-3/P0-3; Brief Q01/Q05–Q09.

### KB-05 · P1 · Активная production publication не объявляет required-scenario coverage

**LIVE + CODE.** Активная publication v2 из 20 инструкций не имеет required_for_scenarios ни у одного module (read-only наблюдение выше). ig_required_policy.py:176–209 при undeclared оставляет ready=True с legacy_unconfigured / legacy_required_scenarios_undeclared. Engine существует, но сама публикация не обещает полного покрытия.

**Сценарий.** На size/custom/objection route есть модель и выбранные инструкции, однако «required coverage ready» в legacy режиме не означает, что критичный сценарий объявлен и гарантированно admitted в budget.

**Контракт / приёмка.** Матрица declared scenarios/content → existing draft/review/publish → actual revision с declared/covered/undeclared и selected required IDs. Required overflow даёт named degradation без provider HTTP. Legacy diagnostic показывается честно; не объявлять механизм отсутствующим или автоматически публиковать seed поверх работающего набора. Уже P1-3, новое доказательство — текущее production-состояние. До изменения проверить содержимое/selector eligibility и rollback; новый owner ответ о механике не нужен.

### KB-06 · P1 · FAQ содержит неурегулированные расхождения с owner direction и guarded условиями

**CODE + LIVE HTTP.** [support_content.py:38](/Users/zainllw0w/TwoComms/site/twocomms/storefront/support_content.py:38) и последующие HELP_FAQ_ITEMS публикуются через :772 и static_pages.py:1218–1236. В [текущем FAQ](https://twocomms.shop/faq/) подтверждены пять стадий QC, обобщённый крой, 30+ циклов и оптовые проценты; сегодня владелец назвал три этапа QC, а D038 запрещает автоматически обещать оптовую цену по числу единиц. В FAQ также есть точные сроки, которые не следует переносить в DM как подтверждённую возможность конкретного заказа. Delivery FAQ :199 широко обещает COD и full COD постоянным клиентам; это шире текущего guarded checkout.

**Почему влияет.** Покупатель сравнивает DM с сайтом. Владелец просит «всё актуальное на сайте», но это надёжно для calculated current price, не для любого исторически написанного маркетингового текста. Автоматически импортировать такой FAQ в approved facts опасно.

**Новые выявленные расхождения care.** support_content.py:143/147 всё ещё30+; :296 описывает естественное старение50+ и обещает12месяцев гарантии. Теперь owner заявляет более100 при уходе и не даёт автоматической полугодовой замены. Сверить FAQ/care/structured data/переводы единым пакетом; просто заменить два числа30 на100 недостаточно. Не превращать owner statement в несуществующий лабораторный протокол.

**Контракт.** Claim inventory: exact field → runtime/business owner → evidence → подтверждено/устарело/спорно → одинаковая короткая публичная формулировка. Три этапа уже названы владельцем: пошив, нанесение принта, упаковка/отправка. Пять стадий сайта не подтверждены. Агент сверяет существующие материалы; без доказательства не публикует пять стадий и не требует нового общего интервью. Проверить не только HTML, но и FAQ structured data/переводы. Текущий аудит ничего на сайте не меняет и не утверждает юридическую корректность старых условий.

**План / приёмка.** Дополнить P1-1/P4-1 cross-channel consistency; не создавать третий money owner. Brief Q01–Q07/Q11–Q15/Q17 указывают существующее противоречие вместо повторного общего вопроса. До ratification точные спорные promises не становятся provider facts.

## Находки: содержательный ответ и коммерческая логика

### SALE-01 · P1 · Даже approved состав принудительно требует manager review и не имеет полного factual coverage

**CODE + LOCAL PROBE, UK/RU/EN.** На вопросе о составе подтверждённой regular-футболки ResponsePlan.coverage (ig_response_plan.py:361–464) принимает size, но оставляет info:question/recovery после правильного ответа95/5. Однако live boundary останавливает такой ответ раньше: capture_product_inquiries (ig_product_inquiries.py:134–139) относит material/composition к product_fact_unverified без проверки scoped approved facts. validate_inquiry_response :294–326 выдаёт product_inquiry_handoff_required без manager=true; с manager=true и уверенным95/5 — product_information_unverified. Этот guard реально вызывается в ig_revision_live.py:893.

**Сценарий.** «L. Який склад футболки?» на base с уже approved95% хлопка/5% эластана. Бот технически знает факт, но inquiry disposition всё равно требует ручное уточнение; простой material-topic fix ещё не позволит корректно ответить. Это воспроизведённые локальные контракты, не измеренная доля таких customer failures на production.

**Контракт / приёмка.** Один captured scoped approved assertion должен попадать одновременно в inquiry disposition, factual guard, prompt/fallback и semantic coverage. Тогда известный ответ не создаёт лишнего manager case/response debt. Для неподтверждённой или ещё не опубликованной области применения факта guard сохраняется. Текущие худи90/10, около320GSM, пенье уже подтверждены владельцем: нужна reviewed публикация с привязкой к основе, а не повторный вопрос о составе. Проверить known/unknown/foreign base, composition+choice, уверенную неподтверждённую цифру, corrected source и missing publication. Добавление info:material без изменения inquiry authority недостаточно; выключать guard целиком нельзя. Existing P0/P4 coverage +P1-1/3; новые вопросы о95/5 владельцу не нужны.

### SALE-02 · P1 · Retail rescue, программы лояльности и сроки кодов требуют разных оснований

**CODE + OWNER.** bot_followups.py:107–136,1823–1830 сохраняет recommendations5/10 при ряде unpaid/thinking/negotiation states; :2812–2901 требует manager approval. Новый ordinary-intent follow-up безскидки. D021 допускает бюджетное исключение, включая явно установленный барьер в первой реплике, без искусственного повторения.

Новый ответQ15 описывает неделю/две/конец года/постоянные коды, не судьбу proactive5/10. Поэтому прежняя рекомендация аудита сократить rescue не превращается в решение владельца. Пользователь одновременно подтвердил множество совместимых program bonuses: это отдельные фактические права, не все одинаковые personal concession.

**Контракт / приёмка.** Explicit reason/case для manager recommendation; program grant с реальными eligibility/expiry/use limit отдельно. Не выдавать скидку по молчанию или question «ціна?» и не отменять действительное право. Срок брать из самого кода;UGC90дней и review upgrades сохраняются по своей программе. Один простой оставшийся бизнес-вопросQ15 — просить ли ручную уступку за «дорого»; технические source/approval guards не ждут этого ответа. P1-4/P4-2.

### SALE-03 · P1 · Разговорное согласие на 200+COD не совпадает с нынешним серверным predicate

**CODE.** ig_checkout_policy.py:15–26,54–108 допускает по последнему USER quick reply либо явному сочетанию 200 + prepayment/COD. ig_revision_checkout.py:70–84,433–436 повторяет sealed-source gate.

**Сценарий.** Консультация по посадке → предложение осмотра по согласованным условиям → «Так, підходить». Или прямое «Можна наложкою?» без числа. Сам такой текст не воспроизводит допуск; пользователь может снова получить full-only/уточнение.

**Ответы Q11/Q12 получены.** Fit/gift/quality-trust after useful consultation, full default;200 входит в goods при выкупе, при обычном отказе удерживается целиком. Это штатный eligible сценарий бота, а не требование нового ручного разрешения на каждую фразу. Custom/full-only и availability guards сохраняются. Несколько test-size legs требуют отдельного учёта; formula неизвестна.

**Контракт.** Source-bound eligible offer и acceptance конкретного order scope; клиенту не нужно повторять технические слова «200 передоплата». Full default и custom full-only остаются. Удержание200 для обычного отказа теперь явно утверждено. Не переносить его на подтверждённый дефект/ошибку магазина или любые неподтверждённые категории; частные расходы и legs не угадывать.

**Приёмка.** Предложение → короткое согласие/отказ/изменение корзины; обычный trust/fit case; custom/international исключения; paid amount, остаток, доставка и комиссия раздельно. D041/P4-2 уже существуют; новый delta — eligibility/acceptance linkage. ОтветыQ11–Q12 записаны; остаётся только неоднозначный multi-leg money treatment. Остальные технические исправления не ждут его.

### SALE-04 · P2 · Validator согласованной цены распознаёт число лучше, чем смысл предложения

**CODE + LOCAL SERVICE PROBE, ограниченный риск.** [ig_checkout.py:208](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_checkout.py:208)–267 требует manager role, совпавшее число и последующее «так/ок». Внутри нет отрицания/вопроса/историчности/configuration/episode scope. Число из «не 900 грн, а 1000 грн» может попасть в amounts. Теперь достигнут настоящий create_or_update_proposal в disposable SQLite: manager «Не800грн, а950грн» + customer «Так, оформлюйте» создал quoted_total800 при catalog_total950. Historical manager question «Раніше ця футболка коштувала800грн?» дал тот же результат. Положительный800 offer — контроль. Это подтверждённое принятие неправильного evidence публичным service, но не воспроизведённый native request→payment exploit; final HTTP/link/оплаты не было.

**Контракт / приёмка.** Однозначное положительное manager offer с source/episode/configuration и fresh approval. Negated/historical/question amounts не авторизуют цену; acceptance относится к этому offer, изменение условий требует review. Сохранить уже работающий ig_checkout_cart_amendments.py:719–721 fresh review для discounted/assisted/prepayment edits. Existing P4-2 authority remainder, не отдельная новая скидочная система. Бизнес-ответ не требуется.

### SALE-05 · P1 · Отдельный legacy/manual renderer превращает оплату в обещание «уже собираем»

**CODE, узкая ветка.** ig_order_fulfillment.py:116–132 обещает packing now/«Собираем» и «в ближайшие пару часов» для изменений. Producer :259–270 и final revalidation :568–575 проверяют verified payment, отсутствие TTN и не done/cancelled; события начала подготовки не требуют. :152–175,336–340 передают обычные hosted-заказы canonical lifecycle, поэтому finding относится к noncanonical/manual/legacy assignments. Активное использование конкретным клиентом не устанавливалось.

**Сценарий.** Подтверждены деньги, физическая подготовка ещё не началась; покупатель уже считает, что заказ собирают и у него искусственный двухчасовой срок.

**Контракт / приёмка.** Receipt сообщает подтверждённую оплату/предоплату/остаток; preparation только по реальному event. Возможность изменения — по фактическому fulfillment state. Проверить manual/noncanonical assignment, а не только основной hosted путь. Это остаток P1-5/P4-2. Новый owner вопрос о таймере не нужен: неподтверждённый таймер убрать по существующему authority contract.

### SALE-06 · P2 · Routing и persisted objections используют разные определения актуального возражения

**CODE, caller coverage требует проверки.** ig_turn_intelligence.py:339 и bot_instruction_routing.py:111–123 исключают quotes/history/negation. ig_objections.py:203–209 — regex по всему тексту; bot_sales_classifier.py:1682–1690 передаёт raw text в persisted lifecycle. Оба пути есть; их активность для каждой revision не доказана. Price-only уже отделён :1568–1575.

**Сценарии.** «Друг каже “дорого”, але мені ціна підходить»; «Раніше було дорого, зараз беру L»; «Не дорого, хочу інший принт». Routing не должен открывать price objection, а CRM затем возвращать его как blocker/reason для follow-up.

**Контракт / приёмка.** Один собственный source/currentness predicate до materialization; historical label не становится текущей потребностью. Проверить ordinary revision и legacy/takeover callers, lifecycle/card/metrics parity. P1-3/P5-1 уже требуют quote/negation safety; это конкретный implementation check, без нового business policy.

### SALE-07 · P1 · Новый full-only shipping rule не выражен в действующей policy

**OWNER POLICY DELTA + CODE.** Сейчас orders/services/shipping_policy.py:51–82 знает только net goods≥threshold. ig_checkout_payment.py:400–410,608–615 и delivery_payment.py:30–77,109–167 не передают/не проверяют pay_type. Generation :1010–1047 уже различает actual prepay_200/online_full и сохраняет attempt.pay_type:1111, но threshold snapshot не использует эту разницу. По новой политике200+COD при≥3000 не должен давать merchant_free.

**Охват.** Кроме платежа: storefront/views/cart.py:114–122 progress, ig_checkout_cart_amendments.py:294–305 display/ACK, ig_reply_authority.py:592–599 разрешённое обещание доставки, Attempt/native delivery contract и TTN payer. Текст вMarkdown не исправляет sender/recipient оплату перевозки.

**Контракт.** Новая версия существующей policy: net eligible goods after permitted discounts≥3000 **AND actual full-payment choice**; fulfillment требует действительного full payment/approved exception. Quote может показывать условную eligibility «за повної оплати», но unknown mode не объявляется уже бесплатным. Full→200 при достигнутом threshold снимает eligibility и требует понятный fresh loss ACK/актуальную payment generation. Промокоды/удаление позиции также переоценивают net goods. COD commission/delivery/deposit не входят в goods basis.

Old agreed/frozen paid terms не пересчитываются задним числом; для изменения неоплаченного offer сохраняются version/consent gatesD018. Последующая оплата наложки после получения не создаёт обещания возврата стоимости уже выполненной перевозки.

**Приёмка.**2999/3000/3001 × full/200/unknown; discount threshold crossing; mode switch/старыйinvoice; paid immutable; split payer; UI/prompt/ACK/TTN consistent. P4-1/P4-2, уточнениеD043, не повторная реализация уже работающего final basket.

### SALE-08 · P1 · Программа100баллов→10% заявлена владельцем, но выдача права остаётся stub

**CODE + PLAN REMAINDER.** storefront/views/profile.py:797–804 предлагает promo10 за100points; :842–852 содержит «Здесь будет логика создания промокода», списывает100 через spend_points и сообщает статический POINTS10 без создания персонального grant. Это уже было замечено вD034-related audit (05_OWNER_DECISIONS.md:1262); новый ответ владельца подтверждает, что программа ожидается рабочей, а не факультативным будущим текстом.

**Контракт / приёмка.** Atomic debit100 + действительный existing UserPromoCode/promo grant с owner/operation/source и usage/expiry. Replay не повторяет списание; failure создания не оставляет клиента без баллов с выдуманным кодом. Два успешных обмена100 не означают один общий unlimited код. Не создавать второй points ledger.

**Совместимость.** Hosted _validate_payload (ig_checkout_payment.py:321–361) и storefront cart.py:2284–2299 пока принимают один promo. Закрыть existing D033 multi-promo reservation/application contract для названных owner каналов, сохранив duplicates/single use/actual expiry и права выданныхUGC. General promo valid_until nullable;UGC factory ig_ugc_rewards.py:499–509 задаёт90дней, review upgrade остаётся отдельной программой. Общая фраза «разные сроки» не отменяет их.

Нельзя сначала убедительно обещать клиенту полноценную покупку/stack бонуса, а затем списать баллы без выдачи. В текущей задаче source/баланс/промокоды не менялись; это evidence кP4-2/P3, а не нехватка ещё одного owner answer.


## Находки: конверсия и измерение результата

### MET-01 · P1 · Существующие показатели не определяют целевую конверсию «обращение → оплаченная покупка»

**CODE.** bot_views.py:8088–8100 считает qualified как клиентов с текущим buying_readiness>=40; :8137–8149 считает distinct paid IgDeal. ig_funnel_analytics.py:926–978 строит отдельные cohorts каждой ступени, с cutoff/right censoring. Эти показатели полезны, но у них разные единицы и основания.

**Сценарий.** Readiness изменился, повторный клиент имеет несколько deals; деление paid/qualified растёт без улучшения нового рекламного customer journey. Текущие >10% этим не доказываются.

**Контракт / приёмка.** Дополнительный versioned cohort-report: единица, qualification на входе, источник/episode, окно оплаты, eligible paid/refund authority и exclusions. Numerator — подмножество denominator; no double count deals/orders/repeats. Late payment/right censoring/timezone/COD отдельно. P2-8 уже требует denominators. OwnerQ27 теперь утвердил confirmed200/full; конкретный cohort/14-day design делегирован агенту и описан ниже. Не требовать от владельца повторного выбора терминов. Достижение 10% — цель после baseline, не гарантированный результат кода.

### MET-02 · P1 · Рекламный отчёт использует текущую client attribution для прежних событий

**CODE.** bot_views.py:7700–7707 выбирает по текущим ad fields; :8014–8027 группирует episode events через client.ad_*; :8271–8275 передаёт все episodes этих client IDs. API честно маркирует basis=current_client_snapshot, historical=False (:8350–8360). Карта ad entry :27–63 лучше разделяет context/selection/reset scope.

**Сценарий.** Клиент пришёл по A, позже по B; старая покупка может попасть к текущему B. Такой snapshot не годится для доказательства исторической эффективности объявления.

**Контракт / приёмка.** Immutable source-cited attribution snapshot commercial episode/order с unknown/ambiguous/legacy, approved attribution policy. A→B→purchase, organic return, reset, repeat orders не переписывают прошлое. Сохранить snapshot metric с честным названием, а не менять исторические данные догадкой. P2-1/P2-8;Q29 делегирован, предложенный first-acquisition/episode-source contract описан ниже.

### MET-03 · P1 · «Оплаты после скидки» могут включать неоплаченный созданный заказ

**CODE.** [ig_funnel_analytics.py:1072](/Users/zainllw0w/TwoComms/site/twocomms/management/services/ig_funnel_analytics.py:1072)–1075 определяет bought_episodes как PAYMENT_CONFIRMED ∪ ORDER_CREATED; :1087–1092 принимает эти события после offer. UI bot.html:4508–4511 называет их «оплат після знижки»/«Оплати без зафіксованої знижки».

**Почему влияет.** Владелец может выбрать убыточную скидочную стратегию по завышенному числу оплат. Частота расхождения на production не измерена.

**Контракт / приёмка.** Payment-confirmed отдельно от order-created; либо явно переименовать объединённый outcome. «После предложения» отдельно от реально применённой скидки и отдельно от причинного эффекта. Unpaid order не увеличивает payments; payment/refund/repeated offers одной покупки согласованы. P2-8; техническая корректность названия не ждёт интервью.

### MET-04 · P2 · Неизмеренный эффект follow-up записан как ноль

**CODE.** ig_funnel_analytics.py:1049–1067 считает SENT tasks, но recovered=0/bought=0 присваивает без поиска исходов. UI consumer этих полей не доказан; finding относится к API/report contract.

**Контракт / приёмка.** Сначала null/not_measured с причиной; затем SENT → own customer response → episode payment в согласованном окне. Measured zero отличается от отсутствия измерения, обычная оплата после DM не называется доказанным causal uplift. P2-8; окно успеха — Brief Q27–Q28.

**Дополнительный PLAN REMAINDER по диагностике.** ig_request_manifest.py:355–360,440–445 честно показывает full_payload_not_retained/not_reconstructable и actual winning model; ig_decision_trace_read_model.py:410 routing_reason_codes=None. Generation, delivery и semantic completeness уже разделены (:388–400), manager receipt не customer reply (:287–292). Для будущих revision дополнить bounded routing reasons/version/source/omission IDs существующей записи; не восстанавливать исторические причины задним числом и не сохранять raw PII prompt ради удобства. Уже P0-3/P2-4/P2-8, журнал :391–397 прямо признаёт ограничения.


## Как сохранить смысл сегодняшних пояснений владельца

Это сводка начального **OWNER input18.10**, дополненная ответами в разделе выше; при расхождении более поздний прямой ответ владельца имеет приоритет. Это не автоматически опубликованные facts. Устаревший Brief не может отменять более позднее принятое решение; narrative также не меняет деньги/availability без соответствующего authority.

| Направление | Уже понятное намерение | Что ещё нужно доказать/уточнить |
|---|---|---|
| Приоритет ассортимента | Перед рекламой главный фокус — худи; футболки и вопросы о других категориях сохраняются | Реальные SKU/base/batch, опубликованные варианты, размеры, availability |
| Посадка | Удерживать выбранный принт; при отсутствии exact фото честно показать approved пример/сетку | Конкретные blank/schematic assets и соответствие основе |
|Качество|Owner specsQ01,30°C,более100стирок как owner claim,QC3этапа|Reviewed scopes/payload;пять стадий/50+/12месяцев сайта не подтверждены|
| Позиционирование | Military × streetwear × graffiti; смысл, комфорт, долговечность и разумная цена | Утверждённые краткие доказательства; клиенту один релевантный аргумент, не манифест |
|История|Основатель-ветеран;own storiesQ08 записаны|Exact design/version binding;новые названия наград не получены|
| Будущее | Минималистичные принты, zip samples, потенциальная вышивка | Только утверждённый статус; не обещать наличие/дату/следующий день |
| Цены | Для нового предложения актуален расчёт сайта по конфигурации; старый Instagram может отставать | Согласованный неизменённый offer не пересчитывается молча; D018 сохраняется |
| Уступки | Общие program bonuses/редкие personal exceptions; brigade/custom военным черезadmin | Multi-promo/grants; спорная bulk-матрица; решение о proactive5/10 не получено |
|200+COD|Full default;fit/gift/quality-trust;при выкупеcredit200,при обычном отказе удерживаются200|Source offer/acceptance; multiple legs отдельно. Не «ничего не теряете»|
|Бесплатная доставка|≥3000 после скидок и full payment;200+COD исключён;split согласован отдельно|Обновить policy/payment-mode snapshots/ACK/TTN;прежний threshold-only код недостаточен|
| Custom | При explicit request или нерешаемом print/design mismatch — одна optional возможность | Feasibility/исходник/макет/финальная цена — команда; готовая покупка не уводится в custom |
| Забота | Полезная связь, отзывы, исправление проблем, улучшение товара | Только соответствующее purpose/channel consent; отзыв не обязательство купить снова |
| Персонализация | Помнить полезное добровольно сообщённое и текущую потребность | Не добывать рацион/друзей/онлайн-активность; не угадывать службу, доход или здоровье |
| Результат | Рост подтверждённых покупок и удовлетворённости; 10% как целевой ориентир | Cohort baseline, окна, denominator, paid/refund и достаточная выборка |

Нельзя публиковать как факт «принт вечный», «любой конкурент хуже/использует китайскую ткань», «одежда точно не выгорает/не скатывается» или «завтра будете носить» без конкретного основания. Краткий рассказ о ценности должен помогать выбору, а не стыдить бюджетом, службой или фразой «скупой платит дважды».

### Существующие решения, которые не нужно спрашивать повторно

Источник: основной docs/instagram_bot_audit/new/2.0/05_OWNER_DECISIONS.md. D014:714–738 — scope спецификации, происхождение ткани ≠ место пошива, старые 90/10 и 320 GSM предварительные. D015–D016:740–780 — exact labelled charts, missing photo и uncertain fit. D017:782–811 — готовому покупателю не нужна лекция, алгоритмы делегированы разработчику. D018:867–894 — текущая новая цена и сохранение неизменённого согласованного предложения. D020:929–937 — изменения принта/custom через команду, нет универсального surcharge. D021–D022:944–1026 — manager discount authority, реальное собственное возражение, отсутствие давления. D025:1115–1132 — claimed prior purchase не verified, новый заказ не блокировать ненужной привязкой истории. D037:1373–1381 — исторический индивидуальный маршрут военной скидки; ответ18.10 сужает его до brigade/custom под подразделение через администрацию. Обычный retail использует общие программы. D038:1383–1397 — старые рекламные цены/опт не дают автоматический quote. D040–D044:1447–1553 — Соломия/адаптация, full default/200 exception, доставка после скидок, approved brand facts. D043 теперь дополнен full-only: бесплатная доставка от3000 после скидок действует только при полной оплате. D051:1691–1699 уточняет сроки entitlements и важнее старой сводной строки.

Прежний вопросник — архив, а не очередь обязательного повторного интервью. Технические таймеры, resolver, schema и выбор retrieval architecture владелец уже делегировал. Brief хранит полученные ответы и точный остаток неизвестного; он не возвращает делегированные решения владельцу.

## Проект рекламного контекста по поручению Q13/Q14

Документация проверена черезContext7: Instagram Platform /llmstxt/developers_facebook_instagram-platform_llms_txt и Marketing API /websites/developers_facebook_marketing-api. Это документация capability, не проверка прав/подписок аккаунтаTwoComms.

### Что Meta действительно может передать

В message webhook для CTD описаны referral ad_id/ref/source и ads_context_data; story reply имеет story id/url, inline reply — mid. Photo может идентифицировать выбранное изображение, но не обязательно товар/номер карточки. Video field в примере описан как thumbnail: нельзя обещать анализ полного ролика. post_id сохранять при наличии, не требовать всегда. [Официальные примеры](https://developers.facebook.com/documentation/instagram-platform/webhooks/examples).

ig.me ref возвращается в message/postback либо отдельном referral существующего диалога. Произвольный параметр text как гарантированное автоматически отправленное сообщение этими первичными extracts не подтверждён. [Meta ig.me](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/ig-me). Native standalone ingestion исправить поAD-07.

Marketing API позволяет читать creative/asset configuration, включая Instagram media/permalink и product set; read permissions и доступ к ad account проверяются отдельно отDM token. Не запрашивать ads_management ради чтения. Порядок/выбор dynamic carousel нельзя восстановить только из списка child_attachments/assets: конфигурация не журнал показа. [Placement metadata](https://developers.facebook.com/docs/marketing-api/dynamic-creative/placement-asset-customization), [Asset feed](https://developers.facebook.com/docs/marketing-api/ad-creative/asset-feed-spec).

### Совместимая цепочка реализации

1. Сохранить source event immutable: namespace/owner/sender, MID либо отдельный event key, timestamp, полученные ad/ref/post/story и bounded context/digest. Не склеивать конфликтующие identities.
2. По неизвестному ad ID запустить bounded deduplicated **асинхронный read importer** с собственным admission/cache/version. Live reply не ждёт Marketing API и не делает lookup на каждой реплике. Нет разрешения/asset — named unavailable, не «рекламы не было».
3. Новая metadata версия: ad/creative/post, API version, fields requested/received, fetched_at, digest, asset identities и candidates. Если metadata получена после клика, это current observation after event, не доказательство исторического вида рекламы.
4. Candidates: retailer IDs/canonical site product links → проверенный mapping → существующее visual identification по owned media → ambiguous/unknown. Один asset может содержать несколько вещей; product set может быть шире показанного. Category hoodie — ориентир, не разрешение угадать неизвестную вещь.
5. Один resolver проверяет published products/themes/conflicts и отдаёт одинаковый captured result Gemini/карте/guard. Exact candidate не равно выбор клиента.
6. Sealed request содержит эту revision/source/candidates и действительно доступные media parts через существующие URL/ownership/budget gates. Ads title — quoted untrusted context, не инструкция. Account-level latest ad не подставляется вместо источника.
7. Сохранить несколько текущих позиций/получателей и retained focus. При uncertainty — одно короткое уточнение со скрином/названием/артикулом/ссылкой, затем не спрашивать заново без изменения.
8. Manual editor остаётся для исправления/исключения/import review, а не единственным способом наполнить каждую рекламу. Manager не нужен на каждом нормальном native ad entry.

**Сайт→Direct.** UTM/Pixel cookies относятся к visit/browser namespace, не раскрывают сайту IGSID. Собственная кнопка может создать opaque signed ref→visit/product-context и получить тот жеref в authenticated webhook. Это доказательство использования ссылки, которую могли переслать; не доказательство личности/ownership заказа. Аккаунт/Order связываются существующим authenticated/signed checkout flow. Не склеивать людей поIP/времени/имени/одному объявлению. Поля _fbc/_fbp описаны в [официальном Meta SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/serverside/user_data.py).

Приёмка: новый/старый диалог, message/postback/standalone, single/carousel/catalog/video/story/organic, конфликтIDs, media expiry, закрытые permissions, reorder/dynamic assets, forwarded ref, несколько товаров, повторныйклиент, reset. Actual request и customer-visible выбранная вещь обязательны; synthetic client-level referral persistence недостаточен.

## Проект KPI, покупок и LTV по поручению Q26–Q29

Это **AGENT DESIGN** на основе прямого делегирования. Владелец утвердил смысл successful conversion=confirmed200/full и запрет считать разговоры/касания покупками; он не называл14дней или конкретную SQL-модель.

### Два разных показателя вместо кругового определения

- Readiness/стадия до оплаты помогает обслуживанию, не даёт факта продажи. «100% квалифицирован после оплаты» отражается как outcome paid_conversion, а не знаменатель из уже оплативших.
- Главный acquisition KPI: доля **доказанно новых коммерческих клиентов**, у которых первая canonical purchase получила verified200/full в течение14дней от первого собственного eligible commercial inquiry.
- Знаменатель фиксируется на входе: unique доказанный client identity, не message/клик/очереднойinvoice. Повторные реплики и ad touches его не увеличивают. Новый клиент не выводится из отсутствия narrative memory; неполная история имеет unknown segment. Входящий коммерческий запрос входит в cohort независимо от того, успел ли бот ответить/распознать товар: ошибки обработки и молчание нельзя исключать из знаменателя.
-7/30-day outcomes дополнительны. Неполное14-day окно — right-censored/pending, не объявленная неудача. Поздняя оплата сохраняется как late outcome; baseline/выборка/interval uncertainty видны. Цель10% не гарантируется кодом.

Сервисный вопрос, вакансия, предложение сотрудничества (включая UGC) и доказанный спам не входят в знаменатель retail-конверсии только на основании такого обращения. UGC не считается спамом сам по себе. Если человек затем действительно хочет купить, возникает соответствующий commercial intent с собственным источником. Язык сам по себе не единственный spam criterion: unknown language не равен нежелательному клиенту.

### Покупка — один экономический объект, не этап разговора

В коде **уже** есть confirmed_purchase_units (bot_payment_truth.py:267–355): provider/manager/order evidence, дедуп по реальному order, partial без известной суммы остаётся amount unknown, не притворяется нулём/полной выручкой. recalculate_client_payment_aggregates:638–677 использует эти units. Это сохраняем.

Связанный Order/существующая подтверждённая purchase lineage — ядро счёта.200 + доплата одногоOrder, два receipt reviews, новая ссылка/amendment, несколько shipment legs и manager takeover не добавляют покупку. Episode sequence — ID консультации, не purchase ordinal. В коде physical_order_count уже отделён от episode count (ig_commercial_episodes.py:1850–1868); неоплаченный физическийOrder также не должен называться подтверждённой покупкой.

Новый настоящий order с verified оплатой — следующая покупка. Partial/staged checkout группируется только по существующей доказанной economic lineage; не сливать разныеOrder по похожей сумме/товару. Orderless legacy reviews и сомнительная связь — отдельный unknown/review, не автоматические несколько покупок.

**Обязательный regression владельца:** длинная переписка, три изменения выбора и три ссылки без оплаты →0новых paid purchases; затем confirmed200 →1; доплата→всё ещё1; следующий настоящий заказ→2. Сервис/обмен не становится повторной продажей. Номер клиенту показывается по реальным paid units, не по количеству этапов.

### Деньги и участие менеджера

Deposit/full outcomes равноправны для owner paid-conversion факта, но amount_received/goods_payable/remaining/COD/fully_settled/refund различаются. Cash/net goods value и retained shipping deposit отдельно;200 при обычном отказе не превращаются автоматически в выручку за доставленный товар. Gross conversion history и net retained purchase outcome показывать раздельно, без стирания refund/cancel evidence.

LTV — наблюдаемая финансовая история доказанно связанного клиента с возвратами/корректировками; не прогноз дохода/платёжеспособности и не сумма всех созданныхinvoices. Повторная покупка не увеличивается из сообщения «спасибо». Verified deposit входит фактически подтверждённой суммой, не всей ценойOrder.

Разрезы: new/repeat, retail/custom, actual applied discount/program, bot-only/human-assisted/hybrid, channel, payment state и source quality. Telegram alert сам неhuman-assistance: нужны реально состоявшиеся действия/сообщения. Это участие, не доказанный causal credit «кто убедил».

### Attribution, которая не переписывает прошлое

Основной acquisition source — первый доказанный commercial entry source, immutable с unknown/ambiguous. Для отдельного заказа — последний доказанный non-direct campaign/source context внутри его commercial episode до первого verified платежа; direct return не стирает уже известный источник. Хранить обе роли с чёткими названиями, не один mutableclient.ad.

Повторная реклама даёт context evidence, **не покупку и не искусственный счётчик касаний в карточке**. Новая покупка связана со своим source snapshot, первая сохраняет свой. Нет verified cross-channel binding — только наблюдаемая история данного scope, не полный lifetime человека. Такая модель конкретизируетP2-8/MET-01/02, не создаёт ещё один money/payment owner.

## Подача качества, бонусов и помощи: короткие правила для публикации

Принцип: **вопрос клиента → полезный scoped факт → доступное доказательство → следующий нужный шаг**. Не грузить всем рассказом ни после «ціна?», ни после подтверждённого размера. Количественный conversion uplift от этих формулировок ещё не измерен.

1. Состав/GSM/пенье — проверенные owner facts текущих основ; «лучший в Украине» и «на века» не подтверждённые сравнительные/безусловные гарантии. Показать ткань/шов/эластичность лучше, чем объявить конкурентов некачественными.
2. Claim более100 сохранить поQ07; условия ухода30°C и отсутствие fake lab test обязательны. Stretch clip подтверждает показанное растяжение, не автоматически число стирок. Не выдавать сомнительную модель головки за факт.
3. Хорошее состояние ранних принтов — опыт владельца, не статистика всех клиентов. Public Highlights — часть отзывов, без буквальной «одной тысячной» и без частныхTelegram скриншотов.
4. Принт со смыслом — exact approved story по design/version. Не делать вывод о службе/травме/убеждениях покупателя и не давить обязанностью поддержать ветерана.
5. Попросилишов/рукав/линейку — существующийasset либо owned request владельцу. Не обещать готовый ролик/время, пока его нет; не запускать новые одинаковыеalertsбезсобытия.
6. Бонусныйhint допустим один раз по релевантности: «У нас бувають приємні бонуси для уважних — деякі підказки заховані просто в контенті бренду». Затем помочь с реальным правом/условиями, не заставлять готового покупателя уходить на поиски. Не обещать код/stack, который кодоваячастьещёневыдаёт.
7. Любая существенная cost/retention информация до принятия оплаты ясна: credit200 при выкупе, удержание200 при обычномотказе,200CODнеfree-shipping, carrier commission отдельно. Игровой hint не скрывает эти условия.
8. Custom от1шт — возможность обсуждения через конструктор/чат/Telegram, не гарантия любого принта/срока/цены. Самоподбор размера и обычный retail вопрос не переправлять вcustom.
9. Стиль: explicit preferences сохраняются, текущие corrections приоритетны. «Подумаю»/молчание не автоматически нужда в скидке; неоднократное «не знаюразмер» требует наглядной fit помощи, не давления.
10. Manager case остаётся паузой только по неизвестному вопросу, а не по всей жизни клиента; известные независимые вопросы отвечаются. Alert содержит проблему/товар/источники/чтоужеответили/чего ждём и остаётся связным с customer receipt.

Примеры после соответствующей публикации:

- **Материал худи:** «Це тринитка пеньє:90% бавовни,10% поліестеру, близько320г/м². Крій помірно вільний,унісекс». Не добавлятьregular выбор.
- **Сомнение в качестве:** «Можу показати нанесення чи шви зблизька. Що вам важливіше роздивитися?» — только если это действительно новый полезный вопрос.
- **Цена:** «У цій основі... [один релевантныйфакт]. Можу показати деталь,щоб вам було легше порівняти». Без «скупойплатитдважды».
- **200:** «Ці200грн входять у вартість речі; при звичайній відмові від посилки не повертаються. Решту товару сплачуєте приотриманні,плюсдоставка йкомісія перевізника».
- **Доставка:** «Безкоштовна доставка від3000грн післязнижок діє за повної оплати. Припередоплаті200грн — затарифомперевізника».
- **Товар неизвестен:** «Надішліть,будь ласка,скриншот або назву — підкажу саме поційречі». Ссылка на нужную категорию помогает, но не подменяет exact answer.
- **Нужен владелец:** «Уточню в адміністрації саме цей момент і повернуся з відповіддю». Не выдумыватьdeadline и не повторять holding.

Техническиеполя,реестрpolicy иproof IDs существуютдляprompt/UI/guard; клиенту не нужна реализационная терминология.


## Предлагаемая архитектура знаний без второго источника истины

Это проектный контракт для уточнения **существующих P1-1/P1-3**, а не набор новых создаваемых файлов в этой задаче.

| Editorial модуль | Что хранит | Кто остаётся runtime authority |
|---|---|---|
| brand/story | Краткое позиционирование, утверждённая публичная история | Reviewed approved facts; tone — published instruction |
| garments/hoodies, tshirts, longsleeves | Base/batch состав, плотность, lining/cut, ощущения и ограничения | Product/base-scoped approved facts + текущие catalog options |
| sizing | Метод измерения, объяснение кроя, допуск | Существующая exact size-grid/configuration authority |
| media | Exact фото, approved другой принт/blank/schematic и подписи | Existing owned asset + native delivery pipeline |
| prints | Design ID, короткий смысл, разрешённые факты/символы | Reviewed print-story scope; visual fingerprint не заменяет автора |
| care/quality | Уход, доказательства тестов, разрешённые claims | Approved base/print facts; конкретный service case — manager |
| objections | Тема сомнения → релевантная помощь/факты/граница остановки | Published selected instructions + текущий собственный source |
| future | Только утверждённый статус тестов/контента | Reviewed status, без stock/date authority |
| коммерческие условия | Editorial ссылки/объяснение, без копии чисел | Checkout/shipping/discount/order owners, actual paid basket |

Минимальные поля знания: stable ID; тип fact/instruction/asset; locale; product/base/batch/design scope; source и review; version/status; даты применимости, если они действительно существуют; разрешённая краткая формулировка; запреты/unknown; runtime owner/consumer. Для ткани и места пошива поля отдельные.

Поток: owner edit → parsed draft → conflict/scope checks → review → versioned обновление соответствующего существующего runtime owner (instruction — immutable publication; canonical fact — reviewed adapter единственного resolver; asset — existing owned authority) → captured current subject/topic → selection с budget → actual provider manifest → source-bound answer/guard → delivery/semantic receipt. Manifest показывает selected/omitted/reason/version. Изменение material/card влияет на соответствующий scope, а не немедленно на любую старую консультацию.

Не нужен большой RAG ради нескольких десятков тем. Сначала точные ключи и bounded selectors; поиск/embeddings добавлять только после измерения пропусков и стоимости. Prompt cache или сжатие применять лишь с сохранением identity/version/scope; cache hit не доказывает актуальность. Не переносить рекламный/клиентский текст в trusted instructions.

### Какая информация о клиенте полезна

| Слой | Полезные сведения | Правило применения |
|---|---|---|
| Текущий запрос | Товар/кандидаты, вопрос, chosen options, количество, бюджет только если назван, срок потребности | Собственные current sources; несколько позиций/получателей раздельно |
| Посадка | Желаемая свобода, мерки удобной вещи, добровольные параметры для подбора, кому вещь | Recipient/garment scope, единицы; история не выбирает размер нового заказа |
| Общение | Язык, explicit кратко/подробно/без emoji, исправления | Последнее explicit указание выигрывает; слабая гипотеза не постоянный ярлык |
| Барьер | Клиент сам назвал цену/посадку/принт/срок/доверие | Quote/negation/history не считаются актуальным возражением; «ціна?» не objection |
| История | Проверенные покупки и актуальные обещания/права | Order/payment source authority; клиентское «уже покупал» отдельно |
| Разрешения | Конкретный purpose/channel согласия, stop, agreed follow-up | Не выводить consent из покупки, молчания или просмотра |
| Наблюдение темпа | Времена собственных входящих, текущий burst, delivery receipts | Оценка очереди/паузы; это не знание online presence, намерения или психотипа |

Город нужен, когда влияет на подтверждённую доставку; он не оправдывает сбор чужих соцсетей. Военный принт не доказывает службу/взгляды/травму. Отсутствие ответа не означает отсутствие денег. Не хранить рацион, друзей, посещаемые места и спекулятивный профиль ради «конверсии». Это практическое ограничение качества данных и доверия; технически недоступное наблюдение нельзя изображать известным.

### Ожидание менеджера: контракт без повторного обдумывания неизменённого

Уже запланированную логику необходимо проверить следующим сценарием, а не писать ещё один polling-loop:

1. Вопрос действительно требует manager authority → один durable case с source/issue/owner.
2. Notification requested/sent фиксируются отдельно от customer holding и resolution. Telegram receipt не означает, что клиент получил ответ.
3. Если уместно и ещё не было сообщения, бот кратко объясняет, что уточняет именно этот вопрос. Не добавляет автоматические извинения/выдуманный срок.
4. Case ждёт **материального события**: manager decision, новый customer source, подтверждённое fulfillment/payment event или реальное operational escalation condition. Ничего не изменилось — нет нового Gemini вызова/повторного уведомления.
5. Новая реплика клиента оценивается сама: на известный независимый вопрос можно ответить, не теряя pending case. Нельзя снова просить уже названный размер только потому, что availability ждёт менеджера.
6. Решение менеджера → проверить актуальность товара/версии/канала → ответить и зафиксировать customer receipt/semantic disposition. Старое решение после изменения корзины не применяется автоматически.

Timeout для ответственности оператора не создаёт новый факт о товаре. UNKNOWN send не равен not sent и не даёт права на слепой replay. Это приёмочный контракт существующих handoff/debt/recovery gates; аудит не доказал, что каждый его переход уже нарушен.

## Как отвечать с точки зрения клиента

Ниже **иллюстрации желаемого поведения**, не готовая публикация новых фактов. Данные, которых нет в approved scope, сначала подтверждаются.

- **Тёплый клиент уже выбрал.** «Зрозуміла, класична посадка. Який розмір вам підійде?» — только если размер ещё неизвестен. Если он уже назван, следующий вопрос касается следующего действительно нужного поля. Не повторять каталог/историю бренда.
- **Нет точной фотографии другого кроя.** «Саме цього принта на класичній футболці поки немає на фото. Можу показати класичний крій на чистій футболці та надіслати обидві сітки для порівняння». Только при наличии approved blank asset; иначе предложить реально доступный честно подписанный пример.
- **Женская футболка.** Для подтверждённых tshirt bases объяснить unisex и разницу посадок, сохранив выбранный принт. Current hoodie unisex теперь тоже подтверждён владельцем, но должен получить собственный published scope; лонгслив/будущие основы не наследуют его автоматически.
- **«Ціна?»** Назвать актуальную цену правильной конфигурации либо одно необходимое уточнение. Не считать вопрос возражением и не начинать с «у нас дороже, зато…».
- **«У конкурентів дешевше».** Коротко признать сравнение, предложить сравнить конкретный параметр и привести один подтверждённый полезный факт. Не унижать конкурентов и не спорить о платёжеспособности.
- **Есть конкретный fit risk.** Сравнить вещь/мерки с exact chart, объяснить различие model photo и кроя. Если сомнение остаётся и conditions approved — обсуждать допустимый COD вариант; не заменять подбор скидкой.
- **«Дякую, подумаю».** Если вопрос закрыт и помощь не запрошена — дать пространство. Один optional bridge уместен при явном нерешённом вопросе, повторный допрос — нет. «Третье подумаю» само не создаёт необходимость менеджера. Seed playbook :59–64 и ig_objections.py:605–609 нужно проверить на эту приёмку.
- **Не нравится именно принт.** «Якщо хочете інший дизайн на такій основі, можемо обговорити індивідуальний варіант із командою». Один optional переход; только после интереса собрать недостающее, не повторять известные garment/size/colour. Не предлагать custom на вопрос «це жіноче?».
- **Будущие zip/вышивка.** Можно сообщить только approved статус тестирования/разработки и релевантную возможность уведомления по разрешённому каналу. Не продавать будущую вещь как доступную.
- **Сервисная проблема после оплаты.** Решить подтверждённую проблему раньше любой повторной продажи, отзыва или reward offer.

Эти принципы развивают D017/D022/D040 и текущие source-purpose contracts. Их влияние на paid conversion — гипотеза до корректного измерения, не установленный процент роста.

## Исследования и пределы переноса

- Первичное usability-исследование [Baymard: apparel UX, обновлено 25.02.2025](https://baymard.com/research-articles/apparel-5-best-practices) связывает нерешённую неопределённость посадки с отказом от покупки и рекомендует соответствующие товару числовые мерки, инструкции и фото на человеке. Наш вывод: exact измерения и честный пример кроя заслуживают приоритета FIT-03/05. Исследование сайтов не доказывает величину эффекта в украинском Instagram DM; schematic временно помогает объяснению, но не заменяет реальные фото.
- [Официальная документация Gemini structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output) прямо отделяет корректный JSON от семантически правильных значений. Наш вывод: schema/guard success нельзя считать доказательством узнавания нужного товара или правдивости всех claims; нужны scope, factual assertions и customer-flow проверки. Это не предложение менять SDK/model в рамках аудита.
- [Microsoft Research: trustworthy experimentation](https://www.microsoft.com/en-us/research/articles/patterns-of-trustworthy-experimentation-pre-experiment-stage/) рекомендует заранее определить гипотезу, пользовательские/guardrail/data-quality метрики и достаточную выборку. Для TwoComms отсюда следует проверять конкретные вмешательства по coherent cohort с paid outcomes, не объявлять любую покупку после сообщения эффектом метода.

Часть прямых Meta-страниц ранее вернула429/error. После ответовQ13/Q14 official documentation получена черезContext7 и добавлена выше: формыreferral/story/ig.me подтверждены, но account permissions и точный carousel item не доказаны. Это не обещание всех optional fields каждому объявлению; новый native ad-entry остаётся отдельным gate.

## Приёмка с точки зрения клиента

Эта матрица дополняет существующую 05_SCENARIO_MATRIX, но не изменяет её статусы. **Expected** — требование, не результат пройденного теста.

| ID | Сценарий | Expected / что наблюдать |
|---|---|---|
| CX01 | Новый native ad referral: один товар | Источник/MID и creative availability видны actual request; товар не selected без собственного выбора |
| CX02 | Креатив с 2–3 вещами: «справа», «друга», «обидві» | Допустимые candidates, правильный порядок, несколько позиций, короткое уточнение при ambiguity |
| CX03 | Referral message A + конфликтующий postback B | Картинка B не получает identity A; named conflict |
| CX04 | A→B в burst, разные получатели | Отдельные scopes; attribution не стирает первую позицию |
| CX05 | Organic exact photo → «звична» → размер | Focus/fit/size сохраняются, не повторяется уже известное |
| CX06 | Tshirt → вопрос о hoodie/longsleeve/zip | Тема меняется без потери корзины; чужие specs/chart не применяются |
| CX07 | «Покажіть фасони й обидві сітки» | Все обязательные native parts либо честное отсутствие; SENT/UNKNOWN/partial различимы |
| CX08 | Exact фото отсутствует | Подпись exact/пример/blank/schematic честная; принт не подменён |
| CX09 | Мерка удобной вещи между M/L | Exact numeric guide, единицы/метод/допуск; без гарантии «идеально» |
| CX10 | Выбор L + вопрос о составе/уходе | Choice и question отдельно закрыты; нет ложного recovery/manager debt |
| CX11 | >24 сообщений/длинное объяснение | Память восстанавливается конечным bounded процессом, не теряет correction |
| CX12 | 13 прошлых inquiry + новое точное фото | Latest valid focus не отбрасывается из-за lifetime cap |
| CX13 | «Коротко, без emoji» → длинная серия | Просьба сохраняется по утверждённому scope; детали заказа достаточны |
| CX14 | 3–5 активных покупателей, первый provider медленный | Последующие не истекают в очереди без полезного исхода; распределение задержек записано |
| CX15 | Provider A outage, B eligible | Независимый budget B доступен; health не делает probes |
| CX16 | Late first MID / UNKNOWN / новая входящая | Нет duplicate; остаток обязательств честно классифицирован |
| CX17 | «Друг каже дорого, мені підходить» | Routing, CRM и follow-up не выдумывают price objection |
| CX18 | Price-only / explicit risk / repeat «подумаю» | Ответ и одна полезная помощь по потребности; отказ/пауза уважаются |
| CX19 | Print mismatch → optional custom | Только релевантное предложение; known brief не спрашивается заново; feasibility у команды |
| CX20 | 200 offer → «так» | Только approved eligibility конкретного заказа; понятные remaining/fees, не технический password |
| CX21 | Удаление позиции ниже 3000; special discount edit | Actual winning basket, fresh delivery ACK/review; старый paid snapshot не подменён |
| CX22 | Manager case без изменений → новая независимая реплика | Нет повторного Gemini/alert без material event; известный вопрос получает ответ |
| CX23 | Verified payment manual order | Receipt не выдумывает начало сборки/двухчасовое ограничение |
| CX24 | Возврат/дефект/неполучение → новая покупка | Service-first, реальные деньги/consent/права сохранены, новый запрос не заблокирован старой историей |
| CX25 | Unpaid order после discount; A→B→paid | Не считается оплатой; историческая attribution не перезаписана текущим ad |
| CX26 | Owner edit → публикация → запрос/rollback | Видны review, selected/omitted/version; спорный FAQ не становится fact автоматически |
| CX27 | Старый диалог, отдельный ig.me referral без message/postback | Источник сохраняется один раз; не создаёт текст клиента, новый purchase или самостоятельный ответ |
| CX28 | Корзина от 3000 после скидок: full → 200+COD | Full допускает бесплатную доставку; 200+COD нет. Новые условия видны до оплаты, нужен свежий ACK; attempt/Order/TTN согласованы, оплаченный договор не переписан |
| CX29 | Осмотр и обычный отказ: внесено 200, перевозка 150 | Удержаны все 200, возврат 50 не придуман; перевозка и удержание учтены отдельно. Подтверждённый дефект/ошибка магазина идут отдельным сервисным маршрутом |
| CX30 | 100 баллов → реальный код; retry выдачи/списания; две допустимые скидки | Одно атомарное списание и одно настоящее право; ошибка не сжигает баллы. Совместимость/expiry проверены, D033 сохранён: последовательные 10%+10% дают 19%, не 20% |
| CX31 | Долгая беседа, три ссылки, затем 200 и доплата | До подтверждённой оплаты 0 новых покупок; после 200 одна; после доплаты всё ещё одна. Новый реальный оплаченный заказ даёт следующую; gross/net отмены различимы |
| CX32 | Худи для девушки, просьба о классике/blank/мерках | Текущая слегка oversize unisex основа, owner90/10≈320 после публикации; regular не выдуман. Preview честно обозначен; без ожидаемой картинки точные новые мерки не созданы |
| CX33 | Непонятный язык или UGC-предложение при известной истории покупателя | Язык не превращается в доказанный spam; сотрудничество имеет свой route. Коммерческий вопрос и прежний контекст не теряются |

Для каждого будущего теста сохранять: разрешённый test client, исходные сообщения/порядок, capture scope и manifest IDs, выбранные approved facts/assets, timings, фактически отправленные части/ссылки, итоговые semantic/debt состояния и downstream order/payment result. Минимизировать PII. Синтетическая SQLite-проверка не заменяет concurrency на отдельной MariaDB и разрешённый настоящий Instagram flow.

### Скорость: что измерять и улучшать

Нынешние QUIET_SECONDS=1.5 и QUIET_CAP_SECONDS=4.0 (ig_turn_revisions.py:30–31) уже дают короткий bounded сбор дробных сообщений. Нет основания добавлять длинную универсальную паузу. Проверить bursts с текстом/фото, «ещё одно сообщение» и несколькими вопросами: не отвечать на половину исправленного запроса, но и не переносить quiet бесконечно.

Измерять p50/p95/p99 отдельно для queue wait, media preparation, provider attempts, first useful receipt и complete useful receipt. Разделять factual local ответ, обычную Gemini консультацию, manager-dependent wait и permitted recovery; не исключать молча timeouts из denominator. Existing pre-provider deterministic facts/charts сохранять, не запускать лишний AI круг ради фразы. Конкретный production SLO назначить после baseline и устранения LAT-01/03; верхний deadline 45 секунд сам не является гарантией клиентского ответа.

### Как проверять рост продаж

До первого эксперимента: baseline по Q26–Q29, unknown coverage и sample. Основной outcome — утверждённый paid результат, вспомогательные — meaningful answer, exact product удержан, решённый fit question, полезные шаги до ссылки, delivery/semantic failures, manager escalation с причиной, stop/жалобы/возвраты. Не оптимизировать readiness score, число вопросов или число скидок как замену продажам.

Первые обоснованные эксперименты: exact measurement assistance; короткий релевантный quality fact; approved print meaning по запросу; optional custom только на design mismatch. Тестировать одно вмешательство на сопоставимых cohorts после correctness fixes. Не A/B тестировать заведомо неверные цены, неподтверждённые гарантии или broken delivery. Fingerprint метода/слово DTF/продолжение диалога не доказывают persuasion: ig_objections.py:109–194,358–476 и ig_quality_observations.py:1–38 описывают execution evidence, а не автоматическое обучение или причинную attribution.

## Передача другому агенту и изменение плана

План не следует расширять отдельным конкурирующим roadmap. В каждый существующий task passport добавить evidence, missing delta и acceptance из этой таблицы:

| Existing plan contract | Что добавить из аудита |
|---|---|
| P0-1/P0-2 source context/turn | AD-01/02/04/05/07, MEM-02, FIT-01; conversation subject отдельно от selection/attribution |
| P0-3 request trace; P2-4/P2-8 | AD-06, KB-01/05, routing/omission limits, MET-01–04 |
| P0-4 memory; P5-1 learning | MEM-01/03 и уже открытый typed preference/recipient consumer; finite catch-up вместо бесконечного fail-closed |
| P1-1/2 reviewed knowledge | KB-01/02/04/06, факты по base/batch и реальный owner editor→consumed |
| P1-3 relevance/required scenarios | KB-03/05, SALE-01/06, short pause/custom examples и actual payload проверка |
| P1-4/5 follow-up/lifecycle | SALE-02/05, manager wait событийный контракт, honest service/fulfillment promises |
| P2-1 карта | Показать те же source/mapping/version/current subject и unknown, не новый UI truth |
| P2-5 media; P4-1 catalogue | AD-01/03/05, FIT-02–05, точные assets/measurements и multi-product |
| P4-2 commercial authority | SALE-02–05/07/08;full-only snapshot/ACK/TTN,atomic points grant/multi-promo;existing paid-basket safeguards сохраняются |
| P2-7/custom engineering39 | Только релевантный optional entrance и сохранение известного brief; no universal feasibility |
| P1-7/P3-9 provider/latency recovery; P7-1 | LAT-01–03 и CX14–16; eligible independent project, queue expiry и actual receipts |
| P5-1 governance → publication → eval | Владелец/ревью gap, Q25; знания не «учатся» сами от любого клиентского текста |

Практический порядок:
1. REL-01 закрыт; начинать из primary main a4818a1b7 либо проверенного потомка. Archive сохраняет прежние drafts, не переносить старый файл целиком. REL-02 фиксирует отличие runtime, не блокируя независимые source fixes.
2. Технические correctness fixes, не требующие решений владельца: compatible referral/resolver, bounded finite recovery, category subject, combined media, semantic coverage, queue/send budgets, metric labels/unknown, legacy false promises.
3. ОтветыQ01/Q02/Q04–Q14/Q25 уже записаны. Подготовить reviewed current facts/assets/правила в existing owners иKB-01/05 publication acceptance. Hoodie numeric chart ждёт обещанное изображение; unknown конкретного claim не блокирует обычную продажу по известным данным.
4. Встроить принятые новые факты/условия в существующие authorities: full-only shipping;200credit/удержание;retail vsbrigade;style persistence. Дляmulti-leg200 и proactive5/10 ещё нет окончательного решения; существующие утверждённые права не отменять. Остальные fixes не ждут этих двух уточнений.
5. Закрывать задачу по четырём независимым доказательствам: code + meaningful tests + deployed SHA/runtime + customer-facing flow. Запись в журнале или зелёная карта не заменяют эти gates.
6. Только после корректного baseline оценивать conversion uplift; natural memory cohort и реальный новый Meta entry остаются отдельной проверкой.

**Что не доказано в этом аудите:** частота описанных веток в production, причина каждого старого клиентского молчания, содержательное качество всех реальных Gemini ответов, natural memory на длинном live cohort, native ad creative availability для всех типов рекламы, ROI/10% и юридическая правильность старых публичных условий. Новый клиентский replay/provider вызов не делался. Ни одна гипотеза о покупателе не названа установленным фактом.

## Связанный Brief


[Вопросы владельцу](2026-10-18_OWNER_BRIEF_GRILL_ME.md). Новые факты из ответов должны проходить существующие review/publication/authority gates.

## Дополнение QA · 20.10.2026

[Расследование молчания и журнал исправлений](2026-10-20_COOPERATION_SILENCE_REMEDIATION.md) содержит доказанную причину обращения361/3401, изменения кода и итоговые gates. Это последующий срез; формулировки «пока» выше описывают состояние на18.10 и не должны отменять более новые проверенные реализации.

Новые подтверждённые дефекты: переполнение captured state до Gemini из-за повторного fulfillment envelope; смешение общих B2B условий доставки с наличием; отсутствие customer acknowledgement и exact manager notice при локальном отказе; ложное закрытие содержательного вопроса после cap25/h; ранний mark_seen; разрыв signed current poll → reducer/outbox; исторический большой PK как ложное новое сообщение; потеря явной коррекции размера и native removal history после изменения корзины. Исправления проверяются через реальные локальные pipeline ветки с fake transport и отдельную migrated MariaDB, а не только parser unit tests.

Дополнительно проверяются границы продаж: generic brand originality изQ08 не требует истории конкретного artwork; неподтверждённая история конкретного принта, абсолютная вечная долговечность, ложный100% состав и обещание возврата обычной передоплаты блокируются до отправки. Подтверждение получения вопроса не является содержательным ответом, а запись manager task не доказывает отправку Telegram. Статусы локальной проверки, публикации, deployment и настоящего клиентского теста разделены в новом журнале. План90 здесь не редактируется.

### Повторная QA · opaque ingress и старые audited reviews

На основном выпуске обнаружены ещё две доказанные причины неверного поведения: неполученные ответы на корректные template/ephemeral вложения без текста и повторная тревога по уже рассмотренным revisions10/11 с точным старым audit. Исправление `27590766c` материализует первый случай и даёт честное уточнение через native outbox без Gemini; второй учитывает как reviewed disposition, сохраняя факт отсутствия клиентского ответа.

Соседние границы проверены вместе: story identity отдельно от message MID; share/ig_post alias dedup переносит URL и capture/provenance решение вместе; произвольная страница не получает право скачивания из-за расширения.jpg; unsupported/ephemeral+story reference не запускает capture; неизвестное пустое own echo не создаёт клиента/перехват. Точные gates и deployment evidence — в [журнале20.10](2026-10-20_COOPERATION_SILENCE_REMEDIATION.md). Это дополнение не объявляет старые сообщения доставленными и не заменяет живую контрольную переписку.


## Повторный инцидент и QA ·21.10.2026

Исторический лог подтверждает остановку до provider HTTP на лимите обязательного контекста. Диагностический replay текущего reader DTO показал11609 символов plan guidance плюс2052 корзины против12000; после lossless projection9827. Это не реконструкция исходного runtime: native capture старой ревизии уже запрещён изменившимся permission epoch. Текущий native reader двух старых сотрудничеств помещался в budget ещё до этого выпуска, поэтому их исторический overflow нельзя объявлять точечно воспроизведённым. Выявлены отдельные потери английского текста в historicalJSON wrapper, неверная эквивалентность2XL/XXL, size/oversize substring, blanket manager coverage и скрытый16-source предел. Дополнительный QA выявил стирание harddeadline после нового expanded-cart review, принятие отказа как later-receipt consent и синхронную блокировку других клиентов одним provider call. Факты, кодовые контракты и независимые gates находятся в [журнале21.10](2026-10-21_RESPONSE_RELIABILITY_RELEASE.md). Не переносить статус «локально исправлено» в «выложено» до его release checkpoint.

Предложение услуг поддержки Direct нужно отличать от creator/портфолио и B2B. Исторические B2B/recruitment сообщения вне messaging window не получают фиктивного нового окна; Gemini-only private draft не являетсяSENT и не закрывает debt. План90 не редактировался.

Последующая native QA нашла дополнительные взаимосвязи: ложный retail debt после полного B2B acknowledgement; отсутствие exact collaboration-case в pending reader; SQL webhook-only guard после принятого Python signed-poll; выбор старогоR1 вместо новогоR2 при равном времени. Отдельный тест доказал перенос известной розничной цены900грн в несогласованную «партнёрскую цену»: прежний validator допускал такой текст без слова «погоджено». Тестовый Meta transport был stub, реальному клиенту этот ответ не отправлялся. Исправление должно разделять monetary authority и текстовое acknowledgement, сохраняя обычный личный retail вопрос даже в том же сообщении. Подробные контракты, версия кода и независимые результаты SQLite/MariaDB/CI/runtime находятся в связанном журнале21.10; его release checkpoint определяет фактический статус выкладки.

Реальная генерация Gemini для двух старых сотрудничеств показала ещё один вид потери качества: структурно корректный и сохранённый private draft может пропустить явно запрошенное извинение, повторить уже известный вопрос или заменить текущий review гипотетическим кадровым резервом. Поэтому transport success недостаточен: private policy и validator теперь связывают извинение с проверенным фактом именно этого ожидания, удерживают текущий pending review и сохраняют исходное требование старта без закупки без выдуманного согласования dropshipping. Отдельная lifecycle QA обнаружила, что закрытое окно мешало архивировать устаревший приватный текст. Узкий archive-only переход не даёт права редактирования/отправки и сохраняет owner/CAS/source/privacy/reset/epoch checks. Статус новой генерации и native проверки фиксируется в журнале, первые тексты клиентам не отправлялись.

На487c новая реальная генерация обоих private drafts прошла ручную и browser проверку; старые тексты архивированы штатно. Operator native24/0skips и replay с запрещённым HTTP прошли. Обнаруженная после pull разница standalone fallback/supervisor ownership устранена штатным maintenance handoff и доказана parent/start-ticks/child-SHA, без изменения очередей или источников. Эти результаты не закрывают остающийся gate свежего Direct и не превращают закрытые исторические окна в отправленные ответы.


## Дополнительный QA 22.10: live-интерфейс и диалоговые границы

Новые подтверждённые находки и проверяемая реализация собраны в [журнале22.10](2026-10-22_LIVE_UX_DIALOGUE_QA.md). Обнаружены inbound-only сортировка, отсутствие обновления карточки без новых сообщений, задержка сохранения профиля за загрузкой аватара, неверная маркировка manager audio, spoken XXL и отдельные задачи вместо объединённого brief. Независимый review дополнительно выявил stale A→B действия, mobile reading anchor и неподтверждённое принятие на работу. Статус каждой проверки и ограничения приведены в журнале; отсутствие нового исходящего сообщения не заменяется фактом уведомления менеджера.


## Дополнение23.10.2026 — подтверждённые стандартные сценарии

1. **Английский фасон:** `regular fit for…` блокировался как несуществующий фасон `for`; отдельный `regular` не закрывал canonical `classic`. Обе ошибки воспроизведены нативной цепочкой, исправлены;10 новых обычных диалогов и204 смежных теста проходят. Это прямой риск остановки тёплого покупателя после выбора фасона.
2. **Смысл вопроса не равен выбору:** «Уточните, пожалуйста, вес худи» мог менять товар/разделять вопрос запятыми. Добавлены source-bound проверки; вес клиента, масса вещи и GSM различаются. По уточнению владельца обычный ответ о массе — известная политика отсутствия отдельного взвешивания, без менеджера и без числовых догадок.
3. **Данные основы:** владелец подтвердил, что плотность зависит от основы. Убрана общая власть числа320 для всех худи; состав90/10 и слегкаoversize сохранены.
4. **Ложные замеры:** read-only MariaDB показала catalog2/grid2 с точным старым программным preset. Публичная страница дополнительно игнорировала actual guide_data; PDPmatrix и бот имели отдельные обходные пути. Общий guard должен применяться во всех потребителях. Показ отсутствия подтверждённой сетки предпочтительнее выдуманной точности; реальные пользовательские замеры сохраняются.
5. **Калькулятор:** явная категория худи должна исключать формулу футболок даже при T-shirt в названии каталога. Далёкий доступныйS не является рекомендацией вместо расчётногоXXXL; нужна честная невозможность подбора и сброс прежнего результата.
6. **Причины неконверсии:** анализатор сейчас не видит финальный ответ за своим inputwatermark. Требуется отдельный receipt-backed диагностический контекст в том же вызове, иначе выводы о качестве/скорости последнего ответа недоказательны. Реальная оплаченная конверсия, модельная готовность и предполагаемые причины отказа должны оставаться разными показателями.

Первая UI/reliability-версия34e6d16ed уже выложена и проверена. Последующие исправления и аналитическое расширение требуют отдельной интеграционной проверки/выкладки; актуальный журнал:2026-10-22_LIVE_UX_DIALOGUE_QA.md. Plan90 автоматически не переписывался.


### Implemented evidence boundaries for conversion diagnostics — 23 October

The analyzer extension uses the existing call and immutable analysis snapshot, with separate admitted customer quotes and verified SENT-effect diagnostics. It records possible causes without turning silence, an objection, a manager notification, a receipt screenshot or a generated payment link into a lost/paid sale. Runtime source/epoch/namespace/reset/episode checks guard current interpretation. Statistics reconstruct receipt timing from canonical sources/effects rather than stored JSON values and require exact ordered source identity. Independent native tests reproduced and closed edited-source and unrelated-source-array attribution bugs.

Owner-facing readiness remains an uncalibrated AI assessment. Actual paid conversion continues to come from canonical provider/manager/linked paid-order truth and mature cohorts. New reason statistics explicitly identify their 31-day window, selected page (maximum 50 commercial episodes), coverage and unknown states; they cannot establish causal uplift or estimate reasons for unreviewed clients. Older empty snapshots are not automatically replayed or rewritten. This is an observability foundation for later owner-reviewed prompt/knowledge improvements, not autonomous self-training.


### Critical availability boundary: analysis ownership must not own customer availability

Live supervisor events and daemon stdout exposed a restart amplification path: a process kill leaves the analyzer’s bounded lease, but startup previously refused to start any customer worker while that lease was busy. Renewal loss also stopped the whole process. One observed recovery had at least 127 seconds without a replacement child. The corrected runner acquires and releases this lease inside the analysis worker, preserving all durable generation/frozen-owner checks and permitting customer startup/replies during the wait. A native sealed outbox regression proves a SENT reply while a manual analysis owner remains active. This is independent from classifier/marketing quality: no prompt can repair a stopped customer consumer. Initial SIGKILL attribution remains unavailable; the application coupling is proven and corrected, without claiming an OOM or Meta cause.
