# 23 · Финальные проверки плана v1.3–v1.4 · 30.09.2026

Проверка по уточнениям владельца: Gemini без расходов на проверки, полезная и недорогая память, автоматическое заполнение современной карты, причины незакрытых старых задач и ожидание без копипаста. Root использовал пять предметных агентов: Gemini, memory, journey producers, delivery/ops, focused validation; правки вносил только root после сверки исходников. Итог — [план v1.4](90_ПЛАН_РЕАЛИЗАЦИИ_3_0.md), код бота не реализован. Ниже сохранены доказательства v1.3; раздел финальной27-point сверки уточняет последнее решение: **никакой автоматической metadata диагностики тоже**, existing manual tools только по отдельному явному действию, не prerequisite работы.

## Gemini: что подтверждено и что усилено

Context7 `resolve_library_id` и два предметных `query_docs` по `/websites/ai_google_dev_gemini-api` и `/websites/ai_google_dev_api`, затем открыты официальные страницы:

- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits): RPM/input TPM/RPD, quota per project, daily reset midnight Pacific; актуальные лимиты в AI Studio, capacity не гарантируется.
- [Models API](https://ai.google.dev/api/models): `models.list/get` возвращают metadata/support, а не выполняют generation. Документация не утверждает, что успешный GET доказывает generation availability или что для metadata нет никаких ограничений.

Вывод для системы: passive generation evidence от полезного трафика + честная freshness; ручная metadata диагностика остаётся лишь отдельно инициируемой возможностью, а не регулярной обязанностью. Полностью доказать текущую способность генерировать без generation невозможно; отсутствие свежих доказательств не является новой деградацией. Реальные503/timeout/429/auth failures сохраняют соответствующий смысл и scope. Нельзя добиться честной панели простым переименованием всех failures в healthy.

Read-only SSH production, без provider requests и без вывода ключей/project labels:

```json
{"configured_alias_count":6,"explicitly_mapped_alias_count":6,
 "distinct_configured_project_labels":6,"missing_mapping_alias_count":0,
 "provider_requests":0}
```

SSH сам по себе подтвердил только configured labels. **После этой проверки владелец прямо уточнил: все6ключей —6разных Google projects, все бесплатные, у каждого отдельная квота.** Это принято как OWNER evidence; повторная верификация ownership не является открытым gate. Текущие buckets/reservations/quota cooldowns остаются независимыми; объединение допустимо только для действительно общего project, если такая конфигурация когда-либо появится. Численные доступные лимиты/остатки по-прежнему не следуют из metadata200.

Одна дополнительная проверка active user crontab вернула ноль прямых упоминаний `probe_ig_gemini_pool`, `check_ig_gemini_metadata_health`, `--confirm-quota-spend`. Обёртки/внешние schedulers этим не исследованы. Никакие probe commands не запускались.

| Evidence текущего кода | Значение для реализации |
|---|---|
| `bot_views.py:1102–1104,1229`; `gemini_v2_panel.js:755` | Status GET пассивный, кнопка metadata-only уже есть; сохранять, не заменять synthetic generations. |
| `probe_ig_gemini_pool.py:23–29,61–65`; `gemini_probe.py:178–190` | Generation diagnostic только explicit flag, role=diagnostic; это квотный request, не metadata. |
| `check_ig_gemini_metadata_health.py:17–23`; `scripts/install_instagram_periodic_jobs_cron.sh:276`; `ig_task_health.py:69` | Manual-only guard и удаление periodic schedule уже реализованы. |
| `probe_ig_gemini_pool.py:90–103`; `gemini_probe.py:284–333`; `gemini_health.py:899,922,985` | CLI GET обновляет last_probe fields, но snapshot читает metadata ledger; унифицировать observation path. |
| `bot_views.py:1192–1221,1269`; `probe_ig_gemini_pool.py:56,81`; `gemini_metadata_health.py:325` | Разные locks/coordination; shared ownership/rate contract нужен для web+CLI+batch. |
| `gemini_keys.py:1287–1292,686–693`; `call_ai_analysis.py:2323–2326` | Cooldown expiry лишь повторная eligibility; bounded real-request recovery claim и epoch-aware success защищают от stampede/stale recovery. |

P1-7/P2-3 дополнены этими контрактами и mock acceptance. Не обещается «все шесть ключей всегда рабочие», не добавлен новый cron или второй quota ledger.

## Память: полезность, стоимость, накопление

| Подтверждённый пробел | Точечное изменение |
|---|---|
| `bot_memory.py:170–200,227–232`: пересказ последних60 сообщений полностью заменяет summary, старый summary не передаётся | P0-4 разделяет narrative окна и долговечные scoped facts; long-history fixture, source-bound correction; запрет превращать предыдущую summary в новое evidence. |
| `bot_memory.py:236–243`: COUNT/modulo, нет consumed watermark в summary fields (`ig_bot_models.py:545–546`) | Dirty/consumed cursors, один pending job, coalescing, bounded successor/backoff;20events и7→9 проверяют actual calls. |
| `ig_typed_memory.py:272–293,637–650,705`; `ig_bot_models.py:6386`: новый одинаковый вывод растит chain,512-depth отвергает весь result | P5-1 semantic change vs re-observation, bounded lifecycle/recovery, genuine correction после множества повторов; depth/integrity guards не снимаются. |
| `ig_typed_memory.py:450–499,1237–1266`: reader заново читает и проверяет цепочку каждого head | Budget queries/rows/CPU, scoped batch/request-local reuse; cache привязан к head/reset/erasure/keyring. |
| `bot_conversation_analysis.py:46–47,593–605,1017–1020,1087–1096`:160messages/30000chars, возможна обрезка внутри реплики | Actual source/coverage manifest с truncated IDs; omission не становится отрицательным фактом. |

Это CODE findings, не измеренные production нагрузочные инциденты. Новые acceptance scenarios ещё не выполнены. Existing CAS/TTL/erasure/provenance gates v1.2 сохранены.

## Карта: неполнота не равна невыполненной функции

- `ig_journey_snapshot.py:1267–1291` может выдать общий available при semantic edge, хотя transcript trace partial. `ig_journey.js:148–156,231,301` не раскрывает все reasons/refresh states. P2-1 теперь требует отдельную coverage слоёв и короткое объяснение с source/next step.
- `ig_journey.js:544,552` сохраняет server nodes при `showPossible=false`; possible aftercare nodes уже создаются `ig_journey_post_purchase.py:54–60`. Уточнён actual-vs-scenario contract без удаления полноценного aftercare или contextual anchors.
- Scanner `ig_journey_trace_refresh.py:117–136` ориентируется на DB IDs; automatic admission `ig_journey_trace_generation.py:210–217` не проверяет исторический event-time. Новый ID старой импортированной реплики не должен разрешать квотный historical rebuild. Это риск по коду, live incident не подтверждён.
- Deterministic adapters существующих facts не требуют LLM backfill. Новая интерпретация старого разговора требует bounded/versioned rebuild. `ig_journey_trace_generation.py:282–290,382–390` уже ограничивает1–6clients и reuse того же fingerprint; повтор rebuild без новой версии не гарантирует иной artifact.

Показывать раздельно: факт с authority/receipt; интерпретацию по переписке; missing/rejected/truncated источник; pending manual review; возможный сценарий. Наличие узла не означает, что sender уже реализован. Разрыв client336 из21 остаётся partial source case, а не доказанным geometry defect; identity rejected шага не установлена.

## Две красные проверки: причина установлена, обещание требует исправления

Исходный baseline21 остаётся **64 tests:61pass/1skip/1failure/1error**. Новый эксперимент в том же shared runtime и `test_settings_no_network_non_dtf`: **4 tests /0.897s /exit0**, no-network, SQLite in-memory/migrations off. Source fixtures synthetic; Gemini/Meta mocked. Repository code не менялся.

1. Original setup `RevisionLiveTests._prepare`, `provider_failure=True`, настоящий classifier/holding/finalization: общий вопрос о рекомендации получает intent unknown, `provider_safe_reply`, `purpose=normal_reply`, `reply_mode=neutral_ack`, `substantive_obligation=none`. Один mocked send/SENT, revision processed, completed/receipt_finalized; manager execution-debt count0, recovery_due_at=None, scheduler reason recovery_not_expired.
2. Поэтому старый assert blocked в `tests_ig_revision_live.py:998` проверяет другую ветку. Rollback fixture затем подменяет общий `django.utils.timezone.now` значением None (`:1039`), воспроизводя ORM ValueError. Это не неисправность production clock.
3. Runtime-only более строгий guard `patch('management.services.ig_revision_holding._neutral_current_request', return_value=False)` позволяет обоим исходным outage/rollback test methods пройти с сохранёнными permission/publication/send guards. Existing `RevisionRecoveryTests.test_newer_inbound_invalidates_recovery_without_head_hook` также проходит.

Временные runner/log: `/tmp/twc-plan-audit-outage-experiment-20260930.py`, `/tmp/twc-plan-audit-outage-experiment-20260930.log`, evidence line368. Команда:

```bash
TWC_PYTHON="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)/.venv/bin/python"
"$TWC_PYTHON" /tmp/twc-plan-audit-outage-experiment-20260930.py \
  > /tmp/twc-plan-audit-outage-experiment-20260930.log 2>&1
```

Для воспроизведения после очистки tmp: отдельный TransactionTestCase переиспользует setUp/_message/_prepare/_generate/_execute указанного fixture и проверяет результат п.1; subclass исходного fixture с единственным patch из п.3 запускает два исходных метода плюс названный newer-inbound test. Нельзя переносить runtime patch в production или считать его исправлением classifier.

**Отдельный implementation gap:** `ig_revision_holding.py:436–438` обещает вернуться с ответом, но `:459–475` закрывает neutral response без substantive obligation. P7-1.A требует исправления обеих семантических ветвей: настоящий no-action ack без будущего обещания; substantive/ambiguous вопрос с durable continuation/owner/gates. Изменение теста или косметическое удаление обещания не закрывает потерянную потребность клиента. Production occurrence не утверждается.

## Почему прежние остатки различаются

| Пункты | Актуальная классификация и действие |
|---|---|
| B03.18 seen/typing | Принято владельцем повторно30.09 целиком. Отдельная повторная работа/приёмка снята. Исторический `[~]` в22 не новый статус. |
| B03.12 human reply | Реальный implementation остаток: whole-command `send_text/receipt` (`ig_human_reply.py:528–555`) и hash не per-part outbox/private drafts. P1-6 теперь прямо содержит этот остаток, сохраняя рабочую паузу. |
| B03.17/19/20 | Преимущественно scoped acceptance имеющихся burst/receipt/admission механизмов; проверять точный contract, не писать вторые FSM по старому checkbox. |
| B03.21 | Exact-source исторический debt требует owner disposition; blanket close/resend недопустим. |
| B04.6/10 | Реальная capacity/admission работа: shared DB-active cap до увеличения live workers; routing не заменяет fairness. |
| Native consent/repost capabilities | Capability gate аккаунта; possible узел не делает sender доступным. Независимые read-only adapters не блокируются. |
| B17.1 | Намеренно отложенный structural refactor после consumers; не срочная перепись монолита. |
| Telegram actionable-only | Уже реализованная политика сохраняется. Новые alerts только под доказанный actionable gap. |

## Граница результата

Проверка v1.3:27уникальных задач,20прямых dependency edges,0циклов;196/196crosswalk с неизменными историческими статусами;30будущих scenarios без повторов; owner map appendix сохранён дословно; относительные ссылки/fences валидны, `git diff --check` без замечаний. Финальная v1.4 добавила4scenario, уточнения ниже. Это проверка согласованности плана, не acceptance будущего кода.

Правки только в docs3.0. Ни runtime flags, ни production data, ни сообщения клиентам не менялись. Ноль real Gemini generations/diagnostic probes; SSH читал только агрегаты конфигурации и признаки cron. Полный корпус переписок, независимая Google-side mapping verification, нагрузочный production тест и disposable MariaDB не заявлены; six-project topology/free tier подтверждены владельцем. План стал конкретнее; реализация и её acceptance остаются следующей сессии.

## Финальная сверка каждого паспорта · v1.4

База сравнения: исходная3.0 до текущего аудита (`/tmp/twc-plan-audit-original-90.md`,1909строк), снимок нашейv1.3 (`/tmp/twc-plan-audit-v1.3-90.md`), current code и source contracts2.0 через22. Временные снимки не runtime dependencies следующей реализации; решения и причины сохранены здесь. Четыре read-only review packages покрыли все27ID без пропусков; пятый агент проверил timing tests. Root сверил изменения и узкие activation gates. Verdict относится к качеству контракта, не к готовности ещё ненаписанного кода.

| ID | Сохранённая цель / результат сравнения |
|---|---|
| P0-1 | Общий live context сохранён; вместо слепого legacy copy/понижения complex при30% — existing router, sealed boundary/CAS и shadow без API. |
| P0-2 | Понятная карточка сохранена; existing authoritative facts вместо второго mutable state, без безусловной власти manager note/обрезки600символов. |
| P0-3 | Видимость actual prompt улучшена: captured manifest отдельно от hypothetical preview и policy-only hash. |
| P0-4 | Закрывает пропущенный producer/reset race; coalescing и долговечность не создают обратной зависимости на новые typed keys P5-1. |
| P1-1 | Reviewed brand publication сохранена. После сверки явно указан runtime target: existing public_policy modules; knowledge resolver не считается обновлённым автоматически и не получает конфликтующий duplicate fact. |
| P1-2 | Core parity сохранена через existing SHA/CAS/epochs; исторические89символов не навязывают текущий diff. |
| P1-3 | Scenario modules сохранены, existing mandatory blocks не переписываются. Retag текущих modules не ждёт brand importer. |
| P1-4 | Исправление episode follow-up сохраняет one-per-intent/client caps и existing FSM вместо второго state/лишних напоминаний. |
| P1-5 | Webhook-first/reconcile сохранены, существующий settlement не заменён; timestamp не idempotency, спасибо не обязательный send. |
| P1-6 | Работающая pause/resume сохранена; human outbox/private drafts — настоящий B03.12остаток, human permission не блокируется bot pause. |
| P1-7 | Existing smart chains/admission/8-2-1budgets/role reserves сохранены. **Исправлена наша перегрузка v1.3:** никаких automatic health I/O/preflight, optional diagnostics/half-open maintenance не блокируют core admission release.6projects независимы. |
| P2-1 | Современная карта/составные блоки/aftercare сохранены; конкретные producer/source gaps, независимая coverage и truthful possible/actual вместо старого redesign. |
| P2-2 | Console filters/new/pause/details сохранены; добавлены scope/cursor/privacy, возвращены useful filter persistence/freshness из original. |
| P2-3 | Existing Overview сохранён без unlimited quota/ложного global red. Passive UI0I/O; отсутствие metadata history не требует проверки и не блокирует панель. |
| P2-4 | Explainability сохранена через единые existing identities; устранены две несовместимые log-схемы и ложное SENT после generation. |
| P2-5 | Story quality сохранена с negative/unknown guards. После сверки возвращены явные content kinds и разные evidence-safe реакции; без второй generation только для taxonomy. |
| P2-6 | Privacy/удаление сохранены на actual filesystem/leases60d вместо выдуманных S3/сроков. Возвращён useful UI actual deletion_due/policy/state. |
| P2-7 | Collaboration/custom/prize intents и manager flow сохранены; source/brief/mockup gates используют существующие cases и узкие зависимости. |
| P2-8 | UI«почему»/report сохранены как consumerP2-4, без второго store и присвоения delivery от текста LLM. |
| P3-9 | Useful alerts сохранены; убраны произвольные timeout/queue alarm и autorestart.429одного проекта с допустимым fallback не запускает alert/probe сам по себе. |
| P3-10 | Mobile/dark/a11y цель сохранена, проверяется нынешний polished DOM, старые вкладки/theme не возвращаются. |
| P4-1 | Актуальность catalogue/demand сохранена через live resolvers, не Markdownstock;201+coverage и exact selected item проверяются. |
| P4-2 | Hosted checkout/offer/payment/gift/amendment contracts2.0 сохранены, без сбора банковских реквизитов в Direct/ложного zero-total payment. |
| P4-3 |3purpose consent и verified channel сохранены; unsupported native capability не блокирует разрешённый independent/in-window путь. |
| P4-4 | Existing service/UGC/reward/D051 сохранены, externalUGC не требует ложного заказа; graph не создаёт award. |
| P5-1 | Typed consumers/materiality теперь исполнимы: TTL/integrity/source, chain lifecycle/read cost, governed learning; logs не authority. |
| P7-1 | Baseline/rollout/rollback сохранены и уточнены. No-repeat/fallback — ранний gate, seen/typing приняты; affected subset не требует всех будущих функций до первого release. |

Ключевые дополнительные code anchors: `gemini_keys.py:252–323` role reserves; `ig_policy_publication.py:198–252` instruction-only snapshot; `bot_knowledge.py:64–79` и `approved_public_facts.py:267–272` separate knowledge source. `ig_revision_conversation_context.py:8–89` и `ig_revision_live.py:461–465` показывают существующую timing protection. `ig_revision_holding.py:376–378` дедуплицирует delivery внутри recovery lineage, но не доказывает suppression между самостоятельными inbound revisions одного ожидания. Existing economic-root/budget guards не заменяются новым retry framework.

### Проверка точной фразы и граница результата

Shared runtime, `test_settings_no_network_non_dtf`, mocked provider/transport: **11 tests /0.971s /exit0** —9existing timing unit/integration tests и2runtime-only fixtures. На ordinary revision с `response_delay_apology_supported=False`:

| Draft | Normalizer → persisted proposal → mocked send |
|---|---|
| «Извините за долгое ожидание. Вот информация о товаре.» | Сохраняется целиком: подтверждённый coverage gap |
| «Извините за задержку. Вот информация о товаре.» | Остаётся «Вот информация о товаре.»: existing guard действует |

Временные `/tmp/twc-plan-audit-timing-experiment-20260930.py` и `.log` (evidence376/378) не меняют repo. Запуск shared `.venv/bin/python` этого runner; в нём DiscoverRunner запускает `management.tests_ig_revision_conversation_context.RevisionConversationContextTests`, `RevisionConversationContextIntegrationTests` и два derived fixtures. Для воспроизведения derived fixture использует existing `_prepare/_execute`, задаёт parsed `reply_text` указанной фразой, проверяет one mocked generation/send и тексты normalize/proposal/effect. Для длинного ожидания ожидается именно сохранение фразы — зелёный repro не означает исправленный продукт.

Это **не** production-воспроизведение повторяющегося спама и **не** проверка последовательности нескольких turns. Sequence fixtures R30-S32 остаются обязательной будущей приёмкой. P7-1.A требует0обязательных holding, максимум одного полезного уведомления за продолжающееся ожидание, сохранения substantive debt и допустимого полезного ответа. Не предлагается broad regex, подавляющий все извинения/одинаковые бизнес-факты, или новый Gemini call для проверки/перефразирования ответа.

### Решение о готовности плана

Известные неоднозначности текущего документа устранены: runtime publication target, узкие dependencies, passive health0I/O, no-repeat contract и недостающие useful UX детали. План готов к поэтапной реализации. Обязательные local/MariaDB/quality/browser/release gates остаются частью реализации, а не обещанием, что ещё ненаписанный код уже прошёл их. External capability/read-only source gaps имеют явные границы; они не разрешают fabricated facts и не блокируют независимую работу. Следующая сессия начинает с90/23 и P7-1.A; runtime не менять по историческим предположениям из старых аудитов.

Финальный Markdown check v1.4:27/27паспортов имеют отдельный verdict;27unique task IDs/20direct edges/0cycles;196/196исторических задач и их31x/66partial/94open/5blocked сохранены;34future scenario без повторов. Owner map appendix byte-for-byte неизменен, relative links/fences валидны, `git diff --check` чистый. Новых code/runtime изменений эта проверка не делала.

## Позднее owner уточнение: apology — решение по контексту, не возврат шаблона

Владелец уточнил, что извинение за долгое ожидание уместно лишь при действительно длительном ожидании важного ответа, например по оплате/серьёзному вопросу: ориентир5–6часов или18–20часов, с учётом ночи. Обычное ожидание менеджера/вопрос о работе не требует такого извинения. Оно должно быть осмысленным решением основного prompt, а не автоматически вставленной фразой.

Root повторно прочитал actual timing code: `response_delay_apology_allowed` (`ig_revision_conversation_context.py:35–51`) допускает apology по complaint/support-delay pattern или recovery lineage, **не проверяя elapsed time/importance**. Поэтому прежний phrase experiment выявляет только симптом; расширение regex недостаточно. `ig_turn_intent.py:465–471` и `bot_followups.py:39–40` используют разные quiet boundaries; их нельзя молча считать одной бизнес-SLA.

В90 добавленI-16, input contractP0-1 и подробная policyP7-1.A: default omit; configurable starting threshold6неночных часов как консервативная реализация ориентира владельца; unresolved important source/waiting_on/actual timestamps/quiet policy/previous apology receipts; model eligibility не обязанность; никакой автоподстановки, отдельного apology send или дополнительной generation. Night exclusion не меняет окно отправки и не откладывает обычный ответ до утра. Если timing/importance неизвестны — полезный ответ без delay claim. Вакансия/обычное ожидание менеджера не превращаются в срочный customer debt; отдельно сохраняется право извиниться за реальную ошибку товара/сервиса, не за вымышленное долгое ожидание.

Added R30-S35:35future scenarios всего. Новые policy/sequence tests не запускались и код не менялся; прежние11/11остаются проверкой старого поведения. При реализации обновить старые expectations, где одного recovery flag достаточно для apology, не меняя delivery fences. Это уточнение семантики внутри существующих паспортов, новых task IDs/dependencies не добавлено.
