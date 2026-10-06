# P1-6: existing human takeover boundary · 06.10.2026

CODE/TEST accepted; production pull pending at this commit. Existing manager permission path/pause/epoch/resume remain owners; no new pause state.

Shared short pause_reply_boundary precedes client/command locks and commits takeover only after the preceding bot physical send edge exits. Correct mandatory cleanup receives client, now and nowait; any failure rolls back command, pause, epoch, cleanup and audit. Already-paused new operations still clear unstarted automation; duplicate operation remains idempotent. Audit records actual before state. Started/SENT/UNKNOWN work stays receipt-owned; human send keeps manager permissions. Endpoint returns finite503/retryable codes before dispatch when transition fails.

- SQLite overlapping109:104PASS/5native skips,4.890s; also exercises API/context/selector candidates. [log](next-focused-sqlite-final.log).
- Native affected human/permission/manual/resume54/54 PASS,0skip,5.260s; forced physical send/takeover ordering and four permission contention checks. [log](human-native-gate.log).
- New HTTP finite failure mapping:1/1 PASS,0.116s, all three codes0dispatch. [log](human-endpoint-gate.log).
- HEAD baseline existing opt-out API test reproduces403 vs409 without this change. [log](manual-resume-head-baseline.log). Only that test principal receives existing explicit operate+PII capabilities; original opt-out/epoch/consent assertions unchanged. Generic staff denial still tested elsewhere.

No production fixtures or actual provider/customer sends. Full P1-6 per-part human receipts/reaper/echo/private draft/note CAS remain separate; natural72h20 gate open. Tests overlap and are not summed as unique cases.
