# P4-3 · production acceptance · 09.10.2026

✅ Код/main/deploy `8ba5a5cb95c89eeea9dd362ada7795c9bf3109e1`. Canonical SSH pull --ff-only, Django check, restart.txt и один supervisor --ensure выполнены. Наблюдение **2026-10-09T11:59:28.815174Z**: main/supervisor/child один SHA, PID1692386, process/main/workers healthy, stalledFALSE, maintenanceOFF. [Machine receipt](PRODUCTION_RECEIPT.json), [native248/248](README.md).

Deployed pure gates: restock reason с неправильным invoice payload, restock event под другим reason, restock purpose и legacy key получают restock_purpose_unverified; payment invoice identity не ошибочно классифицируется как restock. Это проверка classifier/fact fence, не выдача payment permission. Полные claim/HTTP/receipt flows подтверждены native regression suite.

7 data READ ONLY queries, maxstatement8; zero provider HTTP/probes и customer test sends в процедуре проверки. Restock reviews/legacy tasks сейчас0 — естественный inventory→manager cohort ещё не наблюдался; production не использовалась как fixture. Existing post_purchase_marketing invitation SENT1, других purpose invitations нет. Narrative timeline/generation и существующий post-purchase business-consent flagTRUE.

Actual engines: Product/Variant MyISAM; VariantSizeRule/client/task InnoDB. Они не преобразованы. Отдельная временная QA named lock, не связанная с товаром/клиентом, успешно acquired/owned/released тремя SELECT; никакие business rows не изменялись. Effective ATOMIC_REQUESTS FALSE; deployed route explicit non_atomic_requests/default подтверждён. Stock editor's finite contention/independent product/commit callback/release реально проверены двумя native MariaDB connections. Authenticated inventory edit на production не выполнялся, чтобы не создавать искусственные stock events.

Static/templates/UI assets не менялись; collectstatic/compress и новое browser UI acceptance этому срезу не требуются. Предыдущие memory/card/one-button UI evidence сохранены и не пересчитаны как новый тест. Review copy использует существующую manager task presentation, без raw token/customer identity и без выдуманной подписки.

P4-3 остаётся partial. Отдельные payment agreed time/invoice-generation ledger, signed restock invitation/answer/subscription и account-specific native future grant OPEN. Meta overview через Context7 подтверждает стандартный24h reply и отдельный human-agent режим; это не доказательство future marketing eligibility конкретного аккаунта. Прямой fetch official overview вернул429; unsupported не заявляется. Business consent не расширяет окно. UGC unused10→15/staff moderation/исходные90дней не изменены.

Rollback: scoped revert code → push main → canonical SSH pull/check/restart/singleensure. Schema/data migration, engine conversion, receipt erasure, forced send/backfill отсутствуют. Plan1.21/engineering32/all27passports partial после документального reconciliation.
