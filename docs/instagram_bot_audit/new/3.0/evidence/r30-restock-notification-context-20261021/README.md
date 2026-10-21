# 41D · Единая история отправленного уведомления

✅ Код `66c31e3cbdf1d85af90f0c1dcf0e9c932551d978` опубликован в main и развернут штатным SSH `git pull --ff-only`. Runtime checked 2026-10-21T18:52:18.018490+00:00: main/supervisor/child на этом SHA; workers healthy, maintenance OFF,5source hashes match.

Dedicated и shared Gemini context теперь используют один загруженный граф и один чистый DTO-verifier. Проверяются подписи invitation/answer/event, exact target/purpose/body, native task checkpoint, времена и принадлежность опциональной истории. Missing history после durable MID допустима; present foreign history отвергается. UNKNOWN не превращается в SENT. Новых SELECT для receipt не добавлено.

Отмена/expiry/новая корзина/остатки/статус менеджера не стирают доказанную прошлую отправку. Она не доказывает текущее наличие, оплату или право новой отправки. Optional fresh manager review при исчерпании бюджета оставляет готовую историю с unverified physical и coveragefalse; обход прекращается. Exact loaded-column parity позволяет повторно использовать уже полностью проверенное доказательство. Genuine mixed legacy revoke≤24SELECT, admin≤96, prompt2400. UI/assets/геометрия/положительная кнопка не менялись.

**129unique Python PASS0skips:**83native MariaDB +46дополнительныхSQLite. Повтор12 новых методов не суммируется. Full real forward graph, review/storefront reverse/reapply, schema/check без blocked warnings и cleanup verified. Методы и hashes приведены в JSON.

```sh
/Users/zainllw0w/TwoComms/site/.venv/bin/python scripts/run_mariadb_gate.py --server-mode native --suite restock-notification-context
# Из twocomms:
/Users/zainllw0w/TwoComms/site/.venv/bin/python manage.py test management.tests_ig_restock_subscriptions management.tests_ig_restock_legacy_acceptance --settings=test_settings
```

Production:8READONLYSELECT, provider attempts0 в процессе проверки, fixtures0/manualcustomer sends0;81stockrows/digest unchanged. Live SENT-list пока отсутствует: native lifecycle доказан disposable MariaDB, не выдуман из live данных. Свежий visual Direct, полный multipart и ALL-delegation OPEN.90дней не менялись.
