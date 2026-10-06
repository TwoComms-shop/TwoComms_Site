# Первый срез: production release 06.10.2026

Пользователь разрешил деплой и продолжение реализации с субагентами. Выпущен commit `274347c8a34ee33c8605df1f6ed2355857552ed8` на `main`; предыдущий production SHA — `0795423014887c619b3992deba865967ae456e87`.

## Доставка и runtime

- Scoped commit/push успешны. Посторонние dirty changes, включая attentionElapsedLabel и прежние дополнения журнала, не вошли в релиз.
- Документированный SSH pull — fast-forward до ожидаемого SHA. Использованы канонический SSH destination и Keychain loader без вывода секретов.
- Daemon остановлен собственной maintenance lease на время миграции. Management `0218` применена; `manage.py check` — 0 issues.
- `collectstatic --noinput`: 0 copied, 734 unmodified, 1074 postprocessed. `compress --force`: 4 blocks, 60 templates, 1 context. Обновлён `tmp/restart.txt`.
- Собственная maintenance lease снята; `run_instagram_bot --ensure` подтвердил готовность под supervisor.
- Read-only post-pull проверка: `main` и полный SHA совпадают; maintenance off; bot, AI и effective revision enabled. Max HTTP 8, scarce budget 2 сохранены.
- Supervisor и child release SHA равны `274347c8a34ee33c8605df1f6ed2355857552ed8`, PID совпадает; restart_count 0. Process heartbeat и main/worker lanes свежие и healthy.
- Native schema: transition/source больше не unique; decision/source и session/to_revision unique сохранены. Проверка проводилась в read-only transaction; production не использована как fixture.

## Авторизованный production browser

Проверен настоящий management tab в Chrome после загрузки нового HTML. Новый inline renderer `source_selection_display` загружен. Карточка client352 показывает `L` с source №3259 и `Футболка` с source №3247. Раскрытый подбор показывает те же факты и отдельно сообщает неизвестность применимости/наличия. Исторический purchase intent не был выдуман или записан задним числом.

Наблюдались реальные loaded URLs: `ig_journey.28aed29e0fd4.css?v=journey-presentation-v14`, `bot_mobile.6a8a6d6c4db7.css?v=bot-mobile-v1`, `ig_journey_geometry.6b4aac851afb.js?v=journey-presentation-v14`, `ig_journey.3383cb50a336.js?v=journey-presentation-v14`. Внешние assets первым срезом не менялись; подтверждена загрузка изменённого inline renderer. Console errors — 0. Read-only desktop QA; локальный responsive QA описан в исходном README. После проверки пользовательская вкладка возвращена на исходный URL.

Raw chat, личные данные и provider secrets в evidence не сохранялись. Crop screenshot получился недостаточно читаемым и не используется как доказательство; UI факт подтверждён видимым DOM и раскрытой картой.

## Оставшиеся gates

Контролируемый release/post-pull/runtime/browser этап P7-1.C выполнен. Наблюдение 72 часа и минимум 20 подходящих естественных новых ходов остаётся OPEN. Исторические сообщения client352 не переигрывались; synthetic provider calls и тестовые customer sends не выполнялись. Деплой не закрывает остальные паспорта плана.

Следующий срез: P1-7 final nonlive admission и P0-4 captured narrative producer/coalescing. Три прежних accounting failures требуют исправления runtime/reaper и корректных mode fixtures; они не объявляются только oracle drift. Автоматическая generation новой памяти остаётся выключенной до отдельной приёмки admission и producer.
