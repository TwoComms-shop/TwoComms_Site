# Instagram scheduled-task alert correction · 25 September 2026

## Verified incident

Read-only production audit at baseline `43aa93979` found 44 sent
`ig_task_health` notifications in the preceding 48 hours. Every one was the
Nova Poshta `stale` summary shown in the owner's screenshot. At observation,
all seven active heartbeat tasks were healthy, the last recorded tracking
failure was 20 September, and the latest tracking batch took 254 ms.

Production tracking cron used `flock -n` every five minutes on the same
heavy-process lock as the every-minute periodic coordinator and durable task
worker. Failed lock admission occurs before Django can record a heartbeat.
Tracking logs showed successful runs separated by 10–30-minute gaps. This
matches admission starvation; it is not evidence that Instagram replies failed.
The old monitor turned 15-minute age into an hourly Telegram alarm with no
action and could let that alarm mask a later critical task in the same bucket.

## Changed behavior

- Tracking belongs to the existing periodic coordinator: five-minute cadence,
  120-second deadline, priority after the notification backstop. Other lanes
  keep oldest-first scheduling and the coordinator retains its 540-second
  total budget. No additional waiting Django process is introduced.
- Diagnostic `failed`, `stale`, and `degraded` states remain visible. Telegram
  policy is separate: checkout/payment/fulfillment/order-card tasks escalate
  after three failed runs or twice the diagnostic stale threshold. Trace and
  memory delay thresholds are one and two hours respectively.
- Nova Poshta transient delay needs two hours without a successful run and
  outstanding eligible shipments. Retry backoff counts as outstanding work;
  ordinary polling still respects the next-attempt date. Configuration/command
  failure has an immediate, separate fatal escalation slot.
- Each task's incident is anchored to its last success. The same incident
  does not generate hourly duplicates; a different critical task is independent.
  Success permits a new incident. Messages explain impact, action, and CRM link.
- Before a queued technical message is sent, current truth is checked again.
  Recovered episodes and obsolete legacy summaries are audited and resolved;
  temporarily ineligible work is deferred without losing its incident identity.
  Fatal escalation supersedes the pending lower-tier alert. Unknown send results
  are never replayed. Background revalidation is bounded to once a minute per row.
- Conversation escalation, payment review, delivery-unknown, and other existing
  customer-review notifications retain their delivery policy.

## Validation and release procedure

102 focused Django tests passed: task health, notification incident lifecycle,
periodic ownership, tracking command, and tracking provider/deduplication.
36 cron installer/owner-contract tests passed. Django system check, shell syntax,
and scoped whitespace checks passed. An independent review checked severity
transitions, failure counting, missing heartbeat registration, retry backoff,
and reconciliation cost.

The broader legacy notification module still has five pre-existing failures:
three permission fixtures receive HTTP 403 and two media fixtures expect older
receipt handling. The unchanged test module from Git HEAD reproduced the same
five failures. This release does not claim that suite passes or alter those
production authorization/media protections.

After the documented Git push and production pull, retire the old tracking
entry with `install_nova_poshta_tracking_cron.sh --retire`, verify
`--check-retired`, then install/check the periodic coordinator's managed block.
Both installers preserve unrelated cron entries and fail on unknown ownership.
Refresh `tmp/restart.txt` for the bot supervisor; verify the deployed SHA,
supervisor child SHA, health, and natural successful coordinator tracking runs.
No database migration or static asset rebuild is required.

External-provider uptime is not guaranteed by this change. The bounded result
is fairer scheduling and meaningful incident notifications with recovery and
escalation behavior; long-term production observation is separate acceptance.
