# Django 6.1 Stage 6: periodic ownership and rollback evidence contract

Дата подготовки: 2026-08-19. Scope: только non-DTF периодические jobs.

## Что добавлено

`docs/qa/django61-stage6-periodic-owners.json` является машинным inventory
контрактом. В нём перечислены десять non-DTF owners, включая bounded durable
task canary, guarded auto-analysis и image optimization. Для каждого owner зафиксированы cadence,
команда, managed marker, lock path, `flock` и bounded timeout. Это не меняет
crontab и не утверждает, что live production snapshot уже соответствует
контракту.

Для будущего durable owner зафиксированы тот же CloudLinux production settings
context, что проверяет preflight (`DJANGO_ENV=production` и
`DJANGO_SETTINGS_MODULE=twocomms.production_settings`), и `exec flock`: shell
cron заменяется launcher-ом, поэтому budget из трёх процессов не занижен.

Три активных тяжёлых Django owner-а используют один account-wide lock
`tmp/twocomms_heavy_background.lock`. Координатор Instagram является единственным
bounded waiter (`flock -w 50`); durable и Nova Poshta остаются non-blocking
(`flock -n`). Поэтому одновременно может выполняться только одна Python
цепочка (launcher `flock`, `timeout`, Python), а при занятом lock возникает не
более одного дополнительного короткоживущего `flock` waiter: его 50-секундное
окно меньше минутной cadence координатора. Это ограничение сохраняет
account-wide admission и даёт координатору шанс дождаться короткого durable
запуска без запуска второго heavy worker.

`scripts/verify_django61_stage6_periodic_owners.py` принимает manifest и
санитизированный crontab snapshot (`--crontab PATH` или `--stdin`) и завершает
работу с ошибкой при любом из условий:

- объявлен DTF scope или DTF встречается в evidence;
- встречается неизвестный `# BEGIN TWOCOMMS ...` managed block;
- отсутствует или дублируется managed block/owner;
- owner находится вне своего managed block;
- owner installer отсутствует в repository;
- cadence, lock, `flock` или bounded timeout не совпадают;
- отсутствует явный repository rollback path и rollback owner/action.

Validator только читает файлы: он не устанавливает cron, не запускает Django,
не выполняет SSH, migration, DDL, cleanup или worker.

## Lock migration and drain safety

The installers replace managed crontab text but do not terminate processes that
were already spawned from the previous crontab. A future change of the shared
lock path must therefore run in maintenance: stop or disable the scheduler,
wait for every old heavy owner to exit, capture the old lock-holder/PID
evidence, install the replacement owners, and run every installer in `--check`
mode before re-enabling scheduling. Installing a distinct lock path while an
old shared-lock owner is alive can admit old and new Python chains at once and
invalidates the one-chain budget.

## Historical criteria for closing checkboxes

`DJ6-SRV-005` и Stage 6 exit gate «one owner» можно отмечать только после
передачи свежего production `crontab -l` snapshot в этот validator с
результатом `status=ok`, сохранённого вместе с release SHA и датой. Наличие
manifest или green synthetic test недостаточно.

Exit gate «cron remains rollback path» можно отмечать только когда тот же
snapshot, manifest и rollback runbook принадлежат одному release SHA; operator
должен быть явно указан, а rollback script/path доступен в checkout. Этот
commit сам по себе намеренно оставлял оба production gate открытыми.

## Production supersession 2026-08-19

На production SHA `ba032bbd2030421d2340e9314a921eddabe2f582` свежий crontab
snapshot прошел полный validator со `status=ok`: у каждой активной non-DTF
периодики ровно один owner, durable cron имеет ровно один managed marker/owner,
а bounded cron сохраняется как execution/rollback path. Installer `--check`
также green. `product_catalog_image_jobs` остается inventory-only
(`active:false`): block и owner отсутствуют, поэтому это не image-worker
rollout. Полная activation evidence:
`docs/qa/django61-stage6-production-activation.md`.

## Проверка

Один focused unittest gate:

```text
.venv/bin/python -m unittest tests.test_django61_stage6_periodic_owners -v
```

Дополнительно требуется `py_compile` validator-а и `git diff --check` перед
commit. Production database, cron и DTF не затрагиваются.
