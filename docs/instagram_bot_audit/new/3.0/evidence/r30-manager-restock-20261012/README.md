# P2/P4 · проверенное наличие, сроки и согласованное пополнение

✅ CODE/TEST/UI и canonical production deploy приняты: `974823f52`. [Production receipt](PRODUCTION_RELEASE_2026-10-12.md). Это инженерный срез, не полная приёмка пяти паспортов.

## Что работает

Собственный запрос и точная текущая корзина создают настоящую заявку менеджеру до обещания уточнить. Менеджер подтверждает каждую позицию, количество и основание доступности, включая доступный товар вне учёта. Подготовка, отправка и прибытие различаются. Подарок, получатель, срочность, дата и способ отправки сохраняются с provider-source. Новое явное пожелание отменяет старую трактовку; повтор того же пожелания не создаёт лишний alert. Раздельная доставка остаётся предложением.

Один захваченный `fulfillment.review` поступает в карту, Gemini, proposal и manifest; freshness checks могут запретить старое обещание. Складская цифра не подменяет решение менеджера. Решение не создаёт оплату, резерв или остаток. Для доступного товара вне учёта менеджер видит действие: сверить учёт либо подготовить заказ вручную.

Разовое UK/RU/EN restock-согласие требует доставленной карточки с одной положительной кнопкой и собственного подписанного native ответа. Оно относится к точному товару/опциям/количеству и действует 90 дней. Доступна доказанная отмена. Наличие проверяет менеджер; числовое пополнение само не разрешает отправку. Business consent не расширяет окно Instagram.

Два отправителя используют существующий client lease и повторные физические проверки. Фактический ответ о наличии после согласия может исполнить ту же обязанность уведомить: сохранён signed provenance к исходной квитанции, без второго HTTP и выдуманного restock MID. Новые значимые сроки не подавляются как дубль. UNKNOWN удерживает отправку. Канонический API сохраняет одно конечное решение на заявку; попытка заменить его отвергается.

## Проверки

- `fulfillment-release-acceptance.log`: **314/314 PASS, 0 skips, 114.295 s**, 14 модулей нового блока и действующих truth/admin consumers. Реальная disposable MariaDB 11.4, CPython 3.14.6 / Django 6.1. Физические guards, native outbox/quick reply, действительный authenticated manager/API, captured Gemini/source refs и лимит 96 SELECT. Вызовы внешнего транспорта заменены тестовым ответом; production сообщения этим не доказываются.
- `fulfillment-final-regression.log`: **820 selected / 818 PASS / 2 skips, 339.168 s**, 32 модуля оплаты, заказов, повторной покупки, памяти, отзывов, сервиса и followups. Два raw payment trigger теста ошибочно искали имя триггера через `SHOW TRIGGERS LIKE`, который фильтрует таблицу. Исправлен lookup в information_schema; `payment-trigger-native.log`: **38/38 PASS, 0 skips, 30.317 s**, включая оба реальных UPDATE/DELETE запрета. Ни production guard, ни ожидания тестов не ослаблены.
- После финальных UI/API labels: `fulfillment-final-ui-api.log`: **37/37 PASS, 0 skips, 23.718 s**. Новая проверка фактической client detail API подтверждает понятную задачу менеджеру, корректное имя ответа и отсутствие чужого generic policy restart. С предыдущими наборами есть пересечение; повторные прогоны не суммировать.
- `check-fulfillment-final.log`: Django check; management makemigrations dry-run — 0 изменений; 48 AST; 7 InnoDB owner tables / 21 физических триггера. SQLite branch проверен только на install/remove DDL, без заявления native семантики. Node syntax и `git diff --check` PASS.
- Authenticated localhost: настоящий тестовый native consent, исходный stock0, manager available/access_unrecorded/qty1, подготовка и отправка в день проверки; отдельно проверен custom 1–3 business days. POST, сохранение и reload подтверждены. 320/390/default width без горизонтального переполнения; формы имеют 44 px mobile targets, источник согласия читаем, отправка показана отдельно от решения. Ошибки Chrome extension отделены от приложения. [320 px](UI_LOCAL_320.jpg), [390 px](UI_LOCAL_390.jpg). Это локальные синтетические данные, не production cohort.

## Остаток и следующий gate

См. [CONTRACT](CONTRACT.md). Аудируемый manager supply → stock/checkout; переиспользование свежего CART-подтверждения для standing RESTOCK review без второго manager alert; полное split execution/расходы/выбор клиента; deadline до выбора товара; подтверждённый маршрут/arrival feasibility; account/channel native future capability; естественные manager/customer/restock receipts и P7. Полные паспорта остаются `[~]`.

Rollback: purpose flags OFF либо scoped revert/main/canonical SSH deploy. Additive migration0230 и действительные immutable receipts автоматически не удалять. UGC тот же unused10→15 и 90 дней сохранены. Ни production fixtures, ни mass backfill, ни provider health probes не выполнялись.
