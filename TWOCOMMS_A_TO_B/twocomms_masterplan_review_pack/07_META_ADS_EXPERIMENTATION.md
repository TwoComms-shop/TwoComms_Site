# 07. Meta Ads: структура тестов, решения и guardrails

## Базовая задача

На старте цель — не «найти лучший таргет». Цель — доказать, может ли конкретный оффер покупать доставленные заказы с положительным CM2 при текущей мощности и cash buffer.

## Что подтверждено Meta

Meta описывает learning phase как период обучения delivery system; ориентир около 50 optimization events за 7 дней относится к стабильности обучения. Learning Limited не равно убыточности.

Источник: https://www.facebook.com/business/help/112167992830700

Meta позволяет вести рекламу в Instagram Direct; доступные objective/performance goals зависят от текущей конфигурации аккаунта.

Источники:
- https://www.facebook.com/business/help/198088077975174
- https://www.facebook.com/business/help/1214599109289826
- https://www.facebook.com/business/help/416997652473726

## Что из этого не следует

- ждать ровно 7 дней при технической ошибке;
- держать убыточную кампанию «ради обучения»;
- считать Learning Limited провалом;
- дробить $10–15/день на 3–5 ad set;
- называть 2 ролика на малом бюджете строгим A/B-тестом;
- оптимизировать рекламу в Direct на AddToCart: это website event другого маршрута.

## Три независимых маршрута

### Direct

```text
ad → Instagram Direct → started chat → qualified chat → order/deposit → delivered order → CM2.
```

### Website

```text
ad → landing page → ViewContent → AddToCart → checkout → Purchase → delivered order → CM2.
```

### B2B

```text
ad/outreach → qualification → quote → sample → deposit → first order → repeat → contribution/LTV.
```

Не смешивать их в одном ad set.

## Phase 0: технический QA

1. Одна SKU/один оффер.
2. Один креатив.
3. Один CTA.
4. Один маршрут.
5. Проверка Direct, автоответа, CRM, статусов, campaign/ad IDs.
6. Проверка, что доставка/выкуп попадают в отчёт.
7. Проверка, что менеджер способен отвечать корректно.

Это не оценка успеха рекламы. Это проверка трубы.

## Первый исследовательский запуск

При бюджете около $10–15/день не дробить бюджет.

| Элемент | Принцип |
|---|---|
| Кампания | click-to-message / Instagram Direct в актуальной конфигурации |
| Objective | выбрать актуальную цель, которая реально ведёт к Direct |
| Performance goal | начать с conversations; потом оценить leads/purchases through messaging |
| Ad set | один |
| Аудитория | broad или один ясный сегмент |
| География | только доступные зоны доставки/сервиса |
| Оффер | только ready ИЛИ pair/gift ИЛИ custom |
| CTA | одно ключевое слово и шаг |
| Креатив | один главный угол; второй — screening |
| KPI | качество лидов, CAC delivered, CM2, delivery rate, owner time |

## Creative screening

Два креатива в одном ad set на микро-бюджете могут получать неравный расход. Это screening: убрать очевидно непонятный ролик, собрать вопросы клиентов, увидеть разрыв ожиданий. Это не доказательство «вечного победителя».

## Иерархия guardrails

### Технический

После первых показов: открутка, Direct, CRM, обрезка, CTA, policy, корректность данных. Здесь чинят, а не оценивают маркетинг.

### Креативный

Смотреть не только CTR, а понимание товара, качество вопросов, соответствие креатива ожиданиям. Низкий CTR допустим, если CM2 на показы выше.

### Лидовый

```text
Qualified chat rate = qualified chats / started chats.
```

После первых 8–10 чатов нулевое качество — повод разбирать оффер/креатив/первый ответ. Это диагностический сигнал, не строгий статистический вердикт.

### Продажный

3–4 чата без продажи при ожидаемом close rate 10% не доказывают провал. 30 квалифицированных лидов без продаж при ожидаемом close rate 10% — уже сильный негативный сигнал.

### Финансовый

```text
Max CAC = CM1 − required CM2.
Max started-chat cost = Max CAC × qualified-chat rate × qualified-chat close rate.
```

Это и есть логика порога, а не универсальный «чат дороже $X = стоп».

## Переход по воронке

```text
Phase 0: QA
Phase 1: conversations
Phase 2: qualified messaging leads
Phase 3: purchases through messaging при корректной связке
Phase 4: отдельный website destination test
Phase 5: website Purchase optimization при достаточных подтверждённых событиях.
```

## Масштабирование

Перед ростом бюджета нужны одновременно: положительный CM2, допустимый CAC delivered, понятная delivery/return rate, cash buffer, остатки, safe capacity, SLA, скорость ответа и корректная атрибуция.
