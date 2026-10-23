# QA23 — Gemini, сообщения, карточки и контекст клиента

**Срез23.10.2026, QA23.** Выпуск приложения `f42832a9bcc2a7cf47b2dbf67d9f6ac42cdfda5a` закоммичен и отправлен в main. Локальные gates, Django CI и все9MariaDB suites+aggregate пройдены. f428 выложен штатным git pull; процесс/10lanes/77source pins/8static assets проверены. Actual-Gemini серия завершена: 10 логических сценариев, 8 результатов, 2 неисполненных semantic проверки; ошибки и оставшиеся контракты приведены ниже. Это не полный PASS модели и не доказательство Direct-доставки. Первоначальный причинный реестр содержит21группу; последующие подтверждённые находки добавляются ниже: закрытая часть, оставшийся контракт и доказательность перечислены отдельно. Текст отдельных finding описывает исходный воспроизведённый дефект; статус реализации см. таблицу в конце.


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

**P1, CONFIRMED component; ограниченное исправление включено в f428; границы ниже.** `добавьте две футболки`, `додайте дві футболки`, `add two t-shirts` → ADD без quantity → reducer создаёт1. Контроль с цифрой2 даёт2. В `ig_commerce_turns.py:486–495` первоначальный `_line_quantity` распознаёт цифры; `ig_commerce_state.py:183` имеет default1. Canonical identity reparse omission не исправляет. Source facts/обязанности ответа также теряют заявленное количество.

UI честно пишет «За замовчуванням» (`ig_commerce_scope.js:86–90`, `ig_commerce_projection.py:1041`); **ошибочная оплаченная накладная не доказана**. Исправление: ограниченные числительные у explicit ADD+garment, без глобальной замены слов. Обязательно сохранить «два XL» как XXL, product titles, отрицания/цитаты/альтернативы. Acceptance: фактический canonical parser/reducer и inbound дают2 с источником. `худи / чёрное / две / нет одну` — отдельная контекстная задача F04.

## F02 · Новый получатель наследует чужой размер

**P1, CONFIRMED component; ограниченное исправление включено в f428; границы ниже.** При self/M/classic просьба «ещё одну такую же для сестры» (UA/RU/EN) создаёт ещё self/M/classic. Для друга existing safeguard очищает size/fit. Причина — неполный recipient grammar (`ig_commerce_turns.py:387,433`), а copy safeguard (`ig_commerce_state.py:199`) срабатывает только при распознанной смене получателя.

Исправление: обычные роли сестра/брат и gift dative; неоднозначных получателей не превращать в self. Скопировать дизайн допустимо, размер другого человека требует его собственного основания. Историческая memory уже знает sister/gift, но не имеет current-cart authority (`ig_memory_timeline_consumers.py:17,68`). Acceptance включает recipient-specific cleared size/fit, сохранённый product, self и friend controls, отказ/цитату/нескольких получателей. Никакого нового вопроса владельцу не нужно.

## F03 · Исправление без запятой удаляет и правильный цвет

**P2, CONFIRMED component; ограниченное исправление включено в f428; границы ниже.** `не черное а белое`, `не чорне а біле`, `not black but white` убирают black и не добавляют white. Запятая исправляет результат. `_find_prefix_value` захватывает положительный RHS как отрицание; typed correction требует запятую (`ig_commerce_turns.py:287–290,528`). Downstream спрашивает цвет повторно, хотя ответ уже дан.

Acceptance: colour-only correction сохраняет replacement и его source; uncertainty, quoted history, OR и negated commands не становятся выбором. Замена «не худи а футболку» требует также инвалидации exact SKU/configuration и не должна закрываться только изменением regex цвета.

## F04 · Альтернатива и количество, разнесённые по сообщениям

**P2, CONFIRMED parser / integration acceptance open.** `или футболку` / `або футболку` / `or t-shirt` в отдельном источнике становится affirmative tshirt без clarification; две альтернативы внутри одного сообщения распознаются иначе (`ig_commerce_turns.py:296,802–811`). `худи и футболку` не даёт двух line operations или понятной обязанности уточнить. Ingress сохраняет отдельные источники; parser не должен потерять связь между ними.

Крупная работа: не склеивать всё бесконтрольно. Сохранить source IDs/order, короткую burst window и тему предыдущего receipt-backed вопроса, различать OR/AND/correction. При неясном количестве/позиции один конкретный вопрос. Acceptance: split/unsplit equivalence для обычных фраз; позднее сообщение не редактирует уже доставленную/оплаченную позицию задним числом. Ошибочный checkout или реальная тишина для этих примеров пока не доказаны.

## F05 · B2B-ассортимент попадает в личную корзину

**P1, CONFIRMED parser/helper + видимый UI; ограниченная изоляция включена в f428; оставшиеся границы ниже.** Точный SHOP_REQUEST из `tests_ig_business_cooperation.py:14–20` классифицируется business=true, purchase=false, fields={color:black}. `_apply_field_updates` создаёт fresh line:0 qty1 или перекрашивает существующую retail white/M/qty2 в black (`ig_commerce_state.py:437–441,1075`). Дополнительная garment-ветка `:958–960` тоже не требует retail intent. Inbound применяет parse до анализа (`ig_revision_commerce.py:161–164`).

23.10 в authenticated UI URBAN действительно показывает «Модель ще не обрана / Для себе / Чорний /1 за замовчуванням». Это подтверждает симптом интерфейса; изменение уже существовавшей реальной корзины URBAN не утверждается.

Нельзя просто скрыть строки и нельзя игнорировать весь commerce при business=true: hybrid «Хочу магазин. А для себе хочу купити білу футболку M» законен. Исправление должно допускать source-backed личный suffix, не переписывая оригинальный source/digest, и сохранять существующий retail при неоднозначном смешении. Acceptance: fresh B2B без retail session; retail→B2B не меняет вещь; explicit personal sample допускается; отказ от сотрудничества не удаляет личный заказ; модель/память не утверждают партнёрскую цену.

## F06 · Структурированный B2B-профиль частичен

**P2, SOURCE GAP / расширение существующего контракта.** Уже есть business brief, topics, subtypes, source questions и manager scopes (`ig_business_cooperation.py:135–143`, `ig_revision_intents.py:785–818`). Поэтому утверждение «памяти о B2B вообще нет» неверно. Но канал продаж, предполагаемый объём, выкуп/отправка конечному клиенту и ассортимент не имеют отдельных source-backed полей с correction/withdrawal. Narrative memory ограничена8 events/240chars и не является current business authority.

Решение: переиспользовать brief/case memory, отдельные явные business facts/unknown, не retail placeholders. Не считать «хочу без закупки» уже согласованной схемой; не требовать выдуманной квалификации/ФОП. UI показывает только актуальный смысл. Владельцу нужны лишь ещё не утверждённые общие условия (см. второй файл), не повтор Q17.

## F07 · Просьба показать посадки превращается в подбор дизайнов

**P2, CONFIRMED component; исправление среднего размера.** На приложенном скриншоте после classic/oversize клиент пишет «Скиньте приклади, будь ласка», а получает обычные design cards. Текущий `fit_consultation_request` смотрит current bundle и требует fit/audience words (`ig_fit_consultation.py:94–127`): exact UA/RU/EN короткие продолжения дают `{}`. Более того, явный fit-photo request при focus={} проходит validator, а `prepare_fit_illustration` возвращает unhandled (`ig_product_inquiry_presentation.py:156`, `ig_fit_consultation.py:380–382`). Затем обычный SHOW_PRODUCTS превращается в SKU cards (`ig_revision_live.py:1747`).

Это причинно согласуется со скриншотом. Новый managed synthetic run с действительно включённой antecedent history воспроизвёл raw переход от classic/oversize к generic SKU cards; native validator отклонил candidate по unverified_price, delivery effects=0. Исторический полный runtime и customer-visible SENT отказ по-прежнему не воспроизведены. Нельзя исправить глобальным «приклади=посадка»: бывают примеры дизайнов/custom. Нужно receipt-backed continuation binding и category-level fit consultation без принудительного выбора дизайна. Подписать cut/illustration, сохранять независимый assortment intent. Focused-SKU защита и blank previews уже существуют, не создавать вторую систему.

## F08 · Крупный план выбирается обложкой карточки

**P2, CONFIRMED producer; проверка конкретного live asset ещё нужна.** `_first_photo` берёт первый допустимый gallery image раньше main/home covers (`ig_catalog_configured_choices.py:71–81`). Probe [detail,whole] + whole main/home выбирает detail; `_configured_card` передаёт URL без проверки назначения (`ig_catalog_choice_presentations.py:185,254`). Модели изображений имеют order/alt, но не shot purpose.

На скриншоте «Річ Йде Про Двозначні Суми» показана как macro принта10, а не вещь. Владение фото товаром не доказывает пригодность для обзора/посадки. Нужен approved whole-garment hero, variant scope и honest absence; detail сохраняется для просьбы о качестве. Не переставлять все картинки вслепую и не гадать по filename. Для fit пример другой дизайн допустим с явной подписью; ложный exact design запрещён.

**Дополнительная production read-only проверка23.10:** у product28 текущий main=`products/12.2.webp`, первая gallery=`products/extra/IMG_4194_b50WmRB.webp`; настоящий `_first_photo` выбирает вторую,482562bytes, source=product. Это подтверждает текущий конкретный путь выбора namedSKU, не только искусственный порядок gallery. Сам visual role asset и historical payload не устанавливаются по имени файла: для окончательной связи со скриншотом нужен просмотр/сопоставление. Для hoodie29 gallery сначала `extra/12.webp`, затем два detail-кандидата; глобальная перестановка тоже требует variant/media-purpose контроля. БД и assets не менялись, HTTP запрещён до Django setup, SQL ограничен SELECT/SHOW/EXPLAIN.

## F09 · Релевантность товарной подборки не доведена до server gate

**P1, CONFIRMED assembler/coverage; полный native send не доказан.** `rank_candidates` production caller найден только в owned-URL compatibility (`ig_product_references.py:125`), нормальная SHOW_PRODUCTS-ветка им не ограничена. `prepare_catalog_choice_presentations:329–345` принимает published IDs без сравнения с requested garment; `ig_response_plan:1115–1116` считает info:presentation покрытой при любом show_products. Pure actual-source probe: request hoodie, valid shirt ID → accepted+covered.

`tests_ig_palette_intent_20261023:136–141` сам предварительно ранжирует и вставляет IDs в mock Gemini; это проверяет доставку правильной подборки, не отклонение неправильной. Compact native catalog не передаёт per-product garment/category mapping (`bot_catalog:160–174`, `instagram_bot:8995,10045`).

Решение M: captured browsing subject → eligible candidates → controls validation → presentation coverage по принятому релевантному предмету. Отрицательный native тест должен предложить реальный published, но неверный тип/тему без prefilter в mock. Не ослаблять stock/selection/payment proofs. Это конкретизация существующего P4-1/40E, а не новый параллельный каталог.

## F10 · Тематические коллекции существуют, consumer бота не подключён

**P2, SOURCE GAP.** `ProductMerchCollection` уже поддерживает несколько theme/city/brigade/collab assignments (`product_catalog/models.py:144–229`), но graph читает single Product.category и4 semantic traits (`ig_catalog_graph:156–205`). Нельзя обещать, что добавление категорий владельцем само начнёт менять bot recommendations. Garment category не заменять темой — от неё зависят garment derivation и CTA.

В одной задаче с F09 передать verified collections в capture/prompt/eligibility; OR/AND/exclusions/mixed hoodie+tshirt должны иметь явное представление, unknown и dedup. «Без військових» нельзя незаметно ослаблять. Дизайн225 — признак товара, не доказательство службы/статуса клиента. Новая taxonomy ещё наполняется владельцем; отсутствие будущих assignments не записывается как текущая ошибка данных.

## F11 · Действие менеджера «Продовжити policy» не может исполниться

**P2, SOURCE CONFIRMED; ограниченное исправление включено в f428; границы ниже.** Configuration/information cases создаются SKIPPED/EVENT с пустым event_payload (`ig_revision_intents.py:607–615`), но detail serializer даёт continue_url (`bot_views.py:6509–6525`). Handler закономерно возвращает event_boundary_missing (`bot_followups:1363–1372,1733–1734`). Нажатие не даёт decision и не отвечает клиенту.

Исправление: исключить эти две причины из generic continuation, сохранить уже существующие source navigation/decision/private answer workflows. Не подделывать event payload и не считать задачу ответом. Тест serializer + полноценные existing review controls.

## F12 · Подпанель не обновляется и источник черновика не объясняется

**P2, SOURCE CONFIRMED; ограниченное исправление включено в f428; границы ниже.** Assisted controller загружается только при created (`bot.html:2569–2594`): same-client full refresh сохраняет старую no-request/approval панель. Обновление должно перезагружать clean/idle controller на принятом detail render, сохраняя dirty, busy, pending/UNKNOWN и A→B fences.

Отдельный симптом `openFulfillmentReply:2658–2665`: при unsaved A draft запрос ответа наB молча фокусируетA. Текст/пины сохранять правильно, но менеджеру нужно явное объяснение сохранить/отменить прежнюю черновую работу. Автоматическая неверная отправка не доказана. Acceptance — parent-mount regression и заметный feedback, без второй редакторской системы.

## F13 · Пустые коммерческие панели создают шум для любого клиента

**P3, SOURCE + authenticated UI CONFIRMED; larger UX work.** Assisted/waiting/split sections mount всегда (`bot.html:3689–3692`), даже verified absence. В URBAN видны все пустые панели. Сначала свернуть только verified-empty в понятные optional tools; failure/incomplete/dirty/history/UNKNOWN не считать пустотой. Контекстные current stock form/shortcut уже реализованы, переиспользовать их. Не скрывать нужное действие только по B2B категории.

## F14 · Анализ причин и предпочтений теряет область отрицания

**P2, CONFIRMED normalizer; малый fix причины включён в f428; глубокая часть открыта.** Source «Не беру, не через ціну. Ціна підходить.» принимался как price/reported_by_customer/high: `_reported_cause` отрезал negation перед causal connector (`ig_conversion_audit:215–219`). Исправление проверяет polarity самого connector. UA/RU/EN, affirmative price/real size/counterevidence/unknown controls обязательны.

**Открытая более глубокая часть:** exact substring quote не гарантирует истинность мотива. Source «Не на подарунок, беру для себе.» + quote «подарунок» принимает gift/high; обратный пример даёт self/high (`ig_conversion_audit:119–143,201–205,247–258`). Нужна source-context проверка вокруг occurrences, а не только model-selected excerpt. Не доказано, что живой Gemini действительно выбрал такие отрывки. Причины потерь — гипотезы, пока клиент сам их не назвал; молчание не доказывает цену/ошибку бота.

## F15 · Явные просьбы о стиле общения теряются либо меняют смысл

**P2, CONFIRMED extraction; ограниченный fix включён в f428; оставшиеся границы ниже.** «Пожалуйста, не коротко, а подробно» даёт[], «Пиши коротко і покажи принти» теряет brevity из-за `_ART`, «Не пиши коротко, но эмодзи можно» может дать emoji=omit из-за45char negative window (`ig_communication_preferences:35–84`). Raw current text ещё видит Gemini; доказанный дефект — durable preference, не фактический неудачный SENT.

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

**P1, CONFIRMED actual parser+plan+coverage; ограниченный fix включён в f428; оставшиеся границы ниже.** Source «L як прати» даёт size=L и obligations=[size]. Size-only «Ваш розмір L зафіксувала.» получает complete/remaining=[]. Аналогично «L а состав», «L какая ткань», «L яка тканина». Вариант «L яка щільність» сохраняет density. `_requested_topics` не узнаёт як/какая и cloth nouns, а fallback unclassified не добавляется при уже найденном size (`ig_response_plan:574,1871`).

Разбиение тех же слов на [«L», «як прати»] даёт второй unclassified debt: один смысл по-разному обрабатывается из-за границы сообщений. Ошибочный SENT не доказан; доказано, что actual coverage может пропустить независимый вопрос. Acceptance: size-only incomplete; source-backed care/material/price answer покрывает тот же intent в1/2/3 sources без ненужного manager debt. Сохранить quoted titles/negation/finished mass vs human weight.

**ОТКРЫТО: связанный material gap.** «Це бавовна?» имеет material obligation, но selectors `_SPEC`/`approved_public_facts._TOPICS` не выбирают fibre words; действительный base_context composition не учитывается материалом в coverage. Рассмотренный локальный fallback отклонён при независимой проверке: UK reconstruction не совпадает с RU/EN assertion; canonical per-line child не получает readiness. Этот дополнительный код полностью убран до выпуска. Нужен общий exact product/base/locale/source contract с native per-line тестами; текущая правка F19 улучшает распознавание вопроса, не объявляет всю material coverage закрытой. Дополнительно actual Gemini выдал «плотного трикотажа с начёсом, отлично сохраняет тепло» без выбранного факта о составе/начёсе, и неизменённый native guard принял ответ. Значит downstream factual validation тоже требует доработки: не только число GSM/цена, но и конкретные материал/подкладка/свойства должны иметь источник. Глобально запрещать слова о качестве или придумывать новую основу нельзя; нужны source-backed assertions и honest unknown.

## F20 · Недоступное вложение: receipt и semantic waiting пока различаются

**PARTIAL / acceptance gap, не доказанная тишина.** `record_unavailable_media_reply` имеет source/media bindings и origin=media_unavailable (`ig_revision_input:242`), но deterministic coverage в live2088 предусмотрена для иных origins. Clarification может быть SENT/processed, а `response_obligation` формально ещё ждёт semantic coverage. Lane health исключает processed, unanswered collector начинает с существующих debt tasks — поэтому нельзя заявлять, что это автоматически создаёт ложный alert каждому клиенту.

Acceptance: resend request должен честно подтверждать получение и waiting-on-customer, не сообщать, что картинка изучена, не порождать replay; отдельно проверить ранее существовавший debt. Полноценное Gemini visual понимание чужого/размытого изображения требует actual media-capable запроса, не текста с описанием картинки.

## F21 · После сброса UI продолжает показывать категорию старого анализа

**P2, SOURCE + production/UI CONFIRMED; отдельный rollout consumer gap.** При разрешённом владельцем сбросе только testclient5 @zainllw0w стандартный `reset_funnel` создал audit2/floor3376/new episode212. Backend stage=new, language пуст, memory_summary и sales_context пусты, прежние142 сообщения сохранены. Fresh authenticated UI правильно показывает memory0/signals0, но «Стан клієнта: Інтерес до товару» взят из snapshot2973 от15.10, watermark3373/episode135.

Это не generic default: `bot.html` показывает interaction_type_label, `_client_card` берёт latest analysis без reset floor в legacy ветке (`bot_views.py:_client_card`). Production `materiality_mode=off`, `selector_enforced=false`; защищённый current selector поэтому не используется. Данные сброса сами корректны. Старый label не доказывает утечку canonical cart: projection исключает session не из current episode, нет post-reset sources → lines=[]; «позиції не вдалося підтвердити» — общий empty/unavailable UI текст. Исторические tasks/deals остаются историей по контракту, не новой покупкой.

Acceptance: reset/read view не подаёт pre-reset interaction/intent как актуальное; gated selector/rollout и все другие consumers рассматриваются согласованно. Автоматически включать feature flag на production ради UI нельзя. Проверка shared Gemini generation не нашла переноса old snapshot в prompt/routing/allowed actions: `ig_turn_capture:283–300,411–448,533–555,631`, `ig_turn_intelligence:373–377,416–429`, `ig_turn_intent:315–325,417–426`, `ig_client_state_card:680–688` используют текущие source/reset/episode proofs. Однако глобально называть дефект только UI нельзя: `bot_followups:531,579` читает `current_analysis_snapshot→legacy_snapshot` без floor при off (`ig_analysis_materiality:383–413`), и старые support/B2B enums у других клиентов могут подавлять optional followups. Product_interest client5 не входит в suppression и не даёт send permission; FollowCTA также требует24h/current episode/watermark (`ig_follow_cta:329–339`). Включение selector требует materiality shadow + selector enforce (`ig_analysis_materiality:46–69`); одного freshness flag недостаточно. F21 остаётся открытым до согласованной правки readers и регрессий. Нового бизнес-вопроса владельцу не требуется.

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

## Реестр выполненного QA и выпуска

- 10 направлений: media/cards; turns/cart; B2B memory; contextual UX; ad/categories; analyzer/funnel; prompt/naturalness; delivery/resilience; historical/refactor; native Gemini harness.
- Первичные пробы — actual-source component extraction под project Python -B с deny write/network/sqlite; не production fixtures.
- Быстрые fixes имеют непересекающееся владение: turns; commerce_state; communication_preferences; conversion_audit; bot_views/template. Root владеет CI enrollment, двумя Markdown, интеграцией и release.
- Actual provider tests выполнены по новому конечному grant; результаты и ограничения в отдельном разделе ниже. Прошлые grants не переиспользованы.
- Публичный stress-test большим числом людей — следующий этап, не выполнен этим component QA.
- 23.10 владелец подтвердил вход в тестовый @zainllw0w и затем попросил работать через bot interface. Его тестовый сброс разрешён; другие клиенты/платежи/история не удаляются. Фактический reset и его область подтверждены ниже.

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

Focused owner gates: parser151tests/3 ожидаемых SQLite skips; B2B63unique tests с3native fake-SENT continuity checks; UI6Django +9JS files; conversion schema21; preferences26; terse questions48. Эти числа пересекаются и **не суммируются**. Root UI135/135,0skip и runner/workflow79/79 прошли. Первый общий77-source/91-label gate прерван root с exit130 при пересмотре material fallback: это **не PASS**. Повторный полный V2 gate завершён успешно; точные результаты ниже.

Независимый review до выпуска также нашёл и закрыл регрессию positive emoji suffix. Никакие неудачные captures/aborted tests не переименованы в успешные. Существующий `CartSourceFallbackTests.test_languages_use_the_same_line_scope_and_only_one_requested_selector` отдельно не проходит и на исходном HEAD: fixture задаёт UA source, но ожидает RU/EN из profile; test file не менялся. Это не доказанный регресс новых recipient labels.

**Root V2 общий gate: PASS1506/1506,539.224s,91labels,12 ожидаемых backend skips.** Все77 source hashes совпали до/после. После него удалена одна пустая строка EOF только в новом тесте terse_questions; AST тождественен, остальные76 hashes неизменны. Исходный tested freeze сохранён отдельно, новый freeze честно содержит эту test-only нормализацию; focused terse gate повторён:48/48,7.031s. Application source после полного прогона не менялся. Static pipeline PASS:1974 asset files/571manifest entries/4compressor entries; Django system check без ошибок.

**Тестовый сброс выполнен и проверен.** После сбоев подтверждения в browser tool root вызвал тот же штатный сервис только для exact client5+username guard, reason=`qa23_owner_authorized_test_account_only_reset`; внешние HTTP запрещены. Audit2/floor3376/episode212;142 сообщения до=после, память/контекст пусты, paused=false/takeover=false, новых provider вызовов0. Fresh bot UI показывает пустую память. Скрин `/private/tmp/twc-qa23-root/test-account-reset-ui.png`; immutable transcript/financial truth не удалялись. Старая классификация UI отдельно учтена F21. Повторные браузерные попытки без подтверждённой серверной записи не считаются успешным сбросом.


### Выпуск f428 и CI

- [Django CI39944867398](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39944867398) — success на exact f428. Workflow contracts, Instagram/context regressions, size/UI/static checks выполнены; опциональный full-smoke capture в этом push-job пропущен, не назван новым полным проходом всего проекта.
- [MariaDB CI39944867300](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39944867300) — все9suites и aggregate success. Для каждого скачан sanitized artifact и проверен `cleanup=verified`; live MariaDB не была тестовой БД.
- Supported SSH git pull main→f428, check/migrate(no new)/collectstatic/compress/reload выполнены. Policy publication7/hash и epoch11 не менялись. Первый немедленный runtime read поймал cold-start lanes-not-ready и сохранён как неуспешная проверка; последующий полный proof: main+10healthy, maintenanceoff, checkout/supervisor/actualchild=f428,77frozen paths и8static digests совпадают. Health/probe HTTP запрещён:0provider calls. Локальный Python3.14.6/Django6.1, фактический productionPython3.14.8/Django6.1 явно записан, не скрыт.
- Authenticated bot UI перезагружен: client5 @zainllw0w выбран, память пустая, обе изменённые JS загружены с новыми hashed URLs и query pins. Screenshot `/private/tmp/twc-qa23-root/postdeploy-bot-ui.png`, DOM proof `postdeploy-browser-proof.json`. Нового Direct inbound/SENT этим screenshot не доказано.
- При позднем чтении UI подпись уже была «Лише інформація». Ранее подтверждённый F21 legacy reader остаётся открытым, но утверждать, что экран непрерывно показывал один старый label, нельзя.


## Настоящий Gemini: результаты серии 3c26564c

Серия `3c26564c349d41d8a601cdeacf77c7a3` выполнена root ровно один раз после f428 deploy/runtime proof. Manifest SHA256 `bb6d4023cef77a2e7b4682f3d6f310c51f067e43994793b64e647c5b6830fe63`: 475 source pins, 31 package pins, 77 root paths. Два финальных capture7/8 дали одинаковые 10 native payload hashes; предыдущие невалидные/нефинальные captures сохранены. Новый grant: 10 logical, 60 секунд на logical, максимум 3 dispatch admissions; оставшийся бюджет не переиспользован.

Gateway — настоящий `gemini_generate_text`, role=management/lane=analysis, client/source/reply IDs=null, обычная production accounting MariaDB. Meta transport запрещён. Synthetic conversations/catalog/publication локальные, в live БД клиентские fixtures не создавались. Сохранены неизменённый gateway result, точный текст перед native JSON parser, accounting graph и hashes. Это не raw HTTP-body и не утверждение, что synthetic policy publication/management routing полностью совпадают с текущим customer_chat.

**Исполнение:** 10 logical attempted, 11 `provider_started_at`, 8 вернувшихся результатов. Семь chat-кандидатов: шесть `gemini-3.5-flash-lite`, один `gemini-3.6-flash`; успешный analyzer — `gemini-3.5-flash-lite`. Cancelled-before-dispatch rows не считаются provider вызовами. Нулевые usage counters у timeouts не означают нулевую стоимость. Изолированный unchanged native replay: все 7 chat candidates проверены, один analyzer нормализован, без proposal/outbox/Meta effects. Его exit1 отражает два unavailable candidates, а не переименован в полный PASS.

| Сценарий | Оценка смысла сырого ответа | Native replay | Что это доказывает |
|---|---|---|---|
| Три сообщения: себе M×1, другу L×2 | PARTIAL: числа верны; получатели не проговорены, focused clarification отсутствует | REJECT: unverified_price + unverified_availability | Исправление explicit quantities не закрывает contextual burst F04. Merge получателей/ошибочный заказ не доказаны |
| «Скиньте приклади» после classic/oversize | FAIL: обычные Aurora Black/Pink с ценами и show_products1,2 | REJECT: unverified_price | Actual модель проигнорировала antecedent посадок; F07. Запрет случайных карточек нельзя заменить одной price-защитой |
| Только фото Aurora на худи | FAIL: «немає худі…в наявності…лише футболки» | REJECT: price + availability | Missing-photo request превращён в неподтверждённое отсутствие товара; F09/F19. В fixture ID2 — hoodie, изображений нет |
| 50 чёрных худи с логотипом для компании | PASS по routing/relevance: wholesale handoff, manager=true, без retail CTA/цены/твёрдого срока | VALID | «Ми передали» допустимо только при выполнении downstream case/notification contract до отправки. Этот replay намеренно effects не выполняет, external alert/SENT не доказаны |
| Ранее футболки; теперь только худи | FAIL: оба ID названы худи и предложены вместе | REJECT: price + availability | F09: нет per-ID garment mapping и eligibility gate. Не доказано, что guard пропустил эти карточки |
| Английский + кириллическое внешнее название + Friday | PARTIAL: правильные EN/custom/неуверенный срок, но нет уточнения reference | VALID | Язык не сломан в этом sample. Это не воспроизведение живого @manuenggist и не подтверждение реального срочного заказа |
| Кратко, RU, без emoji: тёплое ли худи | FAIL: выдуманы «плотного трикотажа с начёсом, отлично сохраняет тепло» | VALID | Подтверждён upstream source + downstream factual gap F19. Выбран только unconfirmed_bases; approved спецификации отсутствуют |
| Analyzer: «дякую, я подумаю» после цены | UNASSESSED: результата нет | Не выполнялся без candidate | 25.214s read timeout + 12.871s HTTP503 UNAVAILABLE + 16.328s timeout; следующий dispatch остановлен конечным cap. Это availability/timeout, не доказанная ошибка мысли/JSON |
| Analyzer: «задорого, тому поки не купуватиму» | PARTIAL: верная price-причина; ошибочная self-мотивация из «Для мене це задорого» | Normalizer сохраняет reported price, понижает self до hypothesis/low | Защита от unsupported motive сработала. Raw lost/0/confidence1/uncertainties[] не удовлетворяет нашему критерию явной осторожности; постоянный уход клиента этим не доказан |
| Чужое размытое inline image | UNASSESSED: нет provider candidate | Не выполнялся | Nonlive enforce отклонил все кандидаты до dispatch: estimator_uncalibrated, 0 started. Accounting_guard_not_observed сохранён. Никакого обхода admission/подмены медиа текстом не было |

Семантическая оценка 7 chat: 1 PASS / 2 PARTIAL / 4 FAIL; отдельно server validator принял 3 и отклонил 4. Эти числа не складываются в общий процент качества: набор целенаправленно проверяет трудные границы, выборка мала, production traffic и conversion effect не измерялись.

**Важная причинная связь F09:** actual compact catalog даёт `id2 | Aurora —1290 | Pink…` и `id1 | Aurora —790 | Black…`, без category/garment/slug или легенды. Garment words есть в остальном prompt, но mapping конкретных ID отсутствует. Fixture DB знает id1=tshirt, id2=hoodie; модель не получила этого различия и стала угадывать. Значит исправлять нужно producer→capture→validation→coverage, а не объявлять ошибкой лишь «Gemini не понял». Реальные названия часто сами содержат тип вещи, но это не замена явному типу и не гарантия для будущего каталога. Тематические коллекции F10 отдельно ещё не подключены.

**Длительности logical gateway:** шесть быстрых chat 1.544–2.495s, сложный burst 17.446s; analyzer success 5.396s, failed analyzer 54.861s. Это не полное время ingress→SENT и не SLA customer_chat. Analysis lane не должна блокировать живую очередь; provider timeouts требуют своей диагностики, не бесконечного replay.

**Артефакты (private, не добавлены в Git):** `/private/tmp/twc-qa23-gemini-20261023/run-index-3c26564c349d41d8a601cdeacf77c7a3.json`, `raw-*`, `accounting-*`, `provider-candidate-text-*`; hashes проверены после SSH transfer. Local native replay: `actual-native-replay-3c26564c.log`, `capture-replay-actual-3c26564c-*.json`; aggregate runtime/model proof: `/private/tmp/twc-qa23-root/`. Raw failures не исправлены/не пересэмплированы ради зелёного результата.

### Следующий инженерный gate

1. F07/F09/F10/F19: один согласованный контракт предмета запроса, garment/collections, purpose media, source-backed material facts и semantic coverage. Проверить настоящий скомпилированный prompt и отрицательный candidate без предварительного выбора правильных IDs в mock.
2. F04/F06/F16: contextual burst/recipient/quantity, structured B2B и собственное языковое предпочтение; сохранять reset/source/payment/stock authority. Не маскировать UI старую неверную память.
3. F17/F20/F21: multipart receipt, unavailable-media waiting и legacy reset readers. Не превращать manager task/clarification/generated text в SENT или полученный substantive answer.
4. Visual QA заблокирован калибровкой nonlive media admission: выполнить отдельный разрешённый контракт калибровки и новый review/grant, не обходить guard. Затем actual foreign/blurred image, ad carousel и живой Direct test только на @zainllw0w.
5. После ответов на owner brief уточнить план приоритетов. Публичный multi-user stress, положительные/неуместные шутки и прямой вопрос о virtual identity этой серией не закрыты. Plan90 не менялся; работа того окна не объявлена завершённой.


Финальная независимая сверка документов не нашла расхождений между описанными counts, raw outputs и native replay. Дополнительно два модуля, впервые импортированные replay (`ig_revision_intents.py`, `ig_analysis_roles.py`), проверены root на побайтовое совпадение с Git blobs f428; их исходные hashes не были частью 420 pre-capture imports. Оригинальный manifest не переписан задним числом. Все 31 package pins и исходные native imports сохранены. Финальный private review: `actual-native-review-3c26564c349d41d8a601cdeacf77c7a3.json`, SHA256 `7be75fee2e15a593d13547b9339f9fc1577cd2d3de609e2a368509ae0cfbb2ff`.

Сводка статуса: малые исправления находятся в production f428; остальные подтверждённые gaps имеют доказательства, зависимости и следующий gate. Этот аудит не объявляет идеальными все сценарии, не гарантирует рост конверсии и не завершает чужой implementation plan. Финальная синхронизация документов выполняется отдельным docs-only successor commit без изменения проверенных application bytes.

## Дополнительный цикл Q23 / Леся · 23.10.2026

Этот раздел продолжает исходный реестр и не заменяет доказательства прежнего выпуска. До записи нового SHA и фактических gates ниже изменения считаются локальными. Предыдущая серия Gemini `1 PASS / 2 PARTIAL / 4 FAIL` сохраняется как исходное измерение; новый промпт не делает её результаты задним числом успешными.

### F22 · Технический ACK вместо обычного подбора двух футболок

**P1, CONFIRMED production evidence.** На тексте «Привіт! Хочу замовити футболку для себе та хлопця» revision249 отправил подтверждённый провайдером общий ответ о проверке командой. Содержательного подбора не было. Это не решение Gemini: до provider dispatch выполнение не дошло. Обязательная часть native packet заняла **48 151 символ при лимите 48 000**, код `mandatory_policy_exceeds_budget`; сработал `local_policy_fault` и создалась техническая задача менеджеру. Доставка ACK и закрытие содержательного вопроса — разные факты.

Причинная цепочка: большой полный каталог + дублированная JSON-корзина + current-client state и turn → переполнение обязательной части → модель не вызвана → fallback ACK → клиент ожидает ответ команды по обычной покупке. Поэтому исправление только текста приветствия или recipient regex первопричину не решает. В текущей реализации каталог для native turn ограничивается полными релевантными строками с приоритетом текущих SKU; повтор корзины заменяется ссылкой только при доказанном полном совпадении с обязательным state. Полный core, источник выбора, вопросы, отрицания и доказательства не обрезаются. Лимит 48 000 не повышается. Acceptance отдельно проверяет сам compiler admission и запас в mandatory, а не объявляет весь transport JSON короче лимита: после compiler существуют отдельные служебные дополнения.

### F23 · Люди, позиции, количество и свободный разговор — разные сущности

**P1/P2, CONFIRMED source-chain gap.** В исходном свежем запросе источник упоминает клиента и парня, а canonical session сохраняла только self. Размеры M/L и количество по одному из этой фразы не следуют. Наоборот, поздние «себе одну M, хлопцю дві L» содержат уже отдельные подтверждённые значения. Необозначенное «розмір краще M» после этого не разрешает угадать получателя по последнему сообщению.

Решение использует существующую typed commerce session и source-backed операции: отдельные стабильные line/recipient scopes для планирования без выдуманного количества и SKU; отдельные конфигурации и их последующие исправления; текущий вопрос и консультация до покупки; комбинированный quote только для действительно согласованных позиций. Подбор, факт наличия и финансовое оформление не объединяются в одно право. Натуральные формы с «для» и дательные «собі/хлопцю» проверяются отдельно. Source edit, reset и смена сессии должны инвалидировать старое основание. Вопрос о скидке за парный look не активирует B2B-процент и не доказывает готовность оплатить. Общий адрес не выводится из родства.

### F24 · Сброс воронки недостаточен для теста с нуля

**P1/P2, CONFIRMED integration gap.** Старый reset специально сохраняет подтверждённый профиль и не является обещанием стереть весь контекст. Нужна отдельная операция с явным названием/подтверждением и новым source floor. Сохраняются реальные transcript/orders/payments, consent, pause/takeover и сервисные задачи менеджера. Очищаются mutable preferences/profile/contact/ad defaults, активная корзина и производные данные текущего диалога; открывается свежая episode/session. Не запущенные старые автоматические работы прекращаются. `PROVIDER_STARTED`, `SENT` и `UNKNOWN` нельзя подменять отменёнными и повторять.

**Дополнительно подтверждённая гонка:** inbox drainer уже держит raw event, проверяет reset до его выполнения; reset пропускает заблокированную строку через `skip_locked`; после reset handler без inbox highwater мог записать старое сообщение с неизвестным временем, а referral применялся ещё до enqueue fence. Три детерминированных проверки воспроизвели наполнение очищенного контекста. Исправление должно сериализовать materialization и reset по одному client lock до блокировки inbox row и проверять источник **до** referral/classifier/USER/clock mutation. Одного позднего postcheck недостаточно. Старый явный STOP обрабатывается отдельно как согласие, старый START не возобновляет бот.

### F25 · Наличие: положительный точный учёт и неизвестная другая позиция

**P1/P2, UPDATED OWNER CONTRACT.** Ранний общий запрет самостоятельного подтверждения учётного остатка заменён уточнением владельца: достаточный свободный остаток точной конфигурации допускает обычную покупку. Ноль/unknown/отсутствующая запись не доказывают физическое отсутствие; команда проверяет именно спорную позицию. Для двух принтов на одной складской основе спрос суммируется по physical pool с учётом защищённых резервов. Нельзя одновременно продать одну свободную основу двум позициям.

Положительная typed inventory observation — самостоятельный источник, а не подписанное решение менеджера, recount, `warehouse_checked`, резервирование или дата отправки. Старый pending-zero не должен скрывать новый положительный остаток после поставки. Source/config/pool должны повторно совпасть перед реальной отправкой ответа; замена старого доказательства новым положительным значением незаметно для ответа недопустима. Подтверждённый inventory не должен обходить финальные transaction checkout locks.

В интеграции обнаружены и переданы владельцу исправления: optional inventory не должен превышать общий 96-read ceiling, особенно после exhausted waiting-list; положительный warehouse при legacy catalogue rule0 не должен блокироваться каталоговой эвристикой; благодарность без stock assertion не должна закрывать обязанность ответить о наличии; наличие не разрешает приписывать менеджеру несуществующую физическую проверку. Все эти границы требуют native regression, не одной prompt-инструкции.

### F26 · Тематические карточки и отрицания без пунктуации

**P2, CONFIRMED capture/native gap.** В «Покажіть не футболки а худі» отрицание могло исключить обе категории; «не military а Харків» — обе темы. «Не тільки військові» ошибочно исключало military. Отдельно неизвестный именованный принт с известным типом вещи («худі з принтом Одеса») мог превратиться во все худи, если не было слова «тільки». Исправляются область отрицания, противопоставление и сохранение неизвестного именованного ограничения. Перечень «без military і Харків» при этом остаётся исключением обеих тем.

Новая карточка строится по реальным опубликованным товарам и active collections; принадлежность может быть множественной. Нельзя показывать худи на запрос только футболок или отдавать случайный дизайн вместо явно названного неизвестного. Маленькая релевантная подборка сохраняет ограничения и до трёх кандидатов. URL темы разрешён только при реальном поддерживаемом фильтре и соответствующем captured card proof; unsupported OR/exclusions не превращаются в выдуманную точную ссылку. Это не закрывает целиком F07/F08: macro-cover и реальные доступные иллюстрации посадки требуют отдельных фактических материалов/проверок.

### F27 · Согласованные знания о сотрудничестве теряются между сайтом и промптом

**P1/P2, CONFIRMED source-contract gap.** Ответы владельца теперь разделяются на опубликованные UK/RU/EN модули сотрудничества. Public facts: индивидуальные 10–15% от текущей полной цены сайта, обсуждение условий от 8 продаж **в месяц**, отдельно оптовые выкупы кратно8, варианты ТТН с предварительным согласованием отправителя, Telegram как основной канал, кабинет TEST, существующие материалы бренда и отсутствие автоматической бесплатной выдачи вещей. Из этого не следуют конкретный процент, подтверждённые деньги, выплата, договор, права на чужой UGC или срок.

Native право на защищённую public-policy фразу связывается с действительно выбранным опубликованным телом модуля, его ID/version/hash и собственным актуальным вопросом клиента. Новая версия кода со старой публикацией, черновик, процитированный клиентом модуль и одни tags не создают такого права. Независимый review выявил обходы через дописанную гарантию к корректной фразе и ложное отрицание «without doubt guaranteed»; они требуют full-claim проверок. Нельзя просто добавить 10/15 в общий allowlist всех скидок.

Убраны противоречащие общие проценты со страниц cooperation/wholesale и FAQ, включая видимый текст и FAQ JSON-LD UK/RU/EN. Старый кабинет сохраняет отдельную legacy финансовую реализацию; TEST-баннер не означает её миграцию. Пожелания про универсальную минимальную цену перепродажи, автоматические3% и несовершеннолетних не превращены в юридические разрешения — основания записаны в файле вопросов/решений.

### F28 · Неоплаченная ссылка: ложная срочность и повторная проверка перед отправкой

**P2, CONFIRMED source gap; не доказан live customer incident.** Старый `payment_a3` говорил «скоро закроется / лучше сегодня», проверяя только `status=live`, хотя срок счета и его замена могли быть другими. Копия должна оставаться нейтральной без проверки реального ближайшего expiry. Отдельно legacy timer после PROCESSING мог пропустить оплату/новый inbound/смену корзины между ранней eligibility и actual provider boundary: pending-cancel не отменяет уже взятую задачу. Нужна повторная проверка фактов в самой границе отправки с binding к claim и актуальному источнику/корзине/invoice. Согласованные одноразовые payment reminders с отдельным согласием не смешиваются с этой старой policy-лестницей.

Обычные pending reminders после подтверждённых200/полной оплаты уже прекращаются; reset меняет permission epoch. Эти работающие части сохраняются. Частота/Meta-window существенно ограничивают достижимость третьего касания; source bug не объявляется массовым production-инцидентом без receipt evidence.


## Уточнение воронки и полный сброс · 24.10.2026

Изменения этого подраздела ещё не объявлены production до отдельной записи SHA/gates. Они дополняют F22–F28, а не делают старые model samples успешными.

### F29 · Полная корзина ошибочно разрешает предложение без готовности клиента

**P1, CONFIRMED component/native.** На полностью выбранных двух вещах «Поки не готова замовляти, підкажіть ціну… Чи є знижка?» могло пройти через неограниченный поиск purchase commitment. Дополнительно CRM stage мог заменить реальное согласие, а вопрос о скидке оставался долгом уже после формирования предложения. Это два разных барьера: source-backed готовность клиента и покрытие актуальных вопросов. Исправление проверяет собственное действующее намерение, отказ/условие, неоднозначные изменения и реальные ответы до candidate admission и повторно внутри checkout transaction. Предыдущее явное согласие с последующим выбором размера не требует искусственного «подтвердите ещё раз». Слова о готовности без ответа на текущий вопрос не закрывают воронку. Независимая проверка дополнительно воспроизвела короткое «Передумала. Поки не готова» после прежнего согласия, точное «оформлюй обидві», ошибочный выбор 👍 вместо действующего сообщения-согласия и самостоятельное снятие старого перевозчика. Для них нужны отдельные source/negation regressions; прежнее согласие в fixture не должно маскировать поддержку нового подтверждения.

Доставка Новой почтой выбирается в hosted form. Собственный актуальный запрос другого перевозчика, например Укрпочты, требует конкретного human-arrangement case, сохранения корзины и уведомления до customer acknowledgment, а не молчаливого создания NP-предложения. «Не Укрпоштою, а Новою поштою» и последующая явная смена на NP проверяются отдельно. Нет нового универсального carrier backend или обещания сроков.

### F30 · После полного сброса старый hosted link сохраняет финансовую силу

**P1, CONFIRMED component.** Только новая episode/source floor не отзывали прежний bearer URL и cached browser grant. Новый режим сброса отзывает локальные неоплаченные hosted tokens; безопасно не начатые попытки закрывает существующим native terminalizer, освобождая только их доказанные резервы. Реальная оплаченная квитанция и история сохраняются. Provider-created/inflight/uncertain bank evidence сохраняются для review: локальный сброс не отменяет банковский счёт и не обещает это клиенту.

При этом обнаружен inverse lock order: reset держит Client перед Deal, обычный settlement — Deal перед Client. Reset использует NOWAIT для финансового graph и возвращает явный409 reset_busy после полного rollback, вместо ожидания/взаимоблокировки. Отдельный MariaDB two-connection gate должен подтвердить гонку; SQLite skip не считается его выполнением. Partial retirement rollback и неизвестные SQL ошибки проверяются отдельно: неизвестная ошибка не переводится в ложный успешный сброс.

### F31 · Каталоговый цвет теряет доказанную идентичность на соседней позиции

**P2, CONFIRMED native.** Нулевой остаток известного чёрного варианта создавал нужный availability review и лишний configuration review «неизвестный цвет». Для неактивной строки риск повторялся, даже когда активная была другой. Исправление передаёт серверное подписанное доказательство существования конкретного variant только своей строке, product/source/configuration. Оно не подтверждает доступный остаток, наличие на физическом складе или право checkout. Поддельная подпись/чужая строка не дают исключения; неизвестный синий цвет по-прежнему требует собственного review. Подписи не передаются в модель.

### F32 · Запятые разрывают конфигурацию и stock assertion

**P2, CONFIRMED native.** Обычный ответ «Restock Aurora, чорний, розмір M є в наявності» разбивался на отдельные clauses и терял связь с товаром. Typed apposition нормализуется только в validation projection. Новое лицо, товар, ordinal или самостоятельное утверждение остаются отдельными областями; отправляемая пунктуация не переписывается. Нативные проверки охватывают реальную двухпозиционную candidate→proposal→owned hold→fake transport SENT цепочку и запрет чужого цвета/неподтверждённой соседней позиции.

### F33 · Получателя нет на странице проверки предложения

**P2, CONFIRMED view gap.** Конфигурации уже разделялись в commerce cart, но hosted review не показывал, какая вещь для кого. Human label теперь выводится только по проверенной provenance persisted proposal item→quote position→recipient. Нет позиционного/одноимённого SKU угадывания; legacy или неполное доказательство даёт отсутствие подписи. Языки UK/RU/EN проверяются. Это улучшение представления, а не изменение цен/платежа/доставки.

**Поведение исправлений после выдачи ссылки:** используются реальные revision/amendment/successor semantics. Чистый URL показывает актуальную допустимую revision; superseded proposal может потребовать последнюю ссылку из Direct, approved amendment — переход к successor с grant. Нельзя обещать, что любая старая ссылка всегда мгновенно изменяется, или придумывать оплаченный заказ из изменённой корзины.

**Проверка старого финансового gate:** на pristine base воспроизводилась ошибка late verified payment test из-за ненативного reservation fingerprint. Фикстура текущего hold приведена к native fingerprint, отдельная stale-identity regression сохраняет правильный fail-closed review, PAID/CONFIRMED и отсутствие Order/consumption. Production revision fence не ослаблялся. Отдельный исторический public checkout module имеет28 одинаковых ошибок на base и рабочем дереве; это baseline, а не пройденный модуль.


### F34 · Вопрос о размерах становится запросом дизайнов

**P2, CONFIRMED regression introduced during this cycle and repaired before release.** Bare existence matcher «які є» принимал «Підкажіть, які є розміри?» за catalog browse. Без garment/theme/name создавалось unresolved presentation debt; обычный meaningful-caption путь с недоступной картинкой заканчивался `semantic_reply_incomplete`. На pristine ebfa точный native regression проходил, в промежуточном дереве — нет. Нужен реальный предмет merchandise/model/design в existence question; size/color/price/payment/delivery относятся к своим информационным владельцам. Исправление находится в admission, а не ослабляет последующую проверку неизвестного принта. Exact caption снова проходит; attribute matrix не читает каталог (0SQL), genuine browse сохраняется.

Соседний старый catalog-media test содержит3 no-URL subtest assertions, хотя неизменённый `append_catalog_discovery_hint` уже добавляет реальные ссылки на категории. Все3 воспроизведены и на pristine ebfa; они отдельно отмечены baseline, не засчитываются как green broad media suite и не исправляются ослаблением нового browse guard.


### F35 · Общий gate обнаружил связи, не видимые в отдельных модулях

Полный замороженный прогон исполнил **2045/2045 tests за670.204s**, завершился **19 failures,1 error,15 skips**. Это сохранённый неуспешный gate, а не результат выпуска. На его основе открыты отдельные причинные исправления:

- **Реальный parser regression:** `(M, bob L)` правильно не считалось чистой конфигурацией, но затем внутренняя запятая отделяла bob от `item1 for alice`. Цена bob ошибочно проходила как отдельное правдивое утверждение. Сбалансированный foreign parenthetical сохраняется единым конфликтным subject; malformed syntax не перегруппируется. Отдельно проверяются чужой товар и ordinal.
- **Реальный размер binding:** обычные карточки «покажіть худі» дали4110 bytes при cap4096. Увеличивать cap нельзя: три повторяющиеся полные category references заменяются content-addressed ссылками с реконструкцией и проверкой исходных product/category/locale/name/URL. Подписанные payloads, source/snapshot и старый формат сохраняют проверку. Нужны native SENT и tampering negatives.
- **Неполный test-double owner:** новый source-owned carrier reader обращался к current episode у `SimpleNamespace`. Неперсистентный/поддельный owner теперь не даёт authority и не выполняет SQL; настоящие native cases сохраняются.
- **Старые тестовые предположения:** pending-zero и selection-no-op fixtures фактически ставили100 единиц ради прежнего обхода цены. Теперь это честный положительный inventory, поэтому будущая manager-check фраза не закрывала вопрос. Меняются только доказанно неверные setup на0; реальные100-stock autonomy и0/100 price-only continuation проверяются отдельно. Кандидаты и итоговые assertions не ослабляются.
- **Неполный price/check consumer:** легальная цена точной выбранной конфигурации при нулевом остатке и topic-first проверка у менеджера требуют собственных source permissions, которые не равны stock availability. Foreign product/recipient не должен заимствовать ни эту цену, ни конфигурацию.
- **Старые publication assumptions:** исходные пять деклараций сохраняются, добавленные три locale rows используют общий facet `business_cooperation`. Проверяется реальный selection одной локали перед прежним48k compile и отсутствие B2B в creator-only, а не синтетический compile всех локалей сразу.

Последующие результаты исправлений и повторного полного gate записываются отдельно. До них этот подраздел не означает успешный deploy.

Независимая проверка ценового исправления дополнительно нашла реальную подмену типа вещи: точное название и цена худи могли пройти вместе со словом `t-shirt`. Тип теперь приходит из backend readiness и проверяется существующим garment guard; source-only цена не даёт права менять вид одежды или утверждать наличие. Собственная конфигурация, чужие скобки, строки и получатели проверяются отдельно. Финальный компонентный прогон этих связанных модулей: **163 tests,130.430s,OK**; это ещё не полный gate и не production/model delivery proof.

### F36 · Повтор согласия на уведомление создаёт лишнее обращение менеджеру

**P2, CONFIRMED integration regression before release.** Точный native test `test_fresh_notification_optin_keeps_exact_source_binding_and_replay` воспроизводит3 requests вместо2. Свежая просьба уведомить о поступлении правильно требует собственного source-bound review даже при положительном учёте склада; однако новый explicit flag пропускал прежнюю idempotent reuse ветку. Повторная обработка того же source/revision создавала лишний case и alert до возврата уже существующего invitation. Это отдельная причина от наличия/цены. Исправление должно сохранять строгое совпадение source/revision, подписанную конфигурацию, свежесть и ownership; действительно новое сообщение должно создавать новое обращение, а обычная покупка при доказанном положительном остатке — идти самостоятельно. Статус исправления и повторного общего gate фиксируется после выполнения.

### F37 · Проверка цвета без необходимости строит ценовой контекст

**P2, CONFIRMED component regression before release.** Точный `ColorChoiceFamilyCoverageTests.test_other_product_line_cannot_discharge_current_purple_wish` падает с `KeyError: prices`: новый coverage consumer безусловно строил price contexts до разбора color-only obligation. Минимальный допустимый план без ценового authority не должен проходить через ценовой producer. Узкое исправление — вычислять эти контексты только при `info:price`, сохраняя прежнюю проверку соответствия цвета конкретной товарной строке. Это воспроизведение через компонентный `SimpleTestCase`, не доказанный production-инцидент и не реальная отправка. Нельзя расширять допуски или заменять неизвестную цену на предположение ради исправления исключения. Применение и итоговый gate фиксируются отдельно.

## Итог локального интеграционного gate · 24.10.2026

F36/F37 исправлены ровно двумя проверенными изменениями. Новая native regression проверяет положительный остаток без менеджера, opt-in с собственными source/revision, повтор без нового case/alert и новое сообщение с отдельным case. Независимый reviewer проверил итоговые hashes и обратным применением только этих изменений восстановил прежние hashes исходников; дополнительного изменения области нет.

- Второй полный прогон: **2053/2053,694.704s,1 failure+1 error,15 skips** — сохранён как неуспешный, его причины F36/F37 не скрыты.
- Финальный полный прогон: **2054/2054,686.379s,OK,15 skips**;117 labels,97 source paths проверены по SHA до и после выполнения. Локальная SQLite не заменяет MariaDB concurrency proof.
- Связанный stock/price/color/preoffer набор: **219 tests,160.871s,OK**; три точные регрессии отдельно проходят.
- JavaScript: **128/128**; runner/workflow/stage0 contracts: **107/107**. Django check — без замечаний; изменений миграций нет; `git diff --check` чистый.

На момент этой записи новый commit/push/deploy, native CI, публикация новых policy rows, новая actual-Gemini серия и реальный полный reset Леси ещё не объявлены выполненными. Их результат фиксируется отдельной записью после получения доказательств. Старые model failures и baseline checkout/media failures остаются историческими результатами, а не превращаются в PASS этого gate.
