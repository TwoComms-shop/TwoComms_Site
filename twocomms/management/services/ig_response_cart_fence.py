"""Read-only authority fence for the exact captured cart, including inactive lines.

Preparation compares the original DTO with a fresh canonical capture. Later
checks read only owner/head/source frames; they never reconstruct choices,
resolve catalog identities, bootstrap a session, or grant an effect permission.
The caller owns revision/lease, publication, payment and dispatch admission.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import re

from django.db import DatabaseError, connection
from django.db.models import OuterRef, Subquery

VERSION = "source-cart-authority.v1"
MAX_CHECK_READS = 8
MAX_CAPTURE_READS = 80
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE_KEYS = ("client_id", "episode_id", "order_id", "reset_id", "reset_floor", "source_namespace")
_KEYS = frozenset(("schema", "scope", "sender_id", "permission_epoch", "session_id", "generation",
    "selection_revision", "active_index", "active_line_id", "snapshot_digest", "source_ids",
    "source_digest", "source_watermark", "capture_digest", "semantic_capture_digest",
    "revision_id", "revision_snapshot_digest", "seal_watermark", "binding_digest"))


@dataclass(frozen=True)
class SourceCartAuthority:
    ready: bool
    reason: str = ""
    _binding_json: str = ""

    @property
    def binding(self):
        return json.loads(self._binding_json) if self._binding_json else {}

    def as_dict(self):
        return self.binding

    def __bool__(self):
        return self.ready


@dataclass(frozen=True)
class SourceCartCheck:
    ready: bool
    reason: str = ""

    def __bool__(self):
        return self.ready


def _digest(value):
    from management.services.ig_turn_intelligence import capture_digest
    return capture_digest(value)


def _semantic_capture(capture):
    """The only exclusions are nonsemantic whole-capture/owner audit hashes."""
    value = deepcopy(capture)
    value.pop("capture_digest", None)
    value["fence"].pop("owner_digest", None)
    return value


def _identifier(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_head(client_id):
    """Two bounded reads; episode/reset identity accompanies the owner read."""
    from management.models import IgClient, IgCommerceSelectionSession, IgFunnelResetAudit
    from management.services.ig_commerce_projection import _choice_digest
    latest = IgFunnelResetAudit.objects.filter(client_id=OuterRef("pk")).order_by("-pk")
    owner = IgClient.objects.filter(pk=client_id).annotate(
        cart_reset_id=Subquery(latest.values("pk")[:1]),
        cart_reset_after=Subquery(latest.values("reset_after_message_id")[:1]),
    ).values("pk", "igsid", "privacy_erasure_started_at", "reply_permission_epoch",
        "current_commercial_episode_id", "current_commercial_episode__client_id",
        "current_commercial_episode__intended_order_id", "cart_reset_id", "cart_reset_after").first()
    if owner is None:
        return None, "source_cart_client_missing"
    if owner["privacy_erasure_started_at"]:
        return None, "source_cart_client_erasing"
    episode_id = owner["current_commercial_episode_id"]
    if episode_id is not None and owner["current_commercial_episode__client_id"] != client_id:
        return None, "source_cart_episode_changed"
    session = IgCommerceSelectionSession.objects.filter(client_id=client_id, open_slot=1,
        state="open", commercial_episode_id=episode_id).order_by("-generation").first()
    if session is None:
        return None, "source_cart_head_changed"
    snapshot = session.snapshot()
    lines = snapshot.get("lines")
    index = snapshot.get("active_index")
    if (not isinstance(lines, list) or not 1 <= len(lines) <= 16 or not _number(index)
            or index >= len(lines) or any(not isinstance(row, dict) for row in lines)):
        return None, "source_cart_head_changed"
    return {"scope": {"client_id": client_id, "episode_id": episode_id,
        "order_id": owner["current_commercial_episode__intended_order_id"],
        "reset_id": owner["cart_reset_id"], "reset_floor": int(owner["cart_reset_after"] or 0) + 1},
        "sender_id": owner["igsid"], "permission_epoch": owner["reply_permission_epoch"],
        "session_id": session.pk, "generation": session.generation,
        "selection_revision": session.revision, "active_index": index,
        "active_line_id": lines[index].get("line_id"), "snapshot_digest": _choice_digest(snapshot)}, ""


def _revision_boundary(capture, revision):
    from management.services.ig_turn_capture import _source_time
    from management.services.ig_turn_intelligence import TurnContextError
    rows = (revision.bundle_snapshot or {}).get("sources")
    if (revision.client_id != capture["scope"]["client_id"] or revision.erasure_started_at_snapshot
            or not isinstance(rows, list) or not rows or len(rows) > 32
            or _digest(revision.bundle_snapshot) != revision.snapshot_digest):
        raise TurnContextError("source_cart_revision_changed")
    key = max((_source_time(row)[0], row["message_id"]) for row in rows)
    if any(row.get("source_namespace") != capture["scope"]["source_namespace"] for row in rows):
        raise TurnContextError("source_cart_scope_changed")
    return {"message_id": key[1], "event_at": key[0].isoformat()}


def _validate_capture(capture, revision=None):
    from management.services.ig_turn_intelligence import validate_source_cart_capture
    scope = capture.get("scope") or {}
    boundary = {**scope, "line_id": capture.get("active_line_id"),
        "recipient_id": (capture.get("lines") or [{}])[capture.get("active_index", 0)].get("recipient_id", "self"),
        "watermark": _revision_boundary(capture, revision) if revision is not None else capture.get("source_watermark")}
    return validate_source_cart_capture(capture, boundary)


def _read_only_budget(limit):
    reads = 0
    def wrapper(execute, sql, params, many, context):
        nonlocal reads
        from management.services.ig_turn_intelligence import TurnContextError
        if not sql.lstrip().upper().startswith("SELECT"):
            raise TurnContextError("source_cart_check_not_read_only")
        reads += 1
        if reads > limit:
            raise TurnContextError("source_cart_read_bound")
        return execute(sql, params, many, context)
    return connection.execute_wrapper(wrapper)


def capture_source_cart_authority(client, capture, *, revision=None):
    """Admit only the exact current producer DTO; no missing-proof fallback."""
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_turn_capture import detached_payload
    from management.services.ig_turn_intelligence import TurnContextError
    try:
        client_id = getattr(client, "pk", client)
        if not _identifier(client_id):
            raise TurnContextError("source_cart_client_invalid")
        with _read_only_budget(MAX_CAPTURE_READS):
            original = _validate_capture(detached_payload(capture), revision)
            if original["scope"]["client_id"] != client_id:
                raise TurnContextError("source_cart_scope_changed")
            fresh = _validate_capture(capture_current_selection_lines(client_id), revision)
            semantic = _semantic_capture(original)
            if semantic != _semantic_capture(fresh):
                raise TurnContextError("source_cart_capture_changed")
            head, reason = _read_head(client_id)
            if reason:
                raise TurnContextError(reason)
            binding = {"schema": VERSION, **head,
                "scope": deepcopy(original["scope"]), "source_ids": list(original["fence"]["source_ids"]),
                "source_digest": original["fence"]["source_digest"],
                "source_watermark": deepcopy(original["source_watermark"]),
                "capture_digest": original["capture_digest"], "semantic_capture_digest": _digest(semantic),
                "revision_id": revision.pk if revision is not None else None,
                "revision_snapshot_digest": revision.snapshot_digest if revision is not None else "",
                "seal_watermark": _revision_boundary(original, revision) if revision is not None else None}
            # Do not hide a head change between fresh capture and header read.
            if any(binding[key] != original[key] for key in ("session_id", "generation",
                    "selection_revision", "active_index", "active_line_id")) or head["snapshot_digest"] != original["fence"]["snapshot_digest"]:
                raise TurnContextError("source_cart_head_changed")
            binding["binding_digest"] = _digest(binding)
            checked = check_source_cart_authority(client_id, binding, revision=revision)
            if not checked.ready:
                raise TurnContextError(checked.reason)
        return SourceCartAuthority(True, "", json.dumps(binding, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False))
    except TurnContextError as exc:
        return SourceCartAuthority(False, exc.reason)
    except (DatabaseError, AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError):
        return SourceCartAuthority(False, "source_cart_authority_unavailable")


def _validate_binding(client_id, binding):
    from management.services.ig_turn_intelligence import TurnContextError
    if isinstance(binding, SourceCartAuthority):
        binding = binding.binding
    if not isinstance(binding, Mapping) or set(binding) != _KEYS:
        raise TurnContextError("source_cart_binding_invalid")
    binding = deepcopy(dict(binding))
    scope = binding["scope"]
    ids = binding["source_ids"]
    if (binding["schema"] != VERSION or not isinstance(scope, dict) or set(scope) != set(_SCOPE_KEYS)
            or scope["client_id"] != client_id or not _identifier(client_id)
            or any(scope[key] is not None and not _identifier(scope[key]) for key in ("episode_id", "order_id", "reset_id"))
            or not _identifier(scope["reset_floor"]) or not isinstance(scope["source_namespace"], str) or not scope["source_namespace"]
            or not isinstance(binding["sender_id"], str) or not binding["sender_id"]
            or not _identifier(binding["session_id"])
            or any(not _number(binding[key]) for key in ("generation", "selection_revision", "active_index", "permission_epoch"))
            or binding["active_index"] >= 16 or not isinstance(binding["active_line_id"], str) or not binding["active_line_id"]
            or not isinstance(ids, list) or len(ids) > 64 or not ids or any(not _identifier(value) for value in ids)
            or ids != sorted(set(ids)) or any(value < scope["reset_floor"] for value in ids)
            or any(not _HASH.fullmatch(str(binding[key] or "")) for key in ("snapshot_digest", "source_digest",
                "capture_digest", "semantic_capture_digest", "binding_digest"))
            or _digest({key: value for key, value in binding.items() if key != "binding_digest"}) != binding["binding_digest"]):
        raise TurnContextError("source_cart_binding_invalid")
    from management.services.ig_turn_intelligence import _key
    watermark = binding["source_watermark"]
    if (not isinstance(watermark, dict) or set(watermark) != {"message_id", "event_at"}
            or not _identifier(watermark["message_id"]) or not isinstance(watermark["event_at"], str)):
        raise TurnContextError("source_cart_binding_invalid")
    _key(watermark)
    if any(value > watermark["message_id"] for value in ids):
        raise TurnContextError("source_cart_after_seal")
    if binding["revision_id"] is None:
        if binding["revision_snapshot_digest"] or binding["seal_watermark"] is not None:
            raise TurnContextError("source_cart_binding_invalid")
    elif (not _identifier(binding["revision_id"]) or not _HASH.fullmatch(str(binding["revision_snapshot_digest"] or ""))
            or not isinstance(binding["seal_watermark"], dict)
            or set(binding["seal_watermark"]) != {"message_id", "event_at"}
            or not _identifier(binding["seal_watermark"]["message_id"])
            or not isinstance(binding["seal_watermark"]["event_at"], str)
            or _key(watermark) > _key(binding["seal_watermark"])
            or watermark["message_id"] > binding["seal_watermark"]["message_id"]):
        raise TurnContextError("source_cart_after_seal")
    return binding


def check_source_cart_authority(client, fence, *, revision=None):
    """Eight SELECTs maximum; compares the complete head and every source twice.

    Before/after owner/source checks reject changes during this read. They are
    predicates, not locks: callers must retain their existing dispatch CAS and
    ownership/permission locks around the final admission boundary.
    """
    from management.services.ig_admin_state_capture import _namespace
    from management.services.ig_turn_capture import validate_current_source_cart_sources
    from management.services.ig_turn_intelligence import TurnContextError
    try:
        client_id = getattr(client, "pk", client)
        binding = _validate_binding(client_id, fence)
        if revision is not None:
            if (revision.pk != binding["revision_id"] or revision.client_id != client_id
                    or revision.snapshot_digest != binding["revision_snapshot_digest"]
                    or revision.permission_epoch != binding["permission_epoch"] or revision.erasure_started_at_snapshot):
                raise TurnContextError("source_cart_revision_changed")
            if _revision_boundary({"scope": binding["scope"]}, revision) != binding["seal_watermark"]:
                raise TurnContextError("source_cart_revision_changed")
        expected = {key: deepcopy(binding[key]) for key in ("scope", "sender_id", "permission_epoch", "session_id",
            "generation", "selection_revision", "active_index", "active_line_id", "snapshot_digest")}
        expected["scope"].pop("source_namespace")
        source_frame = {"fence": {"source_ids": binding["source_ids"], "source_digest": binding["source_digest"]}}
        with _read_only_budget(MAX_CHECK_READS):
            for _ in range(2):
                head, reason = _read_head(client_id)
                if reason:
                    raise TurnContextError(reason)
                if head != expected:
                    raise TurnContextError("source_cart_head_changed")
                if _namespace() != binding["scope"]["source_namespace"]:
                    raise TurnContextError("source_cart_namespace_changed")
                validate_current_source_cart_sources(source_frame)
        return SourceCartCheck(True)
    except TurnContextError as exc:
        return SourceCartCheck(False, exc.reason)
    except (DatabaseError, AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError):
        return SourceCartCheck(False, "source_cart_authority_unavailable")
