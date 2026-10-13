# Engineering37 · production receipt · 13.10.2026

✅ Финальный код `0527d2f925a7e928e8493d827f03fe462e784655` выпущен canonical SSH pull на production `main`.

Владелец release выполнил собственный finite900s maintenance lease → git pull --ff-only origin main → management migration0232 → Django check → collectstatic → compress --force → restart marker → maintenanceOFF → supervisor ensure/reload. Миграция завершилась OK; новые статические файлы опубликованы. До compress ожидаемое предупреждение старого manifest исчезло после rebuild.

Read-only receipt **2026-10-13T17:08:48.661810+00:00**: [raw](PRODUCTION_RUNTIME_CODE.json). CPython3.14.8/Django6.1/MariaDB11.4.13-MariaDB-cll-lve; local tests CPython3.14.6/MariaDB11.4.12. Все28 owned tables InnoDB,7 new split tables;45 exact BEFORE guards (24 previous +21 split), decimal/date/UUID identities и глобальная уникальная полная ТТН. fulfilled_quantity backfill/bounds/owner/orphan checks0 нарушений. Миграции0230/0231/0232 applied.

Все81 id/product/color/stock строки совпадают с fresh pre-pull baseline2026-10-13T16:48:52.314447Z. SHA256 `3d0a0a693dfb4c387ae1113ec4495df8611de37d58cdf7a65325a189d8b1c3a6` сохранён. PID2934447 проверен через /proc/identity; main/workers healthy, stalled/worker_stalledFALSE, maintenanceOFF, main/supervisor/child exactSHA.41 bounded application-data SELECT в READ ONLY transaction; standard Django driver session initialization выполнена до строгого guard. Provider probes0, synthetic customer sends0, production fixtures0. Новые15 ledger family counts0; естественная business приёмка OPEN.

Авторизованный production browser: actual visible split panel, корректное нейтральное состояние без проверенной multi-cart; sourceCSS96d442011f6f→served18dbaecd0585, sourceJSd29a64b34db6→servede02532e3965e действительно загружены.1816/390/320px: page width=viewport, panel333/337/267px, component overflow0, application console errors0. Только read-only GET и раскрытие панели; никаких соглашений/заказов/ТТН/сообщений настоящих клиентов не менялось.

Local actual POST и carrier progression доказаны отдельно в [приёмке](README.md). Это не natural production split cohort. WAIT discussion-only; full paid execution требует AVAILABLE всей корзины; реальные ТТН регистрируются менеджером. Partial availability/future allocation, automatic multi-waybill, in-line quantity split и amendment остаются OPEN.27 полных паспортов `[~]`, UGC90дней/unused10→15% сохранены.

Rollback: сохранять schema и immutable receipts/платёжные/складские доказательства. При появившихся split records старый whole-order code не является безопасным rollback; нужен guard-aware forward fix. UNKNOWN не повторять, migrations/dispatch receipts не удалять.

Финальная проверка после узкого исправления ordinal: **2026-10-13T17:23:12.863621+00:00**, code/main/supervisor/child `0527d2f925a7e928e8493d827f03fe462e784655`, PID2981611, healthy/maintenanceOFF. [Final raw](PRODUCTION_RUNTIME_FINAL_CODE.json). Те же28InnoDB/45guards/81stock baseline неизменны,41READONLYSELECT/provider0/customer tests0/natural ledger0. Follow-up canonical pull/check/restart/supervisor, без новой миграции или изменения UI-assets.90/90 затронутых native PASS0skips после основного432gate.
