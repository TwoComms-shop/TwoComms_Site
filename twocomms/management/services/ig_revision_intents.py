"""Revision-bound manager cases and deferred technical notifications.

These are local intent writes. The existing notification worker owns delivery;
neither helper calls a provider or changes a customer's commercial stage.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, is_dataclass
from datetime import timedelta
import hashlib
import json
import re

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    IgBotNotification, IgClient, IgCustomerTurnRevision, IgFollowUpTask,
    InstagramBotSettings,
)
from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
from management.services.ig_revision_outbox import PublicationBinding, pre_winner_readiness


MANAGER_ACTION = "manager_escalation_intent"
MANAGER_RECEIPT = "manager_handoff"
RATE_RECEIPT = "rate_alert"
_MANAGER_REQUEST = re.compile(r"\b(?:менеджер\w*|оператор\w*|manager|human\s+(?:agent|support)|speak\s+to\s+(?:a\s+)?human)\b", re.I)
_MANAGER_PROMISE = re.compile(r"(?:переда[мю]|передаю|покличу|запрошу|передал\w*|передан\w*|передамо|залуч\w*|подключ\w*|позов\w*|refer|forward|connect)[^.!?\n]{0,65}(?:менеджер\w*|команд\w*|спеціаліст\w*|специалист\w*|manager|team|specialist)", re.I)


@dataclass(frozen=True)
class RevisionIntentResult:
    ready: bool = False
    reason: str = ""
    task_id: int = 0
    notification_id: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class CandidateMediaComplaintEvidence:
    """Backend-only capability; never model input, persisted action or receipt."""
    response: object
    artifact: object
    artifact_digest: str
    revision_id: int
    client_id: int
    permission_epoch: int
    snapshot_digest: str
    request_id: str
    claims: tuple[tuple[tuple[str, object], ...], ...]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _artifact_digest(artifact):
    try:
        value = asdict(artifact) if is_dataclass(artifact) else artifact
        return _digest(value)
    except (TypeError, ValueError):
        return ""


def capture_candidate_media_complaint_evidence(revision, response, *, request_media_binding, usage):
    """Capture only this response's actual submitted bytes before winner election."""
    from management.models import GeminiRequest
    from management.services.ig_turn_lineage import current_context, current_request_id
    from management.services.ig_media_analysis import bound_media_parts, media_reaction, MediaAnalysisError
    from management.services.ig_revision_proposal import _request_media_projection, current_media_complaint_parts
    artifact = getattr(response, "turn_intelligence", None)
    if artifact is None or not getattr(artifact, "media_observations", ()):
        return None, "media_complaint_absent"
    context, request_id = current_context(), current_request_id()
    expected_turn = f"ig-revision:{revision.pk}"
    if (not request_id or context.get("lane") != "live" or context.get("client_id") != revision.client_id
        or context.get("logical_turn_id") != expected_turn):
        return None, "media_complaint_lineage_invalid"
    graph = GeminiRequest.objects.filter(request_id=request_id, client_id=revision.client_id,
        source_execution_key=expected_turn, logical_turn_id=expected_turn, lane="live").first()
    sealed_ids = {row.get("message_id") for row in (revision.bundle_snapshot or {}).get("sources", ())}
    if (graph is None or graph.source_message_id != context.get("source_message_id") or graph.source_message_id not in sealed_ids
        or graph.accounting_mode not in {GeminiRequest.AccountingMode.SHADOW,
            GeminiRequest.AccountingMode.ENFORCED, GeminiRequest.AccountingMode.EMERGENCY}):
        return None, "media_complaint_request_invalid"
    usage = usage if isinstance(usage, dict) else {}
    supplied = dict(request_media_binding or {})
    supplied["actual_inline_count"] = usage.get("_request_inline_count")
    supplied["actual_content_hashes"] = usage.get("_request_inline_content_hashes")
    media, reason = _request_media_projection(revision, supplied)
    if media is None:
        return None, reason
    try:
        parts = bound_media_parts(parts=media["items"], observations=artifact.media_observations,
            actual_inline_count=media["actual_inline_count"], actual_content_hashes=media["actual_content_hashes"],
            legacy_images=artifact.image_observations)
    except MediaAnalysisError as exc:
        return None, exc.code
    claims = media_reaction({"parts": parts, "capture_outcomes": media["outcomes"]})["complaint_parts"]
    claims, reason = current_media_complaint_parts(revision, claims, request_permission_epoch=revision.permission_epoch)
    if reason:
        return None, reason
    digest = _artifact_digest(artifact)
    if not digest:
        return None, "media_complaint_artifact_invalid"
    return CandidateMediaComplaintEvidence(response, artifact, digest, revision.pk, revision.client_id,
        revision.permission_epoch, revision.snapshot_digest, request_id,
        tuple(tuple(sorted(claim.items())) for claim in claims)), ""


def _candidate_media_complaint_evidence(revision, response):
    capability = getattr(revision, "_candidate_media_complaint", None)
    artifact = getattr(response, "turn_intelligence", None)
    if (not isinstance(capability, CandidateMediaComplaintEvidence) or artifact is not capability.artifact
        or _artifact_digest(artifact) != capability.artifact_digest
        or (revision.pk, revision.client_id, revision.permission_epoch, revision.snapshot_digest) !=
            (capability.revision_id, capability.client_id, capability.permission_epoch, capability.snapshot_digest)):
        return []
    # Legitimate dataclasses.replace keeps the parsed artifact object and its
    # canonical digest. A differently parsed attempt or mutated object cannot.
    from management.services.ig_revision_proposal import current_media_complaint_parts
    current, reason = current_media_complaint_parts(revision,
        [dict(row) for row in capability.claims], request_permission_epoch=capability.permission_epoch)
    return [] if reason else current


def validated_legacy_media_complaint_evidence(message, artifact):
    """Source-owned legacy interpretation; never a fake revision or action receipt."""
    from management.models import InstagramBotMessage, GeminiRequest, GeminiRequestAttempt
    from management.services.ig_turn_revisions import _source_payload
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_media_manifest import normalize_attachment_media
    from management.services.ig_media_analysis import validate_bound_media_analysis, media_reaction, MediaAnalysisError
    if not isinstance(artifact, dict) or not isinstance(artifact.get("media_analysis"), dict):
        return [], "media_complaint_absent"
    current = InstagramBotMessage.objects.select_related("client").filter(pk=message.pk, role="user", source="webhook").first()
    if (current is None or current.client_id != message.client_id or not current.client_id
        or current.sender_id != current.client.igsid or not current.provider_namespace
        or current.provider_namespace != artifact.get("source_namespace")
        or artifact.get("source_message_id") != current.pk
        or current.pk < conversation_route_reset_floor(current.client_id)
        or current.client.privacy_erasure_started_at is not None or current.client.hidden_at is not None
        or current.client.is_blocked or current.client.reply_permission_epoch != artifact.get("request_permission_epoch")
        or current.private_media_state != current.PrivateMediaState.ACTIVE
        or _source_payload(current, ordinal=1)["source_digest"] != artifact.get("source_digest")):
        return [], "media_complaint_scope_changed"
    analysis, request = artifact["media_analysis"], artifact.get("media_request") or {}
    rows = analysis.get("parts")
    submitted = request.get("submitted_parts")
    actual_count = request.get("actual_inline_count")
    actual_hashes = request.get("actual_content_hashes")
    if (not isinstance(rows, list) or not isinstance(submitted, list)
        or len(rows) != len(submitted) or type(request.get("prepared_inline_count")) is not int
        or request.get("prepared_inline_count") != len(submitted)
        or request.get("inline_count_known") is not True
        or type(actual_count) is not int or not 0 <= actual_count <= len(submitted)
        or not isinstance(actual_hashes, list)
        or request.get("request_id") != analysis.get("request_id")
        or request.get("provider_model") != analysis.get("provider_model")):
        return [], "media_complaint_proof_invalid"
    for row, captured in zip(rows, submitted, strict=True):
        if (not isinstance(row, dict) or not isinstance(captured, dict)
            or type(captured.get("original_index")) is not int
            or any(captured.get(key) != row.get(key)
                for key in ("source_part_id", "original_index", "content_hash"))
            or ("source_message_id" in row and row["source_message_id"] != current.pk)):
            return [], "media_complaint_proof_invalid"
    if actual_hashes != [row.get("content_hash") for row in submitted[:actual_count]]:
        return [], "media_complaint_proof_invalid"
    graph = GeminiRequest.objects.select_related("winner_attempt").filter(request_id=analysis.get("request_id"),
        client_id=current.client_id, source_message_id=current.pk, lane="live", terminal_resolution="succeeded").first()
    winner = graph.winner_attempt if graph else None
    if (winner is None or winner.request_graph_id != graph.pk or winner.request_id != graph.request_id
        or winner.model != analysis.get("provider_model") or winner.winner_claimed is not True
        or winner.fsm_state != GeminiRequestAttempt.FsmState.SUCCEEDED or winner.outcome != "succeeded"
        or winner.client_id != current.client_id or winner.source_message_id != current.pk
        or winner.lane != graph.lane or winner.logical_turn_id != graph.logical_turn_id):
        return [], "media_complaint_generation_invalid"
    try:
        current_parts = normalize_attachment_media(current.attachment_media or [], message_scope=current.pk)
        bound = []
        for row in analysis.get("parts", ()):
            matches = [part for part in current_parts if part.get("source_part_id") == row.get("source_part_id")
                and part.get("content_hash") == row.get("content_hash") and part.get("mime") == row.get("mime")
                and part.get("original_index") == row.get("original_index") and part.get("private_storage") is True
                and (part.get("status") == "owned" or part.get("capture_state") == "owned")]
            if len(matches) != 1:
                return [], "media_complaint_owner_changed"
            part = dict(matches[0])
            # Absence is legacy unknown. Copy only the ID genuinely captured
            # in the normalized request, never invent it from the turn anchor.
            if "source_message_id" in row:
                part["source_message_id"] = row["source_message_id"]
            bound.append(part)
        analysis = validate_bound_media_analysis(analysis, media={"items": bound,
            "actual_inline_count": actual_count,
            "actual_content_hashes": actual_hashes,
            "outcomes": analysis.get("capture_outcomes") or ()}, request_id=graph.request_id,
            provider_model=winner.model, legacy_images=artifact.get("image_observations") or ())
    except (MediaAnalysisError, ValueError, TypeError, KeyError):
        return [], "media_complaint_proof_invalid"
    return media_reaction(analysis)["complaint_parts"], ""


def _collaboration_route_present(revision):
    """Return true only for a collaboration route bound to this revision."""
    proposal = getattr(revision, "generation_proposal", None) or {}
    routes = proposal.get("customer_routes") if isinstance(proposal, dict) else None
    intents = routes.get("intents") if isinstance(routes, dict) else ()
    return any(
        isinstance(item, dict)
        and item.get("kind") == "collaboration"
        and item.get("operation", "open") != "withdraw"
        for item in intents or ()
    )


def collaboration_brief_for_revision(revision):
    """Build a source-bound operator brief without reading mutable client state."""
    from management.services.bot_sales_classifier import extract_collaboration_brief

    proposal = getattr(revision, "generation_proposal", None) or {}
    route = proposal.get("customer_routes") if isinstance(proposal, dict) else None
    route_intents = [item for item in (route or {}).get("intents", ()) if isinstance(item, dict)]
    items = []
    for source in (getattr(revision, "bundle_snapshot", None) or {}).get("sources", ()):
        if source.get("role") != "user":
            continue
        brief = extract_collaboration_brief(str(source.get("text") or ""))
        media_parts = source.get("media_parts") or []
        if not brief and _collaboration_route_present(revision) and media_parts:
            kinds = {str(part.get("mime") or "").split("/", 1)[0] for part in media_parts}
            assets = [f"{kind}_content" for kind in ("image", "video") if kind in kinds]
            brief = {
                "schema_version": 2, "subtypes": ["creator"], "primary_subtype": "creator",
                "assets": assets, "requested_percentage": None, "requested_unit_terms": False,
                "contact_present": False, "contact_values": [], "references_present": False,
                "audience_present": False, "volume_or_deadline_present": False,
                "decision_owner": "manager", "multiple_intents": False,
            }
        if brief:
            items.append({
                "message_id": int(source["message_id"]),
                "source_digest": str(source.get("source_digest") or ""),
                "brief": brief,
                "media_parts": [
                    {"source_part_id": str(part.get("source_part_id") or ""),
                     "mime": str(part.get("mime") or "")[:100],
                     "content_hash": str(part.get("content_hash") or "")}
                    for part in media_parts if isinstance(part, dict)
                ],
            })
    return {
        "schema_version": 2,
        "source_snapshot_digest": str(getattr(revision, "snapshot_digest", "") or ""),
        "route_intents": route_intents,
        "items": items[-64:],
    }


def manager_case_reason(revision, response=None):
    from management.services.ig_service_complaints import revision_service_complaint

    if revision_service_complaint(revision):
        return "service_complaint_review"
    from management.services.bot_sales_classifier import (
        SUPPORT_RE,
        extract_collaboration_brief,
        is_explicit_custom_print_request,
    )

    texts = [str(row.get("text") or "") for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    control = response.control if response is not None else {}
    if any(is_explicit_custom_print_request(text) for text in texts) or (
        (control.get("paylink") or control.get("payment"))
        and "custom" in str(revision.client.intent or "").casefold()
    ):
        return "custom_print"
    if any(extract_collaboration_brief(text) for text in texts) or _collaboration_route_present(revision):
        return "collaboration_review"
    if any(_MANAGER_REQUEST.search(text) for text in texts):
        return "customer_manager_request"
    if response is not None and (control.get("manager") or _MANAGER_PROMISE.search(response.reply_text)) and any(SUPPORT_RE.search(text) for text in texts):
        return "business_review"
    if response is not None and (control.get("manager") or _MANAGER_PROMISE.search(response.reply_text)):
        if _candidate_media_complaint_evidence(revision, response):
            return "media_complaint_review"
    return ""


def manager_handoff_promised(response):
    return bool(response is not None and _MANAGER_PROMISE.search(response.reply_text))


def _readiness(revision, token, settings_row, authority, epoch, publication):
    return pre_winner_readiness(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=epoch, publication=publication,
        fact_bindings=authority.get("fact_bindings") or (),
        offer_bindings=authority.get("offer_bindings") or (),
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
    )


def ensure_revision_manager_case(revision_id, token, *, settings_id):
    if connection.in_atomic_block:
        return RevisionIntentResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionIntentResult(reason="revision_missing")
    from management.services.ig_response_control import ResponseControl, ValidatedResponse
    from management.services.ig_alerts import format_operator_alert
    from management.services.instagram_bot import notify_manager

    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionIntentResult(reason="intent_identity_missing")
        proposal = revision.generation_proposal or {}
        if not revision.generation_proposal_digest or _digest(proposal) != revision.generation_proposal_digest:
            return RevisionIntentResult(reason="generation_proposal_changed")
        authority = proposal.get("authority") or {}
        if MANAGER_ACTION not in (authority.get("allowed_actions") or ()):
            return RevisionIntentResult(reason="manager_action_not_authorized")
        execution = proposal.get("execution_binding") or {}
        pub = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
        if execution.get("settings_id") != settings_id or not all(key in pub for key in ("id", "version", "hash")):
            return RevisionIntentResult(reason="proposal_execution_binding_missing")
        selection = (revision.action_receipts or {}).get("client_configuration_update") or {}
        effective_authority = selection.get("after_authority") or authority
        ready = _readiness(revision, token, settings_row, effective_authority, execution["settings_permission_epoch"], PublicationBinding(pub["id"], pub["version"], pub["hash"]))
        if not ready.ready:
            return RevisionIntentResult(reason=ready.reasons[0])
        existing = (revision.action_receipts or {}).get(MANAGER_RECEIPT)
        if existing:
            task = IgFollowUpTask.objects.filter(pk=existing.get("task_id"), client=client).first()
            notification = IgBotNotification.objects.filter(pk=existing.get("notification_id"), client=client).first()
            if existing.get("generation_proposal_digest") != revision.generation_proposal_digest or task is None or notification is None:
                return RevisionIntentResult(reason="manager_receipt_invalid")
            return RevisionIntentResult(True, "already_recorded", task.pk, notification.pk, True)
        stored = proposal.get("response") or {}
        response = ValidatedResponse(reply_text=stored.get("reply_text") or "", controls=tuple(ResponseControl(item["kind"], item["value"]) for item in stored.get("controls") or ()))
        revision.client = client
        reason = manager_case_reason(revision, response)
        from management.services.ig_service_complaints import revision_service_complaint
        service_complaint = revision_service_complaint(revision)
        complaint_evidence = []
        if (proposal.get("turn_intelligence") or {}).get("media_analysis") is not None:
            from management.services.ig_revision_proposal import validated_media_complaint_evidence
            complaint_evidence, media_reason = validated_media_complaint_evidence(revision)
            if complaint_evidence and reason not in {"custom_print", "collaboration_review", "service_complaint_review"}:
                reason = "media_complaint_review"
            elif not reason and (response.control.get("manager") or manager_handoff_promised(response)):
                return RevisionIntentResult(reason=media_reason)
        if not reason:
            return RevisionIntentResult(reason="manager_case_not_requested")
        task_reason = {
            "custom_print": "revision_case:custom_print",
            "collaboration_review": "revision_case:collaboration_review",
            "media_complaint_review": "revision_case:media_complaint",
            "service_complaint_review": "revision_case:service_complaint",
        }.get(reason, "revision_case:manager_handoff")
        event_key = f"ig-revision-case:{client.pk}:{revision.pk}:{reason}"[:180]
        tasks = IgFollowUpTask.objects.select_for_update().filter(client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=task_reason).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED))
        if complaint_evidence or service_complaint:
            # Different complaint sources have independent unresolved service
            # debt. Only this revision's exact case may be reused on replay.
            tasks = tasks.filter(event_key=event_key)
        task = tasks.order_by("id").first()
        now = timezone.now()
        source_refs = [{"message_id": row["message_id"], "source_digest": row["source_digest"]} for row in proposal.get("sources", ())]
        if task is None:
            task = IgFollowUpTask.objects.create(
                client=client, due_at=now, status=IgFollowUpTask.Status.SKIPPED,
                kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=task_reason,
                manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
                manager_approval_requested_at=now,
                message_text=(
                    "Клієнт просить власний принт: перевірити макет, можливість і вартість."
                    if reason == "custom_print" else
                    "Клієнт пропонує creator/фото-відео співпрацю: перевірити портфоліо, результати, умови та контакт."
                    if reason == "collaboration_review" else
                    "Клієнт повідомив про сервісну проблему: перевірити звернення та потребу в рішенні керівника; компенсацію не погоджено."
                    if reason == "service_complaint_review" else
                    "Клієнту потрібна допомога команди: відкрийте поточну розмову."
                ),
                event_key=event_key,
                trigger=IgFollowUpTask.Trigger.EVENT, event_occurred_at=now,
                skip_reason="human_business_decision_required", policy_started_at=now,
                policy_version="revision-case-v1",
            )
        context = dict(task.manager_context or {})
        previous = list(context.get("sources") or [])
        known = {item["message_id"] for item in previous}
        previous.extend(item for item in source_refs if item["message_id"] not in known)
        context.update({
            "schema_version": 1, "case_kind": reason, "sources": previous[-64:],
            "latest_revision_id": revision.pk, "generation_proposal_digest": revision.generation_proposal_digest,
            "required_decisions": (
                ["design_feasibility", "quote_approval"] if reason == "custom_print" else
                ["portfolio_review", "collaboration_terms"] if reason == "collaboration_review" else
                ["customer_request"]
            ),
            "authority": {"price_confirmed": False, "fulfillment_started": False},
        })
        if reason == "collaboration_review":
            context["collaboration_brief"] = collaboration_brief_for_revision(revision)
        if service_complaint:
            context["service_complaint"] = service_complaint
            context["required_decisions"] = ["customer_service_review", "leadership_review"]
            context["authority"].update(refund_confirmed=False, carrier_charge_verified=False)
            # Preserve the response-plan owner's real coverage result. A
            # service acknowledgment cannot mark mixed shopping needs done.
            coverage = (revision.action_receipts or {}).get("response_coverage") or {}
            if isinstance(coverage, dict) and coverage.get("remaining"):
                context["pending_response_obligations"] = list(coverage["remaining"])
                context["response_plan_digest"] = coverage.get("plan_digest", "")
                context["required_decisions"].append("remaining_customer_questions")
        if complaint_evidence:
            context["media_complaint_evidence"] = complaint_evidence
            if "customer_service_review" not in context["required_decisions"]:
                context["required_decisions"].append("customer_service_review")
            from management.services.ig_turn_intent import media_complaint_source_scope
            context["media_complaint_scope"] = media_complaint_source_scope(client, revision,
                [row["message_id"] for row in source_refs])
        task.manager_context = context
        task.save(update_fields=["manager_context", "updated_at"])
        notification_key = f"ig-revision-case:{task.pk}"
        notified = notify_manager(
            format_operator_alert("Клієнту потрібна допомога команди", event_type="escalation", client_id=client.pk, task_id=task.pk, counts={"sources": len(previous)}, instruction_code="escalation"),
            dedupe_key=notification_key, event_type="escalation", client=client,
            metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": reason},
            deliver_immediately=False, raise_on_error=True,
        )
        if not notified:
            raise RuntimeError("manager_notification_not_recorded")
        notification = IgBotNotification.objects.get(dedupe_key=notification_key)
        revision.action_receipts = {**(revision.action_receipts or {}), MANAGER_RECEIPT: {
            "task_id": task.pk, "notification_id": notification.pk, "case_kind": reason,
            "snapshot_digest": revision.snapshot_digest, "generation_proposal_digest": revision.generation_proposal_digest,
            "recorded_at": now.isoformat(),
        }}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionIntentResult(True, "recorded", task.pk, notification.pk)


def ensure_revision_rate_alert(revision_id, token, *, settings_id):
    if connection.in_atomic_block:
        return RevisionIntentResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionIntentResult(reason="revision_missing")
    from management.services.ig_alerts import format_technical_alert
    from management.services.instagram_bot import notify_manager

    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionIntentResult(reason="intent_identity_missing")
        receipt = (revision.action_receipts or {}).get("input_decision") or {}
        if receipt.get("reason") != "rate_limited" or receipt.get("snapshot_digest") != revision.snapshot_digest:
            return RevisionIntentResult(reason="rate_alert_not_requested")
        pub = receipt["publication"]
        ready = _readiness(revision, token, settings_row, receipt["authority"], receipt["settings_permission_epoch"], PublicationBinding(pub["id"], pub["version"], pub["hash"]))
        if not ready.ready:
            return RevisionIntentResult(reason=ready.reasons[0])
        existing = (revision.action_receipts or {}).get(RATE_RECEIPT)
        if existing:
            return RevisionIntentResult(True, "already_recorded", notification_id=existing["notification_id"], replayed=True)
        now = timezone.now()
        notification = IgBotNotification.objects.filter(client=client, event_type="sender_rate_limited", created_at__gte=now - timedelta(hours=1)).order_by("id").first()
        if notification is None:
            key = f"ig-revision-rate:{client.pk}:{revision.pk}"
            notify_manager(
                format_technical_alert("Перевищено частоту повідомлень", event_type="sender_rate_limited", client_id=client.pk, message_id=receipt["source_message_ids"][-1], failure_kind="possible_spam", instruction_code="sender_rate_limited"),
                dedupe_key=key, event_type="sender_rate_limited", client=client,
                metadata={"revision_id": revision.pk}, deliver_immediately=False, raise_on_error=True,
            )
            notification = IgBotNotification.objects.get(dedupe_key=key)
        revision.action_receipts = {**(revision.action_receipts or {}), RATE_RECEIPT: {
            "notification_id": notification.pk, "snapshot_digest": revision.snapshot_digest, "recorded_at": now.isoformat(),
        }}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionIntentResult(True, "recorded", notification_id=notification.pk)
