# Audit Reconciliation and Remediation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reconcile every finding in `docs/qa/AUDIT_FINDINGS_2026-07-09.md`, the QA companion files, and `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md` against current code, tests, and server evidence; correct inaccurate checkboxes and ship each validated fix.

**Architecture:** Treat the QA findings file as the remediation queue and the technical audit as the cross-reference index. For each item, classify evidence as current code proof, local test proof, or production/server proof. Fix only root causes, update the exact checklist/report links immediately after verification, then commit, push, deploy, and run a production smoke check for that slice.

**Tech Stack:** Django/Python, MariaDB on production, pytest/Django tests, Markdown audit reports, GitHub `main`, SSH deployment.

---

### Task 1: Build the reconciliation matrix

**Files:**
- Read: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Read: `docs/qa/PLAN_VS_FINDINGS_2026-07-09.md`
- Read: `docs/qa/IG_BOT_MANAGEMENT_BUGS_2026-07-09.md`
- Read: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Create: `TWOCOMMS_A_TO_B/technical/audit_report_reconciliation_2026-07-16.md`

**Steps:**
1. Extract every `[ ]`, `[x]`, and `o`/partial status from all scoped documents.
2. Map each finding to its detailed report, code owner, existing fix commit, and required local/server accept command.
3. Verify the current branch and remote before any mutation; preserve unrelated dirty files.
4. Record contradictions and stale claims in the reconciliation report.

### Task 2: Validate existing `[x]` and partial marks

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: relevant `TWOCOMMS_A_TO_B/technical/audit_report_*.md`

**Steps:**
1. Re-run focused local tests and static checks for each claimed fix.
2. Run production HTTP/SSH checks for database-backed or deployment-dependent claims.
3. Change false `[x]` to `[ ]`; change partially valid `[x]` to `o` with a precise residual; retain `[x]` only with current evidence and direct report links.
4. Commit and push the documentation-only reconciliation, then deploy and verify the deployed revision.

### Task 3: Remediate the highest-priority open finding

**Files:** Determined by Task 1 matrix; likely one narrow storefront/ops finding at a time.

**Steps per finding:**
1. Reproduce the finding and trace the root cause before editing.
2. Add or update a focused regression test.
3. Implement the smallest root-cause fix.
4. Run the focused local test and the relevant production/server canary.
5. Update the finding checkbox and exact evidence link.
6. Commit only the validated slice, push `main`, deploy via SSH, and verify the live revision/health.

### Task 4: Closeout and handoff

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: `TWOCOMMS_A_TO_B/technical/audit_report_reconciliation_2026-07-16.md`

**Steps:**
1. Re-scan for stale `[x]`, unresolved `o`, and open `[ ]` entries.
2. Confirm every closed item links to the exact supporting report and commit.
3. Run final local checks plus server health/HEAD parity checks.
4. Record residual risks and the next-agent handoff without storing credentials.
