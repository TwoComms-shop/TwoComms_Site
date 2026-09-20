"""Shared daemon process/main-progress health and operator alert contract."""

from __future__ import annotations

import time

from django.core.cache import cache


PROCESS_PULSE_KEY = "ig_bot_daemon_hb"
MAIN_PROGRESS_KEY = "ig_bot_daemon_main_progress"
HEALTHY_MAIN_STATES = frozenset({"starting", "running", "idle"})


def _alive_window_seconds() -> int:
    try:
        from management.services.ig_turn_budget import heartbeat_alive_window_seconds

        return int(heartbeat_alive_window_seconds())
    except Exception:
        return 150


def _cache_observation(key: str, *, now_epoch: float) -> tuple[dict, float | None]:
    payload = cache.get(key)
    if not isinstance(payload, dict):
        try:
            observed_at = float(payload)
        except (TypeError, ValueError):
            return {}, None
        return {"at": observed_at}, max(0.0, now_epoch - observed_at)
    try:
        observed_at = float(payload.get("at"))
    except (TypeError, ValueError):
        return payload, None
    return payload, max(0.0, now_epoch - observed_at)


def daemon_runtime_health_snapshot(*, now_epoch: float | None = None) -> dict:
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    alive_window = _alive_window_seconds()
    process, process_age = _cache_observation(
        PROCESS_PULSE_KEY,
        now_epoch=now_epoch,
    )
    main, main_age = _cache_observation(
        MAIN_PROGRESS_KEY,
        now_epoch=now_epoch,
    )
    process_online = bool(
        process_age is not None and process_age < alive_window
    )
    main_state = str(main.get("state") or "")[:40]
    main_available = main_age is not None
    main_fresh = bool(main_available and main_age < alive_window)
    main_healthy = bool(main_fresh and main_state in HEALTHY_MAIN_STATES)
    if not process_online:
        stalled_reason = ""
    elif not main_available:
        stalled_reason = "main_progress_missing"
    elif not main_fresh:
        stalled_reason = "main_progress_stale"
    elif main_state not in HEALTHY_MAIN_STATES:
        stalled_reason = "main_progress_error"
    else:
        stalled_reason = ""
    from management.services.ig_worker_progress import worker_health_snapshot
    workers = worker_health_snapshot(process.get("worker_lanes"), now=now_epoch)
    worker_stalled = bool(process_online and main_healthy and not workers["healthy"])
    from management.services.ig_maintenance import runtime_root
    from management.services.ig_supervisor_observation import read_supervisor_observation
    supervisor = read_supervisor_observation(
        runtime_root(), expected_child_pid=process.get("pid"), now=now_epoch,
    )
    return {
        "process_online": process_online,
        "process_age_seconds": round(process_age, 1) if process_age is not None else None,
        "alive_window_seconds": alive_window,
        "main_available": main_available,
        "main_healthy": main_healthy,
        "main_age_seconds": round(main_age, 1) if main_age is not None else None,
        "main_state": main_state,
        "stalled": bool(process_online and not main_healthy),
        "worker_stalled": worker_stalled,
        "stalled_reason": stalled_reason or ("worker_lane_stalled" if worker_stalled else ""),
        "process_pid": process.get("pid"),
        "main_cycle": main.get("cycle"),
        "workers_healthy": workers["healthy"],
        "worker_lanes": workers["lanes"],
        "release_generation": process.get("sentinel"),
        "supervisor": supervisor,
    }


def alert_daemon_runtime_health() -> dict:
    """Deliver one hourly technical alert for a live-but-stalled daemon."""
    snapshot = daemon_runtime_health_snapshot()
    snapshot["alerted"] = False
    if not snapshot["stalled"] and not snapshot.get("worker_stalled"):
        return snapshot
    try:
        from management.models import InstagramBotSettings
        from management.services.ig_alerts import alert_dedupe_key, format_alert
        from management.services.ig_maintenance import maintenance_status
        from management.services import instagram_bot as bot

        settings_obj = InstagramBotSettings.load()
        if not settings_obj.is_enabled or maintenance_status()["active"]:
            return snapshot
        reason = snapshot["stalled_reason"]
        title = "🚨 IG daemon не просуває основний цикл" if snapshot["stalled"] else "🚨 IG background lane не просувається"
        affected_workers = tuple(
            name for name, row in snapshot.get("worker_lanes", {}).items()
            if not row.get("healthy")
        )
        text = format_alert(
            title,
            lines=(
                f"Причина: {reason}",
                f"Process pulse: {snapshot['process_age_seconds']} с",
                f"Main progress: {snapshot['main_age_seconds']} с",
                f"Lanes: {', '.join(affected_workers)[:240]}" if affected_workers else "",
                "Клієнтські відповіді вважаються недоступними до відновлення progress.",
            ),
        )
        snapshot["alerted"] = bool(
            bot.notify_manager(
                text,
                dedupe_key=alert_dedupe_key(
                    "ig_worker_lane_stalled" if snapshot.get("worker_stalled") else "ig_daemon_stalled",
                    window_minutes=60,
                    text=reason,
                ),
                event_type="ig_daemon_stalled",
                metadata={
                    "reason": reason,
                    "process_age_seconds": snapshot["process_age_seconds"],
                    "main_age_seconds": snapshot["main_age_seconds"],
                    "worker_lanes": affected_workers,
                    "requires_human_review": False,
                },
                deliver_immediately=True,
            )
        )
    except Exception:
        return snapshot
    return snapshot
