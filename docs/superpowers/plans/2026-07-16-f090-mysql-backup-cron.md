# F-090 MySQL Backup Cron Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce verified daily backups for every production MySQL database, prove they restore, and install a non-overlapping production cron without disturbing existing jobs.

**Architecture:** Keep the repository entry point as a Bash script, but require explicit database names and a private MySQL defaults file. Stage and validate every compressed dump before atomically publishing any of them, serialize runs with `flock`, and expose a small success marker for scheduled-run monitoring. Production acceptance uses temporary restore databases and never reads or writes application rows in place.

**Tech Stack:** Bash, `mysqldump`, gzip, `flock`, Python `unittest` subprocess fakes, MariaDB client, cron.

---

### Task 1: Specify the safe backup contract with RED tests

**Files:**
- Create: `tests/test_backup_mysql_script.py`

- [x] **Step 1: Add a subprocess harness with fake `mysqldump`**

The harness creates a private defaults file, isolated backup root and fake binary directory. It records database arguments but never credentials, and emits enough deterministic SQL for gzip validation.

- [x] **Step 2: Add fail-closed and multi-database tests**

Verify that no explicit database configuration exits non-zero without creating a dump. Verify that `main_db dtf_db` creates two valid archives, mode `0600`, under mode-`0700` directories.

- [x] **Step 3: Add atomic-publication and lock tests**

Make the second database fail and prove neither final archive is replaced. Make gzip validation fail and prove an existing final archive remains unchanged. Hold one fake dump open and prove a concurrent invocation exits with the documented lock status.

- [x] **Step 4: Run the tests and verify RED**

Run: `python -m unittest tests.test_backup_mysql_script -v`

Expected: failures show the current script silently selects one default database, publishes before validation, and lacks multi-database/lock behavior.

### Task 2: Implement the minimum safe script

**Files:**
- Modify: `scripts/backup_mysql.sh`

- [x] **Step 1: Require explicit databases and private client config**

Accept database names as positional arguments, with comma-separated `TWC_DB_NAMES` as the cron-compatible alternative. Require readable `TWC_MYSQL_DEFAULTS_FILE` (default `$HOME/.my.cnf`) and reject unsafe database-name characters.

- [x] **Step 2: Lock and stage the complete set**

Create backup directories with `umask 077`, acquire a non-blocking `flock`, and dump every configured database to a process-scoped temporary file. Use `set -o pipefail`, size validation, and `gzip -t` on each temporary archive.

- [x] **Step 3: Publish, rotate, and record success**

Only after all temporary files pass, atomically rename every daily archive, optionally copy Sunday archives to weekly, rotate per database, and atomically update `last_success` without database names or credentials.

- [x] **Step 4: Verify GREEN**

Run: `python -m unittest tests.test_backup_mysql_script -v`

Run: `bash -n scripts/backup_mysql.sh`

Run if available: `shellcheck scripts/backup_mysql.sh`

Expected: all behavioral tests pass; syntax/static checks return zero.

### Task 3: Document and deploy the repository slice

**Files:**
- Modify: `twocomms/docs/OPS.md`

- [x] **Step 1: Document exact server paths and acceptance**

Record `/home/qlknpodo/TWC/TwoComms_Site/scripts/backup_mysql.sh`, the private defaults file requirement, both database arguments, backup/log paths, retention, lock behavior, restore drill, and safe crontab installation.

- [ ] **Step 2: Review, commit, push, and deploy**

Run the behavioral tests, Bash checks, diff/secret checks, then commit only the F-090 repository files. Fetch before push, pull `main` on production, and rerun the same tests there.

### Task 4: Production dump, restore, and cron acceptance

**Files:**
- Server-only: `$HOME/.my.cnf`, `$HOME/db_backups`, user crontab, temporary restore databases

- [ ] **Step 1: Verify production prerequisites without printing credentials or names**

Confirm exactly the intended MySQL aliases, distinct database names where configured, compatible users/hosts, client tools, private config mode, backup directory location and free space.

- [ ] **Step 2: Create and validate a manual production backup**

Run the deployed script for every production MySQL database. Verify archive count, non-trivial sizes, `gzip -t`, permissions, last-success marker and no leftover temporary files.

- [ ] **Step 3: Restore each archive into an isolated temporary database**

Create uniquely named restore databases, import each archive, compare table counts and selected key table row counts with the source, run the applicable Django checks, then drop only the temporary databases in a guaranteed cleanup step.

- [ ] **Step 4: Install cron without removing existing jobs**

Append one idempotently marked daily job, retain every existing crontab line, run the exact cron command once, and prove a fresh valid archive and success log entry.

### Task 5: Close all audit documents

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/README.md`
- Modify: `twocomms/docs/OPS.md`
- Modify: `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md`
- Modify: `TWOCOMMS_A_TO_B/technical/audit_report_section3_techdebt.md`
- Modify: `TWOCOMMS_A_TO_B/technical/TECHNICAL_TASKS.md`
- Modify: `docs/superpowers/plans/2026-07-16-f090-mysql-backup-cron.md`

- [ ] **Step 1: Mark F-090/W0-3/TD-020/TECH-042 `[x]` only after live acceptance**

Record the runtime commit, archive/restore counts, cron preservation, scheduled-command canary and production HEAD without storing database names or credentials.

- [ ] **Step 2: Commit, push, deploy, and verify the documentation checkpoint**

Fetch before push, pull the checkpoint on production, and verify local/origin/server HEAD equality.
