# QA23 — Gemini, сообщения, карточки и контекст клиента

Дата: 23.10.2026. Оркестратор — текущий чат; 10 отдельных направлений QA. Исходный код проверялся от `e953afcb196b455da47e98998f6a49fc67f87e6a` (main). Это новый общий реестр, а не изменение плана90, который ведётся в другом окне. В конце фиксируются фактические проверки и SHA исправлений. До появления такой записи «исправляется» не означает «в production».

## Как читать доказательства

- **CONFIRMED / component:** воспроизведена текущая функция или связка parser/reducer/assembler. Это не доказательство отправленного клиенту ошибочного ответа.
- **SOURCE GAP:** подтверждён неподключённый consumer или противоречие контрактов; частота реальных инцидентов неизвестна.
- **MODEL:** только сохранённый ответ настоящего Gemini на зафиксированный payload. Ответ оркестратора/подставленный ответ теста не считается model evidence.
- **NATIVE / DELIVERY:** отдельно указываются полный локальный pipeline, fake transport, provider receipt и видимый клиенту ответ. Их нельзя подменять друг другом.
- **HISTORICAL:** старая находка сверена с текущим кодом; журнальная галочка сама по себе ничего не закрывает.

Цель — исправлять реальные потери смысла и ответа. Не создавать новые эвристики только ради количества находок. Исчерпывающая проверка всех вариантов языка невозможна; ниже перечислены конкретные семейства и непроверенные границы.

## Карта причин и порядок работ

1. Собственные сообщения и их области: retail/B2B, получатель, отрицание, исправление, несколько частей запроса.
2. Canonical facts и память: история помогает понять, но не подтверждает новый размер/корзину/оплату/наличие.
3. Response plan и релевантность: показ любого товара не означает ответ на конкретную просьбу.
4. Media purpose: целая вещь, крупный план, иллюстрация посадки и размерная схема имеют разное назначение.
5. Delivery и диагностика: SENT, покрытый вопрос и уведомлённый менеджер — разные факты.
6. UI и статистика показывают эти факты; скрытие ошибочной строки не исправляет upstream данные.

Приоритет P1/P2 здесь предварительный для передачи следующему агенту. Причинно связанные симптомы объединены, а не подсчитаны как независимые баги.

## F01 · Словесное количество теряется в явном добавлении

**P1, CONFIRMED component; ограниченное исправление в работе.** `добавьте две футболки`, `додайте дві футболки`, `add two t-shirts` → ADD без quantity → reducer создаёт1. Контроль с цифрой2 даёт2. В `ig_commerce_turns.py:486–495` первоначальный `_line_quantity` распознаёт цифры; `ig_commerce_state.py:183` имеет default1. Canonical identity reparse omission не исправляет. Source facts/обязанности ответа также теряют заявленное количество.

UI честно пишет «За замовчуванням» (`ig_commerce_scope.js:86–90`, `ig_commerce_projection.py:1041`); **ошибочная оплаченная накладная не доказана**. Исправление: ограниченные числительные у explicit ADD+garment, без глобальной замены слов. Обязательно сохранить «два XL» как XXL, product titles, отрицания/цитаты/альтернативы. Acceptance: фактический canonical parser/reducer и inbound дают2 с источником. `худи / чёрное / две / нет одну` — отдельная контекстная задача F04.

## F02 · Новый получатель наследует чужой размер

**P1, CONFIRMED component; ограниченное исправление в работе.** При self/M/classic просьба «ещё одну такую же для сестры» (UA/RU/EN) создаёт ещё self/M/classic. Для друга existing safeguard очищает size/fit. Причина — неполный recipient grammar (`ig_commerce_turns.py:387,433`), а copy safeguard (`ig_commerce_state.py:199`) срабатывает только при распознанной смене получателя.

Исправление: обычные роли сестра/брат и gift dative; неоднозначных получателей не превращать в self. Скопировать дизайн допустимо, размер другого человека требует его собственного основания. Историческая memory уже знает sister/gift, но не имеет current-cart authority (`ig_memory_timeline_consumers.py:17,68`). Acceptance включает recipient-specific cleared size/fit, сохранённый product, self и friend controls, отказ/цитату/нескольких получателей. Никакого нового вопроса владельцу не нужно.

## F03 · Исправление без запятой удаляет и правильный цвет

**P2, CONFIRMED component; ограниченное исправление в работе.** `не черное а белое`, `не чорне а біле`, `not black but white` убирают black и не добавляют white. Запятая исправляет результат. `_find_prefix_value` захватывает положительный RHS как отрицание; typed correction требует запятую (`ig_commerce_turns.py:287–290,528`). Downstream спрашивает цвет повторно, хотя ответ уже дан.

Acceptance: colour-only correction сохраняет replacement и его source; uncertainty, quoted history, OR и negated commands не становятся выбором. Замена «не худи а футболку» требует также инвалидации exact SKU/configuration и не должна закрываться только изменением regex цвета.

## F04 · Альтернатива и количество, разнесённые по сообщениям

**P2, CONFIRMED parser / integration acceptance open.** `или футболку` / `або футболку` / `or t-shirt` в отдельном источнике становится affirmative tshirt без clarification; две альтернативы внутри одного сообщения распознаются иначе (`ig_commerce_turns.py:296,802–811`). `худи и футболку` не даёт двух line operations или понятной обязанности уточнить. Ingress сохраняет отдельные источники; parser не должен потерять связь между ними.

Крупная работа: не склеивать всё бесконтрольно. Сохранить source IDs/order, короткую burst window и тему предыдущего receipt-backed вопроса, различать OR/AND/correction. При неясном количестве/позиции один конкретный вопрос. Acceptance: split/unsplit equivalence для обычных фраз; позднее сообщение не редактирует уже доставленную/оплаченную позицию задним числом. Ошибочный checkout или реальная тишина для этих примеров пока не доказаны.

## F05 · B2B-ассортимент попадает в личную корзину

**P1, CONFIRMED parser/helper + видимый UI; ограниченная изоляция в работе.** Точный SHOP_REQUEST из `tests_ig_business_cooperation.py:14–20` классифицируется business=true, purchase=false, fields={color:black}. `_apply_field_updates` создаёт fresh line:0 qty1 или перекрашивает существующую retail white/M/qty2 в black (`ig_commerce_state.py:437–441,1075`). Дополнительная garment-ветка `:958–960` тоже не требует retail intent. Inbound применяет parse до анализа (`ig_revision_commerce.py:161–164`).

23.10 в authenticated UI URBAN действительно показывает «Модель ще не обрана / Для себе / Чорний /1 за замовчуванням». Это подтверждает симптом интерфейса; изменение уже существовавшей реальной корзины URBAN не утверждается.

Нельзя просто скрыть строки и нельзя игнорировать весь commerce при business=true: hybrid «Хочу магазин. А для себе хочу купити білу футболку M» законен. Исправление должно допускать source-backed личный suffix, не переписывая оригинальный source/digest, и сохранять существующий retail при неоднозначном смешении. Acceptance: fresh B2B без retail session; retail→B2B не меняет вещь; explicit personal sample допускается; отказ от сотрудничества не удаляет личный заказ; модель/память не утверждают партнёрскую цену.

## F06 · Структурированный B2B-профиль частичен

**P2, SOURCE GAP / расширение существующего контракта.** Уже есть business brief, topics, subtypes, source questions и manager scopes (`ig_business_cooperation.py:135–143`, `ig_revision_intents.py:785–818`). Поэтому утверждение «памяти о B2B вообще нет» неверно. Но канал продаж, предполагаемый объём, выкуп/отправка конечному клиенту и ассортимент не имеют отдельных source-backed полей с correction/withdrawal. Narrative memory ограничена8 events/240chars и не является current business authority.

Решение: переиспользовать brief/case memory, отдельные явные business facts/unknown, не retail placeholders. Не считать «хочу без закупки» уже согласованной схемой; не требовать выдуманной квалификации/ФОП. UI показывает только актуальный смысл. Владельцу нужны лишь ещё не утверждённые общие условия (см. второй файл), не повтор Q17.

## F07 · Просьба показать посадки превращается в подбор дизайнов

**P2, CONFIRMED component; исправление среднего размера.** На приложенном скриншоте после classic/oversize клиент пишет «Скиньте приклади, будь ласка», а получает обычные design cards. Текущий `fit_consultation_request` смотрит current bundle и требует fit/audience words (`ig_fit_consultation.py:94–127`): exact UA/RU/EN короткие продолжения дают `{}`. Более того, явный fit-photo request при focus={} проходит validator, а `prepare_fit_illustration` возвращает unhandled (`ig_product_inquiry_presentation.py:156`, `ig_fit_consultation.py:380–382`). Затем обычный SHOW_PRODUCTS превращается в SKU cards (`ig_revision_live.py:1747`).

Это причинно согласуется со скриншотом; **исторический полный runtime и новый provider/SENT отказ не воспроизведены**. Нельзя исправить глобальным «приклади=посадка»: бывают примеры дизайнов/custom. Нужно receipt-backed continuation binding и category-level fit consultation без принудительного выбора дизайна. Подписать cut/illustration, сохранять независимый assortment intent. Focused-SKU защита и blank previews уже существуют, не создавать вторую систему.

## F08 · Крупный план выбирается обложкой карточки

**P2, CONFIRMED producer; проверка конкретного live asset ещё нужна.** `_first_photo` берёт первый допустимый gallery image раньше main/home covers (`ig_catalog_configured_choices.py:71–81`). Probe [detail,whole] + whole main/home выбирает detail; `_configured_card` передаёт URL без проверки назначения (`ig_catalog_choice_presentations.py:185,254`). Модели изображений имеют order/alt, но не shot purpose.

На скриншоте «Річ Йде Про Двозначні Суми» показана как macro принта10, а не вещь. Владение фото товаром не доказывает пригодность для обзора/посадки. Нужен approved whole-garment hero, variant scope и honest absence; detail сохраняется для просьбы о качестве. Не переставлять все картинки вслепую и не гадать по filename. Для fit пример другой дизайн допустим с явной подписью; ложный exact design запрещён.

**Дополнительная production read-only проверка23.10:** у product28 текущий main=`products/12.2.webp`, первая gallery=`products/extra/IMG_4194_b50WmRB.webp`; настоящий `_first_photo` выбирает вторую,482562bytes, source=product. Это подтверждает текущий конкретный путь выбора namedSKU, не только искусственный порядок gallery. Сам visual role asset и historical payload не устанавливаются по имени файла: для окончательной связи со скриншотом нужен просмотр/сопоставление. Для hoodie29 gallery сначала `extra/12.webp`, затем два detail-кандидата; глобальная перестановка тоже требует variant/media-purpose контроля. БД и assets не менялись, HTTP запрещён до Django setup, SQL ограничен SELECT/SHOW/EXPLAIN.

## F09 · Релевантность товарной подборки не доведена до server gate

**P1, CONFIRMED assembler/coverage; полный native send не доказан.** `rank_candidates` production caller найден только в owned-URL compatibility (`ig_product_references.py:125`), нормальная SHOW_PRODUCTS-ветка им не ограничена. `prepare_catalog_choice_presentations:329–345` принимает published IDs без сравнения с requested garment; `ig_response_plan:1115–1116` считает info:presentation покрытой при любом show_products. Pure actual-source probe: request hoodie, valid shirt ID → accepted+covered.

`tests_ig_palette_intent_20261023:136–141` сам предварительно ранжирует и вставляет IDs в mock Gemini; это проверяет доставку правильной подборки, не отклонение неправильной. Compact live catalog ещё и убирает category (`bot_catalog:160–174`, `instagram_bot:8995,10045`).

Решение M: captured browsing subject → eligible candidates → controls validation → presentation coverage по принятому релевантному предмету. Отрицательный native тест должен предложить реальный published, но неверный тип/тему без prefilter в mock. Не ослаблять stock/selection/payment proofs. Это конкретизация существующего P4-1/40E, а не новый параллельный каталог.

## F10 · Тематические коллекции существуют, consumer бота не подключён

**P2, SOURCE GAP.** `ProductMerchCollection` уже поддерживает несколько theme/city/brigade/collab assignments (`product_catalog/models.py:144–229`), но graph читает single Product.category и4 semantic traits (`ig_catalog_graph:156–205`). Нельзя обещать, что добавление категорий владельцем само начнёт менять bot recommendations. Garment category не заменять темой — от неё зависят garment derivation и CTA.

В одной задаче с F09 передать verified collections в capture/prompt/eligibility; OR/AND/exclusions/mixed hoodie+tshirt должны иметь явное представление, unknown и dedup. «Без військових» нельзя незаметно ослаблять. Дизайн225 — признак товара, не доказательство службы/статуса клиента. Новая taxonomy ещё наполняется владельцем; отсутствие будущих assignments не записывается как текущая ошибка данных.

## F11 · Действие менеджера «Продовжити policy» не может исполниться

**P2, SOURCE CONFIRMED; ограниченное исправление в работе.** Configuration/information cases создаются SKIPPED/EVENT с пустым event_payload (`ig_revision_intents.py:607–615`), но detail serializer даёт continue_url (`bot_views.py:6509–6525`). Handler закономерно возвращает event_boundary_missing (`bot_followups:1363–1372,1733–1734`). Нажатие не даёт decision и не отвечает клиенту.

Исправление: исключить эти две причины из generic continuation, сохранить уже существующие source navigation/decision/private answer workflows. Не подделывать event payload и не считать задачу ответом. Тест serializer + полноценные existing review controls.

## F12 · Подпанель не обновляется и источник черновика не объясняется

**P2, SOURCE CONFIRMED; ограниченное исправление в работе.** Assisted controller загружается только при created (`bot.html:2569–2594`): same-client full refresh сохраняет старую no-request/approval панель. Обновление должно перезагружать clean/idle controller на принятом detail render, сохраняя dirty, busy, pending/UNKNOWN и A→B fences.

Отдельный симптом `openFulfillmentReply:2658–2665`: при unsaved A draft запрос ответа наB молча фокусируетA. Текст/пины сохранять правильно, но менеджеру нужно явное объяснение сохранить/отменить прежнюю черновую работу. Автоматическая неверная отправка не доказана. Acceptance — parent-mount regression и заметный feedback, без второй редакторской системы.

## F13 · Пустые коммерческие панели создают шум для любого клиента

**P3, SOURCE + authenticated UI CONFIRMED; larger UX work.** Assisted/waiting/split sections mount всегда (`bot.html:3689–3692`), даже verified absence. В URBAN видны все пустые панели. Сначала свернуть только verified-empty в понятные optional tools; failure/incomplete/dirty/history/UNKNOWN не считать пустотой. Контекстные current stock form/shortcut уже реализованы, переиспользовать их. Не скрывать нужное действие только по B2B категории.

## F14 · Анализ причин и предпочтений теряет область отрицания

**P2, CONFIRMED normalizer; малый fix причины в работе.** Source «Не беру, не через ціну. Ціна підходить.» принимался как price/reported_by_customer/high: `_reported_cause` отрезал negation перед causal connector (`ig_conversion_audit:215–219`). Исправление проверяет polarity самого connector. UA/RU/EN, affirmative price/real size/counterevidence/unknown controls обязательны.

**Открытая более глубокая часть:** exact substring quote не гарантирует истинность мотива. Source «Не на подарунок, беру для себе.» + quote «подарунок» принимает gift/high; обратный пример даёт self/high (`ig_conversion_audit:119–143,201–205,247–258`). Нужна source-context проверка вокруг occurrences, а не только model-selected excerpt. Не доказано, что живой Gemini действительно выбрал такие отрывки. Причины потерь — гипотезы, пока клиент сам их не назвал; молчание не доказывает цену/ошибку бота.

## F15 · Явные просьбы о стиле общения теряются либо меняют смысл

**P2, CONFIRMED extraction; ограниченный fix в работе.** «Пожалуйста, не коротко, а подробно» даёт[], «Пиши коротко і покажи принти» теряет brevity из-за `_ART`, «Не пиши коротко, но эмодзи можно» может дать emoji=omit из-за45char negative window (`ig_communication_preferences:35–84`). Raw current text ещё видит Gemini; доказанный дефект — durable preference, не фактический неудачный SENT.

Fix — target/clause-scoped communication grammar, quoted/reported/art-only exclusions, corrections; не создавать психотипы по дизайну или военной тематике. Дружелюбие: сначала полезный ответ, естественная длина, один нужный вопрос; юмор лишь уместный положительный conversational cue, без жалоб/оплаты/травмы/сроков. Identity не выдумывать: если спросили, Соломія — виртуальная помощница. Эффект на конверсию нужно измерять, не обещать10% или 10/10.

## F16 · Собственная просьба о языке и наблюдаемый язык — разные приоритеты

**P2, contract mismatch / SOURCE+pure resolver.** Историческое «Пишите по-русски» current «thank you» переключаетEN: current observed language раньше historical explicit (`ig_reply_language:359–363`); это намеренно закреплено текущим тестом213–216, не случайный регресс. Q23 владельца просит сохранять явное пожелание до изменения — контракт нуждается в согласованном изменении и регрессиях.

Другой reproducer: «The manager said to reply in Ukrainian, but I prefer English.» → uk/current_source_requested/explicit. Reported prose не отфильтрована, prefer отсутствует в REQUEST (`ig_reply_language:122–186`). Не менять язык на основании чужой команды или русского названия вещи. Нет нового бизнес-вопроса: различить own explicit preference / reported text / observed phrase технически.

## F17 · Старый multipart остаток листа ожидания остаётся открытым

**P1, SOURCE CONTRADICTION; уже Plan90 41E, не новая частота инцидентов.** Restock ACK/notification допускает1000 UTF8bytes (`ig_restock_subscriptions:730,749`, `ig_restock_subscription_delivery:83`), transport splitter default950 (`ig_revision_transport:52`, `instagram_bot:6137`). При951–1000 ACK делится, но proof требует ровно одну полную часть (`restock:548–555,647`). Notification guard требует одну часть и отклоняет physical_message_changed доHTTP (`delivery:131–133,293`).

Сначала согласовать producer/body/part receipt contract; затем versioned multipart и UNKNOWN/revoke. Не ослаблять receipt guard для формального прохождения теста. Полный Engineering41/ALL delegation не объявлять завершённым по выпуску41A–D.

## F18 · Тест старого prompt producer не доказывает live compiler

**P2, SOURCE cleanup/verification gap.** `_context_sections` и `_bounded_prompt_source` (`instagram_bot:9981,10020`) вызываются только `tests_ig_prompt_degradation`, реальный путь `_assemble_system_instruction:8922` читает manifest/selection. Перенести полезные assertions на настоящий compiler и затем удалить только доказанную мёртвую пару. `_prompt_section` нужен live; `live_routing_decision` имеет production callers и не является мёртвым дублем. Refactor без проверки caller/rollout зависимости запрещён.

## F19 · Вопрос без пунктуации исчезает рядом с выбором размера

**P1, CONFIRMED actual parser+plan+coverage; ограниченный fix в работе.** Source «L як прати» даёт size=L и obligations=[size]. Size-only «Ваш розмір L зафіксувала.» получает complete/remaining=[]. Аналогично «L а состав», «L какая ткань», «L яка тканина». Вариант «L яка щільність» сохраняет density. `_requested_topics` не узнаёт як/какая и cloth nouns, а fallback unclassified не добавляется при уже найденном size (`ig_response_plan:574,1871`).

Разбиение тех же слов на [«L», «як прати»] даёт второй unclassified debt: один смысл по-разному обрабатывается из-за границы сообщений. Ошибочный SENT не доказан; доказано, что actual coverage может пропустить независимый вопрос. Acceptance: size-only incomplete; source-backed care/material/price answer покрывает тот же intent в1/2/3 sources без ненужного manager debt. Сохранить quoted titles/negation/finished mass vs human weight.

**ОТКРЫТО: связанный material gap.** «Це бавовна?» имеет material obligation, но selectors `_SPEC`/`approved_public_facts._TOPICS` не выбирают fibre words; действительный base_context composition не учитывается материалом в coverage. Рассмотренный локальный fallback отклонён при независимой проверке: UK reconstruction не совпадает с RU/EN assertion; canonical per-line child не получает readiness. Этот дополнительный код полностью убран до выпуска. Нужен общий exact product/base/locale/source contract с native per-line тестами; текущая правка F19 улучшает распознавание вопроса, не объявляет всю material coverage закрытой.

## F20 · Недоступное вложение: receipt и semantic waiting пока различаются

**PARTIAL / acceptance gap, не доказанная тишина.** `record_unavailable_media_reply` имеет source/media bindings и origin=media_unavailable (`ig_revision_input:242`), но deterministic coverage в live2088 предусмотрена для иных origins. Clarification может быть SENT/processed, а `response_obligation` формально ещё ждёт semantic coverage. Lane health исключает processed, unanswered collector начинает с существующих debt tasks — поэтому нельзя заявлять, что это автоматически создаёт ложный alert каждому клиенту.

Acceptance: resend request должен честно подтверждать получение и waiting-on-customer, не сообщать, что картинка изучена, не порождать replay; отдельно проверить ранее существовавший debt. Полноценное Gemini visual понимание чужого/размытого изображения требует actual media-capable запроса, не текста с описанием картинки.

## Исторические находки: что уже закрыто и что нельзя переоткрывать

| Контракт | Текущее заключение | Основание/граница |
|---|---|---|
| Память, routing, ad capture отсутствуют | Историческая исходная формулировка устарела | revision_live1241/1258/1343, turn_capture277, shared routing facts; новые конкретные gaps F04/F06/F10 остаются |
| Нет blank fit media / chart early return | Закрыто в source | fit_illustration_assets versioned illustration_only; live1674 combined chart/fit effects; F07 no-focus другой |
| Любой ad image сразу выбирает товар | Защита есть | referral/source conflict fences, candidate≠selection, ordinal reviewed positions; live конкретной рекламы ещё не проверен |
| B2B уведомление = доставленный ответ | Нельзя так считать | stable scopes/brief/fake-SENT regression есть; исторические closed-window customer debts не replay |
| Stock ledger подтверждает отсутствие/ETA | Защита есть | signed manager exactscope, physical stock≠dispatch≠arrival; сохранить |
| Полный Engineering41 завершён | Нет, PARTIAL | multipart/ALL остаются отдельными gates, F17 |
| Fresh Direct и точная сетка худи | Не подтверждены прежним релизом | old test outputs/healthy daemon не livecustomer proof; реальные мерки/per-base GSM нужны из источника |

## Основание разговорных рекомендаций и границы маркетинга

Это engineering рекомендации, а не доказательство роста конверсии. [Google Conversation Design](https://developers.google.com/assistant/conversation-design/learn-about-conversation) описывает сохранение контекста коротких продолжений, релевантность, короткие уточнения и поиск полезных альтернатив после ограничения. Для нашего кейса это означает: «покажите примеры» связывается с только что обсуждёнными посадками; согласованный срок нельзя заменить молчанием; при невозможном сроке предложить реальную альтернативу доставки, не выдумывая её доступность.

Официальные [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output?hl=en) отдельно требуют application validation: подходящий JSON ещё не означает верный бизнес-смысл. Поэтому улучшение prompt без проверки show_products/recipient/stock authority не закрывает F02/F05/F09. Контракт должен сохраняться при смене модели; модели можно менять, source/selection/payment authority — нет.

[Gemini image understanding](https://ai.google.dev/gemini-api/docs/image-understanding) рекомендует чёткие изображения и human/postprocessing evaluation; [media resolution](https://ai.google.dev/gemini-api/docs/media-resolution) описывает компромисс detail/latency/token cost. Это не основание всегда повышать resolution: для непонятного screenshot/детали нужно honest uncertainty и конкретное уточнение, а не уверенная догадка или отказ продавать. Макро принта не становится whole-garment asset от высокой resolution.

Практические acceptance-примеры для будущих проверок:

| Ситуация | Хорошее движение диалога | Не считать улучшением |
|---|---|---|
| Classic/oversize, принт ещё не выбран | короткое отличие, честно подписанный blank fit example, далее один уместный вопрос | три случайных дизайна и перезапуск выбора |
| Английский клиент, кириллическое название | английский ответ, сохранить точную reference, уточнить срок и реальные альтернативы | переключить язык из-за названия или обещать невозможное |
| Дропшиппер перечисляет black/oversize | brief/уже данные не переспрашивать, актуальный статус manager review | personal self-cart/розничный invoice из B2B ассортимента |
| Положительный неформальный тон | ненавязчивая лёгкость, если помогает этому разговору | шутка в жалобе/оплате/срыве срока, имитация человеческой биографии |
| Неясное чужое/размытое фото | что удалось уверенно увидеть, один нужный crop/name/link, custom вариант по согласованию | ложный exact SKU/цена/наличие из похожей картинки |

Психотипы, доход, военный статус и личные обстоятельства не достраиваются по дизайну/аватару/скорости печати. Для адаптации достаточно явно запрошенных языка/краткости/эмодзи, фактических вопросов и подтверждённых shopping preferences. Дороговизна не повод спорить с клиентом или унижать конкурентов: отвечать на конкретное сомнение проверяемыми материалом/замерами/фото, затем предложить подходящий разрешённый шаг. A/B эффект стиля и причинное влияние на оплату должны измеряться отдельно от observed conversation audit.

## Реестр QA и выпуска (обновляется до завершения)

- 10 направлений: media/cards; turns/cart; B2B memory; contextual UX; ad/categories; analyzer/funnel; prompt/naturalness; delivery/resilience; historical/refactor; native Gemini harness.
- Первичные пробы — actual-source component extraction под project Python -B с deny write/network/sqlite; не production fixtures.
- Быстрые fixes имеют непересекающееся владение: turns; commerce_state; communication_preferences; conversion_audit; bot_views/template. Root владеет CI enrollment, двумя Markdown, интеграцией и release.
- Actual provider tests: **ещё не выполнены на момент создания файла**; новый finite budget/input manifest обязателен. Прошлые grants не переиспользуются.
- Публичный stress-test большим числом людей — следующий этап, не выполнен этим component QA.
- 23.10 владелец подтвердил вход в тестовый @zainllw0w и затем попросил работать через bot interface. Его тестовый сброс разрешён; другие клиенты/платежи/история не удаляются. Фактический reset и его область будут записаны отдельно.

### Реализованные малые исправления перед общим gate

| Находка | Реализация | Точная граница |
|---|---|---|
| F01/F02/F03 | explicit ADD1–10 UA/RU/EN; sibling/gift scope; comma-free colour correction | неизвестные произвольные получатели/имена и split alternatives остаются открыты; colleague явно запрашивает уточнение; дваXL сохраняется |
| F02 UI | локализованные sister/brother в fallback и обоих существующих renderers | generic unknown сохранён; JS URL cache-bust обновлён |
| F05 | B2B-only — nonmutation observation до session bootstrap; неоднозначный hybrid сохраняет cart | старые загрязнённые draft не переписывались; generic shop+personal без trade topic и неявный sample ещё требуют richer classifier |
| F11/F12 | убран неисполняемый generic continue; clean same-client refresh; объяснён retained unsaved draft | empty-panel redesign F13 отложен; dirty/busy/UNKNOWN/focus/A→B сохранены |
| F14 малая часть | negated causal connector не становится reported refusal cause | abbreviated quote-context motivation остаётся открытой |
| F15 | preference target coordination/negative correction, включая positive emoji suffix | language precedence F16 не менялась; нет speculative psychology и новых prompt простыней |
| F19 малая часть | punctuation-free care/material/price вопросы не исчезают рядом с size | cotton selector/base coverage открыта; одно распознавание не делает неизвестный факт подтверждённым |

Focused owner gates: parser151tests/3 ожидаемых SQLite skips; B2B63unique tests с3native fake-SENT continuity checks; UI6Django +9JS files; conversion schema21; preferences26; terse questions48. Эти числа пересекаются и **не суммируются**. Root UI135/135,0skip и runner/workflow79/79 прошли. Первый общий77-source/91-label gate прерван root с exit130 при пересмотре material fallback: это **не PASS**. Начат новый полный gate на повторно зафиксированном V2 source.

Независимый review до выпуска также нашёл и закрыл регрессию positive emoji suffix. Никакие неудачные captures/aborted tests не переименованы в успешные. Существующий `CartSourceFallbackTests.test_languages_use_the_same_line_scope_and_only_one_requested_selector` отдельно не проходит и на исходном HEAD: fixture задаёт UA source, но ожидает RU/EN из profile; test file не менялся. Это не доказанный регресс новых recipient labels.

**Root V2 общий gate: PASS1506/1506,539.224s,91labels,12 ожидаемых backend skips.** Все77 source hashes совпали до/после. После него удалена одна пустая строка EOF только в новом тесте terse_questions; AST тождественен, остальные76 hashes неизменны. Исходный tested freeze сохранён отдельно, новый freeze честно содержит эту test-only нормализацию; focused terse gate повторяется. Application source после полного прогона не менялся. Static pipeline PASS:1974 asset files/571manifest entries/4compressor entries; Django system check без ошибок.
