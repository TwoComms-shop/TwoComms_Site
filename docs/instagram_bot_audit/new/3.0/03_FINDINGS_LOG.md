# 03 · Findings log (index)

Every finding has an ID `F3-<AREA>-NN`, lives in its domain file, and is indexed here (one line). Area prefixes:

| Prefix | Area | Domain file |
|---|---|---|
| FUN | Funnel visualisation (inline + full map) & journey data | 10_FUNNEL_VISUALIZATION_AUDIT.md |
| OPS | Overview tab, console, alerts, cron, Telegram noise, server load | 11_OVERVIEW_OPS_ALERTS_AUDIT.md |
| GEM | Gemini keys/quota/routing/effort/errors/latency | 12_GEMINI_ROUTING_AUDIT.md |
| PRM | Prompting, published policy, brand.md, knowledge | 13_PROMPTING_KNOWLEDGE_AUDIT.md |
| MEM | Memory, context assembly, analysis pipeline | 14_MEMORY_CONTEXT_AUDIT.md |
| MED | Media, stories, video, voice | 15_MEDIA_STORY_AUDIT.md |
| COM | Commerce, referral, catalog, sizing, checkout, payment, delivery, opt-in, follow-ups, points | 16_COMMERCE_FOLLOWUP_AUDIT.md |
| COL | Collaboration, custom, creator, spam/injection, handoff/pause | 17_COLLAB_CUSTOM_SAFETY_AUDIT.md |
| CNV | Real production conversation review | 18_PROD_CONVERSATION_REVIEW.md |
| PLN | 2.0 plan item critique | 19_PLAN_2_0_ITEM_REVIEW.md |
| X | Cross-cutting (root agent) | this file |

Severity: P0 lost reply / wrong money-order fact / harmful · P1 major conversion or admin-visibility loss · P2 quality/UX/efficiency · P3 polish.

## Cross-cutting findings (root)

### F3-X-01 · Two diverging copies of the plan and its evidence — P2 · DOC
- **Observed:** the committed 2.0 plan is v2.5 (14.09); the main checkout has uncommitted v2.7 (26.09) plus untracked `01_IMPLEMENTATION.md`, `02_SOURCE_COVERAGE.md`, `04_GRILL_ME_300_QUESTIONS.md`, `05_OWNER_DECISIONS.md`, `06–11_*`, `evidence_d085/`. None of this is in Git, so it is one `git clean`/disk failure away from being lost, and worktrees/other agents see a stale plan.
- **Recommendation:** Phase B must commit the 2.0 evidence set (after privacy scan) or copy the needed parts into 3.0; 3.0 itself is committed on its branch.

### F3-X-02 · Production SSH password was pasted into the task text — P1 · security hygiene
- **Observed:** the brief contains a literal `sshpass -p '<password>'` command. AGENTS.md forbids literal passwords; the Keychain loader works (verified 26.09).
- **Recommendation:** rotate the hosting password; keep using the Keychain loader; never persist the literal in docs/memory.

## Index (filled as domain files land)

| ID | Sev | Title | File |
|---|---|---|---|
| F3-X-01 | P2 | Plan/evidence copies diverge; 2.0 evidence untracked | 03 |
| F3-X-02 | P1 | SSH password in chat; rotate | 03 |
