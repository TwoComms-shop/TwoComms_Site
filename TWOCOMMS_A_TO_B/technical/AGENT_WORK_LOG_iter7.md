# AGENT_WORK_LOG_iter7 — 07.07.2026

## Контекст

- Рабочая директория: `/Users/zainllw0w/TwoComms/site`.
- Ветка: `main`.
- Перед началом работы выполнена сверка с `origin/main`: локальный `main` совпадал с удалённым (`HEAD...origin/main = 0 0`).
- Пользователь явно попросил работать прямо с `main`, закрывать только незакрытые пункты `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`, ничего не чинить в приложении, кроме безопасной зачистки утёкшего SSH-пароля из git-файлов.

## Правила этой сессии

- Не менять Django-код и бизнес-логику.
- Production-проверки делать read-only и батчево, чтобы не спровоцировать SSH/fail2ban rate-limit.
- Не печатать и не коммитить секреты. Пароли, токены, `.env` и DSN не должны попадать в отчёты.
- Перед каждым коммитом проверять staged diff на секреты.
- После небольших законченных шагов коммитить и пушить в `origin/main`.

## Статус чеклиста на старте

Открытые пункты по `rg "^- \\[ \\]" TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`:

- `CRO-051` — baseline конверсии из живой БД `UserAction`.
- `TD-015` — Passenger/лимиты хостинга, Redis/Celery queue, память.
- `TD-016` — логи сервера, ротация, секреты/PII в логах.
- `AEO-001` — страницы, которые цитируют ChatGPT/AI-рефереры.
- `DB-001` — slow query / performance schema.
- `DB-003` — целостность `Order` ↔ `UTMSession` ↔ `User`.
- `DB-004` — рост `UserAction`, размеры таблиц.
- `DB-006` — charset/collation MySQL.
- `DB-007` — синхронность миграций с БД.
- `CB-045` — логи сервера и ротация (пересекается с `TD-016`).

## Выполнено: зачистка SSH-секрета из текущего дерева

1. Проверены текущая ветка и удалённый `main`.
2. Найдены прямые вхождения реального SSH-пароля в текущем дереве:
   - `deploy_finance.sh`;
   - `TWOCOMMS_A_TO_B/technical/NEXT_AGENT_PROMPT.md`;
   - `TWOCOMMS_A_TO_B/technical/SSH_AGENT_PROMPT.md`.
3. В `deploy_finance.sh` hardcoded `SSHPASS` заменён на обязательную переменную окружения:
   - скрипт теперь падает до SSH, если `SSHPASS` не задан;
   - секрет больше не хранится в файле;
   - функциональный сценарий `sshpass -e` сохранён.
4. В `SSH_AGENT_PROMPT.md` убран явный пароль, а команды переписаны на безопасный шаблон `SSHPASS='<password-from-owner-or-secret-manager>' sshpass -e ...`.
5. В `NEXT_AGENT_PROMPT.md` убрана строка с реальным паролем; вместо неё оставлена инструкция использовать пароль только из чата владельца или secret manager без коммита в файлы/логи.

## Что проверить перед продолжением

- `rg -l "<known-leaked-credential-fingerprint>" .` должен вернуть пустой список при локальной проверке с реальным fingerprint, который не записывается в файл.
- `git diff --check` должен пройти без whitespace-ошибок.
- Staged secret scan должен проверять staged diff минимум по паттернам SSH-паролей, `SSHPASS` с реальным значением, приватных ключей, API-ключей и OpenAI-style токенов. Реальные fingerprint-значения не записывать в отчёты.

## Следующий шаг

После коммита и пуша зачистки секрета нужно переходить к серверному read-only сбору данных для всех оставшихся SSH-пунктов. Готовые батчи уже есть:

- `TWOCOMMS_A_TO_B/technical/scripts/server_shell_batch.sh`;
- `TWOCOMMS_A_TO_B/technical/scripts/server_audit_batch.py`.

Их результаты нужно сохранить в `TWOCOMMS_A_TO_B/technical/data/`, проверить на секреты и затем разнести по секционным отчётам:

- `audit_report_section1_cro.md` для `CRO-051`;
- `audit_report_section3_techdebt.md` для `TD-015`/`TD-016`;
- `audit_report_section4_seo.md` для `AEO-001`;
- `audit_report_section5_db.md` для `DB-001`/`DB-003`/`DB-004`/`DB-006`/`DB-007`;
- `audit_report_section6_codebase.md` для `CB-045`.

## Выполнено: коммит зачистки секрета

- Коммит: `d85f6d83 security: remove leaked ssh password from docs`.
- Push: `origin/main`.
- После push проверено: `git rev-list --left-right --count HEAD...origin/main` = `0 0`.
- Важно: текущий HEAD очищен от прямого пароля, но история git всё ещё содержит старые коммиты с секретом. Полная очистка истории требует отдельного согласования, ротации пароля и force-push/history rewrite.

## Выполнено: серверный read-only сбор данных

Сделаны 3 SSH-сессии, все завершились `rc=0`; данные сохранены в:

- `TWOCOMMS_A_TO_B/technical/data/server_shell_batch_output.txt`;
- `TWOCOMMS_A_TO_B/technical/data/server_audit_batch_output.txt`;
- `TWOCOMMS_A_TO_B/technical/data/server_followup_batch_output.txt`.

Проверка output-файлов на явные секреты прошла чисто: прямой старый SSH-пароль, private key, значения Django secret key, DB password, Redis URL, OpenAI API key и OpenAI-style токены в outputs не найдены.

## Ключевые серверные результаты

- `CRO-051`: baseline живой БД = `40 490 product_view → 55 add_to_cart → 6 initiate_checkout/lead → 3 purchase`; 96,23% product_view без `site_session`, значит raw-конверсия грязная.
- `TD-015`: 4 Passenger `lswsgi` процесса 119–161 MB RSS, IG bot daemon/ensure 130/127 MB; Redis broker configured, но DNS Redis Cloud падает (`Name or service not known`), поэтому старую гипотезу роста Redis queue не подтверждать.
- `TD-016`/`CB-045`: `logs/` = 958 MB; `logs/nova_poshta_cron.log` = 826,93 MB; user logrotate не найден; password hits 0, PII-like hits есть; `rum.log` имеет 24 secret-like hits counts-only.
- `AEO-001`: `chatgpt.com` = 119 UTM-сессий, referrer chatgpt = 19; топ AI landing pages: `/` 52, `/catalog/` 13, tshirts pages суммарно 20.
- `DB-001`: slow query log OFF, `Slow_queries=1633`; top SQL невозможен без отдельного sampling.
- `DB-003`: 0/43 заказов с UTM, `UTMSession.is_converted=True` = 0, orphan `UserAction.order_id`: 259 и 260.
- `DB-004`: UserAction 41 377 rows / 11,64 MB / MyISAM; самые большие таблицы — `management_leadparsingresult` 492 MB и `storefront_pageview` 63,65 MB.
- `DB-006`: все 230 таблиц и текстовые колонки utf8mb4/utf8mb4_unicode_ci.
- `DB-007`: `makemigrations --check --dry-run` = No changes detected, unapplied migrations = 0.

## Выполнено: отчеты и чеклист

Обновлены:

- `audit_report_section1_cro.md` — `CRO-051`;
- `audit_report_section3_techdebt.md` — `TD-015`, `TD-016`;
- `audit_report_section4_seo.md` — `AEO-001`;
- `audit_report_section5_db.md` — `DB-001`, `DB-003`, `DB-004`, `DB-006`, `DB-007`;
- `audit_report_section6_codebase.md` — `CB-045`;
- `twocomms_global_audit.md` — все 10 открытых пунктов отмечены `[x]` с прямыми ссылками на отчеты.

## Оставшиеся риски для следующего агента

1. Пароль SSH нужно считать скомпрометированным: текущий tree очищен, но история GitHub остаётся. Нужна ротация пароля; history rewrite — только отдельно согласованным окном.
2. `rum.log` имеет 24 secret-like hits counts-only. Строки не раскрывались и не коммитились; нужна закрытая проверка на сервере.
3. `nova_poshta_cron.log` 827 MB и содержит PII-like hits в tail sample. Нельзя удалять без согласования; сначала выяснить, пишет ли его активный процесс.
4. Все изменения этой сессии — документация/логи аудита и безопасная зачистка секрета. Django-код приложения не менялся.
