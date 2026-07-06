# AGENT WORK LOG — Итерация 5 (2026-07-06)

> Продолжение аудита по `twocomms_global_audit.md`. Предыдущие логи: `AGENT_WORK_LOG_iter3.md`, `AGENT_WORK_LOG_iter4.md`.
> Все проверки read-only, код прода не менялся. Секреты в файлы репо НЕ записываются.

**Старт итерации:** 71 невыполненный пункт `[ ]`.

## План итерации

1. **SSH-батч №1 (первый приоритет)** — единое подключение, закрывает хвосты iter3/iter4:
   CRO-051 (baseline воронки), CRO-050-verify (сессия audit-прогона в БД), AN-004 (правила AnalyticsExclusion),
   AN-039 (PII в search), AN-032 (фрагментация utm_source в живой БД), TD-011 (активный CACHE_BACKEND),
   TD-013 (compress manifest), TD-014 (IMAGE_OPTIMIZATION env + размер optimized_cache), TD-015 (passenger/crontab),
   TD-016 (логи и ротация), DB-006 (charset), DB-007 (makemigrations --check), CB-015 (crontab vs команды),
   CB-045 (размеры логов), NEW-405 (service-account JSON на сервере).
2. Параллельно — статические пункты по репо: AN-014, AN-015, AN-050/051 (доформализовать), TD-021, TD-022,
   TD-030/031/032, CB-блок (001–007, 010–016, 020–025, 030–036, 041, 042), SEO-код-пункты, AEO-код-пункты, DB-код-пункты.
3. Пункты, требующие доступов владельца (GTM UI, Meta Events Manager, GA4 DebugView, GSC) — закрывать кодовой частью
   с пометкой «остаток — ручной шаг владельца».

## ЖУРНАЛ ДЕЙСТВИЙ

- [06.07 iter5 // setup] git fast-forward на origin/main (1ff91374), sshpass 1.09 установлен, ветка v0/tiros69593-2273-39381776, пуш в main: `git push origin HEAD:main`.
- [06.07 iter5 // SSH #1] Батч подготовлен (`/tmp/audit/remote_batch.sh` сандбокса, расширен: +crontab, +логи, +logrotate, +env-файлы, +passenger, +makemigrations --check, +charset, +table sizes). Попытка №1 — `kex_exchange_identification: reset`. Ретрай через 10+ мин.
- [06.07 iter5 // AN-014 ✅] Offline-Purchase при доставке РАБОТАЕТ: `update_tracking_statuses` (команда, нужен крон) → `nova_poshta_service.py:430-524` delivered⇒done+paid⇒`_send_facebook_purchase_event` (идемпотентно, `facebook_events.purchase_sent`). `send_event_for_order_status` — 0 call-sites (мёртвый API). P1: только Meta, TikTok/GA4 не получают delivered-Purchase. P2: refund/cancel-событий нет. Статусов в модели 5, не 2.
- [06.07 iter5 // AN-015 ✅] **P1: `/test-analytics/` публичен (urls.py:555, без auth) и через 3s АВТО-стреляет PageView→…→Purchase(599грн) в боевые Meta+TikTok пиксели**; test_event_code только TikTok и только по ?ttq_test=/env; FB CAPI test-код нигде не определён (None). robots не закрывает /test-analytics/ (только noindex в шаблоне).
- [06.07 iter5 // AN-050 ✅] Consent-баннера и Consent Mode v2 нет (grep=0); формализовано с iter4 NEW-401.
- [06.07 iter5 // AN-051 ✅] Политика privacy (`support_content.py:1134-1171`) — общие слова, не упоминает IP/гео/cookies/пиксели/retention/права; UTMSession+UserAction не чистятся никогда (NEW-404).
