"""Bounded current admin observations; never reconstruct an earlier prompt."""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json

from django.db import DatabaseError, connection, transaction
from django.db.models import Max
from django.db.models.functions import Coalesce
from django.utils import timezone

from management.services.ig_client_state_card import assemble_client_state, client_state_admin_payload

# Interactive reads count SELECTs only; transaction control consumes no read
# budget. A catalog + exact-review fixture uses 48, leaving bounded headroom.
MAX_READ_QUERIES = 64
MAX_SOURCE_REFS = 16
MAX_IDENTIFIER = 2**63 - 1


def valid_identifier(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_IDENTIFIER


class AdminStateReadError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AdminStateResult:
    state: object
    status: str
    reason: str
    as_of: str
    selection_revision: int | None
    read_queries: int = 0
    view_mode: str = "current_admin"

    def as_dict(self):
        return {"state": client_state_admin_payload(self.state), "status": self.status,
            "reason": self.reason, "as_of": self.as_of, "view_mode": self.view_mode,
            "selection_revision": self.selection_revision,
            "diagnostics": {"read_only": True, "read_queries": self.read_queries,
                "max_read_queries": MAX_READ_QUERIES}}


def _stamp(value):
    return value.isoformat() if value else ""


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _namespace():
    from management.models import InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    row = InstagramBotSettings.objects.only("pk", "ig_user_id", "page_id").filter(pk=1).first()
    return ingress_provider_namespace(row) if row else ""


def _source_watermark(scope, namespace, sender_id, now):
    from management.models import InstagramBotMessage
    row = InstagramBotMessage.objects.filter(client_id=scope["client_id"], sender_id=sender_id,
        role="user", source__in=("webhook", "poll"), provider_namespace=namespace,
        pk__gte=scope["reset_floor"]).exclude(status="failed").annotate(
            event_time=Coalesce("provider_created_at", "created_at")).filter(event_time__lte=now).aggregate(
                message_id=Max("pk"), event_at=Max("event_time"))
    return {"message_id": row["message_id"], "event_at": _stamp(row["event_at"]) or None}


def _source_rows(ids):
    from management.models import InstagramBotMessage
    return list(InstagramBotMessage.objects.filter(pk__in=ids).order_by("pk").values(
        "pk", "client_id", "sender_id", "role", "source", "status", "text", "provider_namespace",
        "provider_created_at", "created_at"))


def _unavailable(client_id, now, reason, *, status="unavailable", revision=None, reads=0, historical=False):
    state = assemble_client_state(boundary={"client_id": client_id if reason == "client_erasing" else None,
        "reset_floor": 1, "erasure_started": reason == "client_erasing",
        "historical": historical, "revision_id": revision, "view_mode": "historical" if historical else "current_admin"},
        components={}, captured_at=now)
    return AdminStateResult(state, status, reason, now.isoformat(), None, reads,
        "historical" if historical else "current_admin")


def historical_admin_state(client_id, revision_id, *, now=None):
    """Verify ownership only; the persisted metadata is not a full state artifact."""
    from management.models import IgClient, IgCustomerTurnRevision
    now = now or timezone.now()
    if not valid_identifier(client_id):
        return _unavailable(None, now, "client_id_invalid", historical=True)
    if not valid_identifier(revision_id):
        return _unavailable(client_id, now, "revision_id_invalid", historical=True)
    owner = IgClient.objects.filter(pk=client_id).values("pk", "privacy_erasure_started_at").first()
    if not owner:
        return _unavailable(client_id, now, "client_missing", reads=1, historical=True)
    if owner["privacy_erasure_started_at"]:
        return _unavailable(client_id, now, "client_erasing", reads=1, historical=True)
    if not IgCustomerTurnRevision.objects.filter(pk=revision_id, client_id=client_id).exists():
        return _unavailable(client_id, now, "revision_missing", reads=2, historical=True)
    return _unavailable(client_id, now, "historical_capture_unavailable", status="not_reconstructable",
        revision=revision_id, reads=2, historical=True)


def _owner_fence(client_id):
    from management.models import IgClient, IgCommerceSelectionSession, IgFunnelResetAudit, IgCommercialEpisode
    owner = IgClient.objects.filter(pk=client_id).values("pk", "igsid", "current_commercial_episode_id",
        "privacy_erasure_started_at", "reply_permission_epoch").first()
    if owner is None:
        return None
    if owner["privacy_erasure_started_at"]:
        return {"owner": owner, "episode": None, "session": None, "reset": None}
    episode = IgCommercialEpisode.objects.filter(pk=owner["current_commercial_episode_id"], client_id=client_id).values(
        "pk", "intended_order_id", "deal_id", "primary_payment_review_id", "updated_at").first()
    session = IgCommerceSelectionSession.objects.filter(client_id=client_id, open_slot=1,
        state="open", commercial_episode_id=owner["current_commercial_episode_id"]).order_by("-generation").values(
            "pk", "generation", "revision", "active_index", "lines", "query_constraints", "selection_constraints").first()
    reset = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values("pk", "reset_after_message_id").first()
    return {"owner": owner, "episode": episode, "session": session, "reset": reset}


def _payment_capture(client_id, episode_id, scope):
    from management.models import IgCommercialEpisode
    from management.services.ig_commercial_episodes import payment_truth_snapshot
    if episode_id is None:
        return {}, "payment_episode_unbound"
    episode = IgCommercialEpisode.objects.filter(pk=episode_id, client_id=client_id).select_related(
        "deal", "primary_payment_review", "intended_order").first()
    if episode is None:
        return {}, "payment_episode_unbound"
    deal, review, order = episode.deal, episode.primary_payment_review, episode.intended_order
    if (deal is not None and deal.client_id != client_id) or (review is not None and review.client_id != client_id):
        return {}, "payment_source_scope_mismatch"
    order_id = scope["order_id"]
    for item in (deal, review):
        if item is not None and item.order_id not in (None, order_id):
            return {}, "payment_order_scope_mismatch"
    if review is not None and review.deal_id not in (None, episode.deal_id):
        return {}, "payment_source_scope_mismatch"
    if deal is None and review is None and order is None:
        return {}, "payment_source_unavailable"
    # Disable cross-review fallback; snapshot may derive only this exact binding.
    result = payment_truth_snapshot(episode=episode, deal=deal, review=review, order=order,
        allow_deal_review_fallback=False)
    if result.get("order_id") != order_id or result.get("episode_id") != episode_id:
        return {}, "payment_order_scope_mismatch"
    return {**result, "scope": deepcopy(scope)}, ""


def _current_capture(client_id, expected_selection_revision, now):
    from management.services.ig_commerce_projection import captured_selection_for
    from management.services.ig_checkout_readiness import selection_readiness

    first = _owner_fence(client_id)
    if first is None:
        raise AdminStateReadError("client_missing")
    owner, session, episode, reset = (first[key] for key in ("owner", "session", "episode", "reset"))
    if owner["privacy_erasure_started_at"]:
        raise AdminStateReadError("client_erasing")
    if owner["current_commercial_episode_id"] is not None and episode is None:
        raise AdminStateReadError("current_episode_scope_unknown")
    revision = session["revision"] if session else None
    if expected_selection_revision is not None and revision != expected_selection_revision:
        raise AdminStateReadError("selection_revision_conflict")
    lines = session["lines"] if session else []
    index = session["active_index"] if session else 0
    if not isinstance(lines, list) or not isinstance(index, int) or isinstance(index, bool):
        raise AdminStateReadError("selection_line_scope_unknown")
    line = lines[index] if isinstance(lines, list) and 0 <= index < len(lines) and isinstance(lines[index], dict) else {}
    if len(lines) > 16 or (session and (not line.get("line_id") or sum(
        isinstance(item, dict) and item.get("line_id") == line["line_id"] for item in lines) != 1)):
        raise AdminStateReadError("selection_line_scope_unknown")
    reset_floor = int((reset or {}).get("reset_after_message_id") or 0) + 1
    scope = {"client_id": client_id, "episode_id": owner["current_commercial_episode_id"],
        "order_id": episode["intended_order_id"] if episode else None, "line_id": str(line.get("line_id") or ""),
        "recipient_id": str(line.get("recipient_id") or "self"), "reset_floor": reset_floor}
    for key, expected in (("client_id", client_id), ("commercial_episode_id", scope["episode_id"]),
            ("session_id", session["pk"] if session else None)):
        if key in line and line[key] != expected:
            raise AdminStateReadError("selection_line_scope_unknown")
    namespace = _namespace()
    from types import SimpleNamespace
    selection = captured_selection_for(SimpleNamespace(pk=client_id), episode_id=scope["episode_id"], line_id=scope["line_id"] or None)
    if selection:
        expected_scope = {"client_id": client_id, "episode_id": scope["episode_id"], "line_id": scope["line_id"],
            "recipient_id": scope["recipient_id"], "reset_floor": reset_floor,
            "session_id": session["pk"] if session else None, "generation": session["generation"] if session else None,
            "revision": revision, "active_index": index}
        if not isinstance(selection.get("scope"), dict) or any(
                selection["scope"].get(key) != value for key, value in expected_scope.items()):
            raise AdminStateReadError("current_selection_scope_changed")
    evidence = selection.get("evidence") or {}
    ids = sorted({item.get("source_message_id") for item in evidence.values() if isinstance(item, dict)
        and isinstance(item.get("source_message_id"), int) and not isinstance(item.get("source_message_id"), bool)})
    if len(ids) > MAX_SOURCE_REFS:
        raise AdminStateReadError("state_source_budget_exceeded")
    messages = _source_rows(ids)
    source_fence = _digest(messages)
    proof = {row["pk"]: row for row in messages}
    source_reason = ""
    for item in evidence.values():
        source = proof.get(item.get("source_message_id")) if isinstance(item, dict) else None
        if (source is None or isinstance(item.get("source_message_id"), bool) or not namespace
            or source["client_id"] != client_id or source["sender_id"] != owner["igsid"]
            or source["role"] != "user" or source["source"] not in {"webhook", "poll"} or source["status"] == "failed"
            or source["provider_namespace"] != namespace or source["pk"] < reset_floor
            or item.get("source_digest") != sha256(source["text"].encode()).hexdigest()
            or item.get("observed_at") != _stamp(source["provider_created_at"] or source["created_at"])):
            source_reason = "choice_source_scope_unknown"
            selection = {}
            break
    watermark = _source_watermark(scope, namespace, owner["igsid"], now)
    boundary = {**scope, "source_namespace": namespace, "reset_id": (reset or {}).get("pk"), "erasure_epoch": "",
        "source_watermark": watermark, "source_watermark_kind": "current_observation_vector",
        "selection_revision": revision, "view_mode": "current_admin", "historical": False}
    components = {"source_selection": selection,
        "source_selection_binding": {key: boundary[key] for key in (*scope, "source_namespace", "reset_id", "erasure_epoch")}}
    choices = selection.get("values") or {}
    readiness = selection_readiness(product_id=choices.get("product_id"), selection={}, size=choices.get("size", ""),
        fit=choices.get("fit_option_code", ""), color=choices.get("color", ""), quantity=choices.get("quantity", 1), strict=True)
    components["readiness"] = {**readiness, "scope": scope, "observed_at": now.isoformat()}
    payment, payment_reason = _payment_capture(client_id, scope["episode_id"], scope)
    if payment:
        components["payment_truth"] = payment
    components["consent_state"] = {}  # No native purpose-grant producer exists.
    state = assemble_client_state(boundary=boundary, components=components, captured_at=now)
    final = _owner_fence(client_id)
    if _digest(first) != _digest(final):
        raise AdminStateReadError("current_state_changed")
    final_messages = _source_rows(ids)
    if source_fence != _digest(final_messages):
        raise AdminStateReadError("current_source_changed")
    if _namespace() != namespace or _source_watermark(scope, namespace, owner["igsid"], now) != watermark:
        raise AdminStateReadError("current_source_changed")
    final_payment, final_payment_reason = _payment_capture(client_id, scope["episode_id"], scope)
    if _digest(payment) != _digest(final_payment) or payment_reason != final_payment_reason:
        raise AdminStateReadError("current_payment_changed")
    return AdminStateResult(state, "captured", source_reason or payment_reason, now.isoformat(), revision)


def current_admin_state(client_id, *, expected_selection_revision=None, now=None):
    """No bootstrap/generation; cap reads and reject an accidental writer."""
    now = now or timezone.now()
    if not valid_identifier(client_id):
        return _unavailable(None, now, "client_id_invalid")
    if expected_selection_revision is not None and not valid_identifier(expected_selection_revision):
        return _unavailable(client_id, now, "selection_revision_invalid")
    reads = 0
    violation = ""
    def readonly(execute, sql, params, many, context):
        nonlocal reads, violation
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb not in {"SELECT", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"}:
            violation = "state_read_side_effect_rejected"
            raise AdminStateReadError(violation)
        if verb == "SELECT":
            reads += 1
            if reads > MAX_READ_QUERIES:
                violation = "state_read_budget_exceeded"
                raise AdminStateReadError(violation)
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(readonly):
            # A denied ORM writer can mark the surrounding atomic block for
            # rollback before our exception reaches this boundary. Isolate it
            # in our own savepoint and catch only after that savepoint exits.
            with transaction.atomic():
                result = _current_capture(client_id, expected_selection_revision, now)
                if violation:
                    raise AdminStateReadError(violation)
        return AdminStateResult(result.state, result.status, result.reason, result.as_of, result.selection_revision, reads)
    except AdminStateReadError as exc:
        status = "conflict" if exc.code in {"selection_revision_conflict", "current_selection_scope_changed", "current_state_changed", "current_source_changed", "current_payment_changed"} else "unavailable"
        return _unavailable(client_id, now, exc.code, status=status, reads=reads)
    except DatabaseError:
        return _unavailable(client_id, now, "state_read_unavailable", reads=reads)
    except (TypeError, ValueError, KeyError):
        return _unavailable(client_id, now, "state_component_invalid", reads=reads)
