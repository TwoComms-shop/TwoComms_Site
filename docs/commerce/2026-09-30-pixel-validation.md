# Проверка Meta Pixel и CAPI — 30.09.2026

## Доказанная часть

Dataset `823958313630148` открыт в авторизованном Events Manager аккаунта
TwoComms_ADS. Сайт — `twocomms.shop`, интеграции — Meta Pixel и Conversions API.
В периоде 31.08–27.09 интерфейс показывает Purchase active, 4 события,
несколько источников и Event Match Quality 8/10. Эти числа не являются количеством
оплаченных заказов за последние семь дней.

Локально подтверждён контракт дедупликации Purchase:

- `orders/models.py:262` строит ID из номера, timestamp создания и типа события;
  `get_purchase_event_id()` возвращает этот же ID.
- `templates/pages/order_success.html:2137` передаёт этот ID в браузерный Purchase;
  подтверждённые `paid/prepaid/partial` допускаются, staff preview блокируется
  отдельным `trackingDisabled` guard (`:1887`).
- `orders/facebook_conversions_service.py:836` получает ID из того же метода;
  `:867` задаёт имя Purchase, `:869` передаёт server `event_id`.
- `static/js/analytics-loader.js:365`, `:400` помещает его в browser `eventID`;
  `:1243` сохраняет options при выдаче события из буфера.

Полные пути выше находятся под `twocomms/twocomms_django_theme/` для шаблонов
и static и под `twocomms/` для Python-модулей.

`FacebookConversionsService._validate_response` (`:311`) отклоняет пустой ответ,
errors и нулевой `events_received`. После успешной проверки `:907` сохраняются
имя, ID, время отправки, value и UAH. Точный исторический response, количество
принятых событий и fbtrace ID в payload не сохраняются. Поэтому сохранённый
`sent` подтверждает результат внутреннего gate, а не независимо воспроизводимый
исторический ответ Meta.

## Изолированная проверка

Общий runtime: CPython 3.14.6, Django 6.1. Прошли 23 теста:

- 18 `storefront.tests.test_meta_pixel_configuration`: canonical Pixel ID,
  доступные SDK-классы, normalized user data, Purchase timestamp, value,
  currency UAH, `content_type=product`, SKU IDs и shared event ID.
- Три focused `orders.tests.test_post_payment_recovery`: сохранённый CAPI ID
  используется/восстанавливается из ledger; время Purchase фиксируется до
  попытки отправки провайдеру.
- Два focused `storefront.tests.test_analytics_tracking`: browser endpoint
  отвергает server-only Purchase; повторный success не создаёт вторую внутреннюю
  Purchase.

Django tests использовали собственную SQLite in-memory базу; сеть провайдеров
заменена mocks. Тестовая база удалена после запуска. System check без замечаний.

Дополнительный Node VM выполнил настоящий `analytics-loader.js` с подменёнными
DOM/timers/fbq и отключённой сетью. Подтверждено сохранение shared `eventID` до
загрузки Pixel, после flush буфера и в прямой отправке; сохранение числового value,
UAH и SKU; исключение `event_id` из custom event data; отсутствие второго PageView
после inline init; использование `trackCustom` для собственного события.
Это проверка вызовов SDK, а не receipt Meta.

## Браузерная проверка

Создана отдельная вкладка PixelQA; исходные вкладки пользователя не менялись.
Events Manager → Тестирование событий → Сайт → Тестировать события открыл
`https://twocomms.shop/product/225-hoodie/`. На странице выполнено обычное
переключение вкладки «Розмірна сітка», без корзины, checkout, инвойса или покупки.

DOM подтверждает `data-meta-pixel-id=823958313630148`, маршрут `product`,
analytics-loader `c5cecbdafc5f` с `?v=13`, product-detail `2b6f32a6e638` с
`?v=20260930-fit-price-v1`, карточку с value=1995 и SKU `TC-0092-ЧОРНИЙ-XS`.
Console показывает обработку одного buffered Meta event и Pixel initialized.
Эти данные подтверждают загрузку текущего кода и вызов Pixel, но не успешный
сетевой приём. После одного обычного reload при активном listener Test Events
не показывает browser receipt. Проверен фильтр: стандартные и собственные
события включены. Причина отсутствия browser receipt в этом тесте не установлена;
из отсутствия нельзя объявлять все production browser events сломанными.

## CAPI Test Events

Отправлен ровно один `TestEvent`, ID `qa-pixel-20260930-capi-01`, с обязательным
transient `test_event_code` из интерфейса Meta; action_source website, текущий
timestamp, технический user_agent и зарезервированный IP из официального
примерного payload Meta. Реальные customer identifiers, Orders и Purchase
в этом тесте не используются. Настройки Meta/приложения и production БД не
меняются. Токен читается только в памяти настроенного service и не выводится.

Первая SSH-попытка отдельного агента завершилась `Permission denied` (exit255)
до исполнения Django; в этой попытке событие не отправлялось. Оркестратор затем
выполнил private script через своё рабочее каноническое SSH-соединение.
Ответ SDK: `events_received=1`, `messages_count=0`, `fbtrace_present=true`.
Санитизированный результат: `/tmp/twc-pricing-20260930/pixel-test-result.log`.

Независимо проверен UI receipt: Test Events показывает **TestEvent → Обработка
завершена → Серверные → Ручная настройка**, ID `qa-pixel-20260930-capi-01`,
«Сегодня в 02:50:37» в отображаемом часовом поясе Meta. В деталях URL
`https://twocomms.shop/`, action source `website`, user data keys IP и user_agent.
Screenshot: `/tmp/twc-pricing-20260930/pixel-test-receipt.jpg`.
Это реальное подтверждение приёма технического серверного события Meta,
а не только локальный sent marker.

## Дополнительная проверка отсутствующего browser receipt

Повторное чтение Test Events после окончания первоначального теста показывает
тот же успешно обработанный серверный QA-ID и не показывает PageView/ViewContent.
Новых событий при этой дополнительной проверке не отправляли.

Проверенные возможные причины:

- **Staff suppression:** в Meta context processor, inline init/PageView,
  loader и PDP ViewContent нет staff gate. Исключение staff из внутренней
  Django-аналитики не блокирует эти браузерные Pixel-вызовы. Первоначальный DOM
  уже подтвердил правильный ID. Staff-preview guard относится к order success,
  а не просмотренной карточке товара.
- **Consent:** loader применяет explicit-consent gate только к маршруту
  `ig_checkout_proposal`; просмотренный маршрут был `product`. Этот gate
  не объясняет отсутствие receipt данной карточки.
- **Meta traffic permissions:** авторизованный Settings показывает разрешённый
  домен `twocomms.shop и поддомены`, first-party cookies включены, автоматическое
  сопоставление включено. Домен страницы разрешён. Также подтверждён доступ
  dataset для ad account TwoComms_ADS `1158938424247225`.
- **Event blocking:** чтение перечня закрыто экраном «Я подтверждаю» с принятием
  условий Meta. Экран закрыт без подтверждения; статус конкретного browser
  события в этом перечне не прочитан.
- **Browser privacy/ad blocker:** просмотр `chrome://settings/cookies` отклонён
  URL-политикой browser tool. Обходы и изменения настроек не применялись.
  Первоначальные ephemeral QA tabs закрылись по завершении предыдущего этапа;
  транспортные запросы этого визита не сохранились в доступном инструменте.
  Доказательства конкретного `ERR_BLOCKED_BY_CLIENT`, ограничения cookies,
  HTTP-статуса или network acknowledgement отсутствуют.

Обнаружена конкретная граница текущей диагностической надписи. Inline snippet
создаёт функцию-очередь `fbq` ещё до загрузки `fbevents.js`. Loader (`:1159`)
не создаёт второй SDK script, если эта функция уже есть, ставит `_fbqLoaded=true`
(`:1231`), очищает собственный буфер (`:1253`) и печатает «Meta Pixel initialized»,
проверив лишь `typeof fbq === 'function'` (`:1529`). Поэтому все три сигнала
могут наблюдаться, когда событие осталось в native stub queue и транспорт SDK
не запущен. В таком inline-пути onerror нового loader script также не
устанавливается, потому что действует ранний `if (f.fbq) return`.

Этот случай воспроизведён выполнением настоящего loader в offline VM: у stub
отсутствует `callMethod`, `_fbqLoaded=true`, bridge buffer пуст, ViewContent
остался в stub queue, сообщение initialized напечатано. Это доказывает
недостаточность диагностических сигналов; оно **не доказывает**, что именно
блокировка SDK произошла в реальном Chrome.

PageView и ViewContent вызываются стандартным `fbq('track', ...)`, не
`trackSingle`; используется один canonical ID. Нет найденного требования
менять этот способ отправки для устранения receipt gap. Первопричина
отсутствующего browser receipt остаётся не установлена. Следующее достаточное
доказательство — capture реального browser SDK/network route с именем события,
правильным dataset ID и HTTP/blocked status, затем сопоставление с Test Events;
переключать privacy/security настройки ради такого capture не требуется.

## Граница итогового заключения

Код, локальная сериализация и действующие интеграции подтверждены. Purchase
активна, EMQ 8/10, но 100% атрибуция или 100% дедупликация не доказаны. Существующая
диагностика Meta сообщает, что дедупликация всё ещё анализируется (см. основной
advertising-audit). Receipt технического TestEvent подтвердил серверный маршрут;
он не подтверждает полный Purchase flow, catalog matching конкретной покупки
или атрибуцию рекламному клику.

Синтетическая Purchase не отправлялась. Для закрытия оставшегося business
контракта нужна новая реальная подтверждённая покупка с доступным browser/server
ID и параметрами в Meta, либо полностью изолированный проверяемый тестовый
контур. Старые оплаченные заказы не переоткрывались для повторной конверсии.

## Официальные источники

Документация получена через Context7:

- [Meta Pixel reference](https://developers.facebook.com/documentation/meta-pixel/reference.md):
  browser `eventID` для взаимодействия с CAPI.
- [Facebook SDK Event](https://github.com/facebook/facebook-python-business-sdk/blob/main/_autodocs/api-reference/ConversionsAPI-Event.md):
  совместные event_name/event_id для browser/server deduplication.
- [Facebook SDK EventRequest](https://github.com/facebook/facebook-python-business-sdk/blob/main/_autodocs/api-reference/ConversionsAPI-EventRequest.md):
  `test_event_code` для проверки в Events Manager.
- [Meta catalog Pixel events](https://developers.facebook.com/documentation/meta-pixel/implementation/pixel-for-collaborative-ads.md):
  contents/content_ids, product, value/currency.
