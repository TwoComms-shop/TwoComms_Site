# P2-5 media core · production verification · 07.10.2026

✅ Code commit `07b8f30ff19bd8ca97f2f4d4c225d82f4f9342dd` pushed to main and pulled with the documented SSH/Python3.14 procedure. Django check passes; management has no migration changes. Maintenance disabled; supervisor/child both run this release, PID2068300, main and workers healthy, no stall. Backend release requires no frontend asset rebuild.

The first post-pull pure proof rejected an incomplete synthetic media fixture (missing capture-state/byte fields). The corrected pure proof passes; this rejection did not write a production customer record or call a provider. Final production verification uses two bounded SELECTs in an explicit READ ONLY transaction: 19 USER messages, 20 parts still honestly historical_unknown, zero existing new complaint cases. This is a baseline sample, not an invented live acceptance of the new taxonomy.

Native local acceptance:295/295. Pure server reaction/typed audio limitation/source-fence invariants pass. No extra Gemini/provider I/O. Memory generation/admission flags remain FALSE. Full passport, independent episode-routing producer/schema and natural72h/20 suitable-turn acceptance remain open.

```json
{
  "complaint_cases_at_read": 0,
  "health": {
    "main_healthy": true,
    "process_online": true,
    "process_pid": 2068300,
    "stalled": false,
    "supervisor": {
      "available": true,
      "child_pid": 2068300,
      "child_pid_matches_expected": true,
      "child_release_sha": "07b8f30ff19bd8ca97f2f4d4c225d82f4f9342dd",
      "last_ensure_seen_at": 1791382562.223,
      "observed_at": 1791382473.352,
      "restart_count": 0,
      "status": "current",
      "supervisor_release_sha": "07b8f30ff19bd8ca97f2f4d4c225d82f4f9342dd"
    },
    "workers_healthy": true
  },
  "inspection_states": {
    "historical_unknown": 20
  },
  "maintenance_active": false,
  "memory_flags": [
    false,
    false
  ],
  "provider_calls": 0,
  "pure_server_contract": "PASS",
  "read_only_queries": 2,
  "release_sha": "07b8f30ff19bd8ca97f2f4d4c225d82f4f9342dd",
  "sampled_messages": 19
}
```
