"""Bounded outage successors from original sealed sources, without provider I/O.

Recovery scheduling lives on the revision, separately from operator cases.
An admission is durable before generation; a recovery child never manufactures
new customer consent, extends its parent's deadline, or retries a sent effect.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurn, IgCustomerTurnRevision,
    IgProviderIncident, IgRevisionDeliveryEffect, IgWebhookInboxEvent,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    _active_opt_out, _cas_readiness, _digest, _normal_reply_window_deadline,
    _revision_namespace, _safe_bindings,
)
from management.services.ig_turn_revisions import (
    _copied_source_payloads, _create_source_rows,
)


RECOVERY_ORIGIN = "outage_recovery"
MAX_RECOVERY_GENERATIONS = 8
RECOVERY_LIFETIME = timedelta(seconds=45)
INCIDENT_DELAY = timedelta(seconds=45)
RETRY_DELAYS = (45, 120, 300)
MAX_LINEAGE = 10
EXECUTION_RESUME_KEY = "proposal_execution_resume"
POLICY_READINESS_FAILURE_KEY = "policy_readiness_failure"
LOCAL_POLICY_READINESS_CODES = frozenset({
    "mandatory_policy_exceeds_budget", "required_policy_exceeds_budget",
    "captured_state_exceeds_budget", "invalid_policy_budget",
    "invalid_required_policy_module", "required_scenario_module_missing",
    "invalid_mandatory_policy", "duplicate_mandatory_policy_id",
    "unnamed_mandatory_policy", "approved_public_facts_unavailable",
})


@dataclass(frozen=True)
class RecoveryAdmissionResult:
    ready: bool = False
    reason: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    revision_id: int = 0
    state: str = ""
    reason: str = ""
    child_id: int = 0


def _selection_digest(client):
    # Absence is material too: a product/episode introduced after a failed
    # request must not silently reuse the customer's earlier purchase assent.
    return _digest({name: getattr(client, name) for name in (
        "current_product_id", "current_size", "current_color", "current_qty",
        "current_commercial_episode_id",
    )})


def record_generation_admission(
    revision_id, token, *, settings_id, settings_permission_epoch,
    publication, authority, now=None,
):
    """Persist bounded source/authority fingerprints before opening a graph."""
    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None or not authority.ready:
        return RecoveryAdmissionResult(reason="generation_admission_unavailable")
    try:
        facts, offers = _safe_bindings(authority.fact_bindings), _safe_bindings(authority.offer_bindings)
    except (TypeError, ValueError):
        return RecoveryAdmissionResult(reason="generation_admission_invalid")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if revision is None or client is None:
            return RecoveryAdmissionResult(reason="revision_missing")
        readiness = _cas_readiness(
            revision, client=client, settings_obj=settings_row, revision_token=token,
            settings_id=settings_id, settings_permission_epoch=settings_permission_epoch,
            publication=publication, fact_bindings=facts, offer_bindings=offers,
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
        )
        if not readiness.ready:
            return RecoveryAdmissionResult(reason=readiness.reasons[0])
        receipt = {
            "schema_version": 1, "revision_id": revision.pk,
            "logical_turn_id": f"ig-revision:{revision.pk}",
            "snapshot_digest": revision.snapshot_digest,
            "settings_id": settings_id, "settings_permission_epoch": settings_permission_epoch,
            "publication": {"id": publication.publication_id, "version": publication.version, "hash": publication.snapshot_hash},
            "fact_bindings": facts, "offer_bindings": offers,
            "selection_digest": _selection_digest(client),
        }
        receipt["digest"] = _digest(receipt)
        existing = (revision.action_receipts or {}).get("generation_admission")
        if existing:
            return RecoveryAdmissionResult(existing == receipt, "existing_admission" if existing == receipt else "generation_admission_changed")
        if GeminiRequest.objects.filter(logical_turn_id=receipt["logical_turn_id"]).exists():
            return RecoveryAdmissionResult(reason="generation_admission_missing_before_request")
        revision.action_receipts = {**(revision.action_receipts or {}), "generation_admission": receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RecoveryAdmissionResult(True, "admitted")


def policy_readiness_failure_code(failure):
    """Keep the compiler's finite local reason without inventing a provider failure."""
    if not isinstance(failure, dict) or failure.get("kind") != "invalid_payload":
        return ""
    code = failure.get("policy_readiness")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else ""


def _policy_failure_details(details):
    """Content-free counts and module identities only; never prompt/customer text."""
    if not isinstance(details, dict):
        return {}
    result = {key: value for key in ("budget_chars", "required_chars", "mandatory_total_chars", "mandatory_count")
        if type(value := details.get(key)) is int and 0 <= value <= 10_000_000}
    for key in ("mandatory_ids", "required_ids"):
        values = details.get(key)
        if isinstance(values, (tuple, list)) and len(values) <= 128:
            result[key] = [value for value in values if isinstance(value, str)
                and re.fullmatch(r"[a-zA-Z0-9_:.-]{1,120}", value)]
    lengths = details.get("mandatory_module_lengths")
    if isinstance(lengths, dict) and len(lengths) <= 128:
        result["mandatory_module_lengths"] = {key: value for key, value in lengths.items()
            if isinstance(key, str) and re.fullmatch(r"[a-zA-Z0-9_:.-]{1,120}", key)
            and type(value) is int and 0 <= value <= 10_000_000}
    return result


def record_policy_readiness_failure(
    revision_id, token, *, settings_id, settings_permission_epoch, publication,
    code, details=None, now=None,
):
    """Park a proven local zero-dispatch fault as real source-bound reply debt.

    This is not provider recovery. A separately audited manager Resume may
    create a fresh source successor after the configuration is repaired.
    """
    if code not in LOCAL_POLICY_READINESS_CODES:
        return RecoveryAdmissionResult(reason="policy_failure_code_unsupported")
    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RecoveryAdmissionResult(reason="revision_missing")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if revision is None or client is None:
            return RecoveryAdmissionResult(reason="revision_missing")
        if (revision.generation_proposal_digest or revision.generation_proposal
            or revision.delivery_effects.exists()
            or GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists()):
            return RecoveryAdmissionResult(reason="policy_failure_dispatch_not_excluded")
        admission = (revision.action_receipts or {}).get("generation_admission") or {}
        material = {key: value for key, value in admission.items() if key != "digest"}
        expected_publication = {"id": publication.publication_id, "version": publication.version, "hash": publication.snapshot_hash}
        if (not admission or admission.get("digest") != _digest(material)
            or admission.get("revision_id") != revision.pk
            or admission.get("logical_turn_id") != f"ig-revision:{revision.pk}"
            or admission.get("snapshot_digest") != revision.snapshot_digest
            or admission.get("selection_digest") != _selection_digest(client)
            or admission.get("settings_id") != settings_id
            or admission.get("settings_permission_epoch") != settings_permission_epoch
            or admission.get("publication") != expected_publication):
            return RecoveryAdmissionResult(reason="policy_failure_admission_changed")
        from management.services.ig_revision_holding import _sources_unchanged
        try:
            sources_current = _sources_unchanged(revision)
        except (TypeError, ValueError, KeyError):
            sources_current = False
        if not sources_current:
            return RecoveryAdmissionResult(reason="policy_failure_sources_changed")
        readiness = _cas_readiness(
            revision, client=client, settings_obj=settings_row, revision_token=token,
            settings_id=settings_id, settings_permission_epoch=settings_permission_epoch,
            publication=publication, fact_bindings=admission.get("fact_bindings") or [],
            offer_bindings=admission.get("offer_bindings") or [],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
        )
        if not readiness.ready:
            return RecoveryAdmissionResult(reason=readiness.reasons[0])
        receipt = {
            "schema": "revision-policy-readiness.v1", "revision_id": revision.pk,
            "logical_turn_id": admission["logical_turn_id"], "snapshot_digest": revision.snapshot_digest,
            "source_message_ids": [row["message_id"] for row in revision.bundle_snapshot["sources"]],
            "generation_admission_digest": admission["digest"],
            "settings_id": settings_id, "settings_permission_epoch": settings_permission_epoch,
            "publication": expected_publication, "code": code,
            "details": _policy_failure_details(details), "provider_dispatched": False,
        }
        receipt["digest"] = _digest(receipt)
        existing = (revision.action_receipts or {}).get(POLICY_READINESS_FAILURE_KEY)
        if existing and existing != receipt:
            return RecoveryAdmissionResult(reason="policy_failure_receipt_changed")
        revision.action_receipts = {**(revision.action_receipts or {}), POLICY_READINESS_FAILURE_KEY: receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        _set_state(revision, "manual", code)
        return RecoveryAdmissionResult(True, code)


def _set_state(revision, state, code, *, due_at=None, child_id=0):
    from management.services.ig_response_debt import response_obligation_paused

    # A stale recovery callback cannot wake the exact request after a customer
    # wait, an explicit handled decision, or a confirmed manager reply. A new
    # relevant source or audited resume owns a separate successor revision.
    if response_obligation_paused(revision):
        return RecoveryResult(revision.pk, revision.recovery_state, revision.recovery_code)
    revision.recovery_state = state
    revision.recovery_code = code
    revision.recovery_due_at = due_at
    update_fields = ["recovery_state", "recovery_code", "recovery_due_at", "updated_at"]
    if state == "cancelled":
        # A permission/takeover fence cancels execution ownership as well as
        # the recovery branch. Leaving the old lease in place makes the normal
        # revision queue reclaim a dead claim and emit recurring debt alerts.
        revision.claim_token = ""
        revision.claimed_at = None
        revision.lease_until = None
        update_fields.extend(["claim_token", "claimed_at", "lease_until"])
    revision.save(update_fields=update_fields)
    if state == "manual" and code != "recovery_existing_proposal_requires_execution":
        from management.services.ig_response_debt import park_manual_revision

        park_manual_revision(revision, code)
    return RecoveryResult(revision.pk, state, code, child_id)


def _lineage(head, *, lock=True):
    rows, seen = [], set()
    current = head
    while current is not None and len(rows) < MAX_LINEAGE:
        if current.pk in seen or current.client_id != head.client_id or current.snapshot_digest != head.snapshot_digest:
            return [], "recovery_lineage_invalid"
        rows.append(current)
        seen.add(current.pk)
        if current.origin not in {RECOVERY_ORIGIN, "auto_refresh"}:
            break
        query = IgCustomerTurnRevision.objects.select_for_update() if lock else IgCustomerTurnRevision.objects.all()
        current = query.filter(pk=current.parent_id).first()
    else:
        return [], "recovery_lineage_invalid"
    if not rows or rows[-1].origin not in {"inbound", "manual_resume"}:
        return [], "recovery_lineage_invalid"
    round_number = 0
    for row in reversed(rows):
        if row.origin == RECOVERY_ORIGIN:
            round_number += 1
            expected = {"root_revision_id": rows[-1].pk, "round": round_number, "source_snapshot_digest": head.snapshot_digest}
            if (row.action_receipts or {}).get("recovery_lineage") != expected:
                return [], "recovery_lineage_invalid"
    return rows, ""


def recovery_lineage_for_authority(head):
    """Read-only immutable lineage proof for permission consumers outside atomic.

    Returns head-first rows and an empty reason on success. The caller still
    validates its actor/audit/epoch receipt; lineage alone grants no permission.
    """
    return _lineage(head, lock=False)


def terminal_semantic_failure_reason(revision):
    """A conclusive content rejection is not an unchanged-source outage retry.

    The provider's bounded repair/rotation already finished inside this graph.
    A new user source or explicit manual resume has separate source authority;
    only the identical immutable recovery lineage shares this disposition.
    """
    rows, reason = _lineage(revision, lock=False)
    if reason:
        return ""
    # An audited policy/fact refresh has new context. Only outage successors
    # inherit a completed semantic rejection without producing a new graph.
    unchanged = [rows[0]]
    for previous, parent in zip(rows, rows[1:]):
        if previous.origin != RECOVERY_ORIGIN:
            break
        unchanged.append(parent)
    rows = unchanged
    owners = {f"ig-revision:{row.pk}": row for row in rows}
    graph = GeminiRequest.objects.filter(
        Q(logical_turn_id__in=owners) | Q(source_execution_key__in=owners),
    ).order_by("-pk").first()
    if (graph is None or graph.logical_turn_id != graph.source_execution_key
        or graph.logical_turn_id not in owners or graph.client_id != revision.client_id
        or graph.lane != "live" or graph.terminal_resolution != "failed"
        or graph.terminal_reason != "local_semantic_rejection" or graph.winner_attempt_id):
        return ""
    owner = owners[graph.logical_turn_id]
    if graph.source_message_id not in [row.get("message_id") for row in (owner.bundle_snapshot or {}).get("sources", ())]:
        return ""
    attempt = GeminiRequestAttempt.objects.filter(
        request_graph=graph, provider_started_at__isnull=False,
    ).order_by("-pk").first()
    if (attempt is None or attempt.fsm_state != "failed" or attempt.http_code != 200
        or attempt.failure_kind != "local_semantic_rejection" or not attempt.error_detail
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}(?:,[a-z][a-z0-9_]{0,63}){0,15}", attempt.error_detail)):
        return ""
    return "generation_semantic_rejected"


def continuous_wait_revisions(revision, *, limit=64):
    """Bounded source-scoped history for notification policy, never send authority.

    Missing route scope cannot grant another holding. A reset, proven disjoint
    episode/request or delivered substantive answer bounds the unresolved wait.
    Unknown/started receipts remain part of the wait after a worker restart.
    """
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_response_debt import response_coverage
    from management.services.ig_turn_intent import _scope_for_sources, _disjoint_scopes, confirmed_substantive_reply

    client = revision.client
    if client.privacy_erasure_started_at or revision.erasure_started_at_snapshot:
        return []
    floor = conversation_route_reset_floor(client.pk)
    current_ids = {row.get("message_id") for row in (revision.bundle_snapshot or {}).get("sources", ())}
    current_reply_refs = {row.get("reply_to_provider_message_id")
                          for row in (revision.bundle_snapshot or {}).get("sources", ())} - {None, ""}
    if not current_ids or any(not isinstance(pk, int) or pk < floor for pk in current_ids):
        return []
    scope = _scope_for_sources(client, current_ids)
    rows = list(IgCustomerTurnRevision.objects.filter(
        client_id=client.pk,
        revision__lte=revision.revision,
    ).order_by("-revision")[:limit])
    linked = []
    for row in rows:
        ids = {source.get("message_id") for source in (row.bundle_snapshot or {}).get("sources", ())}
        if (not ids or any(not isinstance(pk, int) or pk < floor for pk in ids)
            or not row.snapshot_digest or _digest(row.bundle_snapshot) != row.snapshot_digest
            or row.erasure_started_at_snapshot):
            continue
        old_scope = _scope_for_sources(client, ids)
        if _disjoint_scopes(scope, old_scope) or _disjoint_scopes(old_scope, scope):
            continue
        # A true complete answer closes the wait, but an ack/partial fallback
        # and an operator's reviewed-no-reply alert never become an answer.
        safe = (row.action_receipts or {}).get("provider_safe_reply") or {}
        same_request = bool(current_ids.intersection(ids) or any(
            source.get("provider_message_id") in current_reply_refs
            for source in row.bundle_snapshot.get("sources", ())))
        if (row.pk != revision.pk and same_request and not response_coverage(row).get("remaining")
            and safe.get("reply_mode") != "neutral_ack" and confirmed_substantive_reply(row)):
            break
        linked.append(row)
    return linked


def wait_notification_receipts(revision, *, exclude_revision_id=None):
    """Existing physical effects/reservations suppress repeat optional notices."""
    from management.services.ig_revision_holding import RECEIPT_KEY, SAFE_RECEIPT_KEY

    receipts = []
    history = continuous_wait_revisions(revision)
    if len(history) >= 64:
        receipts.append({"revision_id": revision.pk, "state": "history_bound_unknown",
                         "notification": True, "text": "", "snapshot_digest": revision.snapshot_digest})
    for row in history:
        if row.pk == exclude_revision_id:
            continue
        ids = {source["message_id"] for source in row.bundle_snapshot.get("sources", ())}
        effects = list(row.delivery_effects.order_by("order_index", "id"))
        for effect in effects:
            if (effect.actor != "bot" or effect.source_message_id not in ids
                or effect.recipient_igsid != revision.client.igsid
                or effect.provider_namespace != _revision_namespace(row)
                or effect.client_permission_epoch != row.permission_epoch
                or effect.revision_snapshot_digest != row.snapshot_digest
                or _digest(effect.payload) != effect.payload_digest
                or effect.state not in {"sent", "unknown", "provider_started", "planned", "claimed"}):
                continue
            admission = (row.action_receipts or {}).get(RECEIPT_KEY) or {}
            neutral = (row.action_receipts or {}).get(SAFE_RECEIPT_KEY) or {}
            text = str((effect.payload.get("message") or {}).get("text") or "")
            receipts.append({"revision_id": row.pk, "effect_id": effect.pk, "state": effect.state,
                             "text": text, "notification": effect.purpose == "technical_holding"
                             or bool(neutral.get("reply_mode") == "neutral_ack"),
                             "snapshot_digest": row.snapshot_digest})
        # Reserve under the existing client lock before effects are planned.
        # A definite failure/cancellation permits retry; SENT/UNKNOWN do not.
        admission = (row.action_receipts or {}).get(RECEIPT_KEY) or {}
        if (not effects and admission.get("snapshot_digest") == row.snapshot_digest
            and admission.get("reply_digest") == _digest(admission.get("reply_text") or "")):
            receipts.append({"revision_id": row.pk, "state": "reserved", "notification": True,
                             "text": admission.get("reply_text") or "", "snapshot_digest": row.snapshot_digest})
    return receipts


def _eligibility(revision, client, rows, now):
    if revision.active_slot != 1 or revision.state not in {"sealed", "claimed"}:
        return "cancelled", "recovery_head_changed"
    if (client.reply_permission_epoch != revision.permission_epoch or client.hidden_at
        or client.bot_paused or client.manager_takeover or client.is_blocked
        or client.privacy_erasure_started_at is not None or revision.erasure_started_at_snapshot is not None
        or _active_opt_out(client)):
        return "cancelled", "recovery_permission_changed"
    source_rows = list(revision.sources.order_by("ordinal", "id"))
    snapshot = revision.bundle_snapshot.get("sources") or []
    if (not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or not source_rows or len(source_rows) != revision.source_count or len(snapshot) != len(source_rows)
        or any(not isinstance(item, dict) or item.get("message_id") != row.message_id
               or item.get("ordinal") != row.ordinal or item.get("source_digest") != row.source_digest
               for item, row in zip(snapshot, source_rows))):
        return "manual", "recovery_snapshot_invalid"
    ids = [row.message_id for row in source_rows]
    if set(InstagramBotMessage.objects.filter(pk__in=ids, client_id=client.pk, role="user").values_list("pk", flat=True)) != set(ids):
        return "cancelled", "recovery_sources_invalidated"
    if InstagramBotMessage.objects.filter(client_id=client.pk, role="user", id__gt=max(ids)).exists():
        return "cancelled", "recovery_newer_inbound"
    if InstagramBotMessage.objects.filter(client_id=client.pk, role="manager", id__gt=max(ids)).exclude(status="failed").exists():
        return "cancelled", "recovery_manager_answered"
    window = _normal_reply_window_deadline(revision)
    if window is None or now + RECOVERY_LIFETIME >= window:
        return "cancelled", "recovery_source_window_closed"
    namespace = _revision_namespace(revision)
    if not namespace:
        return "manual", "recovery_namespace_missing"
    if IgWebhookInboxEvent.objects.filter(namespace=namespace, customer_igsid=client.igsid, decision__in=("accepted", "blocked"), processed_at__isnull=True).exists():
        return "cancelled", "recovery_pending_inbound"
    turn = IgCustomerTurn.objects.filter(pk=revision.turn_id, client_id=client.pk).first()
    # A manual-resume lineage needs its own separately verified authorization;
    # never clear a legacy terminal state to make recovery eligible.
    if turn is None:
        return "cancelled", "recovery_turn_missing"
    if turn.terminal_reason or turn.claim_state in {"processed", "superseded"}:
        from management.services.ig_revision_manual_resume import manual_resume_authority_current

        if not (revision.action_receipts or {}).get("manual_resume_authorization") or not manual_resume_authority_current(revision):
            return "cancelled", "recovery_turn_terminal"
    if IgRevisionDeliveryEffect.objects.filter(revision_id__in=[row.pk for row in rows], state__in=("sent", "provider_started", "unknown", "definite_failed")).exists():
        return "manual", "recovery_delivery_reconciliation"
    return "", ""


def _generation_proof(rows, client, settings_row):
    from management.services.ig_revision_proposal import _generation_graph_matches

    for revision in rows:
        receipt = (revision.action_receipts or {}).get("generation_admission") or {}
        material = {key: value for key, value in receipt.items() if key != "digest"}
        graphs = list(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}")[:2])
        if len(graphs) > 1:
            return "recovery_generation_graph_conflict"
        if not receipt or receipt.get("digest") != _digest(material):
            return "recovery_generation_admission_missing"
        if receipt:
            if (receipt.get("revision_id") != revision.pk or receipt.get("logical_turn_id") != f"ig-revision:{revision.pk}"
                or receipt.get("snapshot_digest") != revision.snapshot_digest
                or receipt.get("settings_id") != settings_row.pk
                or receipt.get("settings_permission_epoch") != settings_row.reply_permission_epoch):
                return "recovery_admission_binding_changed"
            facts = [item for item in receipt.get("fact_bindings", ()) if item.get("claim") != CLAIM_PUBLIC_POLICY_INPUTS]
            offers = receipt.get("offer_bindings") or []
            if (receipt.get("selection_digest") != _selection_digest(client)
                or (facts and not check_fact_bindings(facts, revision=revision, client=client, settings_obj=settings_row))
                or (offers and not check_offer_bindings(offers, revision=revision, client=client, settings_obj=settings_row))):
                return "recovery_financial_authority_stale"
        if not graphs:
            # Admission may have committed just before a pre-HTTP crash. Its
            # original authority still has to match, even without a graph.
            if revision.generation_proposal_digest:
                return "recovery_generation_graph_missing"
            continue
        graph = graphs[0]
        if graph.client_id != client.pk or graph.source_message_id not in list(revision.sources.values_list("message_id", flat=True)):
            return "recovery_generation_graph_conflict"
        if graph.terminal_resolution == "succeeded":
            proposal = revision.generation_proposal or {}
            generation = proposal.get("generation") or {}
            if (not revision.generation_proposal_digest or _digest(proposal) != revision.generation_proposal_digest
                or generation.get("request_id") != graph.request_id
                or not _generation_graph_matches(revision=revision, source_ids=list(revision.sources.values_list("message_id", flat=True)),
                    request_id=graph.request_id, model=generation.get("actual_model", ""), policy_manifest=proposal.get("policy_manifest", {}))):
                return "recovery_success_result_missing"
            if revision.pk == rows[0].pk:
                # The active head already has a conclusive generated answer.
                # Its execution/reconciliation debt must not mint a new model
                # request or discard that proposal in a blank successor.
                return "recovery_existing_proposal_requires_execution"
        elif graph.terminal_resolution != "failed" or graph.winner_attempt_id:
            return "recovery_generation_outcome_unknown"
        # Transport/schema failures retire their frozen candidate scope. The
        # shared provider execution classifier proves global exhaustion below.
    return ""


def execution_resume_is_current(revision, *, now=None):
    """Execution-only deadline exception; never grants provider admission.

    The receipt is immutable and its deadline cannot be renewed. Callers still
    apply the ordinary claim, permission, publication and financial CAS.
    """
    from management.services.ig_revision_provider_execution import _manifest, REFERENCE_KEY
    from management.services.ig_revision_burst_budget import economic_root
    from management.services.ig_revision_proposal import _generation_graph_matches

    now = now or timezone.now()
    receipt = (revision.action_receipts or {}).get(EXECUTION_RESUME_KEY) or {}
    proposal = revision.generation_proposal or {}
    generation = proposal.get("generation") or {}
    root = economic_root(revision) if receipt else None
    manifest = _manifest(root) if root else {}
    if not receipt or not manifest:
        return False
    if revision.pk != root.pk and (revision.action_receipts or {}).get(REFERENCE_KEY) != {"root_revision_id": root.pk, "manifest_digest": manifest["digest"]}:
        return False
    try:
        started = timezone.datetime.fromisoformat(receipt["issued_at"])
        expires = timezone.datetime.fromisoformat(receipt["expires_at"])
        horizon = timezone.datetime.fromisoformat(manifest["horizon_at"])
        window = _normal_reply_window_deadline(revision)
        if (not window or not started <= now < expires
            or expires > min(started + RECOVERY_LIFETIME, horizon, window)):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    sources = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    execution = proposal.get("execution_binding") or {}
    if (receipt.get("digest") != _digest({key: value for key, value in receipt.items() if key != "digest"})
        or receipt.get("version") != 1
        or receipt.get("revision_id") != revision.pk
        or receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("proposal_digest") != revision.generation_proposal_digest
        or not revision.generation_proposal_digest or _digest(proposal) != revision.generation_proposal_digest
        or receipt.get("root_revision_id") != root.pk or receipt.get("manifest_digest") != manifest["digest"]
        or receipt.get("permission_epoch") != revision.permission_epoch
        or receipt.get("settings_id") != execution.get("settings_id")
        or receipt.get("settings_permission_epoch") != execution.get("settings_permission_epoch")
        or receipt.get("settings_id") != manifest["settings_id"]
        or receipt.get("settings_permission_epoch") != manifest["settings_permission_epoch"]
        or not sources or receipt.get("source_message_ids") != sources
        or sources != [item.get("message_id") for item in revision.bundle_snapshot.get("sources", [])]
        or receipt.get("request_id") != generation.get("request_id")):
        return False
    # An ingress fence normally supersedes the head; also fail closed when a
    # newly persisted source has not yet reached that producer.
    if InstagramBotMessage.objects.filter(client_id=revision.client_id, role__in=("user", "manager"), id__gt=max(sources)).exclude(role="manager", status="failed").exists():
        return False
    if revision.delivery_effects.filter(state__in=("provider_started", "unknown", "definite_failed")).exists():
        return False
    graph = GeminiRequest.objects.filter(request_id=receipt["request_id"]).first()
    return bool(graph and graph.winner_attempt_id == receipt.get("winner_attempt_id")
        and _generation_graph_matches(revision=revision, source_ids=sources,
            request_id=graph.request_id, model=generation.get("actual_model", ""),
            policy_manifest=proposal.get("policy_manifest") or {}, lock=False))


def _authorize_proposal_execution(revision, client, settings_row, *, now):
    """Issue once under settings -> client -> revision; no new graph or child."""
    from management.services.ig_revision_provider_execution import _manifest, REFERENCE_KEY
    from management.services.ig_revision_burst_budget import economic_root
    from management.services.ig_revision_outbox import PublicationBinding

    if (revision.action_receipts or {}).get(EXECUTION_RESUME_KEY):
        if execution_resume_is_current(revision, now=now):
            return _set_state(revision, "execution", "recovery_existing_proposal_requires_execution", due_at=now)
        return _set_state(revision, "manual", "proposal_execution_resume_expired")
    rows, code = _lineage(revision)
    if code:
        return _set_state(revision, "manual", code)
    state, code = _eligibility(revision, client, rows, now)
    if state:
        return _set_state(revision, state, code)
    if settings_row is None or not settings_row.is_enabled or not settings_row.ai_enabled:
        return _set_state(revision, "cancelled", "recovery_settings_disabled")
    from management.services.instagram_bot import ingress_provider_namespace

    if ingress_provider_namespace(settings_row) != _revision_namespace(revision):
        return _set_state(revision, "cancelled", "recovery_channel_changed")
    code = _generation_proof(rows, client, settings_row)
    if code != "recovery_existing_proposal_requires_execution":
        return _set_state(revision, "manual", code or "recovery_success_result_missing")
    root = economic_root(revision)
    manifest = _manifest(root) if root else {}
    if not manifest:
        return _set_state(revision, "manual", "provider_manifest_missing")
    if revision.pk != root.pk and (revision.action_receipts or {}).get(REFERENCE_KEY) != {"root_revision_id": root.pk, "manifest_digest": manifest["digest"]}:
        return _set_state(revision, "manual", "provider_reference_invalid")
    if manifest["settings_id"] != settings_row.pk or manifest["settings_permission_epoch"] != settings_row.reply_permission_epoch:
        return _set_state(revision, "cancelled", "provider_permission_changed")
    horizon = timezone.datetime.fromisoformat(manifest["horizon_at"])
    if horizon <= now:
        return _set_state(revision, "manual", "provider_horizon_exhausted")
    if client.automation_lease_token and client.automation_lease_until and client.automation_lease_until > now:
        if client.automation_lease_until >= horizon:
            return _set_state(revision, "manual", "provider_horizon_exhausted")
        return _set_state(revision, "waiting", "proposal_execution_lease_wait", due_at=client.automation_lease_until)
    proposal = revision.generation_proposal
    execution = proposal.get("execution_binding") or {}
    pub = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
    authority = proposal.get("authority") or {}
    if (execution != {"settings_id": settings_row.pk, "settings_permission_epoch": settings_row.reply_permission_epoch}
        or not all(key in pub for key in ("id", "version", "hash"))
        or "authority_digest" not in authority):
        return _set_state(revision, "manual", "proposal_execution_binding_missing")
    readiness = _cas_readiness(revision, client=client, settings_obj=settings_row,
        revision_token=revision.claim_token, settings_id=settings_row.pk,
        settings_permission_epoch=settings_row.reply_permission_epoch,
        publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
        fact_bindings=authority.get("fact_bindings") or [], offer_bindings=authority.get("offer_bindings") or [],
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)
    reasons = set(readiness.reasons) - {"revision_not_current", "revision_deadline_exhausted"}
    if reasons:
        return _set_state(revision, "manual", sorted(reasons)[0])
    expires = min(now + RECOVERY_LIFETIME, timezone.datetime.fromisoformat(manifest["horizon_at"]), _normal_reply_window_deadline(revision))
    if expires <= now:
        return _set_state(revision, "manual", "provider_horizon_exhausted")
    graph = GeminiRequest.objects.get(request_id=proposal["generation"]["request_id"])
    receipt = {"version": 1, "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
        "proposal_digest": revision.generation_proposal_digest, "request_id": graph.request_id,
        "winner_attempt_id": graph.winner_attempt_id, "root_revision_id": root.pk,
        "manifest_digest": manifest["digest"], "permission_epoch": revision.permission_epoch,
        "settings_id": settings_row.pk, "settings_permission_epoch": settings_row.reply_permission_epoch,
        "source_message_ids": list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True)),
        "issued_at": now.isoformat(), "expires_at": expires.isoformat()}
    receipt["digest"] = _digest(receipt)
    revision.action_receipts = {**revision.action_receipts, EXECUTION_RESUME_KEY: receipt}
    # The ordinary 180-second revision lease can outlive generation. Revoke
    # its stale token in the same transaction as the execution-only authority.
    revision.claim_token = ""
    revision.claimed_at = None
    revision.lease_until = now
    revision.state = revision.State.CLAIMED
    revision.save(update_fields=["action_receipts", "claim_token", "claimed_at", "lease_until", "state", "updated_at"])
    return _set_state(revision, "execution", "recovery_existing_proposal_requires_execution", due_at=now)


def _classify_terminal_semantic_failure(revision, client, *, now):
    reason = terminal_semantic_failure_reason(revision)
    if not reason:
        return None
    rows, lineage_reason = _lineage(revision)
    if lineage_reason:
        return _set_state(revision, "manual", lineage_reason)
    state, eligibility_reason = _eligibility(revision, client, rows, now)
    if state:
        return _set_state(revision, state, eligibility_reason)
    return _set_state(revision, "manual", reason)


def schedule_revision_recovery(revision_id, *, now=None):
    """Queue an expired generation for DB-only classification and bounded retry."""
    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RecoveryResult(reason="revision_missing")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=1).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if revision is None:
            return RecoveryResult(reason="revision_missing")
        if revision.recovery_state == "execution" and not execution_resume_is_current(revision, now=now):
            return _set_state(revision, "manual", "proposal_execution_resume_expired")
        if (client is not None and revision.active_slot == 1 and revision.state in {"sealed", "claimed"}
            and revision.recovery_state in {"", "waiting"}):
            semantic = _classify_terminal_semantic_failure(revision, client, now=now)
            if semantic is not None:
                return semantic
        if revision.recovery_state:
            return RecoveryResult(revision.pk, revision.recovery_state, revision.recovery_code)
        if revision.overall_deadline > now or revision.active_slot != 1 or revision.state not in {"sealed", "claimed"}:
            return RecoveryResult(revision.pk, reason="recovery_not_expired")
        # A successful last-budget answer is execution debt, not provider
        # exhaustion. Classify it before inspecting any remaining HTTP slots.
        if revision.generation_proposal_digest and client is not None:
            return _authorize_proposal_execution(revision, client, settings_row, now=now)
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution

        continuation = inspect_revision_provider_execution(revision, now=now)
        if not continuation.ready and continuation.reason != "provider_wait":
            reason = continuation.reason
            if reason == "provider_manifest_missing":
                from management.services.ig_revision_burst_budget import economic_family, economic_root
                from management.services.ig_revision_provider_execution import MANIFEST_KEY

                root = economic_root(revision)
                family = economic_family(root) if root is not None else []
                turn_ids = [f"ig-revision:{row.pk}" for row in family]
                # An input/permission/ingress fence can expire a sealed claim
                # before generation admission. That is unanswered input, not
                # a lost provider budget. Any admitted generation, persisted
                # manifest (even invalid), or graph keeps the strict diagnostic.
                if (root is not None and family
                    and MANIFEST_KEY not in (root.action_receipts or {})
                    and not any("generation_admission" in (row.action_receipts or {}) for row in family)
                    and not GeminiRequest.objects.filter(
                        Q(logical_turn_id__in=turn_ids) | Q(source_execution_key__in=turn_ids),
                    ).exists()):
                    reason = "generation_not_started"
            return _set_state(revision, "manual", reason)
        lineage = (revision.action_receipts or {}).get("recovery_lineage") or {}
        round_number = lineage.get("round", 0)
        delay = RETRY_DELAYS[min(round_number, len(RETRY_DELAYS) - 1)] if isinstance(round_number, int) and round_number >= 0 else RETRY_DELAYS[-1]
        due = continuation.next_due_at or now + timedelta(seconds=delay)
        horizon = timezone.datetime.fromisoformat(continuation.manifest["horizon_at"])
        if due >= horizon:
            return _set_state(revision, "manual", "provider_horizon_exhausted")
        return _set_state(revision, "waiting", "recovery_due", due_at=due)


def due_recovery_revision_ids(*, now=None, limit=25):
    now = now or timezone.now()
    return list(IgCustomerTurnRevision.objects.filter(
        Q(recovery_state="waiting", recovery_due_at__lte=now)
        | Q(recovery_state="manual", recovery_code="recovery_existing_proposal_requires_execution"),
    ).order_by("recovery_due_at", "pk").values_list("pk", flat=True)[:max(0, min(int(limit), 50))])


def prepare_outage_recovery(revision_id, *, now=None):
    """Create one fresh sealed generation, never perform generation or sends."""
    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RecoveryResult(reason="revision_missing")
    with transaction.atomic():
        # Match the execution CAS lock order: settings -> client -> revision.
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=1).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if revision is None or client is None:
            return RecoveryResult(reason="revision_missing")
        if revision.recovery_state == "spawned":
            child = IgCustomerTurnRevision.objects.filter(parent=revision, origin=RECOVERY_ORIGIN).first()
            return RecoveryResult(revision.pk, "spawned", "recovery_existing_child", child.pk if child else 0)
        if revision.recovery_state == "manual" and revision.recovery_code == "recovery_existing_proposal_requires_execution":
            return _authorize_proposal_execution(revision, client, settings_row, now=now)
        if revision.recovery_state == "waiting":
            semantic = _classify_terminal_semantic_failure(revision, client, now=now)
            if semantic is not None:
                return semantic
        if revision.recovery_state != "waiting" or not revision.recovery_due_at or revision.recovery_due_at > now:
            return RecoveryResult(revision.pk, revision.recovery_state, "recovery_not_due")
        rows, code = _lineage(revision)
        if code:
            return _set_state(revision, "manual", code)
        state, code = _eligibility(revision, client, rows, now)
        if state:
            return _set_state(revision, state, code)
        if settings_row is None or not settings_row.is_enabled or not settings_row.ai_enabled:
            return _set_state(revision, "cancelled", "recovery_settings_disabled")
        from management.services.ig_permission_transitions import permission_transition_blocks
        from management.services.ig_revision_echo import revision_echo_blocks
        from management.services.instagram_bot import allowed_sender_ids, ingress_provider_namespace

        if (permission_transition_blocks(settings_id=settings_row.pk, client_id=client.pk)
            or revision_echo_blocks(client.pk, _revision_namespace(revision))):
            return _set_state(revision, "cancelled", "recovery_permission_transition_pending")
        allowlist = allowed_sender_ids(settings_row)
        if (allowlist and client.igsid not in allowlist) or ingress_provider_namespace(settings_row) != _revision_namespace(revision):
            return _set_state(revision, "cancelled", "recovery_channel_changed")
        if revision.generation_proposal_digest:
            return _authorize_proposal_execution(revision, client, settings_row, now=now)
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution, provider_execution_reference, REFERENCE_KEY

        continuation = inspect_revision_provider_execution(revision, now=now)
        if not continuation.ready:
            if continuation.reason == "provider_wait":
                return _set_state(revision, "waiting", continuation.reason, due_at=continuation.next_due_at)
            return _set_state(revision, "manual", continuation.reason)
        if (continuation.manifest["settings_id"] != settings_row.pk
            or continuation.manifest["settings_permission_epoch"] != settings_row.reply_permission_epoch):
            return _set_state(revision, "cancelled", "provider_permission_changed")
        horizon = timezone.datetime.fromisoformat(continuation.manifest["horizon_at"])
        code = _generation_proof(rows, client, settings_row)
        if code:
            return _set_state(revision, "manual", code)
        round_number = sum(row.origin == RECOVERY_ORIGIN for row in rows)
        if round_number >= MAX_RECOVERY_GENERATIONS:
            return _set_state(revision, "manual", "recovery_generations_exhausted")
        # Unlike the legacy gate, an explicitly OPEN incident never ages out
        # into permission to spend a customer recovery generation.
        if IgProviderIncident.objects.filter(role="chat", state="open", failure_class__in=("quota", "unavailable", "timeout", "connect", "empty", "unknown")).exists():
            due = now + INCIDENT_DELAY
            if due >= horizon:
                return _set_state(revision, "manual", "provider_horizon_exhausted")
            return _set_state(revision, "waiting", "recovery_provider_incident_open", due_at=due)
        source_rows = list(revision.sources.order_by("ordinal", "id"))
        next_number = (IgCustomerTurnRevision.objects.filter(client_id=client.pk).order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
        revision.active_slot = None
        revision.state = revision.State.SUPERSEDED
        revision.claim_token = ""
        revision.claimed_at = None
        revision.lease_until = None
        revision.save(update_fields=["active_slot", "state", "claim_token", "claimed_at", "lease_until", "updated_at"])
        IgRevisionDeliveryEffect.objects.filter(revision=revision, state__in=("planned", "claimed")).update(
            state="cancelled", failure_code="outage_recovery_successor", terminal_at=now, claim_token="", lease_until=None, updated_at=now,
        )
        receipts = {"recovery_lineage": {"root_revision_id": rows[-1].pk, "round": round_number + 1, "source_snapshot_digest": revision.snapshot_digest}}
        receipts[REFERENCE_KEY] = provider_execution_reference(revision)
        if (revision.action_receipts or {}).get("manual_resume_authorization"):
            receipts["manual_resume_authorization"] = deepcopy(revision.action_receipts["manual_resume_authorization"])
        child = IgCustomerTurnRevision.objects.create(
            client=client, turn_id=revision.turn_id, parent=revision, revision=next_number,
            origin=RECOVERY_ORIGIN, successor_reason="outage_recovery", state="sealed", active_slot=1,
            quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now,
            overall_deadline=min(now + RECOVERY_LIFETIME, horizon), media_prepare_deadline=revision.media_prepare_deadline,
            source_count=revision.source_count, text_chars=revision.text_chars, media_part_count=revision.media_part_count,
            permission_epoch=revision.permission_epoch, erasure_started_at_snapshot=revision.erasure_started_at_snapshot,
            bundle_snapshot=deepcopy(revision.bundle_snapshot), snapshot_digest=revision.snapshot_digest,
            sealed_at=now, action_receipts=receipts,
        )
        _create_source_rows(child, _copied_source_payloads(source_rows))
        return _set_state(revision, "spawned", "recovery_child_created", child_id=child.pk)
