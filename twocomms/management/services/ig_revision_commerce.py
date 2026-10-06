"""Original-source commerce reductions under the sealed revision's authority."""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import hashlib

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    IgClient, IgCommerceTurnDecision, IgCustomerTurnRevision,
    InstagramBotMessage, InstagramBotSettings, IgTurnRevisionSource,
)
from management.services.ig_revision_outbox import _digest
from management.services.ig_commerce_source_identity import resolve_source_product_request


RECEIPT_KEY = "commerce_reduction"
INGRESS_SOURCE_PRODUCER = "revision-inbound-commerce-v1"
OBSERVATION_KEY = "commerce_source_observations"


def source_producer_owned_q():
    """Only original decisions admitted by revision ingress retain that owner."""
    from django.db.models import Q, Subquery

    sources = IgTurnRevisionSource.objects.filter(
        message__commerce_turn_decision__request_payload__source_binding__producer=INGRESS_SOURCE_PRODUCER,
        message__commerce_turn_decision__delivery_required=False,
    )
    return Q(pk__in=Subquery(sources.values("revision_id"))) | Q(
        action_receipts__commerce_source_observations__producer=INGRESS_SOURCE_PRODUCER)


@dataclass(frozen=True)
class RevisionCommerceResult:
    ready: bool = False
    reason: str = ""
    decisions: tuple[dict, ...] = ()
    replayed: bool = False
    observations: tuple[dict, ...] = ()


class _Blocked(Exception):
    pass


def _valid_observation(observation, client, source, floor):
    """A no-mutation receipt still belongs to an exact original customer MID."""
    return isinstance(observation, dict) and all((
        observation.get("schema") == "commerce-source-observation.v1",
        observation.get("classification") in {"neutral", "reorder_clarification"},
        isinstance(observation.get("reason"), str) and 0 < len(observation["reason"]) <= 100,
        observation.get("client_id") == client.pk,
        observation.get("source_message_id") == source.pk,
        observation.get("source_digest") == hashlib.sha256(str(source.text or "").encode()).hexdigest(),
        observation.get("source_namespace") == source.provider_namespace,
        observation.get("reset_floor") == floor,
        observation.get("event_at") == (source.provider_created_at or source.created_at).isoformat(),
        source.pk >= floor,
    ))


def _owned_source_observation(client, source, floor):
    owner = IgCustomerTurnRevision.objects.filter(client=client, sources__message=source,
        action_receipts__commerce_source_observations__producer=INGRESS_SOURCE_PRODUCER,
        action_receipts__has_key=f"commerce_observation:{source.pk}").order_by("pk").first()
    if owner is None:
        return None
    observation = (owner.action_receipts or {}).get(f"commerce_observation:{source.pk}")
    if not _valid_observation(observation, client, source, floor):
        raise _Blocked("commerce_observation_changed")
    return deepcopy(observation)


def _record_source_observation(client, source, floor, observation):
    if not _valid_observation(observation, client, source, floor):
        raise _Blocked("commerce_observation_changed")
    # The existing revision/source ledger is the observation owner. A plain
    # no-reply source needs no new commercial episode, session or decision.
    owner = IgCustomerTurnRevision.objects.select_for_update().filter(
        client=client, sources__message=source, active_slot=1).order_by("pk").first()
    if owner is None:
        return
    receipts = owner.action_receipts or {}
    key = f"commerce_observation:{source.pk}"
    if key in receipts:
        if receipts[key] != observation:
            raise _Blocked("commerce_observation_changed")
        return
    if sum(name.startswith("commerce_observation:") for name in receipts) >= 64:
        raise _Blocked("commerce_observation_bound")
    owner.action_receipts = {**receipts, OBSERVATION_KEY: {
        "schema": "revision-commerce-observations.v1", "producer": INGRESS_SOURCE_PRODUCER,
    }, key: deepcopy(observation)}
    owner.save(update_fields=["action_receipts", "updated_at"])


def _decision_metadata(decision, source_digest):
    return {
        "source_message_id": decision.source_message_id, "source_digest": source_digest,
        "decision_id": decision.pk, "transition_id": decision.transition_id,
        "session_id": decision.session_id, "episode_id": decision.session.commercial_episode_id,
        "action": decision.transition.action if decision.transition_id else str((decision.result_payload or {}).get("reason") or "observed"),
        "accepted": bool(decision.accepted), "is_stale": bool(decision.is_stale),
    }


def reduce_inbound_commerce_source(client, source, *, expected_provider_namespace=None):
    """Admit one accepted webhook source inside its existing client transaction.

    The caller holds the client/settings locks. This adds no delivery authority
    and uses the same source decision that later sealed revisions reference.
    """
    if not connection.in_atomic_block:
        return RevisionCommerceResult(reason="inbound_client_transaction_required")
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_commerce_state import apply_turn, CommerceNonMutation
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    current = IgClient.objects.select_for_update().filter(pk=client.pk).first()
    owned = InstagramBotMessage.objects.select_for_update().filter(pk=source.pk,
        client_id=client.pk, sender_id=client.igsid, role="user", source="webhook").first()
    if current is None or owned is None:
        return RevisionCommerceResult(reason="commerce_source_owner_changed")
    if not owned.provider_namespace or (expected_provider_namespace is not None and owned.provider_namespace != expected_provider_namespace):
        return RevisionCommerceResult(reason="commerce_source_namespace_changed")
    if current.privacy_erasure_started_at is not None:
        return RevisionCommerceResult(reason="client_erasure_changed")
    reset_floor = conversation_route_reset_floor(current.pk)
    if owned.pk < reset_floor:
        return RevisionCommerceResult(reason="commerce_source_before_scope")
    if owned.quick_reply_payload:
        return RevisionCommerceResult(True, "native_source_owned")
    existing = IgCommerceTurnDecision.objects.filter(source_message=owned).select_related("session", "transition").first()
    digest = hashlib.sha256(str(owned.text or "").encode()).hexdigest()
    if existing is not None:
        binding = (existing.result_payload or {}).get("source_facts") or {}
        if binding and (binding.get("source_message_id") != owned.pk or binding.get("source_digest") != digest):
            return RevisionCommerceResult(reason="commerce_source_changed")
        return RevisionCommerceResult(True, "source_already_reduced", (_decision_metadata(existing, digest),), True)
    try:
        observation = _owned_source_observation(current, owned, reset_floor)
    except _Blocked as exc:
        return RevisionCommerceResult(reason=str(exc))
    if observation is not None:
        return RevisionCommerceResult(True, "source_already_observed", replayed=True, observations=(observation,))
    episode_floor = int(getattr(current.current_commercial_episode, "opened_watermark_message_id", 0) or 0)
    if owned.pk < episode_floor:
        return RevisionCommerceResult(reason="commerce_source_before_scope")
    request = resolve_source_product_request(current, owned, parse_turn(owned.text))
    request = replace(request, source_binding={**dict(request.source_binding), "producer": INGRESS_SOURCE_PRODUCER})
    try:
        decision = apply_turn(current, owned, request, reply_payload={})
    except CommerceNonMutation as exc:
        try:
            _record_source_observation(current, owned, reset_floor, exc.observation)
        except _Blocked as blocked:
            return RevisionCommerceResult(reason=str(blocked))
        _apply_source_language(current, owned, origin="inbound")
        client.refresh_from_db()
        return RevisionCommerceResult(True, "source_observed", observations=(deepcopy(exc.observation),))
    _apply_source_language(current, owned, origin="inbound")
    client.refresh_from_db()
    return RevisionCommerceResult(True, "source_reduced", (_decision_metadata(decision, digest),))


def _valid_scope_receipt(client, previous, *, reset_floor):
    """Original per-source episodes may differ across a recorded repeat boundary."""
    scope = previous.get("scope_binding") or {}
    if (scope.get("terminal_episode_id") != client.current_commercial_episode_id
        or scope.get("reset_floor") != reset_floor
        or scope.get("digest") != _digest({key: value for key, value in scope.items() if key != "digest"})):
        return False
    rows = previous.get("decisions") or []
    ids = [row.get("decision_id") for row in rows]
    if scope.get("decision_ids") != ids:
        return False
    observations = previous.get("observations") or []
    if observations and scope.get("observation_ids") != [row.get("source_message_id") for row in observations]:
        return False
    decisions = {row.pk: row for row in IgCommerceTurnDecision.objects.filter(pk__in=ids).select_related("session", "transition")}
    for row in rows:
        decision = decisions.get(row.get("decision_id"))
        if (decision is None or decision.source_message_id != row.get("source_message_id")
            or decision.transition_id != row.get("transition_id")
            or decision.session_id != row.get("session_id")
            or decision.session.commercial_episode_id != row.get("episode_id")):
            return False
    from management.models import IgCommerceSelectionSession
    sessions = IgCommerceSelectionSession.objects.filter(client=client,
        commercial_episode_id=client.current_commercial_episode_id, open_slot=1)
    if scope.get("terminal_session_id") is None:
        return bool(observations) and not rows and not sessions.exists()
    return sessions.filter(pk=scope["terminal_session_id"]).exists()


def _has_owned_repeat_boundary(client, revision):
    episode = client.current_commercial_episode
    if episode is None or episode.repeat_kind != "explicit_more":
        return False
    ids = {row.get("message_id") for row in revision.bundle_snapshot.get("sources") or []}
    boundaries = set(episode.repeat_evidence_message_ids or []) & ids
    return bool(boundaries) and IgCommerceTurnDecision.objects.filter(
        source_message_id__in=boundaries, session__commercial_episode=episode,
        accepted=True, is_stale=False, request_payload__new_purchase_requested=True,
        source_message__client=client, source_message__sender_id=client.igsid,
        source_message__role="user", source_message__source="webhook").exists()


def _apply_source_language(client, source, *, origin):
    from management.services.bot_sales_classifier import (
        _record_language_request, _sticky_language,
        detect_language, detect_language_request,
    )

    requested = detect_language_request(source.text)
    if origin != "inbound" and not requested:
        return
    # apply_turn may have projected selection context through another instance.
    # Preserve those fields while changing only the existing language preference.
    client.refresh_from_db(fields=["language", "sales_context"])
    if requested:
        language = requested
        _record_language_request(client, requested, message=source)
    else:
        language = _sticky_language(client, detect_language(source.text))
    client.language = language
    client.save(update_fields=["language", "sales_context", "updated_at"])


def _validated_sealed_source(client, revision, snapshot):
    # Recovery children copy source rows but preserve the original snapshot IDs.
    if not IgTurnRevisionSource.objects.filter(revision=revision,
        message_id=snapshot.get("message_id"), ordinal=snapshot.get("ordinal"),
        source_digest=snapshot.get("source_digest")).exists():
        raise _Blocked("commerce_source_digest_invalid")
    source = InstagramBotMessage.objects.select_for_update().filter(
        pk=snapshot.get("message_id"), client=client, sender_id=client.igsid,
        role="user", source="webhook").first()
    if source is None or any((
        source.text != snapshot.get("text"),
        source.quick_reply_payload != snapshot.get("quick_reply_payload"),
        source.reply_to_provider_message_id != snapshot.get("reply_to_provider_message_id"),
        source.provider_namespace != snapshot.get("source_namespace"),
        str(source.mid or "") != str(snapshot.get("provider_message_id") or ""),
        (source.provider_created_at.isoformat() if source.provider_created_at else "") != snapshot.get("provider_created_at"),
    )):
        raise _Blocked("commerce_source_changed")
    return source


def reduce_revision_commerce(revision_id, token, *, settings_id, settings_permission_epoch, publication):
    if connection.in_atomic_block:
        return RevisionCommerceResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionCommerceResult(reason="revision_missing")
    from management.services.ig_commerce_state import apply_turn, CommerceNonMutation
    from management.services.ig_commerce_turns import understand_turn

    try:
        with transaction.atomic():
            settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if settings_row is None or client is None or revision is None:
                raise _Blocked("commerce_identity_missing")
            if not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
                raise _Blocked("revision_snapshot_invalid")
            # Incoming customer facts do not require AI/send/publication/window
            # permission. Erasure and reset retain their independent fences.
            if client.privacy_erasure_started_at is not None or client.privacy_erasure_started_at != revision.erasure_started_at_snapshot:
                raise _Blocked("client_erasure_changed")
            from management.services.ig_conversation_routes import conversation_route_reset_floor
            floor = conversation_route_reset_floor(client.pk)
            episode_floor = int(getattr(client.current_commercial_episode, "opened_watermark_message_id", 0) or 0)
            previous = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if previous:
                if previous.get("snapshot_digest") != revision.snapshot_digest:
                    raise _Blocked("commerce_receipt_changed")
                valid_scope = _valid_scope_receipt(client, previous, reset_floor=floor)
                if previous.get("scope_binding") and not valid_scope:
                    raise _Blocked("commerce_decision_scope_changed")
                if any(int(row.get("message_id") or 0) < (floor if valid_scope else max(floor, episode_floor)) for row in revision.bundle_snapshot.get("sources") or []):
                    raise _Blocked("commerce_source_before_scope")
                decisions_ids = [row.get("decision_id") for row in previous.get("decisions") or []]
                if not valid_scope and IgCommerceTurnDecision.objects.filter(pk__in=decisions_ids).exclude(session__commercial_episode_id=client.current_commercial_episode_id).exists():
                    raise _Blocked("commerce_decision_scope_changed")
                for snapshot in revision.bundle_snapshot.get("sources") or []:
                    if not snapshot.get("quick_reply_payload"):
                        source = _validated_sealed_source(client, revision, snapshot)
                        for observation in previous.get("observations") or []:
                            if observation.get("source_message_id") == source.pk and not _valid_observation(observation, client, source, floor):
                                raise _Blocked("commerce_observation_changed")
                return RevisionCommerceResult(True, "already_reduced", tuple(previous["decisions"]), True,
                    tuple(previous.get("observations") or []))
            decisions = []
            observations = []
            catalog_graph = None
            for snapshot in revision.bundle_snapshot.get("sources") or []:
                # Native button mutation has a separate global source ledger.
                if snapshot.get("quick_reply_payload"):
                    continue
                if int(snapshot.get("message_id") or 0) < floor:
                    raise _Blocked("commerce_source_before_scope")
                source = _validated_sealed_source(client, revision, snapshot)
                observation = _owned_source_observation(client, source, floor)
                if observation is not None:
                    observations.append(observation)
                    continue
                existing = IgCommerceTurnDecision.objects.filter(source_message_id=source.pk).first()
                if existing is not None and existing.delivery_required:
                    raise _Blocked("legacy_commerce_delivery_owned")
                if existing is None and source.pk < episode_floor:
                    raise _Blocked("commerce_source_before_scope")
                if existing is not None and existing.session.commercial_episode_id != client.current_commercial_episode_id and not _has_owned_repeat_boundary(client, revision):
                    raise _Blocked("commerce_decision_scope_changed")
                facts = (existing.result_payload or {}).get("source_facts") or {} if existing else {}
                if facts and (facts.get("source_message_id") != source.pk or facts.get("source_digest") != hashlib.sha256(str(source.text or "").encode()).hexdigest()):
                    raise _Blocked("commerce_source_changed")
                request = understand_turn(snapshot.get("text") or "", media_evidence=deepcopy(snapshot.get("media_parts") or []))
                if existing is None:
                    if catalog_graph is None and not request.exact_product_id:
                        from management.services.ig_catalog_graph import build_catalog_graph
                        catalog_graph = build_catalog_graph()
                    request = resolve_source_product_request(client, source, request, catalog_graph=catalog_graph)
                # The source fact is idempotent. Its old delivery fields never
                # become a second send owner, and no source row is cloned.
                try:
                    decision = existing or apply_turn(client, source, request, reply_payload={})
                except CommerceNonMutation as exc:
                    if not _valid_observation(exc.observation, client, source, floor):
                        raise _Blocked("commerce_observation_changed")
                    observations.append(deepcopy(exc.observation))
                    _apply_source_language(client, source, origin=revision.origin)
                    client.refresh_from_db()
                    continue
                if existing is None:
                    _apply_source_language(client, source, origin=revision.origin)
                client.refresh_from_db()
                decisions.append(_decision_metadata(decision, snapshot["source_digest"]))
            from management.models import IgCommerceSelectionSession
            terminal_session_id = IgCommerceSelectionSession.objects.filter(client=client,
                commercial_episode_id=client.current_commercial_episode_id, open_slot=1).values_list("pk", flat=True).first()
            scope_binding = {"terminal_episode_id": client.current_commercial_episode_id,
                "terminal_session_id": terminal_session_id, "reset_floor": floor,
                "decision_ids": [row["decision_id"] for row in decisions],
                "observation_ids": [row["source_message_id"] for row in observations]}
            revision.refresh_from_db(fields=["action_receipts"])
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: {
                "version": "revision-commerce-v1", "snapshot_digest": revision.snapshot_digest,
                "scope_binding": {**scope_binding, "digest": _digest(scope_binding)},
                "decisions": decisions, "observations": observations, "recorded_at": timezone.now().isoformat(),
            }}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionCommerceResult(True, "reduced", tuple(decisions), observations=tuple(observations))
    except _Blocked as exc:
        return RevisionCommerceResult(reason=str(exc))


def synchronize_selected_session(client, *, source_message_id=None):
    """Called inside the protected selection transaction, never a source replay."""
    if not connection.in_atomic_block:
        raise ValueError("selection session synchronization requires transaction")
    from management.models import IgCommerceSelectionSession, IgCommerceSelectionTransition
    from management.services.ig_commerce_projection import _legacy_line, source_preferences_for, project_active_line_to_legacy_client

    session = IgCommerceSelectionSession.objects.select_for_update().filter(client=client, open_slot=1).order_by("-generation").first()
    if session is None:
        # pin_product has already updated the legacy row. Bootstrapping it now
        # would invent a pre-action selection and erase the authorized change.
        from management.services.ig_commercial_episodes import ensure_open_episode_for_locked_client
        episode = ensure_open_episode_for_locked_client(client, materialization_prefix="commerce-selection")
        generation = IgCommerceSelectionSession.objects.filter(client=client).order_by("-generation").values_list("generation", flat=True).first() or 0
        session = IgCommerceSelectionSession.objects.create(client=client,
            commercial_episode_id=episode.pk, generation=int(generation) + 1,
            open_slot=1, state=IgCommerceSelectionSession.State.OPEN, lines=[], active_index=0)
    before = session.snapshot()
    lines = deepcopy(session.lines or [])
    index = int(session.active_index or 0)
    if not lines:
        lines, index = [{}], 0
    if not 0 <= index < len(lines):
        raise ValueError("selection session active index is invalid")
    selected = _legacy_line(client, session.generation)
    selected["line_id"] = str(lines[index].get("line_id") or selected.get("line_id") or f"line:{index}")
    current = lines[index]
    if not current.get("product_id") and selected.get("product_id"):
        # First identity resolves the existing requirement. Preserve only
        # independently proved partial choices, never legacy bootstrap values.
        partial = source_preferences_for(client)
        if partial.get("session_id") == session.pk and partial.get("line_id") == selected["line_id"]:
            for key in ("size", "color", "fit_option_code"):
                value = (partial.get("values") or {}).get(key)
                if value not in (None, ""):
                    if selected.get(key) not in (None, "", value):
                        raise ValueError("selection_partial_source_conflict")
                    selected[key] = value
    # Projection-only fields are not selection changes. A replay must not
    # manufacture another revision/transition against the same selection action.
    keys = ("product_id", "size", "color", "quantity", "fit_option_code", "color_variant_id", "option_values", "pay_type")
    if all(current.get(key) == selected.get(key) or (current.get(key) in (None, "") and selected.get(key) in (None, "")) for key in keys):
        return {"session_id": session.pk, "before": before, "after": before}
    source = InstagramBotMessage.objects.select_for_update().filter(pk=source_message_id, client=client, sender_id=client.igsid, role="user", source="webhook").first()
    existing = IgCommerceTurnDecision.objects.filter(source_message=source).first() if source else None
    prior_action = IgCommerceSelectionTransition.objects.filter(source_message=source, action="selection_authorized").first() if source else None
    if (source is None or prior_action is not None
        or (existing is not None and (existing.session_id != session.pk or existing.is_stale))):
        raise ValueError("selection_source_already_reduced_or_missing")
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    if client.privacy_erasure_started_at is not None or source.pk < conversation_route_reset_floor(client.pk) or session.commercial_episode_id != client.current_commercial_episode_id:
        raise ValueError("selection_source_scope_changed")
    lines[index] = selected
    session.lines = lines
    session.active_index = index
    session.candidate_product_ids = []
    session.candidate_digest = ""
    session.candidate_prompt_provider_ids = []
    session.candidate_generation = int(session.candidate_generation or 0) + 1
    session.rejected_selection = {}
    session.rejected_reason = ""
    session.pending_field = ""
    session.pending_clarification = ""
    session.revision = int(session.revision or 0) + 1
    session.save(update_fields=["lines", "active_index", "candidate_product_ids", "candidate_digest", "candidate_prompt_provider_ids", "candidate_generation", "rejected_selection", "rejected_reason", "pending_field", "pending_clarification", "revision", "updated_at"])
    from management.services.ig_commerce_state import _event_key
    event_at, event_id = _event_key(source)
    after = session.snapshot()
    transition = IgCommerceSelectionTransition.objects.create(
        session=session, source_message=source, action="selection_authorized",
        from_revision=before["revision"], to_revision=after["revision"],
        previous_snapshot=before, next_snapshot=after, effects={}, reasons=["validated_revision_selection"],
        source_order_key=f"{event_at.isoformat()}|{event_id}",
    )
    if existing is None:
        IgCommerceTurnDecision.objects.create(source_message=source, session=session, transition=transition,
            request_payload={}, result_payload={"reason": "validated_revision_selection"},
            accepted=True, delivery_required=False, delivery_state="not_required")
    project_active_line_to_legacy_client(session, client)
    return {"session_id": session.pk, "transition_id": transition.pk,
            "source_message_id": source.pk, "before": before, "after": session.snapshot()}


def selected_session_receipt_current(client, receipt):
    from management.models import IgCommerceSelectionSession

    session = IgCommerceSelectionSession.objects.filter(pk=receipt.get("session_id"), client=client, open_slot=1).first()
    return bool(session is not None and session.snapshot() == receipt.get("after"))
