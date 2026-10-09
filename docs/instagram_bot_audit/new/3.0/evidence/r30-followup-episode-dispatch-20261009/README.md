# P1-4 · новый intent исторического покупателя · 09.10.2026

✅ CODE/TEST/PROD принят в c68d0754c; [production receipt](PRODUCTION_RELEASE_2026-10-09.md).

Реальная причина: planner сохранял `commerce_binding` текущего episode и создавал ordinary task с `deal=None`, но busy claim/main claim/renew не передавали binding в `_client_allows_followup`. Legacy lifetime-paid fallback отклонял настоящего исторического покупателя как `already_converted`, ещё до canonical intent validation.

Один helper передаёт captured binding только для `ordinary_intent_followup`; missing/nonobject → `{}` и отказ canonical validator. Legacy получает прежний `None`. Claim/busy/renew используют один контракт. Ни деньги, ни коммерческая история не изменяются.

Root native MariaDB: **19/19 PASS / 11.971s / skips0**. Реальный historical `IgPaymentProjection.confirmed` + новый неоплаченный current episode → planning/claim/renew/permission/provider boundary/receipt; 3h и explicit selection90m. Проверены one unanswered, no ladder/size chase, cooldown/busy/pause/takeover/window, forged/stale/absent source, current paid и UNKNOWN без resend. Transport mocked; source/payment/model/SQL guards активны. Четыре старых negative fixtures исправлены через INSERT отдельных frozen tasks, без изменения immutability.

Independent READ_ONLY review: checkpoint_contract approved scoped helper, native authority и send guards. PROD release receipt и широкая проверка будут добавлены после выполнения.


Финальная широкая проверка **192/192 native PASS/52.197s/skips0**, включая19новых. Предыдущий190run обнаружил14устаревших expectations; сравнение с original HEAD bot_followups повторило те же14/14FAIL/1.498s. Обновлены только3testfixtures: no ordinary ladder/no cold, сохранён payment continuation positive, exact manager capabilities и genericstaff denial, typed understood image/source и legacy limitation control. Runtimeguards/auth/businesspolicy не ослаблены. Independent final READ_ONLY review memory_consumers принят. Дополнительное UI refinement v18 различает pending/processing/failed/unknown/not-yet-sent invitation; native grant не подразумевается. PROD этого P1-4 пакета принят в c68d0754c.
