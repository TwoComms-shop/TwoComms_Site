# Shared brief for audit sub-agents (Phase A)

You are auditing one area of the TwoComms Instagram sales bot ("Соломія", management subdomain, Django 6.1). Output = one Markdown domain file in `docs/instagram_bot_audit/new/3.0/` (absolute: `/Users/zainllw0w/TwoComms/site/.claude/worktrees/ig-bot-audit-3/docs/instagram_bot_audit/new/3.0/`). **Do not edit any code, template, migration, or any other doc.** Only create/overwrite your own domain file.

## Read first
1. `3.0/01_OWNER_BRIEF_2026-09-26.md` — owner requirements R-01…R-50 (intent is king).
2. Your area's sections of the latest 2.0 plan: `/Users/zainllw0w/TwoComms/site/docs/instagram_bot_audit/new/2.0/03_IMPLEMENTATION_PLAN_FINAL.md` (v2.7, main checkout — NOT the older worktree copy). Grep by block ID (B01…B17, C01…C19, D0xx).
3. D085 evidence: `/Users/zainllw0w/TwoComms/site/docs/instagram_bot_audit/new/2.0/11_DEEP_REVIEW_HANDOFF_2026-09-26.md` (§2–10 overview, §11 per-task passports — grep `B04.1` etc.).
4. Owner decisions from interview: `/Users/zainllw0w/TwoComms/site/docs/instagram_bot_audit/new/2.0/05_OWNER_DECISIONS.md` (huge; grep keywords) and `04_GRILL_ME_300_QUESTIONS.md`.
5. Code: `/Users/zainllw0w/TwoComms/site/.claude/worktrees/ig-bot-audit-3/twocomms/management/` (== production HEAD `c69925d56`). Services in `services/`, models `ig_bot_models.py`, `models.py` (InstagramBotMessage/InstagramBotLog), `ig_journey_models.py`; UI `templates/management/bot.html`, `static/management/*.js|css`; views `bot_views.py`.
   The main checkout `/Users/zainllw0w/TwoComms/site/twocomms/management/` has *uncommitted* owner edits to `bot_knowledge/brand.md`, `templates/management/bot.html` and two tests — diff against them when relevant.

## Production (read-only, optional, bounded)
- Helper: write a bash script file, then run `zsh /Users/zainllw0w/.claude/jobs/12068485/tmp/ssh_run.sh /path/to/script.sh`. It pipes the script to `bash -l -s` on prod. (Plain one-shot commands containing the word `git` in your local Bash are blocked by a worktree guard — put them inside the script file.)
- Django shell pattern inside the script:
  ```
  cd /home/qlknpodo/TWC/TwoComms_Site/twocomms
  source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate
  export DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings
  python manage.py shell <<'PY'
  from django.db import connection
  with connection.cursor() as c:
      c.execute("SET SESSION TRANSACTION READ ONLY"); c.execute("SET SESSION max_statement_time=5")
  ...
  PY
  ```
- HARD RULES: SELECT-only; always LIMIT; no management commands that write; no Gemini/Meta/Telegram/Nova Poshta/Monobank API calls; no flag changes; no restarts; at most ~12 probes; one at a time. MariaDB account limit is 20 connections shared with the live site.
- Never print or save secrets (env values, tokens, API keys). Redact.

## Privacy in the output
No customer names, usernames, phone numbers, addresses, raw message bodies, media URLs. Refer to `IgClient.id=N` and paraphrase ("customer asked about hoodie size L availability"). Short quotes of *bot* output are OK if they contain no PII.

## What a good finding looks like
Use IDs with your area prefix (given in your task), e.g. `F3-GEM-07`. Each finding:
```
### F3-XXX-NN · <short title>  — severity P0/P1/P2/P3 · evidence CODE|PROD|REPRO|DOC|IDEA
- **Observed:** what is true now (file:line, query result, screenshot path).
- **Why it matters:** impact on customer / conversion / admin visibility / cost / stability; link owner requirement R-xx.
- **Root cause / mechanism:** (if known; say "unknown" otherwise).
- **Recommendation:** concrete change; alternatives considered + why chosen; how to test (meaningful test, not test-for-test); risk of breaking existing behaviour and how to avoid it.
- **Relation to 2.0 plan:** B-ID(s) — already planned / planned wrongly / not planned.
```
Severity: P0 = customer loses reply / wrong money/order fact / bot says something harmful; P1 = significant conversion or admin-visibility loss; P2 = quality/UX/efficiency; P3 = polish.

Evidence strength matters: distinguish "code says" vs "prod shows" vs "idea". If unsure — verify (read code deeper, bounded prod probe, or run a local test using the shared venv per AGENTS.md: `TWC_PYTHON=/Users/zainllw0w/TwoComms/site/.venv/bin/python`, env vars `DEBUG=1 SECRET_KEY=x IG_UGC_IDENTITY_HMAC_KEYRING='{"v1":"0123456789abcdef0123456789abcdef"}' IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID=v1 IG_PRIVATE_MEDIA_ROOT=<abs 0700 dir outside checkout>`, run from `twocomms/` with `--settings=test_settings`). If you cannot verify, write it as a conditional: "test X; if Y then A else B".

## Domain file structure
1. Header: area, date, code SHA, what you read (files), what prod probes you ran (description only).
2. **Current mechanism** — how it actually works now (concise but precise, with file:line).
3. **What works well (keep / do not break)** — invariants a future implementer must preserve.
4. **Findings** (format above), sorted by severity.
5. **2.0 plan items in this area** — for each relevant B-task: status reality check (done/partial/not started/never called), is the plan item correct, keep/change/drop, better design.
6. **Ideas / improvements** beyond the brief (brainstorm many, keep the good ones, say why).
7. **Hypotheses / what-if scenarios** relevant to this area with expected correct behaviour.
8. **Owner questions** (`Q-XXX-NN`) — only things the owner must decide/know (business rules, prices, policies).
9. **Proposed tests / acceptance gates** for the future plan.

Be thorough and concrete; the owner wants "molecular" depth. But no padding: every line should be useful to the person who writes the implementation plan. Finish with a short summary (top 5 findings) in your final reply to the caller.
