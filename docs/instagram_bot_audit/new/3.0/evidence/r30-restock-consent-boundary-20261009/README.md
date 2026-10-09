# P4-3 · Подтверждение наличия без выдуманной подписки

Код и native-проверки приняты; production verification ещё предстоит. [Контракт](CONTRACT.md).

## Что изменено

Автоматический restock без отдельного purpose-specific consent запрещён на прямом admission, policy/event scheduling, claim/renew, continuation и каждой физической HTTP-границе. Общий opt-in, paid order и post-purchase consent не превращаются в подписку на наличие. Legacy reason/payload/key распознаются независимо, поэтому неполная или противоречивая старая строка не обходит запрет.

Точный проверенный inventory event создаёт внутреннюю manager-задачу: наличие зафиксировано, собственный запрос и отдельное согласие нужно проверить, подписка не оформлена. Key зависит от клиента/reset/точного target, не callback time, обновлённого gap.at или inventory hash. Первоначальный snapshot и terminal dispositions сохраняются; stock flapping не создаёт дубли. Клиент перечитывается под lock; gap и соседние очереди не очищаются/не отменяются. Exact owner-shaped информационная задача не блокирует независимую оплату, но не предоставляет никаких send/payment прав. Изменённый оператором или чужой manager case остаётся обычной задолженностью.

Проверяются настоящий published product, его variant, enabled exact size/fit/options, доступность и captured `product_catalog:` revision; stale/future/naive/malformed/cross-product events не принимаются. JSON-prefilter и ordered iterator используют batches100 без введения silent total100 truncation. Это ограничение памяти при обходе, не доказанный latency budget или новая durable paged subscription queue.

Редактор captures hash до commit, closure сохраняет исходный revision. На production Product/Variant — MyISAM: добавлен engine-independent MariaDB named lock на database/product, finite2s/HTTP409, release после transaction/callbacks и при exception. HTTP route явно `non_atomic_requests`; staff/POST/object/positive product identity проверяются до lock, bool/float/nonobject не меняют товар. Другие товары не блокируются. Schema/engines не преобразуются.

## Проверки

**248/248 native MariaDB PASS, 56.816s, без skips**, CPython3.14.6/Django6.1, настоящий scoped migration graph и существующие физические guards. **30 новых методов**:19 restock +11 catalog callback/lock.11 затронутых/смежных модулей: новые restock/catalog, variant_resources, followup policies/delivery FSM/relevance, revision followups/purpose, marketing consent, service promotion/case suppression.

Два настоящих MariaDB соединения проверяют занятый product→409/zero DML, release→save, независимый product, lock при commit callback с эффективным ATOMIC_REQUESTS=True, actual error/release. Disposable parents InnoDB; production parents MyISAM. Engine conversion или ослабление FK/SQL/auth/source guards для тестов не выполнялись; named-lock exclusion проверяется независимо от parent engine.

Первый70-run PASS; он пересекается с248 и не прибавляется к итогу. Intermediate28-run:11 catalog PASS/17 fixture ERROR из одного несуществующего `settings.enabled`; исправлены реальные fixture fields, runtime guards не менялись. Дополнительно reviewer нашёл прямую cancellation admission и root закрыл её до окончательной проверки. Первоначальный total100 cap устранён, поздний matching client после101 unrelated rows проверен. Историческая подтверждённая покупка не отменяет независимый текущий interest/internal review; monetary projection не меняется. Partial firstMID→AMBIGUOUS/no replay сохраняется.

Django check PASS, настоящие management/product_catalog migration graph: no changes; AST6/diff-check PASS. Missing disposable staticfiles warning не означает production UI QA. Один expected missing-Color exception проверяет освобождение lock и существующий generic HTTP400.

Root интегрировал трёх Sol/high workers: scoped implementation/tests, отдельные catalog tests и независимый READ_ONLY review. Worker tests/DB/prod/Git запрещены; root sole execution owner. Нет provider health probes, production fixtures, customer test sends, новых consent grants или изменения UGC10→15/90дней.

## Следующие gates

Отдельный payment-reminder invitation должен фиксировать согласованное время/timezone и exact current episode/proposal/invoice generation. Нынешние25m/4h/23h — policy offsets, не согласие. Restock требует signed single-button UK/RU/EN invitation/answer/revoke и immutable exact subscription; native account capability остаётся unverified. Business consent не расширяет стандартное окно. Затем P5-1 checkpoint/513 trust recovery и natural cohort. Этот safety release не завершает P4-3 или все27 паспортов.
