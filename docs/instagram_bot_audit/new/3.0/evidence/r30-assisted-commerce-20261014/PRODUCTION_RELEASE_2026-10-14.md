# Production release · Engineering39 · 14.10.2026 Europe/Kyiv

✅ Код **`7ea9d1f31bf95b915d7b0953158bfe0ded00adf9`**: scoped59files commit/push→canonical SSH `qlknpodo@195.191.25.63`, main/pull--ff-only→real management0233 migration→check→collectstatic→compress→application restart→supervisor reload. Temporary maintenance lease принадлежала этому релизу и снята. [Before](prod-before-deploy.json), [after](prod-after-code.json).

Read-only runtime verification: Python3.14.8/Django6.1; migration0233;5actualInnoDBtables;15BEFORE/SIGNAL native guards; supervisor/child SHA совпадают с кодом, PID4060544 совпадает; main/workers healthy, stalledFalse, maintenanceOFF. Process_identity_verified отсутствует в этом API: вместо него приняты actual supervisor child_pid_matches_expected и SHA.12dataSELECT в after check; provider I/O0, production fixtures0, synthetic customer sends0.

81catalogue rows; stock/exact-size digest до/после **`c84facc0c868919af3437b71db996ba21a3d615c1bd7cfd8be49684f9df6a30f`** совпал. Ordinary rollout остаётся off/0; новый narrow typed-execution checkout проверен51/51 native production-equivalent cases, включая ordinary/forged rejection. Natural assisted tables0, новый paid customer cohort OPEN.

Авторизованный browser после deploy реально показывает новую панель; hashed CSS/JS обнаружены как loaded resources, CSS65rules, componentwidth293desktop/337at390/267at320 равен scrollWidth. [Receipt](ui-prod-layout.json). Полный mixed UI/payment путь проверен на локальном disposable server, не production fixture. Chrome chooser automation ограничена extension permission; native API upload и browser display прошли.

Перед кодом сохранены внешние main updates до2edb653f3. Основной dirty checkout не сбрасывался и не включался в release; plan mirror отдельно проверяется по baseline digest. Documentation-only фиксация сохраняет этот code SHA; окончательный main HEAD и supervisor identity подтверждаются в отчёте о завершении.
