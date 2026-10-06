# Production release:850baedfb,06.10.2026

Scoped commit/push и документированный SSH pull выполнены. Production `main` — `850baedfb753a0570b60dc21e457bbe9f76a7fbc`, предыдущий release — `274347c8a34ee33c8605df1f6ed2355857552ed8`. Tracked production checkout чистый.

Management0219 применена; read-only information_schema подтверждает все10полей (9client+1settings cursor). `manage.py check` —0issues. Backend release не меняет templates/static; выполнен WSGI restart и запуск daemon через существующий supervisor.

Первый release script завершился после migrate/check на неверном CLI аргументе `--maintenance-off`. Снятие собственной lease повторено с правильным `--maintenance-off r30-850baedfb-release`; затем `--ensure` подтвердил готовность. Одна SSH попытка была отклонена при стандартном наборе authentication methods; пароль из того же Keychain loader с явным password authentication позволил завершить recovery. Credential не выводился и не копировался. Этот промежуточный сбой не обозначается успешным шагом.

## Read-only post-pull

- Maintenance off; bot/AI/effective revision enabled.
- Active accounting, nonlive mode enforce. Max HTTP8/scarce2 сохранены. Probe/metadata/generation preflight не выполнялся.
- Оба memory generation/acceptance flags FALSE. Fair repair cursor3287 наблюдался от штатной работы daemon; новые provider consumer calls не включены.
- Старый существующий narrative имеет `narrative_provenance_missing` и не используется reader. Текст/личные данные в evidence не сохранены.
- Process online; pulse0.1s. Main healthy/idle, age0.2s. Workers healthy; stalled/worker_stalled FALSE.
- Supervisor current; supervisor/child SHA оба850baedfb753a0570b60dc21e457bbe9f76a7fbc, PID3823929 совпадает, restart_count0.

Все schema/flags/health observations получены в transaction READ ONLY с ограничением времени SQL; production не fixture. [Sanitized post-pull log](production-postpull.log).

Авторизованный Chrome management tab после reload сохранил исходный URL client336, отрисовал `follow-indicator-336` и context toggle; новый inline source-selection renderer присутствует, console errors0. UI/static этим backend срезом не изменялись. Карточка и подбор client352 были проверены при первом выпуске; исторический диалог не переигрывался, тестовых customer sends не было.

## Дальше по плану

Локальные/native gates и controlled release этого core выполнены. Автоматическая memory generation остаётся OFF; controlled caller activation, unified context/request manifest, nonlive inline-media estimator и прочие части паспортов отдельно открыты. P7-1.C72часа+20подходящих естественных новых ходов остаётся OPEN. Операционная health не объявляется proof generation quality или закрытием всего плана.

Этот post-release evidence записан после code commit; он включается в следующий scoped documentation release вместе с отметками выполненных блоков в Plan90.
