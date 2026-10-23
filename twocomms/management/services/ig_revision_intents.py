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
    from management.services.ig_business_cooperation import (
        business_cooperation_for_sources, current_cooperation_sources, recruitment_service_for_sources,
    )

    proposal = getattr(revision, "generation_proposal", None) or {}
    route = proposal.get("customer_routes") if isinstance(proposal, dict) else None
    route_intents = [item for item in (route or {}).get("intents", ()) if isinstance(item, dict)]
    sources = current_cooperation_sources((getattr(revision, "bundle_snapshot", None) or {}).get("sources", ()))
    business = business_cooperation_for_sources(sources)
    recruitment = recruitment_service_for_sources(sources)
    items = []
    for source in sources:
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
        "cooperation_kind": "recruitment_service" if recruitment else "business" if business else "creator_or_other",
        "business_cooperation": business,
        "recruitment_service": recruitment,
        "items": items[-64:],
    }


def _ongoing_custom_case_requested(revision):
    """A short answer may continue one exact open native custom request.

    A profile intent or an unrelated old case is insufficient. Independent
    intervening input, changed source proof, reset or permission changes end
    this narrow continuity grant; it grants no price or checkout authority.
    """
    from management.services.bot_sales_classifier import custom_print_requested_for_sources
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_revision_holding import _sources_unchanged
    from management.models import IgCustomerTurnRevision, IgFollowUpTask, InstagramBotMessage

    short = re.compile(r"^(?:так|да|ага|yes|ок|окей|okay|ok)(?:[, ]+(?:одн[ау]|один|дві|две|one|two|[1-9]\d?)(?:\s+(?:шт\w*|pieces?))?)?$|"
                       r"^(?:одн[ау]|один|дві|две|one|two|[1-9]\d?)(?:\s+(?:шт\w*|pieces?))?$", re.I)
    current = [row for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    if (not current or any(not short.fullmatch(own_reply_text(str(row.get("text") or "")).strip(" .!")) for row in current)
        or not getattr(revision, "client_id", None)):
        return False
    floor = conversation_route_reset_floor(revision.client_id)
    candidates = list(IgFollowUpTask.objects.filter(client_id=revision.client_id, kind="manager_task",
        reason="revision_case:custom_print").exclude(status__in=("completed", "cancelled")).order_by("pk")[:2])
    if len(candidates) != 1:
        return False
    task = candidates[0]
    context, pieces = task.manager_context or {}, str(task.event_key).split(":")
    refs = context.get("sources") or []
    if (context.get("schema_version") != 1 or context.get("case_kind") != "custom_print"
        or len(pieces) != 4 or pieces[0] != "ig-revision-case" or pieces[1] != str(revision.client_id)
        or pieces[3] != "custom_print" or not pieces[2].isdigit()
        or not isinstance(refs, list) or not 1 <= len(refs) <= 64
        or any(not isinstance(row, dict) or type(row.get("message_id")) is not int for row in refs)):
        return False
    ids = [row["message_id"] for row in refs]
    current_ids = [row["message_id"] for row in current]
    if min(ids) < floor or max(ids) >= min(current_ids):
        return False
    root = IgCustomerTurnRevision.objects.filter(pk=int(pieces[2]), client_id=revision.client_id,
        permission_epoch=revision.permission_epoch).first()
    latest = IgCustomerTurnRevision.objects.filter(pk=context.get("latest_revision_id"), client_id=revision.client_id,
        permission_epoch=revision.permission_epoch).first()
    if (root is None or latest is None or not root.snapshot_digest or _digest(root.bundle_snapshot) != root.snapshot_digest
        or not _sources_unchanged(root) or not custom_print_requested_for_sources(root.bundle_snapshot.get("sources", ()))
        or not latest.generation_proposal_digest or _digest(latest.generation_proposal) != latest.generation_proposal_digest
        or latest.generation_proposal_digest != context.get("generation_proposal_digest")
        or {row.get("source_namespace") for row in root.bundle_snapshot.get("sources", ())}
           != {row.get("source_namespace") for row in current}):
        return False
    previous = list(InstagramBotMessage.objects.filter(pk__in=ids, client_id=revision.client_id, role="user"))
    if (len(previous) != len(ids) or any(not (custom_print_requested_for_sources([{"role": "user", "text": row.text}])
        or short.fullmatch(own_reply_text(row.text).strip(" .!"))) for row in previous)
        or InstagramBotMessage.objects.filter(client_id=revision.client_id, role="user", pk__gt=max(ids))
            .exclude(pk__in=current_ids).exists()):
        return False
    return True


def manager_case_reason(revision, response=None):
    from management.services.ig_service_complaints import revision_service_complaint

    if revision_service_complaint(revision):
        return "service_complaint_review"
    from management.services.bot_sales_classifier import (
        SUPPORT_RE,
        extract_collaboration_brief,
        custom_print_requested_for_sources,
    )

    texts = [str(row.get("text") or "") for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    control = response.control if response is not None else {}
    from management.services.ig_assisted_commerce_context import AssistedCommerceContext
    assisted = getattr(revision, "_assisted_commerce_context", None)
    current_assisted = (isinstance(assisted, AssistedCommerceContext) and assisted.revision_id == revision.pk
        and assisted.revision_digest == revision.snapshot_digest)
    if not current_assisted and (custom_print_requested_for_sources(revision.bundle_snapshot.get("sources", ()))
                                or _ongoing_custom_case_requested(revision)):
        return "custom_print"
    from management.services.ig_business_cooperation import business_cooperation_for_sources, current_cooperation_sources
    cooperation_sources = current_cooperation_sources(revision.bundle_snapshot.get("sources", ()))
    if (business_cooperation_for_sources(cooperation_sources)
        or any(extract_collaboration_brief(str(row.get("text") or "")) for row in cooperation_sources)
        or _collaboration_route_present(revision)):
        return "collaboration_review"
    if any(_MANAGER_REQUEST.search(text) for text in texts):
        return "customer_manager_request"
    from management.services.ig_revision_checkout import requested_manual_delivery
    if requested_manual_delivery(revision.client, revision.bundle_snapshot.get("sources") or ()):
        return "customer_manager_request"
    if response is not None and (control.get("manager") or _MANAGER_PROMISE.search(response.reply_text)) and any(SUPPORT_RE.search(text) for text in texts):
        return "business_review"
    if response is not None and (control.get("manager") or _MANAGER_PROMISE.search(response.reply_text)):
        if _candidate_media_complaint_evidence(revision, response):
            return "media_complaint_review"
    from management.services.ig_product_inquiries import product_inquiries_for_revision
    inquiry = product_inquiries_for_revision(revision)
    if inquiry.get("status") == "manager_required":
        return inquiry["reason"]
    return ""


def manager_handoff_promised(response):
    return bool(response is not None and _MANAGER_PROMISE.search(response.reply_text))


def _product_information_scopes(task):
    """Exact revision capabilities underlying a combined operator brief."""
    context = task.manager_context or {}
    if not isinstance(context, dict):
        return []
    scopes = context.get("product_inquiry_scopes")
    if scopes is not None:
        return scopes if (isinstance(scopes, list) and 1 <= len(scopes) <= 32
            and all(isinstance(row, dict) and type(row.get("revision_id")) is int and row["revision_id"] > 0
                and isinstance(row.get("product_inquiries"), dict) for row in scopes)) else []
    owner_id = context.get("latest_revision_id")
    if type(owner_id) is not int or owner_id <= 0:
        return []
    owner = IgCustomerTurnRevision.objects.filter(pk=owner_id, client_id=task.client_id).first()
    if owner is None:
        return []
    return [{"revision_id": owner.pk, "snapshot_digest": owner.snapshot_digest,
        "generation_proposal_digest": owner.generation_proposal_digest,
        "product_inquiries": context.get("product_inquiries")}]


def _product_information_subject(revision, inquiry):
    """A SKU or an explicit garment topic, never today's mutable selection."""
    products = {row.get("product_id") for row in inquiry.get("inquiries", ())}
    if len(products) != 1:
        return None
    product = next(iter(products))
    if type(product) is int and product > 0:
        return ("product", product)
    if product is not None:
        return None
    from management.services.ig_product_focus import information_subject, _switches_product
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    subject = information_subject(sources)
    if (subject.get("status") != "identified" or subject.get("future_base_requested")
        or any(_switches_product(row.get("text")) for row in sources)):
        return None
    return ("garment", subject["garment_type"])


def _mergeable_native_manager_task(revision, reason, tasks, *, inquiry=None, collaboration=None):
    """A unique same-kind case, backed by the shared batched native proof path."""
    from management.services.ig_pending_question_context import _prior_native_case_scopes
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_turn_intent import _scope_for_sources
    if len(tasks) > 8:
        return None
    current = revision.bundle_snapshot['sources']
    namespaces = {row.get('source_namespace') for row in current}
    if len(namespaces) != 1 or not next(iter(namespaces)):
        return None
    subject = _product_information_subject(revision, inquiry) if inquiry is not None else None
    if reason == 'product_information_review' and subject is None:
        return None
    kind = (collaboration or {}).get('cooperation_kind')
    candidates = [task for task in tasks if isinstance(task.manager_context, dict)
        and task.manager_context.get('case_kind') == reason and task.manager_approval_status == 'pending'
        and (reason != 'collaboration_review' or task.manager_context.get('cooperation_kind') == kind)]
    if not candidates:
        return None
    floor = conversation_route_reset_floor(revision.client_id)
    episode = _scope_for_sources(revision.client, [row['message_id'] for row in current]).get('commercial_episode_id')
    cart_scope = (revision.generation_proposal.get('source_cart_capture') or {}).get('scope') or {}
    if episode and cart_scope.get('episode_id') and episode != cart_scope['episode_id']:
        return None
    episode = episode or cart_scope.get('episode_id')
    proofs = _prior_native_case_scopes(revision, candidates, floor=floor,
        namespace=next(iter(namespaces)), episode_id=episode, for_projection=False)
    notifications = {}
    if reason == 'product_information_review':
        notification_ids = {pk for proof in proofs.values() if proof for bound in proof['scopes']
            for pk in (bound['owner'].action_receipts[MANAGER_RECEIPT].get('notification_id'),)
            if type(pk) is int and pk > 0}
        notifications = IgBotNotification.objects.filter(client_id=revision.client_id).in_bulk(notification_ids)
    matches = []
    for task in candidates:
        proof = proofs.get(task.pk)
        if not proof or not 1 <= len(proof['scopes']) < 32:
            continue
        sources = {row['message_id']: row for bound in proof['scopes'] for row in bound['sources']}
        refs = task.manager_context.get('sources')
        if (not isinstance(refs, list) or any(not isinstance(row, dict) or type(row.get('message_id')) is not int for row in refs)
            or {row['message_id']: row.get('source_digest') for row in refs} != {pk: row['source_digest'] for pk, row in sources.items()}
            or task.event_key != f"ig-revision-case:{revision.client_id}:{proof['scopes'][0]['owner'].pk}:{reason}"[:180]
            or len(sources.keys() | {row['message_id'] for row in current}) > 64
            or sum(len(row.get('text') or '') for row in [*sources.values(), *current]) > 64000):
            continue
        if reason == 'product_information_review':
            if any(_product_information_subject(bound['owner'], bound['scope']['product_inquiries']) != subject
                or type(bound['owner'].action_receipts[MANAGER_RECEIPT].get('notification_id')) is not int
                or bound['owner'].action_receipts[MANAGER_RECEIPT].get('notification_id') not in notifications
                or product_review_receipt_reason(bound['owner'], task=task,
                    notification=notifications[bound['owner'].action_receipts[MANAGER_RECEIPT]['notification_id']])
                for bound in proof['scopes']):
                continue
            if sum(len(bound['scope']['product_inquiries'].get('inquiries', ())) for bound in proof['scopes']) + len(inquiry.get('inquiries', ())) > 256:
                continue
        task._validated_case_scopes = [bound['scope'] for bound in proof['scopes']]
        matches.append(task)
    return matches[0] if len(matches) == 1 else None


def _native_manager_scope(revision, reason, collaboration):
    return {'revision_id': revision.pk, 'snapshot_digest': revision.snapshot_digest,
        'generation_proposal_digest': revision.generation_proposal_digest, 'case_kind': reason,
        'collaboration_brief': collaboration if reason == 'collaboration_review' else None,
        'source_questions': [{'message_id': row['message_id'], 'question': row.get('text') or ''}
            for row in revision.bundle_snapshot['sources']]}


def _manager_source_refs_valid(context):
    refs = context.get('sources') if isinstance(context, dict) else None
    return bool(isinstance(refs, list) and 1 <= len(refs) <= 64
        and all(isinstance(row, dict) and type(row.get('message_id')) is int and row['message_id'] > 0
            and isinstance(row.get('source_digest'), str) and row['source_digest'] for row in refs))


def _combined_collaboration_brief(scopes):
    """Display union; immutable native scopes remain its evidence, not authority."""
    briefs = [row['collaboration_brief'] for row in scopes]
    result = dict(briefs[-1])
    if len(briefs) == 1:
        return result
    result['case_brief_only'] = True
    result['items'] = [item for brief in briefs for item in brief.get('items', ())]
    for key in ('business_cooperation', 'recruitment_service'):
        parts = [brief.get(key) for brief in briefs if brief.get(key)]
        if not parts:
            continue
        merged = dict(parts[-1])
        for field in ('requested_topics', 'evidence_message_ids', 'subtypes'):
            if any(field in part for part in parts):
                merged[field] = list(dict.fromkeys(value for part in parts for value in part.get(field, ())))
        result[key] = merged
    return result


def _combined_product_information(scopes):
    """Bounded display brief; per-revision capabilities remain separate."""
    result = dict(scopes[-1]["product_inquiries"])
    result["inquiries"] = [row for scope in scopes for row in scope["product_inquiries"]["inquiries"]]
    refs = {row["message_id"]: row for scope in scopes for row in scope["product_inquiries"]["source_refs"]}
    result["source_refs"] = list(refs.values())
    if len(scopes) > 1:
        result["case_brief_only"] = True
        result["digest"] = _digest({key: value for key, value in result.items() if key != "digest"})
    return result


def product_review_receipt_reason(revision, *, task=None, notification=None):
    """The exact native product case must remain open until provider start."""
    from management.services.ig_product_inquiries import REVIEW_REASONS, product_inquiries_for_revision
    from management.models import IgFollowUpTask, IgBotNotification
    receipts = revision.action_receipts or {}
    if not isinstance(receipts, dict):
        return "product_inquiry_handoff_invalid"
    receipt = receipts.get(MANAGER_RECEIPT) or {}
    if not isinstance(receipt, dict):
        return "product_inquiry_handoff_invalid"
    reason = receipt.get("case_kind")
    current = product_inquiries_for_revision(revision)
    if current.get("status") == "none" and current.get("assisted_resolution"):
        from management.services.ig_assisted_commerce_context import AssistedCommerceContext
        assisted = getattr(revision, "_assisted_commerce_context", None)
        if isinstance(assisted, AssistedCommerceContext) and assisted.reference == current["assisted_resolution"]:
            return ""
    if current.get("status") != "manager_required" and reason not in REVIEW_REASONS:
        return ""
    if reason not in REVIEW_REASONS:
        # The source classifier, not altered receipt metadata, determines
        # whether an independently validated higher-priority case owns this.
        priority = manager_case_reason(revision)
        if priority not in REVIEW_REASONS and priority and reason == priority:
            return ""
        return "product_inquiry_handoff_missing" if not receipt else "product_inquiry_handoff_invalid"
    if (current.get("status") != "manager_required" or current.get("reason") != reason
        or receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("generation_proposal_digest") != revision.generation_proposal_digest):
        return "product_inquiry_handoff_invalid"
    task = task or IgFollowUpTask.objects.filter(pk=receipt.get("task_id"), client_id=revision.client_id).first()
    notification = notification or IgBotNotification.objects.filter(pk=receipt.get("notification_id"), client_id=revision.client_id).first()
    if (task is not None and not isinstance(task.manager_context, dict)
        or notification is not None and not isinstance(notification.payload, dict)):
        return "product_inquiry_handoff_invalid"
    scope = next((row for row in _product_information_scopes(task)
        if isinstance(row, dict) and row.get("revision_id") == revision.pk), None) if task is not None and (task.manager_context or {}).get("product_inquiry_scopes") is not None else None
    scoped = scope is not None and scope == {"revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
        "generation_proposal_digest": revision.generation_proposal_digest, "product_inquiries": current}
    declared_scopes = task is not None and "product_inquiry_scopes" in (task.manager_context or {})
    try:
        scopes = _product_information_scopes(task) if declared_scopes else []
        brief_valid = not declared_scopes or bool(scopes and
            task.event_key == f"ig-revision-case:{revision.client_id}:{scopes[0]['revision_id']}:{reason}"[:180]
            and task.manager_context.get("product_inquiries") == _combined_product_information(scopes)
            and len({row['revision_id'] for row in scopes}) == len(scopes)
            and {row['message_id']: row['source_digest'] for row in task.manager_context.get('sources', ())}
                == {row['message_id']: row['source_digest'] for scope_row in scopes
                    for row in scope_row['product_inquiries']['source_refs']})
    except (KeyError, TypeError, IndexError, ValueError, AttributeError):
        brief_valid = False
    if (task is None or notification is None or task.kind != "manager_task"
        or task.reason != "revision_case:" + reason
        or declared_scopes and not scoped or not brief_valid
        or (not scoped and (task.event_key != f"ig-revision-case:{revision.client_id}:{revision.pk}:{reason}"[:180]
            or (task.manager_context or {}).get("product_inquiries") != current))
        or notification.dedupe_key not in {f"ig-revision-case:{task.pk}", f"ig-revision-case:{task.pk}:{revision.pk}"}
        or (notification.payload or {}).get("case_kind") != reason
        or (notification.payload or {}).get("revision_id") != revision.pk
        or (notification.payload or {}).get("manager_task_id") != task.pk):
        return "product_inquiry_handoff_invalid"
    if task.status in {"completed", "cancelled"}:
        return "product_inquiry_handoff_no_longer_open"
    return ""


def _product_inquiry_case_message(preface, disposition):
    """Human source text only; the frontend renders task prose as text."""
    preface = str(preface or "").split("\n\nЗапит клієнта: ", 1)[0]
    questions = list(dict.fromkeys(re.sub(r"[\x00-\x1f\x7f]", " ", str(row.get("question") or "")).strip()
                    for row in disposition.get("inquiries", ()) if isinstance(row, dict)))
    question = " · ".join(value for value in questions if value)[:600]
    return preface + ("\n\nЗапит клієнта: " + question if question else "")


def ensure_product_inquiry_review_case(revision, disposition):
    """Reuse the native product-case identity after the caller's locked gate."""
    from management.services.ig_product_inquiries import product_inquiries_for_revision, REVIEW_REASONS
    from management.services.instagram_bot import notify_manager
    from management.models import IgFollowUpTask, IgBotNotification
    current = product_inquiries_for_revision(revision)
    if (not connection.in_atomic_block or disposition != current
        or current.get("status") != "manager_required" or current.get("reason") not in REVIEW_REASONS):
        raise ValueError("product_inquiry_case_unverified")
    reason, now = current["reason"], timezone.now()
    event_key = f"ig-revision-case:{revision.client_id}:{revision.pk}:{reason}"[:180]
    task, _ = IgFollowUpTask.objects.get_or_create(event_key=event_key, defaults={
        "client": revision.client, "due_at": now, "status": IgFollowUpTask.Status.SKIPPED,
        "kind": IgFollowUpTask.Kind.MANAGER_TASK, "reason": "revision_case:" + reason,
        "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
        "manager_approval_requested_at": now, "trigger": IgFollowUpTask.Trigger.EVENT,
        "event_occurred_at": now, "policy_started_at": now, "policy_version": "revision-case-v1",
        "skip_reason": "human_business_decision_required",
        "message_text": "Перевірте точні параметри або деталі товару за питанням клієнта. Не замінюйте колір чи розмір."})
    if task.status in {task.Status.COMPLETED, task.Status.CANCELLED}:
        raise ValueError("product_inquiry_case_closed")
    task.manager_context = {**(task.manager_context or {}), "schema_version": 1, "case_kind": reason,
        "sources": current["source_refs"], "latest_revision_id": revision.pk,
        "snapshot_digest": revision.snapshot_digest, "product_inquiries": current,
        "required_decisions": ["exact_requested_configuration" if reason == "product_configuration_review" else "exact_product_information"],
        "authority": {"catalog_confirmed": False, "stock_confirmed": False, "checkout_authorized": False,
                      "price_confirmed": False, "fulfillment_started": False}}
    task.message_text = _product_inquiry_case_message(task.message_text, current)
    task.save(update_fields=["manager_context", "message_text", "updated_at"])
    key = f"ig-revision-case:{task.pk}"
    questions = list(dict.fromkeys(row["question"] for row in current["inquiries"]))
    if not notify_manager("Перевірте точний запит про товар: " + " · ".join(questions)[:600],
        dedupe_key=key, event_type="escalation", client=revision.client,
        metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": reason},
        deliver_immediately=False, raise_on_error=True):
        raise RuntimeError("product_inquiry_notification_not_recorded")
    return task, IgBotNotification.objects.get(dedupe_key=key, client_id=revision.client_id)


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
            if not _manager_source_refs_valid(task.manager_context):
                return RevisionIntentResult(reason="manager_receipt_invalid")
            if task.manager_context.get('case_kind') != existing.get('case_kind'):
                return RevisionIntentResult(reason='manager_receipt_invalid')
            scopes = task.manager_context.get('manager_case_scopes')
            if scopes is not None:
                expected = _native_manager_scope(revision, existing['case_kind'],
                    collaboration_brief_for_revision(revision) if existing['case_kind'] == 'collaboration_review' else {})
                if not isinstance(scopes, list) or expected not in scopes:
                    return RevisionIntentResult(reason='manager_receipt_invalid')
            product_reason = product_review_receipt_reason(revision, task=task, notification=notification)
            if product_reason:
                return RevisionIntentResult(reason=product_reason)
            return RevisionIntentResult(True, "already_recorded", task.pk, notification.pk, True)
        stored = proposal.get("response") or {}
        response = ValidatedResponse(reply_text=stored.get("reply_text") or "", controls=tuple(ResponseControl(item["kind"], item["value"]) for item in stored.get("controls") or ()))
        revision.client = client
        reason = manager_case_reason(revision, response)
        collaboration = collaboration_brief_for_revision(revision) if reason == "collaboration_review" else {}
        business = collaboration.get("business_cooperation") or {}
        recruitment = collaboration.get("recruitment_service") or {}
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
            "product_configuration_review": "revision_case:product_configuration_review",
            "product_information_review": "revision_case:product_information_review",
        }.get(reason, "revision_case:manager_handoff")
        event_key = f"ig-revision-case:{client.pk}:{revision.pk}:{reason}"[:180]
        tasks = IgFollowUpTask.objects.select_for_update().filter(client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=task_reason).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED))
        from management.services.ig_product_inquiries import product_inquiries_for_revision, REVIEW_REASONS
        product_inquiry = product_inquiries_for_revision(revision)
        if complaint_evidence or service_complaint or reason in REVIEW_REASONS:
            # Different complaint sources have independent unresolved service
            # debt. Only this revision's exact case may be reused on replay.
            if reason == "product_information_review":
                task = tasks.filter(event_key=event_key).first()
                task = task or _mergeable_native_manager_task(revision, reason, list(tasks.order_by("id")[:9]), inquiry=product_inquiry)
            else:
                task = tasks.filter(event_key=event_key).first()
        elif reason in {'customer_manager_request', 'business_review', 'collaboration_review'}:
            task = tasks.filter(event_key=event_key).first()
            task = task or _mergeable_native_manager_task(revision, reason, list(tasks.order_by('id')[:9]), collaboration=collaboration)
        else:
            task = tasks.order_by("id").first()
        information_scopes = _product_information_scopes(task) if task is not None and reason == "product_information_review" else []
        manager_scopes = getattr(task, '_validated_case_scopes', []) if task is not None else []
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
                    "Клієнт пропонує допомогу з Instagram Direct / підтримкою клієнтів: передати пропозицію адміністрації для розгляду потреби й умов. Вакансію не підтверджено."
                    if reason == "collaboration_review" and recruitment else
                    "Клієнт питає про магазин, перепродаж або бізнес-співпрацю: розглянути його питання й погодити індивідуальні умови."
                    if reason == "collaboration_review" and business else
                    "Клієнт пропонує creator/фото-відео співпрацю: перевірити портфоліо, результати, умови та контакт."
                    if reason == "collaboration_review" else
                    "Клієнт повідомив про сервісну проблему: перевірити звернення та потребу в рішенні керівника; компенсацію не погоджено."
                    if reason == "service_complaint_review" else
                    "Клієнт просить параметри поза підтвердженим каталогом: перевірити можливість саме цього запиту, без заміни кольору чи розміру."
                    if reason == "product_configuration_review" else
                    "Клієнт питає непідтверджені деталі товару: перевірити точне питання перед відповіддю."
                    if reason == "product_information_review" else
                    "Клієнту потрібна допомога команди: відкрийте поточну розмову."
                ),
                event_key=event_key,
                trigger=IgFollowUpTask.Trigger.EVENT, event_occurred_at=now,
                skip_reason="human_business_decision_required", policy_started_at=now,
                policy_version="revision-case-v1",
            )
        if task.manager_context is not None and not isinstance(task.manager_context, dict):
            return RevisionIntentResult(reason="product_inquiry_handoff_invalid")
        context = dict(task.manager_context or {})
        if context and not _manager_source_refs_valid(context):
            return RevisionIntentResult(reason='manager_receipt_invalid')
        # Keep the pending-case anchor stable through this reply's final gate.
        # New questions get their own immutable capability and notification;
        # changing the anchor mid-send would invalidate the captured context.
        pending_anchor = {key: context.get(key) for key in ("latest_revision_id", "generation_proposal_digest")}
        anchored_business = context.get('business_cooperation')
        previous = list(context.get("sources") or [])
        known = {item["message_id"] for item in previous}
        previous.extend(item for item in source_refs if item["message_id"] not in known)
        context.update({
            "schema_version": 1, "case_kind": reason, "sources": previous[-64:],
            "latest_revision_id": revision.pk, "generation_proposal_digest": revision.generation_proposal_digest,
            "required_decisions": (
                ["design_feasibility", "quote_approval"] if reason == "custom_print" else
                ["employment_service_review", "customer_support_scope", "work_conditions"] if reason == "collaboration_review" and recruitment else
                ["business_cooperation_review", "partnership_terms", "customer_questions"] if reason == "collaboration_review" and business else
                ["portfolio_review", "collaboration_terms"] if reason == "collaboration_review" else
                ["customer_request"]
            ),
            "authority": {"price_confirmed": False, "fulfillment_started": False},
        })
        if reason == "customer_manager_request":
            from management.services.ig_revision_checkout import requested_manual_delivery
            delivery_request = requested_manual_delivery(client, revision.bundle_snapshot.get("sources") or ())
            if delivery_request:
                from management.services.ig_commerce_projection import capture_current_selection_lines
                cart = capture_current_selection_lines(client.pk)
                context["delivery_request"] = delivery_request
                context["delivery_scope"] = {key: (cart.get("scope") or {}).get(key)
                    for key in ("client_id", "episode_id", "reset_floor", "source_namespace")}
                context["delivery_cart"] = [{"line_id": row["line_id"], "recipient_id": row["recipient_id"],
                    "fields": row.get("fields") or {}} for row in cart.get("lines") or []]
                context["required_decisions"] = (["requested_carrier_arrangement"] if delivery_request.get("carrier") else []) + (
                    ["distinct_destinations_arrangement"] if delivery_request.get("multiple_destinations") else []) + ["customer_delivery_confirmation"]
                quotes = [delivery_request["source_quote"]] if delivery_request.get("source_quote") else []
                if delivery_request.get("multiple_destinations"):
                    quotes.append(delivery_request["multiple_destinations"]["source_quote"])
                task.message_text = ("Клієнт просить узгодити доставку вручну. Стандартна сторінка оформлення "
                    "використовує Нову пошту й одну адресу на всю корзину. Не замінювати вибір клієнта автоматично.\n"
                    + "\n".join(dict.fromkeys(quotes)))
        if reason == "collaboration_review":
            context["collaboration_brief"] = collaboration
            context["cooperation_kind"] = "recruitment_service" if recruitment else "business" if business else "creator_or_other"
            context["business_cooperation"] = business
            context["recruitment_service"] = recruitment
            if recruitment.get("creator_work_also_offered"):
                context["required_decisions"] += ["portfolio_review", "collaboration_terms"]
            if recruitment:
                task.message_text = "Клієнт пропонує допомогу з Instagram Direct / підтримкою клієнтів: передати пропозицію адміністрації для розгляду потреби й умов. Вакансію не підтверджено."
            elif business:
                # A pre-existing creator case must not keep its old portfolio
                # instructions when this current source opens a trade inquiry.
                task.message_text = "Клієнт питає про магазин, перепродаж або бізнес-співпрацю: розглянути його питання й погодити індивідуальні умови."
        if reason in {'customer_manager_request', 'business_review', 'collaboration_review'}:
            manager_scopes.append(_native_manager_scope(revision, reason, collaboration))
            context['manager_case_scopes'] = manager_scopes
            context['latest_manager_case_revision_id'] = revision.pk
            context['source_questions'] = [row for scope in manager_scopes for row in scope['source_questions']]
            if reason == 'collaboration_review':
                context['collaboration_brief'] = _combined_collaboration_brief(manager_scopes)
                context['current_business_cooperation'] = context['collaboration_brief'].get('business_cooperation') or {}
            task.message_text = _product_inquiry_case_message(task.message_text,
                {'inquiries': context['source_questions']})
            if reason == 'collaboration_review' and business:
                from management.services.ig_business_cooperation import business_source_observations, render_business_observations
                observations = business_source_observations(revision.bundle_snapshot.get('sources') or [])
                if observations:
                    # Current display supplement only. Never change schema-1
                    # business facts or immutable historical case scopes.
                    context['business_source_observations'] = observations
                    task.message_text += '\n\n' + render_business_observations(observations)
            if task.event_key != event_key:
                context.update(pending_anchor)
                if reason == 'collaboration_review' and business:
                    context['business_cooperation'] = anchored_business
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
        if product_inquiry.get("status") == "manager_required":
            context["product_inquiries"] = product_inquiry
            if reason == "product_information_review":
                information_scopes.append({"revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
                    "generation_proposal_digest": revision.generation_proposal_digest, "product_inquiries": product_inquiry})
                context["product_inquiry_scopes"] = information_scopes
                context["product_inquiries"] = _combined_product_information(information_scopes)
                context["latest_product_inquiry_revision_id"] = revision.pk
                if task.event_key != event_key:
                    context.update(pending_anchor)
            context["required_decisions"] = list(dict.fromkeys([*context["required_decisions"],
                "exact_requested_configuration" if product_inquiry["reason"] == "product_configuration_review" else "exact_product_information"]))
            context["authority"].update(catalog_confirmed=False, stock_confirmed=False, checkout_authorized=False)
            if reason in REVIEW_REASONS:
                task.message_text = _product_inquiry_case_message(task.message_text, context["product_inquiries"])
        if complaint_evidence:
            context["media_complaint_evidence"] = complaint_evidence
            if "customer_service_review" not in context["required_decisions"]:
                context["required_decisions"].append("customer_service_review")
            from management.services.ig_turn_intent import media_complaint_source_scope
            context["media_complaint_scope"] = media_complaint_source_scope(client, revision,
                [row["message_id"] for row in source_refs])
        task.manager_context = context
        task.save(update_fields=["manager_context", "message_text", "updated_at"])
        notification_key = f"ig-revision-case:{task.pk}"
        if reason in {'product_information_review', 'customer_manager_request', 'business_review', 'collaboration_review'} and task.event_key != event_key:
            notification_key += f":{revision.pk}"
        alert = format_operator_alert("Клієнту потрібна допомога команди", event_type="escalation", client_id=client.pk, task_id=task.pk, counts={"sources": len(previous)}, instruction_code="escalation")
        if product_inquiry.get("status") == "manager_required":
            questions = list(dict.fromkeys(row["question"] for row in context["product_inquiries"]["inquiries"]))
            alert += "\n\nТочний запит про товар: " + " · ".join(questions)[:600]
        elif context.get('source_questions'):
            alert += '\n\nПитання клієнта: ' + ' · '.join(dict.fromkeys(row['question'] for row in context['source_questions']))[:600]
        if context.get('business_source_observations'):
            from management.services.ig_business_cooperation import render_business_observations
            alert += '\n\n' + render_business_observations(context['business_source_observations'])[:1200]
        notified = notify_manager(
            alert,
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
