# P5-1 same-value materiality core · production · 07.10.2026

✅ Functional release `8f45dfb5e77031f468c892ac10c5db49fbb857bc` pushed/pulled on main through documented SSH/Python3.14 procedure. Django check and management migration-state check pass; no schema changes. Main/supervisor/child SHA match, PID2105736, main and workers healthy, no stall, maintenanceOFF.

Pure deployed same-value/changed-value classification, input immutability and public outcome shape pass. Explicit production READ ONLY transaction: exactly2SELECTs,0memory heads/0facts. This empty baseline does not prove natural publication behavior; no customer fixture or publisher call was run on production. Provider calls0; memory generation/admission acceptance flagsFALSE.

Root native acceptance:70 tests,67PASS/3existing SQLite schema-profile skips, including real two-publisher contention and all source/integrity/TTL/rollback guards. Full immutable checkpoint, bounded long-chain reads, consumer activation and natural72h/20 suitable-turn acceptance remain open. [Contract/tests](README.md).

```json
{
  "fact_count": 0,
  "head_states": {},
  "health": {
    "main_healthy": true,
    "process_online": true,
    "process_pid": 2105736,
    "stalled": false,
    "supervisor": {
      "available": true,
      "child_pid": 2105736,
      "child_pid_matches_expected": true,
      "child_release_sha": "8f45dfb5e77031f468c892ac10c5db49fbb857bc",
      "last_ensure_seen_at": 1791383166.588,
      "observed_at": 1791383167.146,
      "restart_count": 0,
      "status": "current",
      "supervisor_release_sha": "8f45dfb5e77031f468c892ac10c5db49fbb857bc"
    },
    "workers_healthy": true
  },
  "maintenance_active": false,
  "memory_flags": [
    false,
    false
  ],
  "provider_calls": 0,
  "pure_server_contract": "PASS",
  "read_only_queries": 2,
  "release_sha": "8f45dfb5e77031f468c892ac10c5db49fbb857bc",
  "sampled_heads": 0
}
```
