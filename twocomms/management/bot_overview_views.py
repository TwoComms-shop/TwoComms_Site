"""Passive status seams: observations cannot initialize or mutate runtime rows."""
from contextlib import ExitStack

from django.db import DatabaseError, connections


class _ReadOnlyStatus:
    def __init__(self):
        self.violated = False

    def __call__(self, execute, sql, params, many, context):
        normalized = " ".join(sql.strip().upper().split())
        if not normalized.startswith("SELECT ") or any(value in normalized for value in
                (" FOR UPDATE", " LOCK IN SHARE MODE", " INTO OUTFILE", " INTO DUMPFILE")):
            self.violated = True
            raise DatabaseError("status_read_only_violation")
        return execute(sql, params, many, context)


def unavailable_status(reason="observation_unavailable"):
    return {"state": "unavailable", "running": False, "daemon_online": None,
        "pending": None, "settings_revision": None, "status_available": False,
        "unavailable_reason": reason if reason in {"observation_unavailable", "read_only_violation", "settings_unavailable"} else "observation_unavailable"}


def read_status_snapshot(getter):
    guard = _ReadOnlyStatus()
    try:
        with ExitStack() as stack:
            for connection in connections.all():
                stack.enter_context(connection.execute_wrapper(guard))
            status = getter()
    except Exception:
        return unavailable_status("read_only_violation" if guard.violated else "observation_unavailable")
    if guard.violated or not isinstance(status, dict):
        return unavailable_status("read_only_violation" if guard.violated else "observation_unavailable")
    return status


def read_overview_snapshot(*, can_view):
    if can_view is not True:
        return {"schema_version": "ig-overview.v1", "available": False,
            "reason": "access_denied", "components": {}}
    from management.services.ig_overview_read_model import build_overview_payload, compose_overview_payload
    from django.utils import timezone
    try:
        return build_overview_payload()
    except Exception:
        # All components remain explicitly unavailable; no failure becomes a
        # zero queue or a successful owner observation.
        return compose_overview_payload(components={}, captured_at=timezone.now())
