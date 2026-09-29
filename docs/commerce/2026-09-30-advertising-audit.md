# Технический аудит рекламной аналитики — 30.09.2026

## Граница доказательств

Этот раздел проверяет связку storefront → Meta Pixel/CAPI → товарный каталог,
UTM-атрибуцию и внутренние метрики. Основание: текущий локальный код, чтение
существующих тестов, новые изолированные проверки и документация через Context7.
Рекламные кампании и настройки аккаунтов не изменялись. Синтетические события
конверсии в живой dataset не отправлялись. Локальная проверка не подтверждает
развёртывание, фактический приём событий Meta или состояние Commerce Manager.

## Исправленные дефекты

### 1. Первый рекламный визит терял связь для фильтров админки

`twocomms/twocomms/settings.py:476` запускает identity → UTM → page analytics.
`storefront/utm_middleware.py:278` пытается связать UTM со SiteSession раньше,
чем SiteSession создана. При первом рекламном заходе FK оставался NULL; обычные
следующие страницы используют сохранённые UTM и не повторяют связывание.
При этом фильтры source/campaign/device в
`storefront/services/admin_analytics.py::_resolve_scope` требуют `utm_data`.
Визит был записан, но исчезал из такой отфильтрованной выборки.

Исправление: `storefront/tracking.py::SimpleAnalyticsMiddleware` заполняет
отсутствующий FK после создания SiteSession в той же транзакции. Поиск строго
по текущему session_key; существующие связи не переназначаются. Обычный визит
также восстанавливает ранее пропущенную связь. Массовый production backfill
не выполнялся.

Проверки: первый tagged landing виден в source/campaign/device фильтрах;
старый NULL FK восстанавливается; существующий FK сохраняется.

### 2. Новый Meta-клик мог сохранить старый `_fbc`

`twocomms_django_theme/static/js/analytics-loader.js::ensureFbcCookie` прежде
возвращал существующий `_fbc` до чтения нового `fbclid`. Собственный fallback
мог передать прошлый click ID, когда нативный Pixel ещё не обновил cookie.

Исправление: новый click ID обновляет cookie; тот же click ID сохраняет
первоначальную временную отметку, включая cookie с дополнительным Meta appendix.
Отсутствующий `fbclid` не создаёт `_fbc` и сохраняет имеющийся идентификатор.
Legacy query parser декодирует значение. Это соответствует поведению
[Meta Parameter Builder](https://github.com/facebook/capi-param-builder/blob/main/php/capi-param-builder/src/ParamBuilder.php), полученному через Context7.

Проверки исполняют настоящий loader в локальном Node VM с отключёнными
таймерами и пустыми pixel IDs: новый/тот же/первый клик, отсутствие клика,
Meta appendix, обычная навигация и legacy parsing. Сеть не используется.

### 3. Генерируемый fallback `_fbp` имел буквенную random-часть

При отсутствии native cookie loader применял base36 random string.
[Официальный Meta Parameter Builder](https://github.com/facebook/capi-param-builder/blob/main/nodejs/capi-param-builder/src/ParamBuilder.js)
генерирует целое число. Новый fallback теперь использует числовой random
компонент; существующие cookie сохраняются. Отдельная offline runtime-проверка
подтверждает формат нового значения и сохранение старого ID.

Это исправление формы параметра; оно не доказывает изменение Event Match
Quality живого аккаунта.

### 4. Revenue/AOV админки считали сумму до скидки заказа

`Order.final_total` = `max(total_sum - discount_amount, 0)`
(`orders/models.py:347`), CAPI берёт эту итоговую стоимость
(`orders/facebook_conversions_service.py:538`). Основные виджеты админки
суммировали `total_sum`. При заказе 1200 грн со скидкой 200 грн админка показывала
1200 грн, а Meta Purchase — 1000 грн.

В `storefront/services/admin_analytics.py::_orders_queryset` добавлено общее
выражение итоговой стоимости. Его используют overview revenue/AOV, сравнения,
временные ряды, sales summary/daily series и source LTV. Отрицательная итоговая
сумма ограничивается нулём, как в модели. Популяция `payment_status=paid`
сохранена. Продуктовые суммы `OrderItem.line_total` остаются валовыми суммами
позиций: распределение общей скидки по позициям требует отдельного контракта.

Проверки: два оплаченных заказа дают 1700 грн вместо 2000 грн и AOV 850 грн;
prepaid/unpaid исключаются из этой paid-выборки; проверены clamp и предыдущий
период, daily/time series и source LTV.

## Уже реализованные контракты Meta

| Контракт | Подтверждение в коде | Граница |
|---|---|---|
| Единый dataset ID для browser/CAPI | `META_PIXEL_ID`, context processor, base HTML, `FacebookConversionsService` | Фактический production ID и подключение аккаунта требуют чтения настроек |
| PageView после открытия страницы | `templates/base.html:301`, inline init/PageView | Блокировщики и приём Meta не подтверждены |
| ViewContent на PDP | `static/js/product-detail.js:3001` | Точное совпадение ID с импортированным catalog item требует Commerce Manager |
| AddToCart после успешного ответа сервера | `static/js/main.js:1757` | Пользовательский клик сам по себе не считается успешным ATC |
| InitiateCheckout с ID корзины | `static/js/main.js:228`, общий guard | Для сообщений это другой путь и цель |
| Purchase browser/server используют одно имя и ID | `Order.get_purchase_event_id`, `order_success.html:2137`, CAPI `send_purchase_event` | Это условие dedup, но не доказательство его приёма Meta |
| Каталог/события имеют SKU IDs | `utils/analytics_helpers.py::get_offer_id`, `marketplace_feeds.py:1470`, CAPI `content_type=product` | Отдельные ручные позиции `manual-*` не являются catalog SKU |
| Purchase только подтверждённого состояния оплаты | post-payment dispatcher в `views/utils.py`, server-only UserAction writers | Гарантии реальной оплаты зависят от доверенного writer и payment verification |
| Предоплата учитывается один раз | `paid/prepaid/partial`, Purchase ID по заказу, delivery dedup | `value` заказа не равен полученной предоплате |

Формат SKU: `TC-{product_id:04d}-{COLOR}-{SIZE}`. Отдельный group ID:
`TC-GROUP-{product_id}`. Использование `content_type=product` с SKU корректно;
набор из нескольких SKU не превращается в `product_group`.

[Meta Pixel reference](https://developers.facebook.com/documentation/meta-pixel/reference.md), полученный через Context7,
требует value/currency для Purchase и contents/content_ids для catalog events;
для совместной работы с CAPI рекомендует `eventID`. Эти параметры присутствуют.
[Meta implementation example](https://developers.facebook.com/documentation/meta-pixel/implementation/pixel-for-collaborative-ads.md)
подтверждает форму contents/id/quantity и `content_type=product`.

## Метрики, которые нельзя смешивать

1. **Meta Purchase value** — полная итоговая стоимость подтверждённого заказа,
   включая заказ с успешной предоплатой. Например, предоплата 200 грн за заказ
   2600 грн даёт одну Purchase с value=2600 грн. Это действующий бизнес-контракт,
   а не ошибка, которую следует заменять отправкой value=200.
2. **CAPI paid_value** — фактически подтверждённая сумма из payment payload;
   отдельное custom property. Стандартный Meta ROAS не становится cash ROAS
   только из-за наличия этого свойства.
3. **Admin paid revenue/AOV** — итоговая стоимость заказов со статусом paid,
   за период создания заказа. Успешный prepaid не входит в paid revenue;
   во внутренней воронке его purchase уже присутствует.
4. **Полученные деньги / возвраты / прибыль** — отдельный денежный учёт.
   Основной revenue виджет не вычитает автоматически все возвраты,
   комиссию, себестоимость или рекламный расход.
5. **Время визита** — `last_seen - first_seen` по сохранённой Django-сессии;
   dashboard ограничивает вклад одним интервалом 30 минут. Это не активное
   время: нет heartbeat/visibility учёта и настоящего разбиения по inactivity.
   Последняя страница может читаться без нового события, а визиты в разные дни
   могут иметь один session_key. Детальный список показывает сырой интервал.
6. **Посетители** — новый dashboard считает `visitor_id` с fallback session_key;
   старый UTM-report в `utm_analytics.py:101` считает разные IP. Это разные
   методы, NAT/VPN не позволяют трактовать IP как число людей.
7. **UTM** — один UTMSession на Django session_key; его первые заполненные
   source/campaign сохраняются. Это не тождественно Meta attribution window
   или last-click. Обновлённый `_fbc` не переписывает исторический first-touch.

При последующем функциональном редизайне аналитики нужны явно подписанные
популяции, даты конверсии, подтверждённые суммы оплаты/возврата и active-time
контракт. Сейчас нельзя выводить эффективность рекламы из одного числа
«доход» или ожидать полного равенства Meta и внутренних отчётов.

## Что ещё нужно увидеть в Meta перед выводом о готовности

- Dataset/Pixel ID совпадает с ID сайта (подтверждено ниже); отдельно проверить
  подключение этого dataset к нужному ad account.
- Commerce Manager: правильный каталог и data source, успешное последнее
  обновление, одобренные товары, реальные URL/цены/availability, нужный product
  set; каталог связан с этим dataset.
- Для имеющихся ViewContent/AddToCart/Purchase: реальные content_ids/contents
  совпадают с retailer IDs товаров выбранного каталога; отсутствуют массовые
  product-ID mismatch diagnostics.
- Events Manager для существующей подтверждённой покупки: browser/server
  event_name и eventID/event_id, value/currency, обе delivery sources,
  действительное dedup evidence. «Код содержит eventID» и `events_received=1`
  сами по себе не доказывают корректную атрибуцию или catalog matching.
- Event Match Quality и конкретные diagnostics, наличие fbp/fbc, hashed matching
  fields и корректного event_source_url. Не выгружать customer payload в отчёт.
- Для последующего продвижения в сообщения: связанный Instagram/Page аккаунт,
  доступные messaging destinations и отдельная цель/optimization в интерфейсе
  Meta. Website Purchase pipeline не доказывает messaging measurement.

`management/services/ig_meta_events.py` содержит выключаемый feedback hook;
IG-only stages без заказа/достаточных match data не отправляются. Наличие этого
модуля не подтверждает включённую нативную оптимизацию Meta на сообщения.

Старые Meta docs endpoints при прямом чтении вернули login/429; содержимое
официальных Pixel reference и Parameter Builder получено через Context7.
Никакой вывод о состоянии аккаунта из этих сетевых ошибок не делается.

## Локальная валидация

Runtime: общий `.venv`, CPython 3.14.6, Django 6.1; SQLite in-memory через
`test_settings`. Новые runtime-проверки cookies не обращаются в сеть.
Итоговый целевой запуск: **40 тестов прошли**, system check без замечаний.
Запущены `FirstLandingAttributionTests`, два browser-runtime теста cookies,
полные `test_utm_normalization` и `test_admin_analytics_api`; эти проверки
охватывают новые изменения и смежные UTM/admin query контракты.

При более широком запуске до финальной проверки наблюдались два baseline
сбоя вне этих изменений: ignored backup `pages/order_success_old.html`
присутствует локально, хотя тест ожидает отсутствие; custom_print_safe_exit
сохраняет lazy translation `__proxy__` в JSON snapshot (`views/static_pages.py`),
что даёт TypeError. Они не маскируются как успешные тесты и требуют отдельного
решения владельца соответствующего изменения.

## Рекомендуемый production read-only срез

Запускать только чтение через установленный production runtime, без отправки
CAPI, синтетических заказов или backfill. В отчёт выводить только агрегаты.

```python
from datetime import timedelta
from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Sum
from django.utils import timezone
from orders.models import Order
from storefront.models import SiteSession, UTMSession

since = timezone.now() - timedelta(days=7)
utm = UTMSession.objects.filter(first_seen__gte=since)
same_key_session = SiteSession.objects.filter(session_key=OuterRef('session_key'))
print({
    'meta_pixel_configured': bool(settings.META_PIXEL_ID),
    'meta_token_configured': bool(settings.FACEBOOK_CONVERSIONS_API_TOKEN),
    'utm_rows': utm.count(),
    'unlinked_utm_rows': utm.filter(session__isnull=True).count(),
    'unlinked_with_existing_site_session': utm.filter(session__isnull=True)
        .annotate(has_session=Exists(same_key_session)).filter(has_session=True).count(),
})
print(list(utm.values('utm_source', 'utm_medium').annotate(
    total=Count('pk')).order_by('-total')[:15]))
orders = Order.objects.filter(created__gte=since)
print(list(orders.values('payment_status').annotate(
    orders=Count('pk'), gross=Sum('total_sum'), discount=Sum('discount_amount'))))
for state in ('sent', 'pending', 'failed', 'disabled', 'unknown', 'ambiguous'):
    print('meta_purchase', state, orders.filter(
        payment_payload__post_payment_channels__meta_purchase__state=state).count())
```

Этот срез показывает runtime/data health и исторические NULL связи. Он не
заменяет authenticated browser QA и Events Manager acceptance. После release
нужно проверить фактически загруженную версию `analytics-loader.js` и
работоспособность source/campaign/device фильтров. Cache-busting управляется
`base.html`; одного изменения JS на диске недостаточно.

## Production baseline и историческое восстановление

Оркестратор подтвердил агрегаты read-only через production runtime за последние
7 дней до применения repair-команды:

| Агрегат | Значение |
|---|---:|
| UTMSession | 257 |
| UTM с NULL SiteSession FK | 218 |
| NULL FK с существующей SiteSession по тому же session_key | 217 |
| Google UTM | 172 |
| ChatGPT UTM | 60 |
| Facebook paid UTM | 14 |
| Instagram UTM | 10 |
| Gemini UTM | 1 |
| Оплаченные полностью заказы | 2; gross 2740 грн |
| Prepaid заказы | 2; gross 2450 грн |
| Meta purchase channels в sent | 3 |
| Meta purchase channels в failed/pending | 0 |

Pixel и CAPI token присутствуют (проверены только boolean configured flags).
Четвёртый заказ не объявляется доставленным в Meta только из этих агрегатов.
Значение gross в таблице — сумма `total_sum` до скидок, а не cash/revenue.
Это подтверждает масштаб пропущенного join; изменения в коде ещё не доказывают
восстановление всех исторических данных или приём событий Events Manager.

Для исторических строк добавлена
`storefront/management/commands/reconcile_utm_session_links.py`:

- Без параметров — dry-run последних 7 дней, только агрегированные счётчики.
- `--days` меняет lookback по `UTMSession.first_seen`; значение должно быть
  положительным. `--batch-size` ограничивает память одной страницы, default
  500, диапазон 1–2000. Начальный maximum PK фиксирует конечный объём запуска.
- `--apply` заполняет только существующий NULL FK по точному совпадению
  непустого session_key, если SiteSession ещё не занята другой UTM-строкой.
- Перед каждым update блокируются и повторно проверяются обе существующие
  строки. Уже заполненные, исчезнувшие или изменившиеся связи пропускаются.
- Источник, campaign/click IDs, conversion flags, даты, Order/UserAction и
  данные посетителя не меняются; новые сессии и рекламные события не создаются.

Порядок после доставки кода, в установленном production Python runtime:

```text
manage.py reconcile_utm_session_links --days 7
manage.py reconcile_utm_session_links --days 7 --apply
manage.py reconcile_utm_session_links --days 7
```

Сначала сверить recoverable/missing/occupied счётчики с baseline; после apply
повторный dry-run должен показывать recoverable=0 при неизменившемся трафике.
Одна исходная NULL-строка без SiteSession не является допустимой целью repair.
**В этом отчёте применение команды на production пока не подтверждено.**

## Events Manager и серверные отправки: уточнённое live evidence

В authenticated Events Manager оркестратор подтвердил Pixel
`823958313630148`, совпадающий с production, и интеграции Pixel + Conversions
API. После выбора «Все доступные» и окончания перезагрузки таблица показывает
Purchase: **4**, active, несколько интеграций, EMQ **8.0/10**, последнее событие
около суток назад. Также видны AddToCart **12**, InitiateCheckout **10**,
AddPaymentInfo **10** (для последнего EMQ **8.0/10**). Общий индикатор показывал
3611 событий за 28 дней. Эти UI числа относятся к выбранному UI периоду,
а не к нашему серверному срезу трёх заказов.

Первоначальное отсутствие Purchase в ещё не обновлённой таблице с периодом
2–29 сентября **не подтверждает дефект доставки**: после изменения фильтра
и загрузки событие видно. Из наблюдения нельзя отделить влияние фильтра от
влияния незавершённой загрузки; неверный вывод о missing Purchase снят.

Узкая read-only проверка production выполнена 29 сентября в 23:24 UTC
(30 сентября по Europe/Kiev). `FACEBOOK_CAPI_TEST_EVENT_CODE` не задан ни в
Django settings, ни в environment; значения проверялись только как boolean.
Новые события или повторные Purchase не отправлялись.

| Внутренний Order ID | CAPI sent_at, UTC | Purchase value, UAH | Статус оплаты |
|---|---|---:|---|
| 327 | 2026-09-24 00:19:17 | 1090 | paid |
| 328 | 2026-09-28 08:13:07 | 1650 | prepaid |
| 329 | 2026-09-28 08:18:08 | 800 | prepaid |

У всех трёх сохранены `facebook_events.purchase_sent=true`, Purchase event_id,
`fb_conversions_api.sent_at/value/currency`, канал `meta_purchase.state=sent`.
`already_sent=false`: эти channel записи не отмечены как перенос старого флага.
Времена событий находятся внутри 2–29 сентября, поэтому граница периода не
объясняет первоначальное отсутствие этих Purchase.

Текущий код `orders/facebook_conversions_service.py::_validate_response`
возвращает успех при непустом ответе без `errors` и truthy `events_received`,
после чего сохраняет сокращённый `fb_conversions_api` envelope; dispatcher
ставит purchase_sent и channel sent. Сам ответ Meta, точный `events_received`,
`events_dropped`, сообщения и trace ID в заказе **не сохраняются**. Поэтому
историческое `events_received=1` для каждой из этих трёх отправок из БД
восстановить нельзя. `events_dropped` сейчас только логируется как warning,
а sent не доказывает dedup, итоговую рекламную атрибуцию или catalog matching.

Live UI подтверждает наличие активного Purchase и качество matching в этом
срезе. Остались отдельные account-level проверки из списка выше: связь с ad
account/каталогом, retailer-ID matching, dedup evidence для конкретной покупки,
предметные diagnostics и messaging destination. Число 4 в UI и 3 в серверном
срезе имеет разные периоды/популяции; причина различия не утверждается без
детализации событий.

## Дополнительная правка цены общего marketplace feed

Commerce Manager live evidence оркестратора: основной источник Meta —
`https://twocomms.shop/media/google-merchant-v3.xml`, 424 SKU, upload без errors;
два других источника используют `https://twocomms.shop/media/instagram-feed.xml`
(90 SKU, 65 SKU получают атрибуты из нескольких источников). Product matching
показывает 97.5%; это не доказывает правильность цены. Поэтому поправлен общий
`storefront/services/marketplace_feeds.py`, используемый обоими serializers.

До изменения генератор брал Product.price либо variant.price_override,
затем самостоятельно применял скидку. Он пропускал выбранную на PDP посадку,
надбавки материала/опций и повторно снижал уже конечный variant override.
Теперь существующий color × size offer получает цену из того же
`product_option_context` / `variant_public_context`, что PDP: доступная
default посадка, остальные default опции, ближайшая доступная комбинация при
запрещённой полной комбинации, материал и точный combination override.
Product discount применяется до надбавок; variant.price_override повторно
не дисконтируется. Для legacy товара без цветовых строк сохранён PDP
`product.final_price`, включая его целочисленное округление.

ID/SKU/group ID и color/size landing URL не мигрируются. Недоступный размер
для выбранной конфигурации определяется существующим `variant_allows_purchase`:
offer сохраняет ID, но получает out of stock / quantity 0. Профиль может скрыть
доступный offer, однако force_in_stock / product quantity не отменяют запрет
размера/комбинации. Made-to-order quantity floor для доступных offer сохранён.
`build_profile_offers` не содержит price overrides; поэтому общие SKU Google
и Instagram получают одинаковые price/sale_price. Profile filters, картинки,
тексты и forced availability по-прежнему могут различаться.

Валидация: **31 тест пройден** — 8 новых configuration tests, 15 существующих
marketplace tests, 8 существующих profile runtime/serializer tests. Новые
тесты сравнивают цену с реальным локальным PDP response context, проверяют
oversize-only/default, материал + variant override, скидочное округление,
недоступную exact combination и disabled size независимо от profile stock
override. Google/Instagram price/sale_price/id/link сверены для одного набора
offer. `git diff --check` чистый. Production regeneration в этой записи не
объявляется выполненным; после release нужно перестроить оба файла.

Ограничение остаётся явным: отдельного SKU для каждой fit/lining комбинации
сейчас нет. Один color × size ID описывает default доступную конфигурацию
landing, а не весь диапазон цен других пользовательских выборов. Расширение
этого контракта потребует согласованной миграции catalog IDs и всех events.
В Events Manager dedup UI всё ещё показывает «По-прежнему идёт анализ данных»:
степень дедупликации на этом этапе unknown, её нельзя объявлять подтверждённой.

## Живая проверка Meta и каталога, 30.09.2026

В авторизованном браузере открыт TwoComms_ADS. Действующий dataset `pixel` (`823958313630148`) совпадает с production настройкой сайта; это проверено серверным boolean без вывода токенов. Events Manager показывает интеграции Meta Pixel + Conversions API, сайт twocomms.shop и Purchase active с EMQ8/10. Первоначальный неполностью обновившийся список показывал только PageView/ViewContent; после обновления периода Purchase появилась — вывод о её отсутствии неверен.

Для видимого периода31.08–27.09: Purchase4, AddToCart12, InitiateCheckout10; это не число оплаченных заказов магазина за последние7дней. В деталях Purchase присутствуют серверные и браузерные события. Раздел дедупликации сообщает, что Meta ещё анализирует данные: фактическую степень дедупликации интерфейс пока не подтверждает.

В «Действиях» Meta рекомендует улучшить покрытие fbc, в частности для AddPaymentInfo. Наше исправление обработки нового fbclid устраняет конкретную потерю нового click ID, но не гарантирует рост EMQ и не должно создавать fbc органическому трафику. Также обнаружен посторонний домен fb.com: его не добавляли в разрешённые как собственный.

Commerce Manager: каталог `Каталог_товары` (`638052352157351`) связан с правильным pixel. Совпадение за показанный период31.08–27.09 — **97,5%**, ViewContent97,3%, AddToCart100%, Purchase100%. Два «критических» предупреждения относятся к отсутствию ATC/Purchase за7дней, а не несовпадению ID. Они противоречат более свежей странице Events Manager («1 день назад») и реальным server sent28.09; считать их актуальной поломкой или подтверждённо исправленными нельзя без обновления диагностики Meta. Синтетические покупки для очистки предупреждений не отправлялись. Неактивные дополнительные наборы данных не отключались.

Каталог загружает основной файл `https://twocomms.shop/media/google-merchant-v3.xml` (source1897517731198327,424SKU, последняя проверенная загрузка30.09 02:25,0ошибок). Источник blackfriday1343025254185920 и supplemental1791304291589372 используют `https://twocomms.shop/media/instagram-feed.xml` (90SKU). У65товаров атрибуты поступают из нескольких источников. Поэтому обновить только Instagram XML недостаточно: оба файла должны быть согласованы. Расписания/подключения не изменялись. Текущие названия старых источников не доказывают действующую скидку Black Friday.
