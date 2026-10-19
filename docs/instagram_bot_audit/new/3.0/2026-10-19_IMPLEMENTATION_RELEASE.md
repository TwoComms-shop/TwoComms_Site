# Внедрение аудита конверсии и контекста · 19.10.2026

Поручение: реализовать все находки и сценарии с субагентами, проверить, commit → push main → SSH pull → runtime/customer checks. Начальная версия main/origin/production: `a4818a1b7c621b9f70af818271f94a5b110f2d9a`. Начальный tracked diff пуст. Несвязанные untracked материалы не принадлежат этому релизу.

Источники: [38 находок](2026-10-18_CONVERSION_CONTEXT_FINDINGS.md), [ответы владельца](2026-10-18_OWNER_BRIEF_GRILL_ME.md). План90 другой ветки работ не переписывается.

## Этап

Идёт интеграция и проверка. Наличие кода/локального теста не означает деплой. Все9 тематических исполнителей передали изменения; дополнительный исполнитель интегрировал платёжные формы, независимый ревьюер проверил денежные контракты. Root отвечает за общую сборку, сохранение контекста, публикацию, Git и сервер.

Локальный runtime: обязательные CPython3.14.6/Django6.1. Серверный baseline3.14.8/Django6.1 сохраняется: patch-разница зарегистрирована, спецификацияAGENTS не меняется. Production MariaDB никогда не используется как тестовая база.

## Покрытие находок

| ID | Исходная находка | Состояние интеграции |
|---|---|---|
| REL-01 | Закрыто · Основной checkout приведён к production без потери прежних файлов | Source baseline сверен с main/origin/production; финальный SHA фиксируется после release |
| REL-02 | P2 · Source parity не равна идентичности среды исполнения | Runtime проверяется отдельно: local3.14.6/server3.14.8, Django6.1; pin сохранён |
| AD-01 | P1 · Содержимое креатива теряется между webhook и sealed request | Creative/source role проходит sealed manifest до provider; ad media не selection/payment/UGC authority |
| AD-02 | P1 · Два resolver по-разному считают mapping разрешённым | Один resolver published/reviewed mapping для intake и prompt |
| AD-03 | P1 · Обычный редактор не задаёт точные товары; несколько товаров не представлены | Редактор single/multi/theme; ordered candidates и native ordinal/both reducer |
| AD-04 | P1 · Несколько рекламных касаний текущего burst сжимаются в одно | Ordered touches; отдельные source/recipient scopes, первая позиция не стирается |
| AD-05 | P1 · Merge может соединить identity одной рекламы и креатив другой | Identity-compatible merge; конфликтующие identity/media не склеиваются |
| AD-06 | P2 · Mapping не имеет версии, привязанной к креативу | Mapping/source digest в capture; текущий warm follow-up проверяет mapping, accepted replay immutable |
| AD-07 | P1 · Standalone referral существующего диалога отбрасывается до attribution | Signed standalone receipt + дедупликация; только следующее настоящее сообщение создаёт turn |
| LAT-01 | P1 · Очередь способна превратить обычное продолжение покупки в лишний manager debt | Один JIT preparation successor после получения lease; stale source/повторное истечение блокируются |
| LAT-02 | P1 · Recovery ждёт другой проект даже при локально eligible независимом кандидате | Eligibility независимого projectB не блокируется cooldown projectA |
| LAT-03 | P1 · Допустимая длительность первой отправки может оставить ответ частичным | Ограниченный transport tail exactplan; independent groups/fallback продолжаются без UNKNOWN replay |
| MEM-01 | P1 · Existing head может застрять на incremental backlog; bootstrap и reader имеют другие пределы | Конечные24-source/12k windows, overlap длинного источника, CAS/checkpoints |
| MEM-02 | P2 · Reset-era cap блокирует media-derived focus после13 owner rows | Latest valid photo focus сохраняется после13 исторических ownerrows |
| MEM-03 | P2 · Явная просьба о стиле ответа не имеет столь же устойчивого контракта, как язык | Source-owned persistent/temporary preferences; delayed slots без narrative rewind |
| FIT-01 | P1 · Смена категории может сохранить старую футболочную тему | Information subject отделён от корзины; hoodie вопрос не наследует tee facts |
| FIT-02 | P1 · Консультацию по худи нельзя считать готовой из футболочной реализации | Текущие hoodie слегкаoversize/unisex; exact CAS visibility для102/115, без новых мерок |
| FIT-03 | P2 · Утверждённый blank/schematic пример кроя не представлен отдельным видом media | Signed blank constructor previews: основа/view/source/version и честная подпись |
| FIT-04 | P1 · В одном запросе фото кроя и сетка могут конкурировать | Две exactfit charts + fit media; независимые offsets/fences/fallback и signed absence |
| FIT-05 | P1 · Картинка сетки не заменяет точные числовые замеры в контексте | Фактические bounded numeric guides в prompt; никакой подстановки tee tolerance вhoodie |
| KB-01 | P1 · Владелец получает ложное обещание, что commit brand.md обновит знания | brand.md явно editorial; runtime знания публикуются отдельным CAS publisher |
| KB-02 | P1 · Не хватает scoped owner-maintainable knowledge по основам, темам и принтам | Разделены bases/care/payment/stories/scenarios, relevance и knowledge lineage |
| KB-03 | P1 · Mandatory sales block подаёт custom/rescue/collaboration каждому non-service ходу | Короткое universal core; custom/rescue/collaboration только по собственному текущему запросу |
| KB-04 | P1 · Guard success ещё не означает проверку всех заявлений о ткани и качестве | Scoped composition/material/density/care/quality assertions, coverage и finalguard |
| KB-05 | P1 · Активная production publication не объявляет required-scenario coverage | Пять reviewed requiredscenarios проверяются preview и публикуются existing CAS API |
| KB-06 | P1 · FAQ содержит неурегулированные расхождения с owner direction и guarded условиями | Public FAQ/HTML/JSON-LD/locale и шесть exacthashed liveinstruction bodies согласованы |
| SALE-01 | P1 · Даже approved состав принудительно требует manager review и не имеет полного factual coverage | Known facts отвечаются безmanager; disposition сохраняется до finaltransport, вопросы независимы |
| SALE-02 | P1 · Retail rescue, программы лояльности и сроки кодов требуют разных оснований | Реальные grant/expiry/ownership отдельно от rescue recommendation; новыеQ15 уступки не выдуманы |
| SALE-03 | P1 · Разговорное согласие на 200+COD не совпадает с нынешним серверным predicate | Разговорное согласие на актуальный200offer — проверяемое offer_acceptance |
| SALE-04 | P2 · Validator согласованной цены распознаёт число лучше, чем смысл предложения | Денежная роль суммы, exactbasket/currentoffer; доставка/скидка не цена товара |
| SALE-05 | P1 · Отдельный legacy/manual renderer превращает оплату в обещание «уже собираем» | Payment receipt без ложного начала сборки/двухчасового обещания; deadlegacybody удалён |
| SALE-06 | P2 · Routing и persisted objections используют разные определения актуального возражения | Routing и persisted objections используют один own/current source predicate |
| SALE-07 | P1 · Новый full-only shipping rule не выражен в действующей policy | Новаяv2 доставка: netgoods≥3000 +full; freshACK при потере; старый frozenv1 сохранён |
| SALE-08 | P1 · Программа100баллов→10% заявлена владельцем, но выдача права остаётся stub | 100points→один настоящийpersonal10%grant; atomic debit/history/retry/rollback |
| MET-01 | P1 · Существующие показатели не определяют целевую конверсию «обращение → оплаченная покупка» | Immutable inbound inquiry до ошибки ответа,14day uniqueclient cohort и paid evidence |
| MET-02 | P1 · Рекламный отчёт использует текущую client attribution для прежних событий | Acquisition и purchase attribution заморожены отдельно; unknownhistory не guessednew |
| MET-03 | P1 · «Оплаты после скидки» могут включать неоплаченный созданный заказ | Только подтверждённые платежи;200+balance одногоOrder — одна покупка, refunds отдельно |
| MET-04 | P2 · Неизмеренный эффект follow-up записан как ноль | Неизмеренный followup effect=NULL; показана неопределённость,10% не обещано |

## Новые важные проверки интеграции

- Старая v1 доставка1550/3000 должна сохранить progress51 и валидность paid/TTN-договора. Новое округление толькоv2.
- Денежная роль: «футболка950, доставка80» не согласует товар за80; «скидка100, цена850» не согласует цену100. Дополнительные реальные servicecreate-пробы добавлены.
- Три денежные таблицы production используютMyISAM: accounts_userpoints, accounts_pointshistory, storefront_userpromocode. Миграция0238 переводит только их вInnoDB; auth_user не меняется.
- Миграция0237 добавляет BotAdCampaign.context и отдельную durable очередь ad metadata с lease/CAS. Read capability выключена без явной конфигурации; здоровье/статус не делают provider/Graph probes.
- Native MariaDB gate выявил старую несовместимость fresh-schema finance0019/0020: принудительныйMyISAM funding source не совместим сInnoDB transaction FK. Исполнитель отдельно исправляет выбор engine по фактическим родителям, сохраняя legacyproduction и не конвертируяauth.
- Числовая сетка худи не получена. Код читает реальные существующие numeric guides; новых чисел не выдумываем.
- Не определена формула нескольких тестовых отправок200 и новый порядок proactive5/10: неоднозначное не автоматизируется, остальные исправления не ждут их.

## Нужные root-интеграции / контроль перед релизом

- AD media legacy/source purpose, captured context+version+manifest, canonical inbound and background importer wiring.
- Source-bound category facts before inquiry capture; preserve backend base_context_assertions; known fabric answer semantic coverage.
- Общий live worker: queued preparation successor только при полученном automation lease; finite exact-plan transport tail, legacy execution lease.
- Chart и fit photos независимо закрывают свои native obligations; blank-only fallback; actual numeric guide DTO.
- Memory/preference DTO одинаковы в prompt/state/manifest; никаких inferred profile claims.
- Knowledge lineage optional backward-compatible accounting field; required five scenarios через существующий review/publish и coreCAS отдельно.
- MET: каждое своё коммерческое входящее наблюдается до возможной ошибки ответа; unknown history честно отделена; оплаченная покупка не равна consultation sequence.
- JS/static versioning, authenticated browser, реальные отправленные сообщения только при настоящем authorised клиентском событии.

## Проверки

Первый root smoke:150 новых тематических тестов прошли, disposableSQLite/no-network. Это не полный native/end-to-end gate и не сумма уникальных тестов всех исполнителей. Последующие результаты и финальный SHA будут добавлены ниже.

## Деплой

Ещё не выполнен. Поддерживаемый путь только AGENTS: scoped commit/pushGitHubmain, SSHpullmain, нужные migrations/static/process/publication, затем read-only runtime/DB и клиентскийflow. Секреты остаются вKeychain/loader, влог/документы не попадают.

## Повторная интеграция 19.10 — подтверждённые результаты до Git/deploy

- Общий root-запуск: **371 тест прошёл** (`/private/tmp/twocomms-conversion-release-gate-20261019.log`). Дополнительная проверка последних связующих изменений: **129 прошли**. Это пересекающиеся наборы, складывать их как уникальные тесты нельзя.
- Native MariaDB11.4.12, локальные CPython3.14.6/Django6.1: **36 реальных тестов без пропусков** в пяти suites. Полный финальный conversion-context gate15/15 прошёл также migrations до0239, reverse/reapply, database/schema check и cleanup. Остальные21 тест прошли до исправления общих runner post-checks; полные исправленные gates для четырёх suites остаются LinuxCI gate точного pushed commit.
- **61** runner/workflow contract прошёл; эти mocked contracts не заменяют предыдущие native доказательства. Исправлены ReviewVote InnoDB expectation и восстановление исходного BlogCategory только в принадлежащей runner тестовой базе. Production не использовался как fixture.
- Независимые замечания закрыты реальными регрессиями: UNKNOWN одной media-группы не блокирует независимый текст; старый execution resume получает один отдельный immutable transport tail; delayed preference сохраняется без narrative rewind; «а худі?» + «який склад?» не получает95/5 от retainedtee; composition иdensity требуют независимых ответов; API capture_response_plan безrevision снова работает.
- «Коротко, без емодзі» сохраняет две явные настройки; цитата, описание собственного стиля и временная просьба не становятся чужими постоянными предпочтениями.
- Уточнение по крою не выбирает его: «покажіть classic» сохраняет фактическую ранее выбраннуюoversize. Явное «беруclassic, покажітьсітку» сохраняет реальный выбор. Набор parser/source/response-plan91/91 прошёл.
- Native исполнение рекламы: упорядоченное «другу/обидві» и отдельные получатели проверяются через canonical reducer, seal, mocked provider validation, outbox и повторное чтение корзины. Неопределённое «справа» допускает полезное уточнение с неизменённой корзиной. Межходовый bridge проверен native execution: M1 без carttransition → SENT вопрос → M2 «другу», прежняя корзина/получатель сохраняются; редактирование M1 перед отправкой блокируется finalCAS.
- Совместная отправка chart иfit потребовала signed offsets и независимого CAS обоих assets при сохранённом4096 bindingcap. Регрессии двух exactfit charts, отсутствующей одной/всех сеток и сохранения реального выбора прошли native mockedtransport execution;250/250 affected +3/3 finaltargets. Это две посадки одногоSKU, не доказательство multiSKU chartcomparison.
- Прямая read-only проверка production: main по-прежнемуa4818a1b; unified context/memory включены; activehead2/draft2 совпадают,20 инструкций. Шесть source-hashed старых тел (включая запрет200) согласованы с ownerrules через CAS-publisher; пять requiredscenarios добавляются отдельно, чужой draft блокирует публикацию.
- Новая migration0239 только фиксирует modeldefault canonicalcore; существующая DB-инструкция меняется отдельной явной coreCAS publication.

### Честные границы

Новая картинка hoodiechart не получена; неизвестные плечи/рукав/GSMдляновойосновы не генерируются. Принты с неопределённой версией не получают придуманную историю. MarketingAPI importer существует, но отключён без явных readcredentials/APIversion; native webhook referral/context не зависит от таких запросов.

Q15 новых automatic5/10 concessions и распределение200 при нескольких тестовых отправках остаются неутверждёнными. Обычный отказ удерживает все200, дефект/ошибка магазина рассматриваются отдельно. Actual managercase API позволяет записать200retained/150carrier без возврата50 и без изменения paidledger; это **casecontext, не автоматическая финансовая проводка удержания**. Такой новый financeworkflow этим релизом не заявляется. Свежий известный вопрос по товару полностью отвечает при открытом старом servicecase, не снимая promotionhold.

10% — бизнес-цель, а не установленный результат. Verifiednew требует completehistory/identityproof; unknownhistory отдельно, receipt неpaid,200+доплата одногоOrder — одна покупка.

## Последние сквозные исправления перед финальной сборкой

- CX02/CX04: `ig_ad_choices.py` реализует собственное «другу/обидві», а не просто показывает candidates модели. `test_both_reviewed_candidates_materialize_two_distinct_owned_positions`, `test_ordinal_clauses_keep_self_and_friend_on_separate_positions`, `test_two_ad_burst_explicit_own_links_keep_each_sku_and_recipient` проверяют реальные cartrows/outbox. «Справа» без координат уточняется. «Друг каже: зараз беру другу» не становится выбором клиента; явное «але я беру...» имеет отдельную собственную область.
- CX07: `test_both_requested_fit_charts_and_examples_are_delivered_without_new_selection` проверяет SENT двух сеток одногоSKU и примеров кроя. Отсутствие одной/всех сеток имеет signed absence и честный текст; изменившаяся вторая сетка отклоняется; failed modelgraph использует native fallback без нового modelrequest.
- CX10: generation и finalpurpose guard раньше расходились по knowncare после refreshrevision. Теперь `ProductInquiryCapture.recompute` под source/lease/CAS сохраняется в immutable coverage и для известных фактов. Неавторизованное фото не подтверждает уход. «30 градусів/градусов/degrees» и90 распознаются тем же allowlistguard, что30°C.
- CX22/CX24: свежий известный состав95/5 получает полный ответ при существующем открытом servicecase; promotionhold сохраняется. Это не доказательство полного нового paidorder из serviceflow.
- CX29: actual managercaseAPI сохраняет контекст200retained/150carrier и не возвращает50; отдельных автоматических финансовых проводок удержания в этом релизе нет.
- Migration0240 переводит только `storefront_productfitoption` изMyISAM вInnoDB до изменения default/active. Отдельная actualMariaDB регрессия70rows/35products проверила storedgeneratedcolumn/indexes, duplicate-default rejection, откат двух flags и idempotency. `auth_user` не конвертируется.
- Исправлен старый CIcommand inventory: baseline уже174, этот релиз добавляет две команды, reviewed count176;15stage0contracts проходят. Ограничения import/parser/DTF/DB сохранены. JScheckout/state/stats:41/41 прошли; statsfixture дополнен отсутствующим DOMcollection API без изменения productionrenderer.

Дальше обязательны финальный общий50module gate, exactGitSHA, полный native conversiongate через0240 (уже16/16, zero-warning check/reverse/reapply/cleanup), GitHubLinux gate, SSHpublication/migrate и browser QA. До появления этих доказательств этот файл не является актом завершённого деплоя.

### Завершённая native проверка0240 и исправления общей сборки

Финальный фактический MariaDB11.4.12 gate через0240: **16/16,0skip**, reverse/reapply management0238–0240, database check0warnings, generatedcolumns/indexes и endpointHASH проверены, cleanup подтверждён. `/private/tmp/twocomms-conversion-mariadb-20261019/evidence-summary.json` содержит sourcehashes, отдельный70row fitprobe и границу остальных четырёх ранее выполненных teststages. Полные пять gates конкретного committedSHA проверяются вLinuxCI.

Первый широкий root1021test прогон обнаружил6ошибок localfallback JSON: backend-only `ProductInquiryCapture` оказался в serializable proof. Исправлен только projection localfallback: capability не сохраняется какJSON, а реальный typed coverage всё равно заново записывается через source/lease/CAS. Native fit24/24 после исправления проходят. Care/manager/unknown/temperature/service proofs49/49 также проходят. Ошибки старых APIfixtures были отсутствующими явными правами, а не основанием ослабить доступ: fixtures получили только существующие viewPII/operate;66/66 sales+selection проходят. Проверки correction читают actual compressedfield_columns/values и исходную auditprovenance. Policy/core/publication/routing/responseguard54/54 прошли.

Текущий окончательный набор расширен до55модулей; успешный итог будет записан передGit. Пересекающиеся прогоны не складывать в число уникальных тестов.

## Финальный локальный release gate — PASS

19.10.2026, обязательные CPython3.14.6/Django6.1, disposable in-memory SQLite, без реальных Gemini/Meta/Telegram запросов:

- **55 модулей,1103 теста,0 ошибок/падений,3 MariaDB-onlyskip**,224.966s. Лог: `/private/tmp/twocomms-conversion-final-accepted-20261019.log`.
- **41/41 JS checkout/cart/state/stats**,0skip; native DOMrenderer/CAS/consent и CSRF сохранены.
- **61/61 runner/workflowcontracts**, **15/15 Stage0contracts**; parser/import inventory176 проверен.
- Actual nativeMariaDB: финальный16/16 wholeconversiongate через0240,0skip; предыдущие21 teststages других четырёх suites прошли отдельно. Это37 native testcases с разной полнотой post-check, не37 повторно запущенных wholegates.
- `manage.py check --database=default` чистый; `makemigrations --check --dry-run` — Nochanges; runtime verifierCPython3.14.6/Django6.1OK. Scoped152Python/5JS syntax, `git diff --check` чистые.
- До staging index пустой, sourcebaseline main/originmain по-прежнемуa4818a1b. Явный release pathlist172 файлов; прочие untracked пользовательские файлы не включаются.

GitHubCI exactSHA и production приёмка выполняются следующим шагом. Этот локальный PASS не означает отправку ответа реальному клиенту или уже выполненный деплой.
