# AGENT WORK LOG — Итерация 6 (2026-07-07)

> Продолжение аудита по `twocomms_global_audit.md`. Предыдущие логи: `AGENT_WORK_LOG_iter3.md`, `AGENT_WORK_LOG_iter4.md`, `AGENT_WORK_LOG_iter5.md`.
> Все проверки read-only, код прода не менялся. Секреты в файлы репо НЕ записываются.

**Старт итерации:** первый невыполненный пункт — CRO-051 (базовая линия воронки), далее AN-001/003/010/011/012/020/038, TD-015/016, TD-030/031/032, SEO-, AEO-, DB-, CB-блоки.

## План итерации

1. **SSH-батч №1** (единая сессия, сервер режет частые подключения): CRO-051 (baseline воронки + UA-бот-эвристика),
   CRO-050-verify (audit-сессия jrg8b3*/f2cd68* в БД), AN-004-остаток (count правил AnalyticsExclusion в проде),
   AN-039-остаток (PII-скан search-метаданных), AN-032-данные (фрагментация utm_source), DB-004 (SHOW TABLE STATUS),
   DB-006 (charset/collation), DB-007 (makemigrations --check), TD-015 (passenger+crontab), TD-016/CB-045 (логи),
   NEW-405 (service-account JSON в webroot).
2. Параллельно (пока SSH недоступен из-за rate-limit) — статические пункты по коду:
   AN-001 (инвентаризация GTM-обвязки в коде), AN-003 (GA4-события: код-слой), AN-010 (Meta Pixel клиент),
   AN-011 (CAPI сервер: retry/токен), AN-012 (дедуп Pixel↔CAPI: event_id), AN-020 (TikTok Pixel клиент+сервер),
   AN-038 (UTM-отчётность админки: код-слой).
3. Пункты, требующие интерфейсов владельца (GTM UI, Meta Events Manager, GA4 DebugView) — закрываются кодовой
   частью с пометкой «остаток — ручной шаг владельца».

## ЖУРНАЛ ДЕЙСТВИЙ

- [07.07 iter6 // setup] Ветка v0/vetala9012-8605-a795bcb5 от origin/main (3951db93). sshpass 1.09 установлен (dnf).
- [07.07 iter6 // SSH #1] Батч подготовлен (/tmp/audit6/remote.sh сандбокса: python-shell блок CRO-051/CRO-050/AN-004/AN-039/AN-032/DB-004/006 + shell блок TD-015/016/CB-045/NEW-405/DB-007). Попытка №1 — `kex_exchange_identification: reset` (rate-limit). Ретрай через 10+ мин.
