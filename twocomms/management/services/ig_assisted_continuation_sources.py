"""Native residual wishlist adoption; no text fabrication or copied authority."""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac

from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

ACTION = "assisted_residual_adoption"
SCHEMA = "assisted-residual-adoption.v1"
_ISSUER = object()
_FIELDS = ("product_id", "fit_option_code", "color", "size", "quantity", "garment_type", "model_query")


class AssistedAdoptionError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class VerifiedAssistedAdoption:
    transition_id: int
    source_id: int
    revision_id: int
    parent_request_id: str
    intention_id: str
    coverage_digest: str
    line_map: tuple
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise AssistedAdoptionError("adoption_requires_native_producer")


def _cap(proof):
    return VerifiedAssistedAdoption(proof["transition_id"], proof["source_id"], proof["revision_id"],
        proof["parent_request_id"], proof["intention_id"], proof["coverage_digest"],
        tuple(deepcopy(proof["line_map"])), _ISSUER)


def _rows(history, residual, source_id):
    originals = {row["line_id"]: row for row in history["original_lines"]}
    if (not isinstance(residual, list) or not residual or len(residual) > 16
        or len(set(residual)) != len(residual) or any(identity not in originals for identity in residual)):
        raise AssistedAdoptionError("residual_line_scope_invalid")
    lines, mapping, origins = [], [], []
    for index, identity in enumerate(residual):
        original = originals[identity]
        values = {key: deepcopy(value) for key, value in original["source_choices"].items()
                  if key in _FIELDS and key != "purchase_requested" and value not in (None, "")}
        values["quantity"] = original["quantity"]
        current_id = f"line:{source_id}:{index}"
        line = {**values, "line_id": current_id, "recipient_id": original["recipient_id"]}
        lines.append(line)
        mapping.append({"line_id": identity, "recipient_id": original["recipient_id"],
            "original_index": original["original_index"], "current_line_id": current_id, "current_index": index})
        origins.append({"line_id": identity, "values": values, "source_evidence": deepcopy(original["source_evidence"]),
                        "quantity": original["quantity"], "recipient_id": original["recipient_id"]})
    return lines, mapping, origins


def _signed_manifest(transition):
    from management.services import ig_assisted_commerce as commerce
    signed = (transition.effects or {}).get(SCHEMA) or {}
    value = signed.get("manifest") or {}
    signature = commerce._sign("residual_adoption", value, signed.get("key_id"))
    if not signature or not hmac.compare_digest(str(signed.get("hmac") or ""), signature):
        raise AssistedAdoptionError("adoption_signature_invalid")
    if (value.get("schema") != SCHEMA or value.get("source_id") != transition.source_message_id
        or value.get("session_id") != transition.session_id or value.get("to_revision") != transition.to_revision
        or value.get("previous_digest") != commerce._digest(transition.previous_snapshot)
        or value.get("next_digest") != commerce._digest(transition.next_snapshot)):
        raise AssistedAdoptionError("adoption_transition_changed")
    return value


def verify_assisted_adoption_transition(transition, *, now=None):
    """Historical native leaf; it cannot confer fresh manager or stock truth."""
    from management.models import IgCommerceSelectionTransition, IgCustomerTurnRevision, IgFollowUpTask
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_assisted_scopes import original_intention_source_history
    from management.services.ig_assisted_partial_delivery import validate_card_choice_source, AssistedPartialSourceError
    try:
        actual = IgCommerceSelectionTransition.objects.select_related("session", "source_message", "source_message__commerce_turn_decision").get(pk=getattr(transition, "pk", transition))
        if actual.action != ACTION or actual.from_revision != 0 or actual.to_revision != 1 or actual.previous_snapshot.get("lines"):
            raise AssistedAdoptionError("adoption_transition_invalid")
        value = _signed_manifest(actual)
        revision = IgCustomerTurnRevision.objects.get(pk=value["revision_id"], client_id=actual.session.client_id)
        from management.models import InstagramBotSettings
        settings_obj = InstagramBotSettings.objects.get(pk=value["settings_id"])
        native = validate_card_choice_source(actual.source_message, revision, settings_obj, now=now,
            expected_task_id=value["task_id"], require_current=False)
        if (native.action != "resume" or native.source_proof != value["source_proof"]
            or native.receipt != value["receipt"] or native.card["proof"]["card_digest"] != value["card_digest"]):
            raise AssistedAdoptionError("adoption_native_source_changed")
        Request = commerce._models()[0]
        parent = Request.objects.get(pk=value["parent_request_id"], client_id=actual.session.client_id)
        if parent.envelope_hmac != value["parent_request_digest"]:
            raise AssistedAdoptionError("adoption_parent_changed")
        # This historical source-projection leaf runs inside the canonical
        # SELECT-only capture. Reuse the same native immutable ancestry/source
        # verifier without opening a paid-coverage transaction or SAVEPOINT.
        # Fresh coverage is independently mandatory in adopt_residual_source.
        history = original_intention_source_history(parent, now=now)
        if not history.get("ready"):
            raise AssistedAdoptionError(history.get("reason") or "adoption_history_invalid")
        lines, mapping, origins = _rows(history, value["residual"], actual.source_message_id)
        decision = actual.source_message.commerce_turn_decision
        if (history["intention_id"] != value["intention_id"] or history["root_request_id"] != value["root_request_id"]
            or history["root_request_digest"] != value["root_request_digest"]
            or history["root_full_capture_digest"] != value["root_full_capture_digest"]
            or value.get("origin_scope") != {"scope": history["origin_full_capture"]["scope"],
                **{key: history["origin_full_capture"][key] for key in ("session_id", "generation", "selection_revision")}}
            or origins != value["origins"] or mapping != value["line_map"]
            or actual.next_snapshot.get("lines") != lines or decision.transition_id != actual.pk
            or decision.session_id != actual.session_id or not decision.accepted or decision.is_stale
            or decision.delivery_required or decision.request_payload != {"producer": ACTION, "native_payload": actual.source_message.quick_reply_payload}
            or decision.result_payload != {"reason": ACTION, "adoption_digest": commerce._digest(value)}):
            raise AssistedAdoptionError("adoption_origin_or_decision_changed")
        return {"ready": True, "reason": "", "transition_id": actual.pk, **deepcopy(value)}
    except (AssistedAdoptionError, AssistedPartialSourceError, commerce.AssistedDecisionError, KeyError, TypeError, ValueError,
            ObjectDoesNotExist) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "adoption_native_proof_invalid")}


def revalidate_verified_assisted_adoption(value, *, now=None):
    if not isinstance(value, VerifiedAssistedAdoption) or value._issuer is not _ISSUER:
        raise AssistedAdoptionError("adoption_requires_native_producer")
    proof = verify_assisted_adoption_transition(value.transition_id, now=now)
    if not proof.get("ready") or _cap(proof) != value:
        raise AssistedAdoptionError(proof.get("reason") or "adoption_changed")
    return proof


def adopt_residual_source(native_choice, revision, settings_obj, now=None):
    from management.models import IgClient, IgCommerceTurnDecision, IgCommerceSelectionTransition
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_assisted_partial_delivery import revalidate_native_card_choice
    from management.services.ig_assisted_scopes import original_intention_history, intention_coverage
    from management.services.ig_commercial_episodes import commercial_episode_client_lock, start_repeat_episode
    from management.services.ig_commerce_projection import authoritative_session_for, capture_current_selection_lines, project_active_line_to_legacy_client
    from management.services.ig_commerce_state import append_assisted_adoption_transition
    now = now or timezone.now()
    source_id = native_choice.source_proof["message_id"]
    with commercial_episode_client_lock(revision.client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=revision.client_id)
        existing = IgCommerceTurnDecision.objects.select_for_update().filter(source_message_id=source_id).select_related("transition").first()
        if existing:
            proof = verify_assisted_adoption_transition(existing.transition, now=now) if existing.transition else {}
            if not proof.get("ready") or proof.get("revision_id") != revision.pk:
                raise AssistedAdoptionError("adoption_operation_conflict")
            return _cap(proof)
        native = revalidate_native_card_choice(native_choice, now=now, require_current=True)
        if native.action != "resume" or native.source_proof["revision_id"] != revision.pk:
            raise AssistedAdoptionError("adoption_resume_source_required")
        card = native.card["proof"]
        Request = commerce._models()[0]
        parent = Request.objects.select_for_update().get(pk=card["request_id"], client=client)
        if parent.envelope_hmac != card["request_digest"]:
            raise AssistedAdoptionError("adoption_parent_changed")
        history = original_intention_history(parent, now=now)
        coverage = intention_coverage(client.pk, parent.intention_id, now=now, lock=True)
        if (not history.get("ready") or not coverage.get("ready") or coverage.get("coverage_digest") != card["coverage_digest"]
            or coverage.get("inflight") or coverage.get("inflight_line_ids")):
            raise AssistedAdoptionError("adoption_coverage_changed")
        residual = card["residual"]
        deferred = coverage.get("deferred_line_ids", coverage.get("deferred", []))
        if residual != deferred:
            raise AssistedAdoptionError("adoption_residual_changed")
        current = capture_current_selection_lines(client.pk, now=now)
        owner = card.get("owner_scope") or {}
        if (current.get("status") != "captured" or not current.get("coverage_complete")
            or any(current.get(key) != owner.get(key) for key in ("session_id", "generation", "selection_revision"))
            or current.get("scope") != owner.get("scope")):
            raise AssistedAdoptionError("adoption_unrelated_current_cart")
        lines, mapping, origins = _rows(history, residual, source_id)
        source = revision.sources.select_related("message").get(message_id=source_id).message
        episode = start_repeat_episode(client, repeat_kind="explicit_more", evidence_message_ids=[source.pk], confidence=1,
            analysis_model="deterministic_source", analysis_prompt_version=SCHEMA, _lock_held=True, preserve_client_stage=True)
        client.refresh_from_db()
        session = authoritative_session_for(client)
        if session.commercial_episode_id != episode.pk or session.lines or session.revision:
            raise AssistedAdoptionError("adoption_empty_new_session_required")
        previous = session.snapshot()
        next_snapshot = {**deepcopy(previous), "lines": lines, "active_index": 0, "revision": 1,
                         "last_provider_message_id": str(source.mid)}
        manifest = {"schema": SCHEMA, "source_id": source.pk, "revision_id": revision.pk, "settings_id": settings_obj.pk,
            "parent_request_id": str(parent.pk), "parent_request_digest": parent.envelope_hmac,
            "intention_id": str(parent.intention_id), "root_request_id": history["root_request_id"],
            "root_request_digest": history["root_request_digest"], "root_full_capture_digest": history["root_full_capture_digest"],
            "origin_scope": {"scope": history["origin_full_capture"]["scope"],
                **{key: history["origin_full_capture"][key] for key in ("session_id", "generation", "selection_revision")}},
            "coverage_digest": coverage["coverage_digest"], "residual": residual, "line_map": mapping, "origins": origins,
            "task_id": native.task_id, "card_digest": card["card_digest"], "receipt": native.receipt,
            "source_proof": native.source_proof, "old_owner": owner,
            "episode_id": episode.pk, "session_id": session.pk, "generation": session.generation, "to_revision": 1,
            "previous_digest": commerce._digest(previous), "next_digest": commerce._digest(next_snapshot)}
        key_id = next(iter(commerce._keys()))
        signed = {"key_id": key_id, "manifest": manifest, "hmac": commerce._sign("residual_adoption", manifest, key_id)}
        transition = append_assisted_adoption_transition(client, source, session, lines,
            expected_revision=0, next_snapshot=next_snapshot, signed_receipt=signed)
        proof = {"transition_id": transition.pk, **manifest}
        project_active_line_to_legacy_client(session, client)
        return _cap(proof)
