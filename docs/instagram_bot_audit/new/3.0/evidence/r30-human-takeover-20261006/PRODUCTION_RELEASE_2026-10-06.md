# Human takeover release · 06.10.2026

Production main and daemon supervisor/child: `cbf159d0fe8d426e01b1c47191aa8c0b518c557f`. Scoped SSH pull, Django check0, runtime code checks and restart completed; maintenance lease cleared.

The first health snapshot arrived before the restarted daemon emitted its heartbeat and failed its assertion. This is retained in [initial log](human-production-postpull.log). A bounded independent [read-only recheck](human-production-health-recheck.log) confirmed process/main/workers healthy, matching PID/SHA and maintenance OFF. The actual `IG_MEMORY_GENERATION_ENABLED` and `IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED` settings were both FALSE.

No synthetic provider calls, customer sends or production fixtures. Existing takeover boundary is released; per-part durable delivery/private drafts/notes remain a following slice. Full passport and natural72h/20 turn gates remain open.
