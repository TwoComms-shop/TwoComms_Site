# 02 · Audit progress (Phase A)

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done (with evidence pointer) · `[!]` blocked (reason)

Update this file the moment an area changes state. One line of evidence per tick.

## Setup
- [x] Worktree `worktree-ig-bot-audit-3` created; prod HEAD = `c69925d56` = local HEAD (SSH 26.09 22:52). Prod host: 20 CPU, 128 GB RAM shared, load avg ~5–6.
- [x] Owner brief structured → `01_OWNER_BRIEF_2026-09-26.md` (R-01…R-50)
- [x] Read 2.0 plan head (D085/D084/D082-G) and D085 handoff §1–10

## Audit areas (each writes its own domain file)

| # | Area | File | Status | Evidence / notes |
|---|---|---|---|---|
| A1 | Funnel visualisation: inline mini-funnel + full map, objection-marker regression, data model behind it | `10_FUNNEL_VISUALIZATION_AUDIT.md` | [x] | Done 26.09 directly. 5 findings F3-FUN-01…05: empty objection nodes hidden since `ac62e2da3` (14.09); parallel-edge grouping hides per-outcome detail; bypass `via_objection` edges are cosmetic |
| A2 | Overview tab, console texts, ops alerts, cron/Telegram noise | `11_OVERVIEW_OPS_ALERTS_AUDIT.md` | [x] | Done 26.09 directly. 6 findings F3-OPS-01…06: "У черзі" mixes runnable with preserved evidence; "Модель" is a last-call sample; console noise; alert contract narrow; hourly-only stall detection; cron still poll-driven where webhooks exist |
| A3 | Gemini routing: task classes, model chains, quota tracking, health probes | `12_GEMINI_ROUTING_QUOTA_AUDIT.md` | [x] | Done 27.09 directly. **P0 F3-GEM-01:** revision lane bypasses rich routing — objections/product/fit/custom never set, only image/audio. Also: quota invisible (F3-GEM-02), health narrow (F3-GEM-03), pin no UI (F3-GEM-04), escalation opaque (F3-GEM-05), deadline not verified (F3-GEM-06) |
| A4 | Prompting, published policy, brand.md, knowledge sources, tone | `13_PROMPTING_KNOWLEDGE_AUDIT.md` | [x] | Done 27.09 directly. **F3-PRM-01:** brand.md claims the bot reads it — it does NOT (`bot_knowledge.py` reads only `approved_public_facts`). F3-PRM-02: prod core is 89 chars behind canonical (`1728ccc2f`), same v3 label. F3-PRM-03: tone split 3 places. F3-PRM-04: instruction selection budget-truncated, collaboration/ugc tags select nothing |
| A5 | Memory & context assembly (what Gemini sees per turn), analysis pipeline | `14_MEMORY_CONTEXT_AUDIT.md` | [x] | Done 27.09 directly. **P0 F3-MEM-01:** live revision lane passes NO memory/client-context to the prompt (legacy path did) — breaks R-07/R-28/R-29. F3-MEM-02: admin prompt preview shows a different prompt than production. F3-MEM-03: two memory systems, neither live |
| A6 | Media: photo, story mention/reply/repost, video, voice; model choice & fallbacks | `15_МЕДИА_СТОРИС_АУДИТ.md` | [x] | Done 27.09 directly. F3-MED-01: UGC taxonomy missing (unboxing/wearing/custom/review not distinguished). F3-MED-02: video >8 MiB falls out of analysis. F3-MED-03: photo→product matching has no "unsure→cards" threshold. F3-MED-04: voice transcription works but no playbook rules. F3-MED-05: 31 attachment_limit + 16 signature rejections unresolved |
| A7 | Commerce: ad referral mapping, catalog/cards, sizing, checkout, payment webhook, Nova Poshta, opt-in, follow-ups, points/UGC | `16_КОММЕРЦИЯ_АУДИТ.md` | [x] | Done 27.09 directly. F3-COM-01: ad context doesn't reach model (see F3-MEM-01). F3-COM-02: ad mapping fully manual, silent degradation. F3-COM-03: payment webhook→bot reaction not confirmed as event-driven. F3-COM-04: follow-up suppressed by old deal (D085-F1 reproduced). F3-COM-05: Direct points not implemented. F3-COM-06: native marketing opt-in not confirmed. F3-COM-07: catalog candidates 200 cap. F3-COM-08: thresholds in prompt not typed facts |
| A8 | Collaboration/custom/creator flows, spam & prompt-injection handling, manager notifications/handoff/pause | `17_СПІВПРАЦЯ_КАСТОМ_АУДИТ.md` | [x] | Done 27.09 directly. F3-COL-01: manager pause not confirmed as explicit mechanism. F3-COL-02: custom DTF not implemented as separate episode. F3-COL-03: collaboration/partnership not distinguished category. F3-COL-04: decision timeline (R-48) not confirmed. F3-COL-05: console doesn't show "why COMPLEX" |
| A9 | Real production conversation review (read-only, PII-free summaries) | `18_PROD_CONVERSATION_REVIEW.md` | [!] | Reading customer message bodies on prod was blocked by the session's permission classifier (PII). Needs explicit owner authorisation (or a settings rule) before a session reads transcripts; alternative: owner exports 20–30 anonymised chats. Do NOT bypass. |
| A10 | 2.0 plan item-by-item critique (196 tasks) — keep / change / drop | `19_PLAN_2_0_ITEM_REVIEW.md` | [ ] | |
| A11 | Scenario & hypothesis matrix | `05_SCENARIO_MATRIX.md` | [~] | Draft v1 written 26.09 (≈90 scenarios, 9 groups); Status column to fill after domain audits |
| A12 | Observability / decision-log design for later automated analysis | `80_OBSERVABILITY_AND_DECISION_LOG_DESIGN.md` | [ ] | |
| A13 | Funnel redesign proposal (after A1 + A9) | `20_FUNNEL_REDESIGN_PROPOSAL.md` | [ ] | |
| A14 | Cross-check pass: re-verify top findings, resolve contradictions | `03_FINDINGS_LOG.md` | [ ] | |

## Phase B
- [x] `90_ПЛАН_РЕАЛИЗАЦІЇ_3_0.md` — Done 27.09. Comprehensive implementation plan with detailed solutions for all P0/P1/P2/P3 findings; preserves positive elements from 2.0 plan; fixes identified issues; includes root-cause analysis, technical approach, testing strategy, and rollback plans for each task
