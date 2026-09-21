"""Shared daemon process/main-progress health and operator alert contract."""

from __future__ import annotations

import time

from django.core.cache import cache

from management.services.ig_technical_debt import technical_debt_snapshot


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
    try:
        technical_debt = technical_debt_snapshot(limit=100)
    except Exception as exc:  # health must remain bounded when DB/storage is down
        technical_debt = {
            "observed_at": "",
            "fingerprint": "",
            "cases": [],
            "case_count": 0,
            "coverage_complete": False,
            "errors": [type(exc).__name__[:64]],
            "sample_limit": 100,
        }
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
        "technical_debt": technical_debt,
    }


def _unhandled_technical_debt_cases(cases: list[dict]) -> list[dict]:
    """Keep current debt alertable until its acknowledged observation changes."""
    if not cases:
        return []
    try:
        from django.utils import timezone

        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import _reconciler_case

        identities = {
            f"{str(case.get('reason') or '')[:64]}:{str(case.get('scope') or '')[:64]}"
            for case in cases
        }
        handled = {
            row["case_key"]: row["observation_fingerprint"]
            for row in IgTechnicalDebtCase.objects.filter(
                case_key__in=identities,
                status__in=(
                    IgTechnicalDebtCase.Status.ACKNOWLEDGED,
                    IgTechnicalDebtCase.Status.CLAIMED,
                ),
            ).values("case_key", "observation_fingerprint")
            if row.get("observation_fingerprint")
        }
        if not handled:
            return cases
        now = timezone.now()
        return [
            case for case in cases
            if handled.get(
                f"{str(case.get('reason') or '')[:64]}:{str(case.get('scope') or '')[:64]}"
            ) != _reconciler_case(case, now=now)["case_fingerprint"]
        ]
    except Exception:
        # Alerting must remain visible if lifecycle storage is unavailable.
        return cases


def alert_daemon_runtime_health() -> dict:
    """Deliver one hourly technical alert for a live-but-stalled daemon."""
    snapshot = daemon_runtime_health_snapshot()
    snapshot["alerted"] = False
    technical_debt = snapshot.get("technical_debt") or {}
    debt_cases = _unhandled_technical_debt_cases(technical_debt.get("cases") or [])
    if not snapshot["stalled"] and not snapshot.get("worker_stalled") and not debt_cases:
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
        debt_fingerprint = str(technical_debt.get("fingerprint") or "")[:24]
        # Technical debt has its own durable fingerprint and remains actionable
        # even when liveness observations are incomplete; preserve the debt
        # case instead of collapsing it into a generic worker-stall alert.
        debt_alert = bool(debt_cases)
        if debt_alert:
            reason = "technical_debt"
        title = (
            "⚠️ IG: накопичився технічний борг"
            if debt_alert else
            "🚨 IG daemon не просуває основний цикл" if snapshot["stalled"]
            else "🚨 IG background lane не просувається"
        )
        affected_workers = tuple(
            name for name, row in snapshot.get("worker_lanes", {}).items()
            if not row.get("healthy")
        )
        text = format_alert(
            title,
            lines=(
                f"Причина: {reason}",
                f"Debt cases: {len(debt_cases)}" if debt_alert else "",
                f"Debt fingerprint: {debt_fingerprint}" if debt_alert else "",
                f"Process pulse: {snapshot['process_age_seconds']} с",
                f"Main progress: {snapshot['main_age_seconds']} с",
                f"Lanes: {', '.join(affected_workers)[:240]}" if affected_workers else "",
                "Клієнтські відповіді вважаються недоступними до відновлення progress."
                if not debt_alert else
                "Цей алерт фіксує технічний борг; доступність відповідей потребує окремої перевірки.",
            ),
        )
        snapshot["alerted"] = bool(
            bot.notify_manager(
                text,
                dedupe_key=alert_dedupe_key(
                    "ig_technical_debt" if debt_alert else ("ig_worker_lane_stalled" if snapshot.get("worker_stalled") else "ig_daemon_stalled"),
                    window_minutes=60,
                    text=debt_fingerprint if debt_alert else reason,
                ),
                event_type="ig_technical_debt" if debt_alert else "ig_daemon_stalled",
                metadata={
                    "reason": reason,
                    "technical_debt_fingerprint": debt_fingerprint,
                    "technical_debt_cases": [
                        {"reason": str(row.get("reason") or "")[:64],
                         "scope": str(row.get("scope") or "")[:64],
                         "count": int(row.get("count") or 0),
                         "oldest_age_seconds": row.get("oldest_age_seconds")}
                        for row in debt_cases[:20]
                    ],
                    "technical_debt_coverage_complete": bool(technical_debt.get("coverage_complete")),
                    "process_age_seconds": snapshot["process_age_seconds"],
                    "main_age_seconds": snapshot["main_age_seconds"],
                    "worker_lanes": affected_workers,
                    "requires_human_review": bool(debt_alert),
                },
                deliver_immediately=True,
            )
        )
    except Exception:
        return snapshot
    return snapshot
