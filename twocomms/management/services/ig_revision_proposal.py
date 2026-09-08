"""Write-once validated generation proposal for one claimed turn revision."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    GeminiRequest,
    GeminiRequestAttempt,
    IgClient,
    IgCustomerTurnRevision,
    InstagramBotSettings,
)
from management.services.gemini_accounting_contract import (
    RequestPolicyManifestError,
    sanitize_request_policy_manifest,
)
from management.services.ig_media_manifest import (
    MediaManifestError,
    map_image_observations,
)
from management.services.ig_response_control import (
    PROVIDER_CONTROL_KINDS,
    ResponseControl,
    ValidatedResponse,
)
from management.services.ig_revision_authority import (
    RevisionAuthorityBindingSet,
    check_fact_bindings,
    check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding,
    _safe_bindings,
    pre_winner_readiness,
)
from management.services.ig_revision_media import BINDING_VERSION


SCHEMA_VERSION = 1
MAX_PROPOSAL_BYTES = 128 * 1024
MAX_INLINE_PARTS = 8
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_INTENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class RevisionProposalResult:
    stored: bool = False
    created: bool = False
    digest: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevisionInspectionProjectionResult:
    projected: int = 0
    skipped: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RevisionCustomerRouteCapture:
    """Backend capability captured before HTTP; only prompt_context reaches Gemini."""

    revision_id: int
    revision_token: str
    settings_id: int
    settings_permission_epoch: int
    client_permission_epoch: int
    reset_floor: int
    watermark_message_id: int
    input_digest: str
    expected_previous_decision_id: int | None
    user_sources: tuple[tuple[int, str], ...]
    active_intent_keys: tuple[str, ...]
    focus_key: str
    captured_at: datetime

    def binding(self):
        return {"schema_version": "route-source.v1", "settings_id": self.settings_id,
            "settings_permission_epoch": self.settings_permission_epoch,
            "client_permission_epoch": self.client_permission_epoch,
            "reset_floor": self.reset_floor, "watermark_message_id": self.watermark_message_id,
            "input_digest": self.input_digest}

    def prompt_context(self):
        return {"sources": [{"message_id": pk, "text": text} for pk, text in self.user_sources],
            "active_intent_keys": list(self.active_intent_keys), "focus_key": self.focus_key}


def capture_revision_customer_routes(revision_id, revision_token, *, settings_id,
    settings_permission_epoch, publication, now=None):
    """Short settings/client/revision transaction; no provider or business effects."""
    from management.models import IgConversationRouteDecision, InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_revision_outbox import _cas_readiness

    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return None
    with transaction.atomic():
        settings_obj = InstagramBotSettings.objects.select_for_update().select_related(
            "active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["client_id"]).first()
        if revision is None or client is None or settings_obj is None:
            return None
        ready = _cas_readiness(revision, client=client, settings_obj=settings_obj,
            revision_token=revision_token, settings_id=settings_id,
            settings_permission_epoch=settings_permission_epoch, publication=publication,
            fact_bindings=[], offer_bindings=[], now=now)
        if not ready.ready:
            return None
        sealed, _parts = _snapshot_sources(revision)
        if not sealed:
            return None
        ids = [item["message_id"] for item in sealed]
        floor = conversation_route_reset_floor(client.pk)
        current = {row.pk: row for row in InstagramBotMessage.objects.filter(client=client, pk__in=ids)}
        user_sources = []
        for row in revision.sources.order_by("ordinal", "id"):
            message = current.get(row.message_id)
            if (message is None or message.role != row.role or message.text != row.text
                or row.message_id < floor):
                return None
            if row.role == InstagramBotMessage.Role.USER and row.text.strip():
                user_sources.append((row.message_id, row.text))
        if not user_sources:
            return None
        previous = IgConversationRouteDecision.objects.filter(client=client, reset_floor=floor).order_by(
            "-sequence").first()
        return RevisionCustomerRouteCapture(revision.pk, revision_token, settings_id,
            settings_permission_epoch, revision.permission_epoch, floor, max(ids), revision.snapshot_digest,
            previous.pk if previous else None, tuple(user_sources),
            tuple(item["key"] for item in previous.active_intents) if previous else (),
            previous.focus_key if previous else "", now)


def _customer_route_projection(response, capture, revision, revision_token, settings_obj, generated_at):
    """Route defects abstain locally; reply/control validation stays independent."""
    from management.models import InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_customer_route_contract import CustomerRouteProposal, normalize_customer_routes

    if not isinstance(response.customer_routes, CustomerRouteProposal):
        return {"route_abstention_reason": response.route_abstention_reason or "route_missing"}
    normalized = normalize_customer_routes(response.customer_routes.to_dict())
    if normalized.abstained:
        return {"route_abstention_reason": normalized.reason_code}
    if not isinstance(capture, RevisionCustomerRouteCapture):
        return {"route_abstention_reason": "route_binding_missing"}
    if (capture.revision_id != revision.pk or capture.revision_token != revision_token
        or capture.settings_id != settings_obj.pk
        or capture.settings_permission_epoch != settings_obj.reply_permission_epoch
        or capture.client_permission_epoch != revision.permission_epoch
        or capture.input_digest != revision.snapshot_digest
        or capture.captured_at > generated_at
        or capture.reset_floor != conversation_route_reset_floor(revision.client_id)):
        return {"route_abstention_reason": "route_binding_changed"}
    sealed_rows = list(revision.sources.order_by("ordinal", "id"))
    if (not sealed_rows or capture.watermark_message_id != max(row.message_id for row in sealed_rows)
        or capture.user_sources != tuple((row.message_id, row.text) for row in sealed_rows
            if row.role == InstagramBotMessage.Role.USER and row.text.strip())):
        return {"route_abstention_reason": "route_source_changed"}
    ids = [pk for pk, _text in capture.user_sources]
    current = {row.pk: row for row in InstagramBotMessage.objects.filter(
        client_id=revision.client_id, pk__in=ids, role=InstagramBotMessage.Role.USER)}
    if any(pk not in current or current[pk].text != text for pk, text in capture.user_sources):
        return {"route_abstention_reason": "route_source_changed"}
    evidence = {pk for intent in normalized.proposal.intents for pk in intent.evidence_message_ids}
    if not evidence.issubset(ids):
        return {"route_abstention_reason": "evidence_outside_source"}
    return {"customer_routes": normalized.proposal.to_dict(), "route_binding": capture.binding(),
        "route_expected_previous_decision_id": capture.expected_previous_decision_id}


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bounded_token(value, maximum: int) -> str:
    token = str(value or "").strip()
    return token if len(token) <= maximum and _TOKEN_RE.fullmatch(token) else ""


def _response_projection(response) -> tuple[dict | None, str]:
    if not isinstance(response, ValidatedResponse) or not response.valid:
        return None, "response_not_validated"
    reply = str(response.reply_text or "").strip()
    if not reply or len(reply) > 4000:
        return None, "reply_text_invalid"
    if len(response.controls) > 32:
        return None, "control_count_invalid"
    controls = []
    for control in response.controls:
        if (
            not isinstance(control, ResponseControl)
            or control.kind not in PROVIDER_CONTROL_KINDS
            or not isinstance(control.value, (str, bool))
        ):
            return None, "control_invalid"
        value = control.value
        if isinstance(value, str) and len(value) > 1000:
            return None, "control_invalid"
        controls.append({"kind": control.kind, "value": value})
    projected = {"reply_text": reply, "controls": controls}
    if response.follow_cta is not None:
        text = str(response.follow_cta.text or "").strip()
        if not text or len(text) > 500:
            return None, "follow_cta_invalid"
        projected["follow_cta"] = {"text": text}
    return projected, ""


def _snapshot_sources(revision) -> tuple[list[dict], dict[tuple[int, str], dict]]:
    snapshots = (revision.bundle_snapshot or {}).get("sources")
    if not isinstance(snapshots, list) or len(snapshots) != revision.source_count:
        return [], {}
    source_rows = list(revision.sources.order_by("ordinal", "id"))
    if len(source_rows) != len(snapshots):
        return [], {}
    sources = []
    parts = {}
    for row, snapshot in zip(source_rows, snapshots, strict=True):
        if (
            not isinstance(snapshot, Mapping)
            or int(snapshot.get("message_id") or 0) != row.message_id
            or str(snapshot.get("source_digest") or "") != row.source_digest
        ):
            return [], {}
        sources.append({
            "message_id": row.message_id,
            "source_digest": row.source_digest,
            "ordinal": row.ordinal,
        })
        media = snapshot.get("media_parts")
        if not isinstance(media, list):
            return [], {}
        for item in media:
            if not isinstance(item, Mapping):
                return [], {}
            part_id = str(item.get("source_part_id") or "")
            key = (row.message_id, part_id)
            if not part_id or key in parts:
                return [], {}
            parts[key] = dict(item)
    return sources, parts


def _request_media_projection(revision, value) -> tuple[dict | None, str]:
    if not isinstance(value, Mapping):
        return None, "request_media_manifest_invalid"
    items = value.get("items")
    actual_count = value.get("actual_inline_count")
    actual_hashes = value.get("actual_content_hashes")
    if (
        not isinstance(items, list)
        or len(items) > MAX_INLINE_PARTS
        or isinstance(actual_count, bool)
        or not isinstance(actual_count, int)
        or not 0 <= actual_count <= len(items)
        or not isinstance(actual_hashes, list)
        or value.get("version") != BINDING_VERSION
    ):
        return None, "request_media_manifest_invalid"
    collection_digest = str(value.get("digest") or "").casefold()
    base = {
        key: value[key]
        for key in (
            "version", "revision_id", "revision_snapshot_digest", "items",
            "outcomes", "actual_content_hashes", "coverage",
        )
        if key in value
    }
    # The collector digest predates provider-reported admission fields.
    base["actual_content_hashes"] = [
        str(item.get("content_hash") or "") for item in items
    ]
    if not _HASH_RE.fullmatch(collection_digest) or _digest(base) != collection_digest:
        return None, "request_media_digest_mismatch"
    if (
        int(value.get("revision_id") or 0) != revision.pk
        or value.get("revision_snapshot_digest") != revision.snapshot_digest
    ):
        return None, "request_media_binding_mismatch"
    _sources, sealed_parts = _snapshot_sources(revision)
    if not sealed_parts and items:
        return None, "revision_sources_invalid"
    normalized = []
    seen = set()
    for inline_index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            return None, "request_media_manifest_invalid"
        try:
            message_id = int(raw.get("source_message_id") or 0)
            original_index = int(raw.get("original_index") or 0)
            byte_length = int(raw.get("bytes") or 0)
        except (TypeError, ValueError):
            return None, "request_media_manifest_invalid"
        part_id = str(raw.get("source_part_id") or "")
        content_hash = str(raw.get("content_hash") or "").casefold()
        mime = str(raw.get("mime") or "")[:100]
        key = (message_id, part_id)
        sealed = sealed_parts.get(key)
        if key in seen or sealed is None or sealed.get("capture_outcome") != "owned":
            return None, "request_media_binding_mismatch"
        if (
            str(sealed.get("content_hash") or "").casefold() != content_hash
            or str(sealed.get("mime") or "") != mime
            or int(sealed.get("bytes") or 0) != byte_length
            or int(sealed.get("original_index") or 0) != original_index
            or not _HASH_RE.fullmatch(content_hash)
            or byte_length <= 0
            or raw.get("inline_index") != inline_index
        ):
            return None, "request_media_part_mismatch"
        sealed_origin = str(sealed.get("identity_origin") or "")[:32]
        request_origin = str(raw.get("identity_origin") or "")[:32]
        if sealed_origin != request_origin:
            return None, "request_media_identity_origin_mismatch"
        seen.add(key)
        normalized.append({
            "source_message_id": message_id,
            "source_part_id": part_id,
            "original_index": original_index,
            "identity_origin": str(raw.get("identity_origin") or "")[:32],
            "mime": mime,
            "bytes": byte_length,
            "content_hash": content_hash,
            "capture_state": "owned",
            "source_image_index": inline_index,
        })
    expected_hashes = [item["content_hash"] for item in normalized[:actual_count]]
    if [str(value or "").casefold() for value in actual_hashes] != expected_hashes:
        return None, "request_media_binding_mismatch"
    try:
        from management.services.ig_media_manifest import inline_part_evidence

        inline_part_evidence(
            normalized,
            image_count=len(normalized),
            actual_inline_count=actual_count,
            actual_content_hashes=actual_hashes,
        )
    except MediaManifestError:
        return None, "request_media_binding_mismatch"
    return {
        "version": str(value.get("version") or "")[:40],
        "collection_digest": collection_digest,
        "items": normalized,
        "actual_inline_count": actual_count,
        "actual_content_hashes": expected_hashes,
    }, ""


def _intelligence_projection(artifact, media, request_id, model, revision, prize_programme):
    if artifact in (None, {}):
        return {}, ""
    if not isinstance(artifact, Mapping) or artifact.get("schema_version") != 1:
        return None, "turn_intelligence_invalid"
    request = artifact.get("media_request")
    if not isinstance(request, Mapping):
        return None, "turn_intelligence_invalid"
    submitted = request.get("submitted_parts")
    if not isinstance(submitted, list) or len(submitted) != len(media["items"]):
        return None, "turn_intelligence_media_mismatch"
    for expected, actual in zip(submitted, media["items"], strict=True):
        if (
            not isinstance(expected, Mapping)
            or str(expected.get("source_part_id") or "") != actual["source_part_id"]
            or str(expected.get("content_hash") or "").casefold()
            != actual["content_hash"]
        ):
            return None, "turn_intelligence_media_mismatch"
    if (
        request.get("request_id") != request_id
        or request.get("provider_model") != model
        or request.get("inline_count_known") is not True
        or request.get("actual_inline_count") != media["actual_inline_count"]
    ):
        return None, "turn_intelligence_request_mismatch"
    try:
        observations = map_image_observations(
            media["items"],
            artifact.get("image_observations") or [],
            image_count=len(media["items"]),
            actual_inline_count=media["actual_inline_count"],
            actual_content_hashes=media["actual_content_hashes"],
            prize_programme=prize_programme,
        )
    except MediaManifestError:
        return None, "turn_intelligence_observations_invalid"
    expected_image_indexes = {
        index for index, item in enumerate(media["items"][:media["actual_inline_count"]])
        if item["mime"].startswith("image/")
    }
    if {item["source_image_index"] for item in observations} != expected_image_indexes:
        return None, "turn_intelligence_observations_incomplete"
    candidates = artifact.get("catalog_candidates")
    if not isinstance(candidates, list) or len(candidates) > 8:
        return None, "turn_intelligence_invalid"
    clean_candidates = []
    seen_products = set()
    for item in candidates:
        if not isinstance(item, Mapping):
            return None, "turn_intelligence_invalid"
        try:
            product_id = int(item.get("product_id") or 0)
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            return None, "turn_intelligence_invalid"
        if product_id <= 0 or product_id in seen_products or not 0 <= confidence <= 1:
            return None, "turn_intelligence_invalid"
        seen_products.add(product_id)
        clean_candidates.append({
            "product_id": product_id,
            "title": str(item.get("title") or "")[:160],
            "slug": str(item.get("slug") or "")[:180],
            "confidence": round(confidence, 4),
            "evidence": str(item.get("evidence") or "")[:240],
        })
    intent = str(artifact.get("intent") or "").strip().casefold()
    transcript = str(artifact.get("transcript") or "")[:4000]
    audio_status = str(artifact.get("audio_status") or "").casefold()
    try:
        confidence = float(artifact.get("confidence") or 0)
    except (TypeError, ValueError):
        return None, "turn_intelligence_invalid"
    has_audio = any(
        item["mime"].startswith("audio/")
        for item in media["items"][:media["actual_inline_count"]]
    )
    candidate_digest = str(artifact.get("candidate_set_digest") or "").casefold()
    resolution = str(artifact.get("catalog_resolution") or "")
    auto_product_id = artifact.get("auto_product_id")
    if auto_product_id is not None:
        try:
            auto_product_id = int(auto_product_id)
        except (TypeError, ValueError):
            return None, "turn_intelligence_invalid"
        if auto_product_id <= 0 or auto_product_id not in seen_products:
            return None, "turn_intelligence_invalid"
    if (
        not _INTENT_RE.fullmatch(intent)
        or not 0 <= confidence <= 1
        or (candidate_digest and not _HASH_RE.fullmatch(candidate_digest))
        or resolution not in {
            "auto_select", "clarify", "single_low_confidence", "no_match",
        }
        or audio_status not in {"not_applicable", "transcribed", "unintelligible"}
        or (not has_audio and (transcript or audio_status != "not_applicable"))
        or (has_audio and audio_status == "not_applicable")
        or (has_audio and audio_status == "transcribed" and not transcript.strip())
        or (has_audio and audio_status == "unintelligible" and transcript.strip())
        or artifact.get("request_permission_epoch") != revision.permission_epoch
    ):
        return None, "turn_intelligence_invalid"
    return {
        "schema_version": 1,
        "candidate_set_version": str(artifact.get("candidate_set_version") or "")[:40],
        "candidate_set_digest": candidate_digest,
        "candidate_set_size": int(artifact.get("candidate_set_size") or 0),
        "catalog_candidates": clean_candidates,
        "transcript": transcript,
        "intent": intent,
        "audio_status": audio_status,
        "confidence": round(confidence, 4),
        "image_observations": observations,
        "media_request": {
            "request_id": request_id,
            "provider_model": model,
            "inline_count_known": True,
            "actual_inline_count": media["actual_inline_count"],
            "prepared_inline_count": len(media["items"]),
            "submitted_parts": [
                {
                    "source_message_id": item["source_message_id"],
                    "source_part_id": item["source_part_id"],
                    "original_index": item["original_index"],
                    "content_hash": item["content_hash"],
                }
                for item in media["items"]
            ],
        },
        "catalog_resolution": resolution,
        "auto_product_id": auto_product_id,
        "request_permission_epoch": revision.permission_epoch,
    }, ""


def _generation_graph_matches(
    *,
    revision,
    source_ids,
    request_id: str,
    model: str,
    policy_manifest: dict,
) -> bool:
    graph = (
        GeminiRequest.objects.select_for_update()
        .select_related("winner_attempt")
        .filter(request_id=request_id)
        .first()
    )
    if graph is None or graph.winner_attempt_id is None:
        return False
    winner = graph.winner_attempt
    expected_turn = f"ig-revision:{revision.pk}"
    return bool(
        graph.accounting_mode in {
            GeminiRequest.AccountingMode.SHADOW,
            GeminiRequest.AccountingMode.ENFORCED,
            GeminiRequest.AccountingMode.EMERGENCY,
        }
        and graph.lane == "live"
        and graph.client_id == revision.client_id
        and graph.source_message_id in source_ids
        and graph.logical_turn_id == expected_turn
        and graph.source_execution_key == expected_turn
        and graph.policy_manifest == policy_manifest
        and graph.terminal_resolution == "succeeded"
        and winner.request_graph_id == graph.pk
        and winner.request_id == request_id
        and winner.model == model
        and winner.fsm_state == GeminiRequestAttempt.FsmState.SUCCEEDED
        and winner.outcome == "succeeded"
        and winner.winner_claimed is True
        and winner.client_id == revision.client_id
        and winner.source_message_id == graph.source_message_id
        and winner.logical_turn_id == expected_turn
        and winner.lane == "live"
    )


def store_revision_generation_proposal(
    revision_id: int,
    revision_token: str,
    *,
    source_message_ids,
    settings_id: int,
    settings_permission_epoch: int,
    publication: PublicationBinding,
    request_id: str,
    actual_model: str,
    generated_at: datetime,
    response: ValidatedResponse,
    turn_intelligence,
    request_media_manifest,
    policy_manifest,
    authority: RevisionAuthorityBindingSet,
    fact_checker=check_fact_bindings,
    offer_checker=check_offer_bindings,
    prize_programme=None,
    customer_route_capture: RevisionCustomerRouteCapture | None = None,
) -> RevisionProposalResult:
    """Persist one exact validated proposal before any business or send effect."""
    if connection.in_atomic_block:
        return RevisionProposalResult(reasons=("caller_transaction_active",))
    request_id = _bounded_token(request_id, 40)
    model = _bounded_token(actual_model, 80)
    if not request_id or not model or not isinstance(generated_at, datetime):
        return RevisionProposalResult(reasons=("generation_identity_invalid",))
    if timezone.is_naive(generated_at):
        return RevisionProposalResult(reasons=("generated_at_naive",))
    response_value, reason = _response_projection(response)
    if response_value is None:
        return RevisionProposalResult(reasons=(reason,))
    if not isinstance(authority, RevisionAuthorityBindingSet) or not authority.ready:
        return RevisionProposalResult(reasons=("authority_invalid",))
    try:
        safe_policy = sanitize_request_policy_manifest(policy_manifest)
        safe_facts = _safe_bindings(authority.fact_bindings)
        safe_offers = _safe_bindings(authority.offer_bindings)
    except (RequestPolicyManifestError, ValueError) as exc:
        return RevisionProposalResult(reasons=(getattr(exc, "code", str(exc)),))
    if (
        not _HASH_RE.fullmatch(str(authority.authority_digest or ""))
        or fact_checker is not check_fact_bindings
        or offer_checker is not check_offer_bindings
    ):
        return RevisionProposalResult(reasons=("authority_invalid",))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id"
    ).first()
    if identity is None:
        return RevisionProposalResult(reasons=("revision_missing",))
    with transaction.atomic():
        settings_obj = (
            InstagramBotSettings.objects.select_for_update()
            .select_related("active_instruction_publication")
            .filter(pk=settings_id)
            .first()
        )
        client = IgClient.objects.select_for_update().filter(
            pk=identity["client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["client_id"]
        ).first()
        if settings_obj is None or client is None or revision is None:
            return RevisionProposalResult(reasons=("proposal_identity_missing",))
        sources, _parts = _snapshot_sources(revision)
        ordered_ids = tuple(item["message_id"] for item in sources)
        try:
            supplied_ids = tuple(int(value) for value in source_message_ids)
        except (TypeError, ValueError):
            supplied_ids = ()
        if not ordered_ids or supplied_ids != ordered_ids:
            return RevisionProposalResult(reasons=("source_identity_mismatch",))
        if not _generation_graph_matches(
            revision=revision,
            source_ids=set(ordered_ids),
            request_id=request_id,
            model=model,
            policy_manifest=safe_policy,
        ):
            return RevisionProposalResult(reasons=("generation_graph_invalid",))
        readiness = pre_winner_readiness(
            revision.pk,
            revision_token,
            settings_id=settings_obj.pk,
            settings_permission_epoch=settings_permission_epoch,
            publication=publication,
            fact_bindings=safe_facts,
            offer_bindings=safe_offers,
            fact_checker=fact_checker,
            offer_checker=offer_checker,
            now=timezone.now(),
        )
        if not readiness.ready:
            return RevisionProposalResult(reasons=readiness.reasons)
        pub = safe_policy.get("instruction_publication") or {}
        if (
            pub.get("id") != publication.publication_id
            or pub.get("version") != publication.version
            or pub.get("hash") != publication.snapshot_hash
        ):
            return RevisionProposalResult(reasons=("policy_publication_mismatch",))
        media, reason = _request_media_projection(revision, request_media_manifest)
        if media is None:
            return RevisionProposalResult(reasons=(reason,))
        intelligence, reason = _intelligence_projection(
            turn_intelligence, media, request_id, model, revision, prize_programme
        )
        if intelligence is None:
            return RevisionProposalResult(reasons=(reason,))
        proposal = {
            "schema_version": SCHEMA_VERSION,
            "execution_binding": {
                "settings_id": int(settings_obj.pk),
                "settings_permission_epoch": int(settings_permission_epoch),
            },
            "sources": sources,
            "generation": {
                "request_id": request_id,
                "actual_model": model,
                "generated_at": generated_at.isoformat(),
            },
            "response": response_value,
            "turn_intelligence": intelligence,
            "request_media_manifest": media,
            "policy_manifest": safe_policy,
            "authority": {
                "allowed_actions": list(authority.allowed_actions),
                "fact_bindings": safe_facts,
                "offer_bindings": safe_offers,
                "authority_digest": authority.authority_digest,
            },
        }
        try:
            # Catch OUTSIDE the savepoint: a route DB error must roll back its
            # own work and clear the broken-transaction state before the already
            # won main proposal is saved in this outer transaction.
            with transaction.atomic():
                route_projection = _customer_route_projection(response, customer_route_capture,
                    revision, revision_token, settings_obj, generated_at)
        except Exception:
            route_projection = {"route_abstention_reason": "route_projection_unavailable"}
        proposal.update(route_projection)
        encoded = _canonical(proposal)
        if len(encoded) > MAX_PROPOSAL_BYTES:
            return RevisionProposalResult(reasons=("proposal_too_large",))
        proposal_digest = hashlib.sha256(encoded).hexdigest()
        if revision.generation_proposal_digest:
            same = (
                revision.generation_proposal_digest == proposal_digest
                and revision.generation_proposal == proposal
            )
            return RevisionProposalResult(
                stored=same,
                created=False,
                digest=revision.generation_proposal_digest,
                reasons=() if same else ("proposal_mismatch",),
            )
        revision.generation_proposal = proposal
        revision.generation_proposal_digest = proposal_digest
        revision.generation_proposed_at = generated_at
        revision.save(update_fields=[
            "generation_proposal", "generation_proposal_digest",
            "generation_proposed_at", "updated_at",
        ])
        return RevisionProposalResult(
            stored=True, created=True, digest=proposal_digest
        )


def project_revision_image_inspections(
    revision_id: int,
    revision_token: str,
) -> RevisionInspectionProjectionResult:
    """Project exact per-part observations without replacing source artifacts."""
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id"
    ).first()
    if identity is None:
        return RevisionInspectionProjectionResult(reasons=("revision_missing",))
    from management.models import InstagramBotMessage
    from management.services.ig_media_manifest import normalize_attachment_media

    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(
            pk=identity["client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["client_id"]
        ).first()
        if client is None or revision is None:
            return RevisionInspectionProjectionResult(
                reasons=("projection_identity_missing",)
            )
        if (
            revision.state != revision.State.CLAIMED
            or revision.active_slot != 1
            or not revision_token
            or revision.claim_token != revision_token
            or not revision.lease_until
            or revision.lease_until <= timezone.now()
            or client.reply_permission_epoch != revision.permission_epoch
            or client.privacy_erasure_started_at
            != revision.erasure_started_at_snapshot
            or client.privacy_erasure_started_at is not None
            or client.hidden_at is not None
            or client.is_blocked
        ):
            return RevisionInspectionProjectionResult(
                reasons=("revision_not_current",)
            )
        proposal = revision.generation_proposal
        if (
            not revision.generation_proposal_digest
            or not isinstance(proposal, Mapping)
            or _digest(proposal) != revision.generation_proposal_digest
        ):
            return RevisionInspectionProjectionResult(
                reasons=("proposal_invalid",)
            )
        intelligence = proposal.get("turn_intelligence")
        media = proposal.get("request_media_manifest")
        if not isinstance(intelligence, Mapping) or not isinstance(media, Mapping):
            return RevisionInspectionProjectionResult(
                reasons=("proposal_media_invalid",)
            )
        request = intelligence.get("media_request")
        submitted = request.get("submitted_parts") if isinstance(request, Mapping) else None
        media_items = media.get("items")
        observations = intelligence.get("image_observations")
        if not all(isinstance(value, list) for value in (
            submitted, media_items, observations,
        )) or len(submitted) != len(media_items):
            return RevisionInspectionProjectionResult(
                reasons=("proposal_media_invalid",)
            )
        observation_by_index = {
            int(item.get("source_image_index")): item
            for item in observations
            if isinstance(item, Mapping)
            and isinstance(item.get("source_image_index"), int)
        }
        message_ids = sorted({
            int(item.get("source_message_id") or 0)
            for item in submitted if isinstance(item, Mapping)
        })
        messages = {
            row.pk: row
            for row in InstagramBotMessage.objects.select_for_update().filter(
                pk__in=message_ids,
                client_id=client.pk,
                private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            )
        }
        projected = skipped = 0
        changed_messages = set()
        media_by_message = {}
        actual_count = int(media.get("actual_inline_count") or 0)
        generation = proposal.get("generation") or {}
        for index, (submitted_item, media_item) in enumerate(
            zip(submitted, media_items, strict=True)
        ):
            if not isinstance(submitted_item, Mapping) or not isinstance(media_item, Mapping):
                skipped += 1
                continue
            message_id = int(submitted_item.get("source_message_id") or 0)
            message = messages.get(message_id)
            if message is None or not str(media_item.get("mime") or "").startswith("image/"):
                skipped += 1
                continue
            if message_id not in media_by_message:
                try:
                    media_by_message[message_id] = normalize_attachment_media(
                        message.attachment_media
                        if isinstance(message.attachment_media, list) else [],
                        message_scope=message.pk,
                    )
                except MediaManifestError:
                    media_by_message[message_id] = []
            part_id = str(submitted_item.get("source_part_id") or "")
            content_hash = str(submitted_item.get("content_hash") or "").casefold()
            matches = [
                item for item in media_by_message[message_id]
                if str(item.get("source_part_id") or "") == part_id
                and str(item.get("content_hash") or "").casefold() == content_hash
                and (item.get("status") == "owned" or item.get("capture_state") == "owned")
            ]
            if len(matches) != 1:
                skipped += 1
                continue
            observation = observation_by_index.get(index)
            if observation is not None:
                matches[0]["inspection"] = {
                    "version": "ig-media-inspection-v1",
                    "state": "inspected",
                    "source_part_id": part_id,
                    "source_image_index": index,
                    "outcome": str(observation.get("outcome") or "")[:32],
                    "evidence_code": str(observation.get("evidence_code") or "")[:32],
                    "type_code": str(observation.get("type_code") or "")[:32],
                    "content_hash": content_hash,
                    "request_id": str(generation.get("request_id") or "")[:40],
                    "provider_model": str(generation.get("actual_model") or "")[:80],
                    "revision_id": revision.pk,
                }
            else:
                matches[0]["inspection"] = {
                    "version": "ig-media-inspection-v1",
                    "state": "uninspected",
                    "source_part_id": part_id,
                    "source_image_index": index,
                    "outcome": (
                        "provider_omitted" if index >= actual_count
                        else "observation_missing"
                    ),
                    "content_hash": content_hash,
                    "revision_id": revision.pk,
                }
            projected += 1
            changed_messages.add(message_id)
        for message_id in changed_messages:
            message = messages[message_id]
            # Deliberately leave `turn_intelligence_artifact` untouched: it is
            # historical source evidence, while the proposal is revision-owned.
            message.attachment_media = media_by_message[message_id]
            message.save(update_fields=["attachment_media"])
        reasons = ("parts_skipped",) if skipped else ()
        return RevisionInspectionProjectionResult(projected, skipped, reasons)


__all__ = [
    "RevisionInspectionProjectionResult",
    "RevisionProposalResult",
    "project_revision_image_inspections",
    "store_revision_generation_proposal",
]
