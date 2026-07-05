# 03. Измерение, атрибуция и CRM

## Почему это центральная тема

Если UTM не доходят до заказа, лиды не имеют статусов, а «источник продажи» ставится вручную и неполно, можно измерять клики и ощущения, но нельзя уверенно измерять бизнес.

## Источник истины по слоям

| Слой | Источник истины |
|---|---|
| Расход | invoice рекламной платформы + банковская выписка |
| Сайт | аналитика + Pixel/CAPI + серверные логи при необходимости |
| Лид | CRM/админка/Inbox с ID и статусом |
| Заказ | БД заказов |
| Оплата | NovaPay/эквайринг/банк |
| Доставка | статусы ЭН/накладной |
| Себестоимость | закупки + журнал производства |
| Маржа | финансовая модель на фактических данных |

## Единая связка идентификаторов

```text
campaign / ad set / ad
→ lead_id
→ customer_id
→ order_id
→ shipment_id
→ payment and delivery status.
```

## Поля заказа

| Поле | Зачем |
|---|---|
| `order_id` | ключ заказа |
| `customer_id` | повторы/LTV |
| `order_type` | ready_b2c / custom_b2c / b2b_merch / dtf |
| `status` | created / deposit / paid / produced / shipped / delivered / cancelled / returned |
| `cancel_reason` | источник потерь |
| `first_touch_source` | первый известный источник |
| `last_non_direct_touch` | последний измеримый канал |
| `self_reported_source` | слова клиента |
| `utm_*` | источник сайтового трафика |
| `fbclid`, `fbc`, `fbp` | Meta web-атрибуция |
| `ttclid` | TikTok web-атрибуция |
| `lead_id` | связка с перепиской |
| ad IDs | детализация paid |
| `discount` | чистая цена |
| `payment_fee` | комиссия |
| `delivery_subsidy` | стоимость бесплатной доставки |
| `shipment_cost` | фактическая логистика |
| `return_cost` | стоимость возврата |
| `actual_variable_cost` | фактический CM1 |
| `delivered_at` | настоящий завершённый заказ |

## Лид не равен заказу

Статусы лида:

```text
new → contacted → qualified → offer_sent → deposit_requested → deposit_paid → won / lost.
```

Для `lost` обязательна причина: цена, срок, нет размера, доставка, не отвечает, передумал, файл/макет, конкурент, нецелевой запрос, другое.

## Что считать раздельно

### Direct

```text
started chats → qualified chats → quotes → deposits → created orders → paid orders → delivered orders → CM2.
```

### Website

```text
landing page views → ViewContent → AddToCart → InitiateCheckout → Purchase → delivered orders → CM2.
```

### B2B

```text
target contacts → replies → qualified opportunities → quotes → samples → deposits → first order → repeat.
```

## UTM не решают Direct-атрибуцию

UTM нужны для URL. В click-to-Instagram Direct требуются также:

- ключевое слово/автоответ по кампании;
- CRM-тег;
- ad metadata;
- ручная проверка выигранных заказов;
- при возможности возврат подтверждённых событий в Meta через CRM/CAPI.

## Pixel + CAPI QA

Проверить:

- отсутствие browser/server дублей;
- `event_id`;
- корректные `value`, `currency`, `content_ids`;
- Purchase срабатывает на правильном статусе;
- Test Events;
- сохранение UTM/click ID при редиректах;
- отсутствие потери событий на мобильном Safari.

## Еженедельный review

| Блок | Вопрос |
|---|---|
| Деньги | сколько реально списано и поступило |
| Воронка | где теряется клиент |
| Реклама | какой оффер даёт качественный результат |
| Производство | брак, простой, остатки |
| Логистика | доставка, невыкуп, возвраты |
| Клиенты | причины отказов, вопросы, отзывы |
| Решение | что оставить, изменить, остановить |

Не делать выводы по одному дню, но и не ждать «магические семь дней», если есть техническая ошибка или нулевое качество лидов.
