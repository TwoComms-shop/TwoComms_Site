# 3.0 · Instagram bot — audit → new implementation plan

**Start date:** 2026-09-26 · **Code/prod base:** `c69925d566ccbe2109953644e1d1187ecd38260c` (prod HEAD verified by SSH 26.09 22:52 local)
**Branch:** `worktree-ig-bot-audit-3` (docs only; no product code changed in phase A)

> **Location note (26.09, updated):** the files are edited in the worktree `.claude/worktrees/ig-bot-audit-3/docs/instagram_bot_audit/new/3.0/` (for Git) and **mirrored after every step** into the owner's local checkout `/Users/zainllw0w/TwoComms/site/docs/instagram_bot_audit/new/3.0/` (real folder, not a symlink) with `rsync -a <worktree>/3.0/ <local>/3.0/`. **No subagents** (owner's token limits): audit sequentially, write each finding immediately.

## Why this folder exists

The owner asked (26.09) for a full re-audit of the Instagram sales bot (management subdomain) and a *new* prioritised implementation plan that supersedes `../2.0/03_IMPLEMENTATION_PLAN_FINAL.md` (196 tasks B01–B17, D061–D085 journal). The owner's full brief is preserved verbatim-in-meaning in [01_OWNER_BRIEF_2026-09-26.md](01_OWNER_BRIEF_2026-09-26.md). **Read it before designing anything.**

Two phases, strictly ordered (owner's instruction):

1. **Phase A — audit & findings (current).** Verify everything: code, production data (read-only), prompts, funnel UI, Gemini routing, memory, media, follow-ups. Every discovery is written immediately to a findings file. No prioritisation yet.
2. **Phase B — implementation plan.** Only after Phase A is complete and re-checked: write `90_IMPLEMENTATION_PLAN_3.0.md` with checkboxes, priorities, tests, gates, rollback, owner-confirmation points.

## Resume protocol (for any agent / model after context loss)

1. Read this file, then [02_AUDIT_PROGRESS.md](02_AUDIT_PROGRESS.md) — it says which audit areas are done, in progress, or not started.
2. Read [03_FINDINGS_LOG.md](03_FINDINGS_LOG.md) (index of all findings, IDs `F3-xxx`) and the domain files it links.
3. Read [04_OWNER_QUESTIONS.md](04_OWNER_QUESTIONS.md) — open questions for the owner; do not guess answers to those, design around them.
4. Continue the first area in 02 that is not `[x]`. When you finish something, tick it in 02 **with the evidence pointer**, add findings to the domain file, index them in 03.
5. Never re-do a `[x]` area unless code/prod changed after its date (check `git log` on the listed files).

## Source hierarchy (what to trust)

| Source | Location | Trust |
|---|---|---|
| Owner brief 26.09 | `01_OWNER_BRIEF_2026-09-26.md` | Highest for intent |
| Owner decisions (Grill-me) | main checkout `docs/instagram_bot_audit/new/2.0/04_GRILL_ME_300_QUESTIONS.md`, `05_OWNER_DECISIONS.md` (**untracked, only in `/Users/zainllw0w/TwoComms/site`**) | High, but dated 07–09.09; later owner statements override |
| 2.0 plan (latest) | main checkout `.../2.0/03_IMPLEMENTATION_PLAN_FINAL.md` v2.7 (**uncommitted edits — the worktree copy is older v2.5**) | Requirements + journal |
| D085 deep review | main checkout `.../2.0/11_DEEP_REVIEW_HANDOFF_2026-09-26.md` + `evidence_d085/` | Evidence base 26.09 (untracked) |
| Code | this worktree (== prod HEAD) | Truth for behaviour |
| Production | SSH read-only (see below) | Truth for data/runtime |

⚠️ The main checkout has uncommitted edits to `twocomms/management/bot_knowledge/brand.md`, `templates/management/bot.html`, two test files. Those are the owner's latest local state; compare, never overwrite.

## Production access (read-only in Phase A)

- Credential: Keychain via `~/.config/twocomms/deploy-env.zsh` → `sshpass -e`. **Never** paste the password anywhere.
- Helper used in this audit: `$CLAUDE_JOB_DIR/tmp/ssh_run.sh <script.sh>` pipes a local script to `bash -l -s` on `qlknpodo@195.191.25.63`.
- Django shell on prod: `cd /home/qlknpodo/TWC/TwoComms_Site/twocomms && source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate && DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings python manage.py shell < probe.py`. First statement inside: `SET SESSION TRANSACTION READ ONLY`.
- Model locations: `InstagramBotMessage`, `InstagramBotLog` → `management/models.py`; `IgClient`, `IgObjection`, `IgClientStageEvent` … → `management/ig_bot_models.py`; journey trace → `management/ig_journey_models.py`.
- **Never** in docs: customer names, usernames, message bodies, phone numbers, URLs with tokens. Use `IgClient.id` and paraphrased summaries.

## File map

| File | Purpose |
|---|---|
| 01_OWNER_BRIEF_2026-09-26.md | Structured restatement of the owner brief (requirements R-xx) |
| 02_AUDIT_PROGRESS.md | Checklist of audit areas with status + evidence |
| 03_FINDINGS_LOG.md | Index of all findings F3-xxx with severity and domain file |
| 04_OWNER_QUESTIONS.md | Questions only the owner can answer (Q-xx) |
| 05_SCENARIO_MATRIX.md | Customer/partner scenarios & hypotheses ("what if…") with expected behaviour |
| 10_… – 29_… | Domain audit files (funnel UI, Gemini, prompts, memory, media, commerce, follow-ups, ops) |
| 80_OBSERVABILITY_AND_DECISION_LOG_DESIGN.md | Design of the audit-trail/logging system for later automated analysis |
| 90_IMPLEMENTATION_PLAN_3.0.md | Phase B output (not yet written) |
