# docs/qa — handoff for the **fix agent**

**Read this file first.** Then open the master findings checklist. Do **not** re-audit the whole site unless a finding says to re-verify after fix.

**Date pack:** 2026-07-09  
**Main site:** `https://twocomms.shop`  
**Management IG bot:** management subdomain (separate from storefront ads gate)  
**Ads launch gate (storefront):** **BLOCKED** until storefront P0 attribution/pixel fixed.

---

## 1. What to open, in order

| # | File | When to read |
|---|------|----------------|
| **1** | **This README** | Always first |
| **2** | [`AUDIT_FINDINGS_2026-07-09.md`](./AUDIT_FINDINGS_2026-07-09.md) | **Master work list** — every finding ID, fix checkbox, severity, and “Detail in …” pointer |
| **3** | [`PLAN_VS_FINDINGS_2026-07-09.md`](./PLAN_VS_FINDINGS_2026-07-09.md) | If fixing something marked DONE wrongly in the implementation plan (W2-1, W2-7, ADS-*, false `[x]`) |
| **4** | [`IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`](./IG_BOT_MANAGEMENT_BUGS_2026-07-09.md) | If fixing Instagram bot on **management** (hide, stats UA, Message Requests, transfer) |
| **5** | [`PRE_ADS_MASTER_AUDIT_CHECKLIST.md`](./PRE_ADS_MASTER_AUDIT_CHECKLIST.md) | Optional: original walk checklist (already fully walked Pass A). Use for re-test IDs only |
| **6** | `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` | Code fix waves W0–W7 / ADS / SERVER / OWNER; many `[x]` reopened with `RE-VERIFY` notes |

**Template only (ignore for fixes):** `AUDIT_FINDINGS_TEMPLATE.md`

---

## 2. Map: topic → MD file → finding IDs

### A. Storefront — attribution / ROAS (highest priority)

| Topic | Primary IDs | Detail docs | Code (start here) |
|-------|-------------|-------------|-------------------|
| Order UTM always empty | F-021, F-033, F-071, F-045 | Findings detail sections + PLAN_VS **W2-1** | `storefront/utm_tracking.py` `link_order_to_utm`; mono+checkout create paths |
| is_converted dead | F-019 | PLAN_VS **W2-2** | `utm_tracking.mark_as_converted` / purchase path |
| purchase UserAction undercount | F-083 | PLAN_VS **W2-3** | mono `_apply_monobank_status` vs utils path |
| Dual mono webhook path / CAPI | (plan W2-7 reopen) | PLAN_VS §W2-7 dual path | `monobank.py` `_apply_monobank_status` vs `utils._record_monobank_status_locked` |
| session_key gaps | F-044, F-068, F-073, F-074 | Findings + PLAN_VS | checkout COD ensure session; historical prepay |
| Dirty utm_source live | F-020, F-057, F-084 | PLAN_VS **W2-8** nuance | `utm_middleware` / `utm_utils` |
| CheckoutCapture.converted | F-075 | PLAN_VS W3-11 / mono | mono missing capture update |

### B. Storefront — pixel / ads chrome

| Topic | IDs | Detail | Code |
|-------|-----|--------|------|
| BFCache pixel crash | F-030, F-079 | PLAN_VS **ADS-1** | `analytics-loader.js` `initializePixelsImmediately` |
| Early PageView OK but incomplete ADS-1 | F-042 PASS; ADS-1 partial | PLAN_VS ADS-1 | `base.html` + loader |
| FACEBOOK_PIXEL_ID empty settings | F-089 | PLAN_VS | env + settings |

### C. Storefront — SEO

| Topic | IDs | Detail | Code / data |
|-------|-----|--------|-------------|
| Category titles cut mid-word | F-001, F-023 | PLAN_VS **ADS-3** (DB not code-only) | MySQL `Category.seo_title*`; `seo_utils.TITLE_LIMIT` |
| Color landing grammar | F-002, F-006 | Findings §F-002 | color landing templates/SEO generators |
| Product title ≠ H1 | F-004, F-094 | Findings | product SEO fields |
| RU/EN H1 Ukrainian | F-005 | PLAN_VS ADS-2 | home/catalog templates + locales |
| Feed g:link | F-003, F-027, F-077 | F-077 revised product links often OK | feed generators |
| help-center / kontakty 404 | F-043, F-078 | Findings | redirects |
| Empty image alts | F-059 | Findings | ProductImage.alt_text |

### D. Storefront — cart / NP / security / ops

| Topic | IDs | Detail | Code |
|-------|-----|--------|------|
| NP Latin Kyiv 502 | F-050 | Findings | NP city API |
| ubd_docs public | F-087 | PLAN_VS W1-11 | media serving |
| TG webhook secret empty | F-088 | PLAN_VS W3-9 | env |
| MySQL backup cron | F-090 | PLAN_VS W0-3 | `scripts/backup_mysql.sh` + crontab |
| deploy_paramiko password in git | F-093 | PLAN_VS W0-1 REPO | **delete/sanitize file** (owner SSH password already rotated F-092) |
| Capacity / MySQL gone away | F-029, F-031, F-080 | Findings | ops |

### E. Management Instagram bot (separate product surface)

| Topic | IDs | Detail file |
|-------|-----|-------------|
| Hide UX | F-095 = IG-001 | **IG_BOT_MANAGEMENT_BUGS** full |
| Stats English / thin | F-096 = IG-004 | same |
| Message Requests unlabeled | F-097 = IG-005, IG-013 | same |
| No transfer button | F-098 = IG-003 | same |
| Likes/reactions, alert noise, etc. | IG-006…IG-014 | same (not all mirrored as F-*) |

**IG bot code:** `management/services/instagram_bot.py`, `management/bot_views.py`, `management/templates/management/bot.html`, `management/ig_bot_models.py`  
**Last feature commit:** `1743661c` Upgrade Instagram sales bot automation

---

## 3. Recommended fix order (for the other agent)

### Wave Fix-A — storefront P0 (ads ROAS)
1. F-071 / F-021 / F-033 / F-045 — first_touch → Order.utm_* + session ensure  
2. F-019 / F-083 — is_converted + purchase recording  
3. Plan **W2-7** — unify mono webhook with on_commit CAPI path (see PLAN_VS)  
4. F-030 — pixel BFCache function  
5. F-001/F-023 — reseed category titles in DB  

### Wave Fix-B — security / ops
6. F-087 ubd_docs  
7. F-093 deploy_paramiko  
8. F-088 TG secret (SERVER)  
9. F-090 backup cron (SERVER)  

### Wave Fix-C — SEO / cart polish
10. F-002, F-004/F-094, F-005, F-043/F-078, F-050, F-059, …  

### Wave Fix-D — management IG bot
11. F-095…F-098 + full IG-001…IG-014 in IG_BOT file  

**Do not** re-check PASS/INFO findings (F-012, F-016, F-024, …) unless regression.

---

## 4. How findings checkboxes work

In `AUDIT_FINDINGS_2026-07-09.md`:

| Checkbox | Meaning for fix agent |
|----------|----------------------|
| `[ ]` + Fix? YES | **Must fix** or explicitly risk-accept with owner |
| `[x]` + PASS/INFO | Do not “fix”; already verified OK / process note |
| `[x]` + RECONF | Parent finding still OPEN; reconf is evidence only |
| `[x]` + DONE_OWNER | Owner action done (e.g. SSH password) |

After each fix: mark finding `[x]`, note commit, re-run the **Accept** line if present, update IMPLEMENTATION_PLAN only when **live accept** passes.

---

## 5. Environment notes

- Production truth: live HTTP + production MySQL (SSH). Owner **rotated SSH password** (F-092); use key or new secret — **never** commit passwords.  
- `deploy_paramiko.py` may still contain an old password literal (F-093) — remove it.  
- Main site vs management: different URL/app; IG bot bugs are management-only.  
- Secrets: never write tokens into findings.

---

## 6. Pass workflow (context)

```
Pass A  walk PRE_ADS checklist on prod     → done
Pass B  write findings                     → done (this pack)
Pass C  independent confirm                → pending
Pass D  fix CONFIRMED / OPEN findings      → YOU ARE HERE
```

---

## 7. Quick “where is the long explanation?”

| If you need… | Open… |
|--------------|--------|
| One-line + checkbox for every F-ID | **AUDIT_FINDINGS** master index |
| Long evidence for F-001…F-090 storefront | **AUDIT_FINDINGS** sections `### F-xxx` |
| Why plan `[x]` was wrong | **PLAN_VS_FINDINGS** |
| IG hide/stats/requests deep dive | **IG_BOT_MANAGEMENT_BUGS** |
| Original audit walk rows | **PRE_ADS_MASTER_AUDIT_CHECKLIST** |
| Historical implementation waves | **TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md** |

---

*Handoff README for fix agent — keep short; put new findings only in AUDIT_FINDINGS.*
