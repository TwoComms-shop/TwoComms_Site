# P4-3 · согласованное напоминание об оплате

Реализован отдельный current-purchase purpose: реальный запрос клиента с точным временем → одна положительная native quick reply → подтверждённая доставка карточки → подписанный ответ → PAYMENT task → проверка актуального состояния непосредственно перед HTTP → native MID → карта клиента/Gemini. [Контракт](CONTRACT.md).

## Проверки 12.10.2026

- **590/590 PASS, 0 skips** в двух последовательных запусках на отдельной локальной MariaDB11.4 с настоящими миграциями и physical guards: core/followups/source-cart/current-state/checkout **364 за121.796s**; новый payment-purpose и marketing/UGC/review/restock/manifest **226 за43.762s**. Это два непересекающихся набора; промежуточные повторные запуски не суммируются.
- **57 новых методов**: reminder33, context/readback16, physical/privacy/current-admin/generic-queue/replacement/inbound8. Полный TransactionTestCase проходит реальный execute_claimed_revision для request→signed accept→signed cancel, три native effects/MID, без Gemini. Только внешний Send API/Monobank HTTP заменён синтетическим транспортом; admission, sealed source, source cart, quote, generation, answer, guards и outbox настоящие.
- Реальные оплаченные/повторные/current-generation сценарии, natural invoice expiry без stale URL/reissue, поздний worker>5min, source/reset/recipient/namespace/HMAC/UNKNOWN, raw yes, недоставленная cancel-card, takeover/service/no-buy/new-inbound на HTTP boundary, MID-before-history failure/recovery без нового HTTP; новое согласие заменяет даже same-clock старую pending задачу и сохраняет чужой purpose.
- Physical SQL UPDATE/DELETE запрет всех трёх ledger; erasure только после persisted exact-owner privacy fence. **9/9 MariaDB triggers**. SQLite проверен только на vendor DDL install/remove9/9; это не подмена MariaDB business acceptance.
- Admin current capture read-only и сохраняет64query budget. Внутри одного captured request переиспользуется подготовленный ORM catalogue только с точным owner/head/digest seal; fresh catalogue parity, zero extra SELECT, изменённые owner/head/digest/product set отвергаются. Нет global cache или ослабления stock/payment guards.
- Django check PASS, management makemigrations --check **0diff**, scoped AST27PASS, git diff --check PASS. Рабочий runtimeCPython3.14.6/Django6.1.

Два старых regression fixture исправлены: signed consent создаётся до forward shipment, после forward delivery assignment перечитывает committed Order перед event materializer. Production consent/UGC policy не ослаблена. Невалидные промежуточные запуски (ошибочные module labels, старые fixture chronology, случайно одновременные local runner) исключены из acceptance.

## Оркестрация и review

Root владеет Git/DB/schema/provider stubs/deploy/UI. Sol/high workers: отдельные core, ingress/daemon, context/catalogue и тесты; правки каталогов предварительно scoped. Независимый READ_ONLY review ingress-worker нашёл MID/history, no-buy и lifetime-history scan; исправлены и повторно проверены. Дополнительный reviewer spawn отклонён capacity; новый reviewer/model не заявляется. Финальный review не выявил дополнительных unsafe-send/source/migration/privacy blockers. Root сузил replacement до exact-client/purpose pending tasks с eager evidence; все590 финальных тестов приняты.

## Ограничения и следующий gate

Нативные quick replies Meta поддерживаются мобильным Instagram, не desktop. Выбранное время внутри original-source conservative23h и текущей12h proposal; согласие не расширяет24h и не означает native future marketing grant. Уточнение требует полного нового запроса; отдельный bare-time continuation пока OPEN. Natural customer cohort после релиза проверяется без тестовых production-клиентов и массового backfill. Signed restock subscription/answer/revoke, account-specific native future capability и затем checkpoint513/fullP7 остаются следующими блоками. UGC unused10→same15/90days сохранён.

Rollback: сначала IG_PAYMENT_REMINDERS_ENABLED=false через штатную конфигурацию либо scoped revert с canonical main deploy. Не откатывать0229 и не удалять реальные receipts автоматически; schema additive, ledger сохраняет исторические доказательства. Unknown customer sends не replay.
