# P1-7 / P0-4: admission и captured memory, 06.10.2026

Следующий ограниченный срез после production `274347c8a34ee33c8605df1f6ed2355857552ed8`. Корневой агент отвечает за интеграцию, общий/native gate, scoped Git и production; GPT-6.1 Sol агенты — за admission, memory producer, lifecycle и независимое review.

## Контракт

Enforced nonlive DENY/UNKNOWN/accounting error останавливают HTTP до provider-started и нового расхода. Attempts наследуют mode канонического graph; reaper обрабатывает shadow и enforced с прежним late-success/CAS. Source callback отказ завершает запрос без ротации; quota отказ оставляет другие независимые проекты доступными. Часы обновляются после graph/quota lock и source checks; timeout повторно ограничивается перед физическим POST. Истечение после уже зарегистрированного admission останавливает HTTP, сохраняя консервативный расход без слепого refund.

Memory source enqueue независим от SENT клиентского ответа: accepted webhook/current poll/manager events, собственный exact human SENT и проекция revision reply. Historical/import, UNKNOWN/failed human outbox и erased sources не получают новую generation authority. Dirty/consumed coalescing, один claim и один successor, bounded debounce/backoff; генерация использует существующую analysis lane после более приоритетной работы. Reset/erasure очищают private snapshot и инвалидируют head. Unsafe прежняя narrative omit с причиной; canonical size/source proof сохраняется за пределами окна60 и меняется только собственным correction source.

Captured source IDs/digests/event-time+ID order, reset/episode/line/recipient scope и предыдущий head входят в публикационный CAS. Read integrity и exact human receipts проверяются пакетно, без N+1. Final callback читает lane/settings/source без блокировок или создания записей внутри provider accounting transaction. Для automatic generation нужны оба default-off flags и фактически active enforced accounting. Rollback в shadow/off/invalid не разрешает generation.

## Проверки

Provider gate: 171 tests, 166 PASS / 5 native skips, 14.863s. Native provider final: 9/9 PASS, 6.870s, включая реальную блокировку quota row за пределами deadline, конкуренцию за последний permit, forced reaper/late success и shadow races. Это локальная disposable MariaDB11.4.12/InnoDB, не production fixture.

Final combined gate: 358 tests, 352 PASS / 6 native skips, 0 failures/errors, 23.448s. Native memory gate: 9/9 PASS, 5.604s — шесть настоящих contention cases, две проверки producer→actual facade и отдельная repair-reservation race. Native reader/lifecycle/privacy/lane regressions:126/126 PASS,34.585s. Все шесть memory skips выполнены на InnoDB; отдельный provider skip для repair reservation также выполнен на InnoDB. Наборы пересекаются; результаты не суммируются как число уникальных тестов.

Reconciliation имеет отдельный durable cursor на settings: fair bounded page, enqueue в отдельных client transactions, затем cursor CAS без удержания settings lock при захвате client. Проверены31missing-hook clients, crash replay и competing scans. Native interleaving с настоящим restricted inbound закрывает client→settings deadlock; native deadline/owner lease waits не публикуют поздний head. Все10additive fields0219 (9client+1settings) соответствуют моделям; свежий native migration graph применён, IgClient InnoDB. Независимое итоговое review — без открытых actionable findings. Django check и scoped diff checks успешны.

Этот раздел фиксирует приёмку до production pull. Post-pull schema/flags/supervisor/child проверяются отдельно; они не подменяются локальным green gate.

## Baseline и границы

Полный daemon-progress suite: одинаковые2 SQLite closed-connection errors на HEAD и новом command source, 49 executed tests / 1 skip в обоих прогонах. Те же два сценария по отдельности проходят2/2. Raw baseline/current logs сохраняются отдельно, набор не объявляется зелёным. Две другие daemon fixtures были неполны уже на HEAD: SimpleTestCase не mock-ировал durable lane acquisition, page-only fixture зависел от inherited instagram_login. Они исправлены с сохранением исходных exception/starting-file/chunk-permission assertions;11/11PASS даже при inherited instagram_login. Privacy fixture добавляет разрешённый receipt key с сохранением immutable producer receipts; runtime guard не ослаблен.

Исходный combined356 завершился3failures: эти две daemon fixtures и неверное ожидание корневого facade test, считавшего безопасный source refusal failed вместо discarded. Итоговый test проверяет claimed/published, durable source_admission_denied,0HTTP/0добавленного расхода; operational preconditions изолированы. Interrupted native run остановлен до правки cursor inversion и не используется как acceptance.

Read-only production profile inspection: active enforce; только3.6-flash и3.5-flash-lite имеют `json_bytes_div4_v1`. Проверка с настоящим model chain/key iterator доказывает0HTTP для uncalibrated3.7/3.8 и ровно1mockedHTTP на допустимой3.6 без chat reserves. Inline media в nonlive остаётся UNKNOWN/0HTTP до принятия estimator; live shadow policy сохранена. Probe/metadata/generation API в автоматических проверках не вызывались.

Автоматическая memory generation остаётся выключенной. Все паспорта P1-7/P0-4 и весь план не объявляются закрытыми; controlled activation, последующий unified context/manifest и72ч+20естественных ходов остаются отдельными gates.

## Evidence

- [Combined SQLite](combined-sqlite-final.log), [provider SQLite](provider-sqlite-final.log).
- [Native provider](native-provider-final.log), [native memory contention/facade/repair](native-memory-final.log), [native regressions](native-memory-regressions.log), [real schema](native-schema-final.txt).
- [Daemon progress baseline](daemon-progress-baseline.log), [same-order current](daemon-progress-current.log), [original fixture baseline](daemon-fixture-baseline.log), [original fixture current](daemon-fixture-current.log), [corrected fixtures](daemon-fixture-gate.log).
- [Django check](django-check.log), [test module manifest](test-modules.json).
