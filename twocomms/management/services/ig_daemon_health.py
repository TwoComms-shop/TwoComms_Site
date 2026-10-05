"""Shared daemon process/main-progress health and operator alert contract."""

from __future__ import annotations

import time

from django.core.cache import cache

from management.services.ig_technical_debt import technical_debt_snapshot, technical_debt_fingerprint


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
            row["case_key"]: (
                (row["evidence"] or {}).get("handled_observation_fingerprint", row["observation_fingerprint"])
                if isinstance(row["evidence"], dict) else row["observation_fingerprint"]
            )
            for row in IgTechnicalDebtCase.objects.filter(
                case_key__in=identities,
                status__in=(
                    IgTechnicalDebtCase.Status.ACKNOWLEDGED,
                    IgTechnicalDebtCase.Status.CLAIMED,
                    IgTechnicalDebtCase.Status.RESOLVED,
                    IgTechnicalDebtCase.Status.DISMISSED,
                ),
            ).values("case_key", "observation_fingerprint", "evidence")
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


_DEBT_LABELS = {
    "canonical_delivery_incomplete": "Частину відповіді не відправлено",
    "canonical_delivery_unknown": "Результат відправлення потребує звірки",
    "legacy_send_unknown": "Результат попереднього відправлення потребує звірки",
    "legacy_processing_claim_expired": "Обробка повідомлення не завершилася",
    "inbound_pending_unreconciled": "Вхідний запит залишився без обробки",
    "turn_claim_expired": "Обробка діалогу не завершилася",
    "revision_claim_expired": "Обробка відповіді не завершилася",
    "unsent_delivery_intent": "Заплановану частину відповіді не відправлено",
    "delivery_claim_expired": "Відправлення не завершилося",
    "deferred_provider_echo": "Потрібна звірка історії повідомлень",
    "private_media_delete_claim_expired": "Очищення медіа не завершилося",
    "webhook_ingress_pending": "Вхідну подію не оброблено",
    "webhook_ingress_blocked": "Вхідна подія потребує розбору в CRM",
    "media_capture_claim_expired": "Отримання медіа не завершилося",
    "orphan_private_media": "Потрібна звірка збережених медіа",
}
DEBT_ALERT_POLICY_VERSION = 2


def technical_debt_alert_decision(technical_debt):
    """One durable notification per unhandled observation, without time buckets."""
    from management.services.ig_alerts import format_alert, management_base_url

    cases = _unhandled_technical_debt_cases(technical_debt.get("cases") or [])
    if not cases:
        return None
    fingerprint = technical_debt_fingerprint(cases)
    total = sum(int(row.get("count") or 0) for row in cases)
    facts = [f"{_DEBT_LABELS.get(row.get('reason'), 'Потрібна технічна перевірка')}: {int(row.get('count') or 0)}"
             for row in cases[:8]]
    return {
        "dedupe_key": f"ig_technical_debt:{fingerprint}",
        "text": format_alert(
            "⚠️ IG: потрібен розбір незавершених операцій",
            lines=(f"Операцій: {total}", *facts,
                   "Перевірте відповідні задачі та технічний борг у CRM. Невідомі відправлення автоматично не повторюються."),
            url=f"{management_base_url()}/bot/", url_label="CRM:",
        ),
        "metadata": {
            "reason": "technical_debt",
            "technical_debt_alert_policy_version": DEBT_ALERT_POLICY_VERSION,
            "technical_debt_fingerprint": fingerprint,
            "technical_debt_cases": [
                {"reason": str(row.get("reason") or "")[:64],
                 "scope": str(row.get("scope") or "")[:64],
                 "count": int(row.get("count") or 0),
                 "oldest_age_seconds": row.get("oldest_age_seconds")}
                for row in cases[:20]
            ],
            "technical_debt_coverage_complete": bool(technical_debt.get("coverage_complete")),
            "requires_human_review": True,
        },
    }


def revalidate_technical_debt_notification(notification_id, *, snapshot=None, now=None):
    """Retire obsolete queued alarms using a complete current inventory.

    UNKNOWN Telegram sends are never replayed. Incomplete inventory cannot
    prove recovery, so keep the row and defer a retry instead of resolving it.
    """
    from datetime import timedelta
    from django.db import transaction
    from django.utils import timezone
    from management.models import IgBotNotification, IgBotNotificationAudit

    now = now or timezone.now()
    snapshot = technical_debt_snapshot(now=now) if snapshot is None else snapshot
    decision = technical_debt_alert_decision(snapshot) if snapshot.get("coverage_complete") else None
    with transaction.atomic():
        row = IgBotNotification.objects.select_for_update().filter(
            pk=notification_id, event_type="ig_technical_debt",
            status__in=("pending", "failed", "unknown", "dead_letter"),
        ).first()
        if row is None:
            return False
        payload = row.payload if isinstance(row.payload, dict) else {}
        if not snapshot.get("coverage_complete"):
            if row.status in {"pending", "failed"}:
                row.next_attempt_at = now + timedelta(minutes=5)
            row.save(update_fields=["next_attempt_at", "updated_at"])
            return False
        if decision and row.dedupe_key == decision["dedupe_key"] and payload.get("technical_debt_alert_policy_version") == DEBT_ALERT_POLICY_VERSION:
            row.payload = {**payload, **decision["metadata"], "text": decision["text"]}
            row.save(update_fields=["payload", "updated_at"])
            return row.status in {"pending", "failed"}
        resolution = "debt_alert_obsolete" if decision else "debt_alert_recovered"
        before = row.status
        row.status = IgBotNotification.Status.RESOLVED
        row.failure_kind = resolution
        row.next_attempt_at = None
        row.payload = {**payload, "review_status": resolution}
        row.save(update_fields=["status", "failure_kind", "next_attempt_at", "payload", "updated_at"])
        IgBotNotificationAudit.objects.create(
            notification=row, actor=None, action=resolution,
            from_status=before, to_status=row.status,
            note="technical debt alert revalidated against current canonical inventory",
        )
        return False


def reconcile_technical_debt_notifications(*, limit=100, force=False):
    from datetime import timedelta
    from django.utils import timezone
    from management.models import IgBotNotification

    candidates = IgBotNotification.objects.filter(
        event_type="ig_technical_debt", status__in=("pending", "failed", "unknown", "dead_letter"),
    )
    if not force:
        candidates = candidates.filter(updated_at__lte=timezone.now() - timedelta(minutes=1))
    ids = list(candidates.order_by("updated_at", "id").values_list("id", flat=True)[:max(1, min(int(limit), 500))])
    if not ids:
        return 0
    snapshot = technical_debt_snapshot()
    for notification_id in ids:
        revalidate_technical_debt_notification(notification_id, snapshot=snapshot)
    return len(ids)


def alert_daemon_runtime_health() -> dict:
    """Report runtime stalls independently of durable technical-debt incidents."""
    snapshot = daemon_runtime_health_snapshot()
    snapshot["alerted"] = False
    technical_debt = snapshot.get("technical_debt") or {}
    debt_decision = technical_debt_alert_decision(technical_debt)
    if not snapshot["stalled"] and not snapshot.get("worker_stalled") and debt_decision is None:
        return snapshot
    try:
        from management.models import InstagramBotSettings
        from management.services.ig_alerts import alert_dedupe_key, format_alert
        from management.services.ig_maintenance import maintenance_status
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once
        from management.services import instagram_bot as bot

        settings_obj = InstagramBotSettings.load()
        if not settings_obj.is_enabled or maintenance_status()["active"]:
            return snapshot
        # The diagnostic snapshot stays read-only. Only the operational monitor
        # persists operator inventory, reusing the existing case ledger.
        affected_workers = tuple(
            name for name, row in snapshot.get("worker_lanes", {}).items()
            if not row.get("healthy")
        )
        if snapshot["stalled"] or snapshot.get("worker_stalled"):
            reason = snapshot["stalled_reason"]
            title = ("🚨 IG daemon не просуває основний цикл" if snapshot["stalled"]
                     else "🚨 IG background lane не просувається")
            snapshot["alerted"] = bool(bot.notify_manager(
                format_alert(title, lines=(
                    f"Причина: {reason}",
                    f"Process pulse: {snapshot['process_age_seconds']} с",
                    f"Main progress: {snapshot['main_age_seconds']} с",
                    f"Lanes: {', '.join(affected_workers)[:240]}" if affected_workers else "",
                    "Клієнтські відповіді вважаються недоступними до відновлення progress.",
                )),
                dedupe_key=alert_dedupe_key(
                    "ig_worker_lane_stalled" if snapshot.get("worker_stalled") else "ig_daemon_stalled",
                    window_minutes=60, text=reason,
                ),
                event_type="ig_daemon_stalled",
                metadata={"reason": reason, "process_age_seconds": snapshot["process_age_seconds"],
                          "main_age_seconds": snapshot["main_age_seconds"], "worker_lanes": affected_workers,
                          "requires_human_review": False},
                deliver_immediately=True,
            ))
        if debt_decision:
            try:
                reconcile_ig_technical_debt_once(dry_run=False, snapshot=technical_debt)
            except Exception as exc:
                # The notification outbox may still be available when case
                # persistence fails. Keep the incident visible in that case.
                snapshot["technical_debt_persistence_error"] = type(exc).__name__[:64]
            alerted = bot.notify_manager(
                debt_decision["text"], dedupe_key=debt_decision["dedupe_key"],
                event_type="ig_technical_debt", metadata=debt_decision["metadata"],
                deliver_immediately=True,
            )
            snapshot["alerted"] = bool(alerted or snapshot["alerted"])
    except Exception:
        return snapshot
    return snapshot
