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
| A1 | Funnel visualisation: inline mini-funnel + full map, objection-marker regression, data model behind it | `10_FUNNEL_VISUALIZATION_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A2 | Overview tab, console texts, ops alerts, cron/Telegram noise | `11_OVERVIEW_OPS_ALERTS_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A3 | Gemini keys / quota / routing / effort / error taxonomy / latency | `12_GEMINI_ROUTING_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A4 | Prompting, published policy, brand.md, knowledge sources, tone | `13_PROMPTING_KNOWLEDGE_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A5 | Memory & context assembly (what Gemini sees per turn), analysis pipeline | `14_MEMORY_CONTEXT_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A6 | Media: photo, story mention/reply/repost, video, voice; model choice & fallbacks | `15_MEDIA_STORY_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A7 | Commerce: ad referral mapping, catalog/cards, sizing, checkout, payment webhook, Nova Poshta, opt-in, follow-ups, points/UGC | `16_COMMERCE_FOLLOWUP_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A8 | Collaboration/custom/creator flows, spam & prompt-injection handling, manager notifications/handoff/pause | `17_COLLAB_CUSTOM_SAFETY_AUDIT.md` | [ ] | (sub-agent attempt 26.09 aborted — credit limit; redo directly, no subagents) |
| A9 | Real production conversation review (read-only, PII-free summaries) | `18_PROD_CONVERSATION_REVIEW.md` | [!] | Reading customer message bodies on prod was blocked by the session's permission classifier (PII). Needs explicit owner authorisation (or a settings rule) before a session reads transcripts; alternative: owner exports 20–30 anonymised chats. Do NOT bypass. |
| A10 | 2.0 plan item-by-item critique (196 tasks) — keep / change / drop | `19_PLAN_2_0_ITEM_REVIEW.md` | [ ] | |
| A11 | Scenario & hypothesis matrix | `05_SCENARIO_MATRIX.md` | [~] | Draft v1 written 26.09 (≈90 scenarios, 9 groups); Status column to fill after domain audits |
| A12 | Observability / decision-log design for later automated analysis | `80_OBSERVABILITY_AND_DECISION_LOG_DESIGN.md` | [ ] | |
| A13 | Funnel redesign proposal (after A1 + A9) | `20_FUNNEL_REDESIGN_PROPOSAL.md` | [ ] | |
| A14 | Cross-check pass: re-verify top findings, resolve contradictions | `03_FINDINGS_LOG.md` | [ ] | |

## Phase B
- [ ] `90_IMPLEMENTATION_PLAN_3.0.md` — only after A1–A14 are `[x]`
