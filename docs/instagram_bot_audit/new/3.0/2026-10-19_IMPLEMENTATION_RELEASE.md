# Внедрение аудита конверсии и контекста · 19.10.2026

**Код, миграции и знания выложены на production.** Проверенный application head — `306ab17ed1809145275c06eeb53ec4c7ec94c53e`; local/origin/production source сверен. Linux CI, read-only runtime/DB и authenticated browser проверки прошли. Живая контрольная переписка ещё ожидает входящего сообщения с подтверждённого владельцем `@zainllw0w`; её результат не подменяется mocked transport.

Основание: [38 находок](2026-10-18_CONVERSION_CONTEXT_FINDINGS.md) и [ответы владельца](2026-10-18_OWNER_BRIEF_GRILL_ME.md). Эти документы и план90 не изменены. Код, локальные тесты, database publication и видимое клиенту поведение учитываются отдельно.

## Состав релиза

| Commit | Назначение |
|---|---|
| `2f1ac9b3f6f06e96f08a336c820e6d0f2bd185dc` | Application release: контекст, память, товарная консультация, знания, коммерческие правила, метрики, UI и migrations 0237–0240 |
| `e3d110b1bc473d3e04ebae8d2adb591d27055304` | Только MariaDB runner и его тесты: восстановление исходной цепочки родителей после test flush устраняет FK 1452; безопасная диагностика без вывода содержимого дочернего процесса |
| `6423f4eed84e8b23e68d9efa8ab4e3268a6338ab` | Только CI workflow и его contracts: пять параллельных suites в изолированных pinned MariaDB 11.4.12 containers, отдельные artifacts и стабильная aggregate-проверка `lifecycle` |
| `b537e5c86556bfb30a922eb418bf6ff5d414442c` | Только MariaDB runner и contracts: актуальная UGC-уникальность `(reward_id, generation)` из уже существующей миграции 0227 вместо прежнего reward-only ограничения |
| `91d43d0ff63ebba0702b21b1e5c2c0c048d7c0fe` | Только shipping copy комплекта и presentation tests: UK/RU/EN badge, примечания и диалог указывают полную оплату; расчёты цен/скидок не меняются |
| `306ab17ed1809145275c06eeb53ec4c7ec94c53e` | CAS-настройка посадки пропускает черновики: опубликованный 102 исправлен, draft 115 сохранён; статус и обе исходные identity остаются в snapshot |

Миграции 0237–0240 и обе отдельные CAS publications **применены**. Локальный обязательный runtime — CPython 3.14.6/Django 6.1; сервер проверен: CPython 3.14.8/Django 6.1, MariaDB 11.4.13. Patch-разница сохранена отдельно от source parity и не меняет pin AGENTS. Production использовалась только для штатного релиза и read-only проверок, никогда как тестовая база.

## Покрытие 38 находок

Таблица описывает внедрённый source contract; отдельные доказательства публикации, runtime и браузера приведены ниже. Реальная клиентская доставка и рост продаж не выводятся из наличия кода.

| ID | Исходная находка | Реализация в committed source |
|---|---|---|
| REL-01 | Закрыто · Основной checkout приведён к production без потери прежних файлов | Application head 306ab17ed сверен с production; посторонние untracked материалы сохранены и не включены в релиз |
| REL-02 | P2 · Source parity не равна идентичности среды исполнения | Проверены local 3.14.6/server 3.14.8 и общий Django 6.1; source parity не выдаётся за одинаковую среду |
| AD-01 | P1 · Содержимое креатива теряется между webhook и sealed request | Creative и source role доходят до sealed request; рекламное media не даёт selection/payment/UGC authority |
| AD-02 | P1 · Два resolver по-разному считают mapping разрешённым | Intake и prompt используют один resolver опубликованного reviewed mapping |
| AD-03 | P1 · Обычный редактор не задаёт точные товары; несколько товаров не представлены | Редактор single/multi/theme; собственные ordinal/both choices проходят canonical reducer |
| AD-04 | P1 · Несколько рекламных касаний текущего burst сжимаются в одно | Упорядоченные touches и отдельные source/recipient scopes сохраняют несколько позиций |
| AD-05 | P1 · Merge может соединить identity одной рекламы и креатив другой | Merge требует совместимой identity; конфликтующие identity и media не соединяются |
| AD-06 | P2 · Mapping не имеет версии, привязанной к креативу | Mapping/source digest сохранены; новый follow-up проверяет mapping, accepted replay использует исходное решение |
| AD-07 | P1 · Standalone referral существующего диалога отбрасывается до attribution | Standalone receipt дедуплицируется; только следующее настоящее сообщение создаёт customer turn |
| LAT-01 | P1 · Очередь способна превратить обычное продолжение покупки в лишний manager debt | Один preparation successor создаётся после получения worker lease; stale source и повторное истечение блокируются |
| LAT-02 | P1 · Recovery ждёт другой проект даже при локально eligible независимом кандидате | Eligible независимый project B не ждёт cooldown project A; окончательный quota admission сохранён |
| LAT-03 | P1 · Допустимая длительность первой отправки может оставить ответ частичным | Конечный transport tail связан с точным планом; независимые группы и fallback не повторяют SENT/UNKNOWN |
| MEM-01 | P1 · Existing head может застрять на incremental backlog; bootstrap и reader имеют другие пределы | Catch-up использует окна 24 sources/12k, overlap длинного источника и CAS checkpoints |
| MEM-02 | P2 · Reset-era cap блокирует media-derived focus после13 owner rows | Поиск последних валидных photo proofs не блокируется тринадцатью историческими owner rows |
| MEM-03 | P2 · Явная просьба о стиле ответа не имеет столь же устойчивого контракта, как язык | Явные persistent/temporary preferences имеют source proof; delayed slot сохраняется без narrative rewind |
| FIT-01 | P1 · Смена категории может сохранить старую футболочную тему | Текущая информационная тема отделена от корзины; короткий hoodie follow-up не наследует T-shirt facts |
| FIT-02 | P1 · Консультацию по худи нельзя считать готовой из футболочной реализации | Слегка oversize/unisex описаны hoodie facts; опубликованный 102 получил oversize default и inactive classic; draft 115 явно пропущен |
| FIT-03 | P2 · Утверждённый blank/schematic пример кроя не представлен отдельным видом media | Подписанные blank constructor previews имеют base/view/source/version и честную подпись |
| FIT-04 | P1 · В одном запросе фото кроя и сетка могут конкурировать | Две сетки посадок одного SKU и fit media имеют независимые offsets, CAS, fallback и signed absence |
| FIT-05 | P1 · Картинка сетки не заменяет точные числовые замеры в контексте | Prompt получает существующие bounded numeric guides; T-shirt tolerance не переносится на hoodie |
| KB-01 | P1 · Владелец получает ложное обещание, что commit brand.md обновит знания | brand.md обозначен editorial-источником; runtime publication выполняет отдельный CAS publisher |
| KB-02 | P1 · Не хватает scoped owner-maintainable knowledge по основам, темам и принтам | Разделены bases/care/payment/stories/scenarios; добавлены relevance selectors и knowledge lineage |
| KB-03 | P1 · Mandatory sales block подаёт custom/rescue/collaboration каждому non-service ходу | Короткий общий core; custom/rescue/collaboration включаются по текущему собственному запросу |
| KB-04 | P1 · Guard success ещё не означает проверку всех заявлений о ткани и качестве | Scoped material/composition/density/care/quality assertions проверяются guard и semantic coverage |
| KB-05 | P1 · Активная production publication не объявляет required-scenario coverage | Пять reviewed required scenarios опубликованы в head 3 и проверены чистым selector по всем пяти admission cases |
| KB-06 | P1 · FAQ содержит неурегулированные расхождения с owner direction и guarded условиями | Выложены согласованные FAQ/HTML/JSON-LD/locale и шесть source-hashed instruction bodies; дополнен full-payment текст комплекта UK/RU/EN |
| SALE-01 | P1 · Даже approved состав принудительно требует manager review и не имеет полного factual coverage | Известные факты отвечаются без manager review; независимые вопросы и disposition сохранены до отправки |
| SALE-02 | P1 · Retail rescue, программы лояльности и сроки кодов требуют разных оснований | Grant, expiry и ownership отделены от rescue recommendation; новых автоматических Q15 уступок нет |
| SALE-03 | P1 · Разговорное согласие на 200+COD не совпадает с нынешним серверным predicate | Разговорное согласие привязано к актуальному 200+COD offer и проверяемому offer acceptance |
| SALE-04 | P2 · Validator согласованной цены распознаёт число лучше, чем смысл предложения | Проверяются смысл суммы, точная корзина и текущий offer; доставка/скидка не становятся ценой товара |
| SALE-05 | P1 · Отдельный legacy/manual renderer превращает оплату в обещание «уже собираем» | Подтверждение оплаты не обещает начало сборки или два часа; устаревшее тело legacy renderer удалено |
| SALE-06 | P2 · Routing и persisted objections используют разные определения актуального возражения | Routing и persisted objections используют один predicate собственного текущего источника |
| SALE-07 | P1 · Новый full-only shipping rule не выражен в действующей policy | Новая shipping v2: net goods ≥3000 после скидок плюс full payment; при потере условия нужен fresh ACK, frozen v1 сохранён |
| SALE-08 | P1 · Программа100баллов→10% заявлена владельцем, но выдача права остаётся stub | 100 points дают один персональный 10% grant с atomic debit/history и проверками retry/rollback |
| MET-01 | P1 · Существующие показатели не определяют целевую конверсию «обращение → оплаченная покупка» | Inbound inquiry фиксируется до возможной ошибки ответа; 14-day unique-client cohort отделён от paid evidence |
| MET-02 | P1 · Рекламный отчёт использует текущую client attribution для прежних событий | Acquisition и purchase attribution заморожены отдельно; unknown history не объявляется новым клиентом |
| MET-03 | P1 · «Оплаты после скидки» могут включать неоплаченный созданный заказ | Только confirmed payment; 200 и доплата одного Order — одна покупка, refunds учитываются отдельно |
| MET-04 | P2 · Неизмеренный эффект follow-up записан как ноль | Неизмеренный follow-up effect остаётся NULL; целевая конверсия ≥10% не выдаётся за результат |

## Подтверждённые проверки

- **55 модулей, 1103 теста, 0 failures/errors, 3 существующих MariaDB-only skips; 224.966 s.** Disposable in-memory SQLite, CPython 3.14.6/Django 6.1, без реальных Gemini/Meta/Telegram запросов. Лог: `/private/tmp/twocomms-conversion-final-accepted-20261019.log`.
- **41/41 JS tests** для checkout/cart/state/stats; 0 skips. CSRF, consent, DOM renderer и CAS сохранены.
- **6/6 presentation tests** после видимого на PDP расхождения: отрисованный shipping badge/details и JSON диалога на UK/RU/EN явно требуют full payment; послеоплата остаётся платной.
- **9/9 fit-alignment tests** после фактической проверки статусов: опубликованный 102 меняется, draft 115 и его fit flags сохраняются; status race отклоняется до записи; rollback/idempotency сохранены.
- **94/94 runner/workflow + Stage0 contracts/tooling** после финального исправления UGC-проверки. Это mocked/structural contracts, а не повторный запуск native database gates. Exact import/parser inventory — 176; DTF exclusions и запрет DB connections сохранены.
- `manage.py check --database=default` — без замечаний; `makemigrations --check --dry-run` — No changes. Runtime verifier, scoped Python/JS syntax и `git diff --check` прошли.

Пять полных gates MariaDB 11.4.12 прошли в Linux на `b537e5c86`, включая test stage, reverse/reapply, database/schema assertions и cleanup:

| Suite | Фактические тесты | Полнота |
|---|---:|---|
| conversion-context | 16/16, 0 skips | Полный gate через 0240: reverse/reapply 0238–0240, database check с 0 warnings, schema/generated columns/indexes/endpoint hash и cleanup |
| lifecycle | 10/10 | Полный Linux gate; дополнительно локальный whole gate e3d110b1 — 590.7 s |
| checkout-concurrency | 1/1 | Полный Linux gate |
| follow-ugc-concurrency | 9/9 | Полный Linux gate с актуальным generation unique contract |
| lease-concurrency | 1/1 | Полный Linux gate |

Эти 37 native testcases не складываются с пересекающимися SQLite/тематическими прогонами. Linux artifacts: `/private/tmp/twocomms-conversion-ci-b537e5c86-evidence/`; дополнительное native evidence: `/private/tmp/twocomms-conversion-mariadb-20261019/evidence-summary.json`. Отдельная проверка fit migration — 70 rows/35 products, generated columns/indexes, duplicate-default rejection, rollback двух flags и idempotency.

Проверены source/CAS-регрессии: независимый текст после UNKNOWN media без replay; конечный transport tail старого execution resume; delayed preferences; hoodie follow-up при сохранённой T-shirt корзине; отдельные composition/density ответы; API без revision; собственный ordinal/both ad choice и получатели; SENT-вопрос → следующий короткий ответ с исходным source fence; combined chart/fit и signed absence; known care при открытом service case без снятия promotion hold. Media/provider execution в этих application tests использует mocked transport: реальную отправку клиенту это не доказывает.

## Linux CI текущего head

- [MariaDB matrix · 39296383863](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39296383863) — **PASS** всех пяти suites и aggregate `lifecycle` для `b537e5c86`.
- [Django 6.1 · 39298457458](https://github.com/TwoComms-shop/TwoComms_Site/actions/runs/39298457458) — **PASS** для `306ab17ed`; включённые проверки зелёные, optional full-smoke/baseline-comparison steps skipped по условиям workflow.

MariaDB CI проверяет `b537e5c86`. Последующие 91d43d0ff/306ab17ed меняют только публичный bundle copy, scope CLI-настройки посадки и их тесты. Новых schema/бот-generation/денежных изменений в них нет; scoped 6+9 tests и Django CI проверены отдельно. Это не выдаётся за повторный native прогон нового SHA. Последующая документальная фиксация результатов не изменяет код.

Переход к matrix устраняет последовательное суммирование пяти длительных schema/setup/test gates внутри одного 30-minute job. Каждый suite сохраняет собственный 30-minute limit и pinned container; aggregate `lifecycle` запускается всегда и требует success всей матрицы. Failure, cancellation или skipped result не дают зелёную aggregate-проверку. Предыдущие Django runs для `2f1ac9b3` и `e3d110b1` прошли включённые проверки, но optional full-smoke/baseline-comparison steps были skipped.

На `6423f4ee` Django CI и четыре полных MariaDB suites прошли. Follow/UGC test stage прошёл, но post-check обнаружил устаревшее ожидание runner: миграция 0227 уже заменила reward-only unique на `(reward_id, generation)`. Это независимо подтверждено read-only production metadata и disposable native schema, созданной из актуального migration graph: два поколения одной награды допускаются, дубликат одного поколения отклоняется errno 1062. Исправление не меняет application migrations. Регрессия сверяет реальное migration state и отклоняет пять вариантов неверного/отсутствующего индекса. Native proof: `/private/tmp/twocomms-conversion-mariadb-20261019/ugc-generation-native-schema-proof.json`.

## Миграции и публикация

- **0237:** durable ad-context observations/metadata с lease/CAS. Marketing API read capability остаётся выключенной без явных read credentials/API version; native webhook referral не зависит от неё. Health/status/preflight не запускают provider probes.
- **0238:** только `accounts_userpoints`, `accounts_pointshistory`, `storefront_userpromocode` переводятся из MyISAM в InnoDB для транзакционных grants. `auth_user` не конвертируется.
- **0239:** model default canonical core. Существующее DB-тело инструкции меняется отдельной явной core CAS publication.
- **0240:** `storefront_productfitoption` переведена в InnoDB до изменения default/active. Production CAS применён только к опубликованному 102; 115 имеет status=draft и не изменён.
- Совместимость fresh-schema finance 0019/0020 исправлена выбором engine по фактическим родителям; legacy production engine и `auth_user` не меняются этим решением.
- Reviewed знания, пять required scenarios и замены шести source-hashed instruction bodies опубликованы существующим CAS API: head 2 → 3, 20 → 25 инструкций. Active/draft совпадают; commit Markdown сам по себе runtime знания не обновляет.

## Production приёмка · 19.10.2026

Поддерживаемый путь AGENTS: scoped commits → push main → SSH pull main. Два ограниченных maintenance leases сняты; новый daemon запущен штатно. При первом fit dry-run guard обнаружил draft 115 и не изменил ни одной посадки. Бот был возобновлён до доработки CLI, затем применён проверенный published-only контракт. Ошибка не скрыта и не обойдена ручной ORM-записью.

- Core: `2026-10-19.core.v4`, SHA256 `a865b88a9a77290c68fe4daddee7e5f303301aa535ec70b7c6bd741ac6d3371c`.
- Instructions: publication **3**, draft revision **13**, **25** инструкций; active=draft hash `298439ad09d7ab2e9dfb80b963a06aa7f8433621ad8fa21afbdb0ecfc64d7177`. Все пять required scenarios имеют declared coverage; шесть reviewed bodies сверены полностью.
- Четыре назначенные таблицы — InnoDB; `auth_user` сохранена MyISAM. Количество строк до/после: points 78, history 13, personal promos 12, fits 70. Migrations 0237–0240 применены; database check чистый.
- Product 102: classic 67 inactive/not-default, oversize 68 active/default. Draft 115 и обе его fit rows 65/66 сохранены. Повторный dry-run не находит изменений; public cache invalidated/version 2.
- Повторные core/scenario dry-runs идемпотентны: core changed=false, пять scenarios unchanged, compatibility=[]; заново ничего не публикуется.
- Daemon: current code, process/main/workers healthy. Health HTTP: `status=ok`, `bot_state=running`, cron unhealthy=0; dangerous backlog, overdue revisions, reply pending и unresolved notifications — 0.
- 18 прежних pending sources сохранены как `revision_owned_pending` evidence и не являются legacy/runnable queue; legacy pending=0. Четыре прежние terminal analysis failures остаются историей. Они не удалены ради нулевого счётчика.
- Read-only SQL wrapper отклоняет мутации при финальной сверке. Health/preflight/provider probe calls — 0. Runtime proof: `/private/tmp/twocomms-conversion-production-verified-20261019.json`; deployment logs — `/private/tmp/twocomms-conversion-production-deploy-20261019.log` и `...-fit-completion-20261019.log`.

## Authenticated browser приёмка

- Management: «Процес онлайн», «Опубліковано · версія 3», publish disabled без чужого draft; новые пять сценариев видны. Редактор рекламы показывает single/multi/theme, reviewed creative и выбор опубликованных товаров.
- Stats: виден новый блок «Звернення → підтверджена покупка», 14-day cohort, отдельные 7/30 окна и unknown history. Нулевая новая выборка честно названа отсутствием наблюдений; оплаты после предложения не выдаются за доказанный эффект скидки.
- Загружена `/static/management/ig_journey.aaa96d4cd27a.js?v=journey-confirmed-purchases-v17`.
- Реальный PDP 102: oversize selected; classic видна **disabled**, купить её нельзя. Это не утверждение, что DOM-элемент удалён. Доставка явно «після знижок · лише за повної оплати»; уход 30°C.
- Реально загружена `/static/js/main.09f4a52f5a11.js?v=20261019-conversion-v1`, прежняя была `main.57cf0766fb36.js?v=20261012-list-events-v5`. HTTP 200 отдельно не выдаётся за это browser proof.
- Скриншот фактического PDP с selected oversize, disabled classic и full-payment условием: `/private/tmp/twocomms-conversion-production-pdp-20261019.png`.

## Ограничения и остаток приёмки

- Новая hoodie chart не получена. Используются реальные существующие guides; неизвестные числовые мерки и свойства будущей основы не генерируются. Combined chart proof покрывает две посадки одного SKU, не произвольное multi-SKU сравнение. Неопределённая версия принта не получает придуманную историю.
- Q15 новых автоматических уступок 5/10% и распределение 200 грн при нескольких тестовых отправках остаются неутверждёнными. При обычном отказе удерживаются все 200; дефект/ошибка магазина рассматриваются отдельно. Manager-case API фиксирует контекст 200 retained/150 carrier без возврата 50, но **не создаёт автоматическую финансовую проводку удержания**.
- Старый frozen shipping v1 сохраняет ранее согласованный paid/TTN договор. Новая v2 требует total после скидок ≥3000 и full payment; receipt клиента не является paid authority. 200 и последующая доплата одного Order — одна покупка. Unknown history не приравнивается к verified new.
- Конверсия не ниже 10% — бизнес-цель, не гарантия и не измеренный результат релиза. Неизмеренный follow-up effect остаётся NULL.

Владелец подтвердил `@zainllw0w` для живого теста. Его client 5 не заблокирован и не paused/takeover; последнее реальное входящее до проверки — 15.10. В доступном Chrome авторизован только брендовый Instagram; открыт обычный вход в тестовый аккаунт. Предложено отправить три настоящих сообщения с телефона либо войти в аккаунт. Пока свежего входящего нет, live Gemini tone/latency и Meta delivery этого релиза **не объявляются проверенными**. Production webhook/сообщения/платежи не подделывались ради теста.
