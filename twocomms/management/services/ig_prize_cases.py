"""Durable business-review case producer for validated prize candidates.

This module never confirms entitlement, money, payment, a promo code, or an
award.  It creates one local manager task and one notification intent; another
authorized workflow owns the eventual business decision and alert delivery.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Mapping

from django.db import transaction
from django.utils import timezone

from management.models import (
    IgBotNotification,
    IgClient,
    IgCustomerTurnRevision,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_manager_media_projection import project_manager_media
from management.services.ig_prize_programme import (
    PrizeProgramme,
    validate_prize_observation,
)


CASE_SCHEMA_VERSION = "ig-prize-case-v1"
CASE_POLICY_VERSION = "prize-case-v1"
CASE_REASON_PREFIX = "prize_review:"
NOTIFICATION_EVENT_TYPE = "prize_review"
MAX_CASE_EVIDENCE = 32
MAX_CASE_PREFERENCES = 32
PREFERENCE_KINDS = frozenset({"catalog", "custom"})


@dataclass(frozen=True)
class PrizeCaseResult:
    task_id: int | None = None
    notification_id: int | None = None
    created: bool = False
    evidence_added: int = 0
    preference_added: bool = False
    reason: str = ""


def _case_reason(programme: PrizeProgramme) -> str:
    return f"{CASE_REASON_PREFIX}{programme.programme_id}"[:120]


def _case_document(task: IgFollowUpTask | None) -> dict:
    if task is not None:
        parsed = task.manager_context if isinstance(task.manager_context, dict) else {}
        if (
            isinstance(parsed, dict)
            and parsed.get("schema_version") == CASE_SCHEMA_VERSION
        ):
            return parsed
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_state": "open",
        "candidate_status": "uncertain",
        "evidence": [],
        "preferences": [],
        "authority": {
            "entitlement": "unconfirmed",
            "payment": "not_asserted",
            "reward_value": None,
            "promo_code": None,
        },
    }


def _operator_summary(document: Mapping[str, object]) -> str:
    evidence_count = len(document.get("evidence") or [])
    preference_labels = {"catalog": "каталог", "custom": "власний принт"}
    preferences = [
        preference_labels[item.get("kind")]
        for item in document.get("preferences") or []
        if isinstance(item, Mapping) and item.get("kind") in PREFERENCE_KINDS
    ]
    preference_note = (
        "; вибір: " + ", ".join(dict.fromkeys(preferences))
        if preferences
        else ""
    )
    return (
        "Потребує перевірки: кандидат сертифіката стрілецького призу; "
        f"доказів: {evidence_count}{preference_note}. "
        "Підтвердьте програму, право та умови."
    )


def _matching_media_part(
    source: InstagramBotMessage,
    observation: Mapping[str, object],
) -> Mapping[str, object] | None:
    source_part_id = str(observation.get("source_part_id") or "")
    content_hash = str(observation.get("content_hash") or "")
    matches = [
        item
        for item in (source.attachment_media or [])
        if isinstance(item, Mapping)
        and str(item.get("source_part_id") or "") == source_part_id
        and str(item.get("content_hash") or "") == content_hash
    ]
    if len(matches) != 1:
        return None
    part = matches[0]
    mime = str(part.get("mime") or "").split(";", 1)[0].strip().casefold()
    if (
        part.get("status") != "owned"
        or part.get("private_storage") is not True
        or not str(part.get("storage_name") or "").strip()
        or not mime.startswith("image/")
    ):
        return None
    return part


def _validated_candidate_evidence(
    source: InstagramBotMessage,
    programme: PrizeProgramme,
) -> list[dict]:
    artifact = (
        source.turn_intelligence_artifact
        if isinstance(source.turn_intelligence_artifact, dict)
        else {}
    )
    if (
        artifact.get("source_message_id") != source.pk
        or not programme.programme_id
        or not programme.version
        or programme.manager_required is not True
    ):
        return []
    request = (
        artifact.get("media_request")
        if isinstance(artifact.get("media_request"), dict)
        else {}
    )
    request_id = str(request.get("request_id") or "")[:40]
    provider_model = str(request.get("provider_model") or "")[:80]
    if (
        not request_id
        or not provider_model
        or request.get("inline_count_known") is not True
    ):
        return []
    result = []
    for observation in artifact.get("image_observations") or []:
        if not isinstance(observation, Mapping):
            continue
        prize = observation.get("prize_certificate")
        if not isinstance(prize, Mapping):
            continue
        validated_prize = validate_prize_observation(
            dict(prize),
            programme=programme,
        )
        if validated_prize is None:
            continue
        cues = list(validated_prize.cue_codes)
        if (
            observation.get("type_code") != "certificate"
            or observation.get("outcome") not in {"understood", "uncertain"}
            or validated_prize.status not in {"recognized", "uncertain"}
            or not cues
        ):
            continue
        normalized_cues = tuple(str(code or "").strip().casefold() for code in cues)
        if (
            len(normalized_cues) != len(set(normalized_cues))
            or not set(normalized_cues).issubset(set(programme.cue_codes))
        ):
            continue
        media_part = _matching_media_part(source, observation)
        if media_part is None:
            continue
        inspection = (
            media_part.get("inspection")
            if isinstance(media_part.get("inspection"), Mapping)
            else {}
        )
        if (
            inspection.get("state") != "inspected"
            or str(inspection.get("source_part_id") or "")
            != str(observation.get("source_part_id") or "")
            or str(inspection.get("content_hash") or "")
            != str(observation.get("content_hash") or "")
            or str(inspection.get("request_id") or "") != request_id
            or str(inspection.get("provider_model") or "") != provider_model
        ):
            continue
        result.append({
            "source_message_id": source.pk,
            "source_part_id": str(observation.get("source_part_id") or "")[:36],
            "original_index": int(observation.get("original_index") or 0),
            "content_hash": str(observation.get("content_hash") or "")[:64],
            "request_id": request_id,
            "provider_model": provider_model,
            "outcome": str(observation.get("outcome") or "")[:32],
            "type_code": "certificate",
            "candidate_status": "uncertain",
            "programme_id": programme.programme_id,
            "programme_version": programme.version,
            "cue_codes": list(normalized_cues),
            "reason_code": validated_prize.reason_code[:64],
        })
    return result[:8]


def _canonical_digest(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision_observations_for_source(revision, source):
    proposal = revision.generation_proposal
    intelligence = proposal.get("turn_intelligence")
    generation = proposal.get("generation")
    if not isinstance(intelligence, Mapping) or not isinstance(generation, Mapping):
        return (), "proposal_intelligence_invalid"
    request = intelligence.get("media_request")
    submitted = request.get("submitted_parts") if isinstance(request, Mapping) else None
    observations = intelligence.get("image_observations")
    request_id = str(request.get("request_id") or "") if isinstance(request, Mapping) else ""
    provider_model = str(request.get("provider_model") or "") if isinstance(request, Mapping) else ""
    if (
        not isinstance(submitted, list)
        or not isinstance(observations, list)
        or len(submitted) > 8
        or len(observations) > 8
        or not request_id
        or not provider_model
        or request_id != str(generation.get("request_id") or "")
        or provider_model != str(generation.get("actual_model") or "")
    ):
        return (), "proposal_intelligence_invalid"
    rows = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            return (), "proposal_intelligence_invalid"
        index = observation.get("source_image_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(submitted):
            return (), "proposal_intelligence_invalid"
        part = submitted[index]
        if not isinstance(part, Mapping):
            return (), "proposal_intelligence_invalid"
        try:
            source_message_id = int(part.get("source_message_id") or 0)
        except (TypeError, ValueError):
            return (), "proposal_intelligence_invalid"
        if source_message_id != source.pk:
            continue
        if (
            str(observation.get("source_part_id") or "")
            != str(part.get("source_part_id") or "")
            or str(observation.get("content_hash") or "").casefold()
            != str(part.get("content_hash") or "").casefold()
        ):
            return (), "proposal_intelligence_invalid"
        rows.append((observation, part, request_id, provider_model))
    return tuple(rows), ""


def _validated_revision_candidate_evidence(
    revision,
    source: InstagramBotMessage,
    programme: PrizeProgramme,
) -> tuple[list[dict], bool, str]:
    rows, reason = _revision_observations_for_source(revision, source)
    if reason:
        return [], False, reason
    observations = []
    candidate_present = any(
        observation.get("prize_certificate") is not None
        for observation, _submitted, _request_id, _provider_model in rows
    )
    for observation, submitted, request_id, provider_model in rows:
        media_part = _matching_media_part(source, observation)
        if media_part is None:
            continue
        inspection = (
            media_part.get("inspection")
            if isinstance(media_part.get("inspection"), Mapping)
            else {}
        )
        try:
            inspection_revision_id = int(inspection.get("revision_id") or 0)
        except (TypeError, ValueError):
            inspection_revision_id = 0
        if (
            inspection.get("state") != "inspected"
            or inspection_revision_id != revision.pk
            or str(inspection.get("source_part_id") or "")
            != str(submitted.get("source_part_id") or "")
            or str(inspection.get("content_hash") or "").casefold()
            != str(submitted.get("content_hash") or "").casefold()
            or str(inspection.get("request_id") or "") != request_id
            or str(inspection.get("provider_model") or "") != provider_model
        ):
            continue
        observations.append(dict(observation))
    intelligence = revision.generation_proposal["turn_intelligence"]
    artifact = dict(intelligence)
    artifact["source_message_id"] = source.pk
    artifact["image_observations"] = observations
    proxy = SimpleNamespace(
        pk=source.pk,
        attachment_media=source.attachment_media,
        turn_intelligence_artifact=artifact,
    )
    result = _validated_candidate_evidence(proxy, programme)
    for item in result:
        item["revision_id"] = revision.pk
        item["generation_proposal_digest"] = revision.generation_proposal_digest
    return result, candidate_present, ""


def _revision_preference(revision) -> dict | None:
    intelligence = (revision.generation_proposal or {}).get("turn_intelligence")
    if not isinstance(intelligence, Mapping):
        return None
    kind = {"prize_catalog": "catalog", "prize_custom": "custom"}.get(
        str(intelligence.get("intent") or "")
    )
    if not kind:
        return None
    preference = {"kind": kind}
    if kind == "catalog":
        try:
            product_id = int(intelligence.get("auto_product_id") or 0)
        except (TypeError, ValueError):
            product_id = 0
        if product_id > 0:
            preference["product_id"] = product_id
    return preference


def _preference_entry(
    source: InstagramBotMessage,
    preference: Mapping[str, object] | None,
) -> dict | None:
    if not isinstance(preference, Mapping):
        return None
    kind = str(preference.get("kind") or "").strip().casefold()
    if kind not in PREFERENCE_KINDS:
        return None
    entry = {
        "source_message_id": source.pk,
        "kind": kind,
    }
    if kind == "catalog":
        try:
            product_id = int(preference.get("product_id") or 0)
        except (TypeError, ValueError):
            product_id = 0
        if product_id > 0:
            entry["product_id"] = product_id
    return entry


def _append_distinct(rows: list, additions: list, *, identity) -> tuple[list, int]:
    output = [dict(item) for item in rows if isinstance(item, Mapping)]
    known = {identity(item) for item in output}
    added = 0
    for item in additions:
        key = identity(item)
        if key in known:
            continue
        output.append(dict(item))
        known.add(key)
        added += 1
    return output, added


def _notification_payload(task: IgFollowUpTask, document: dict) -> dict:
    from management.services.ig_alerts import client_admin_url

    evidence = [
        item for item in document.get("evidence") or [] if isinstance(item, Mapping)
    ]
    media = project_manager_media([
        {
            "role": "prize_certificate_candidate",
            "message_id": item.get("source_message_id"),
            "source_part_id": item.get("source_part_id"),
            "provenance": "live_webhook",
        }
        for item in evidence
    ])
    return {
        "text": (
            "🏆 IG: потрібна перевірка кандидата призового сертифіката. "
            "Зображення не підтверджує право на приз. "
            f"Картка клієнта: {client_admin_url(task.client_id)}"
        ),
        "requires_human_review": True,
        "case_kind": "prize_review",
        "task_id": task.pk,
        "client_url": client_admin_url(task.client_id),
        "programme_id": document.get("programme_id"),
        "programme_version": document.get("programme_version"),
        "candidate_status": "uncertain",
        "evidence_count": len(evidence),
        "preference_count": len(document.get("preferences") or []),
        "media": media,
    }


def _merge_notification_payload(previous: object, current: dict) -> dict:
    """Update case facts without erasing or replaying notification delivery."""
    previous = dict(previous) if isinstance(previous, Mapping) else {}
    merged = {**previous, **current}
    previous_media = [
        dict(item)
        for item in previous.get("media") or []
        if isinstance(item, Mapping)
    ]
    current_media = [
        dict(item)
        for item in current.get("media") or []
        if isinstance(item, Mapping)
    ]

    def identity(item):
        return (
            str(item.get("preview_url") or ""),
            str(item.get("availability") or ""),
            str(item.get("role") or ""),
        )

    previous_by_key = {identity(item): item for item in previous_media}
    media = []
    seen = set()
    for item in current_media:
        key = identity(item)
        old = previous_by_key.get(key, {})
        combined = dict(item)
        for field in (
            "delivery_status",
            "delivery_message_id",
            "delivery_error",
        ):
            if field in old:
                combined[field] = old[field]
        media.append(combined)
        seen.add(key)
    for item in previous_media:
        key = identity(item)
        if key not in seen:
            media.append(item)
            seen.add(key)
    merged["media"] = media[:8]
    return merged


def upsert_prize_review_case(
    source_message: InstagramBotMessage | int,
    *,
    programme: PrizeProgramme | None,
    preference: Mapping[str, object] | None = None,
    expected_permission_epoch: int | None = None,
    revision_id: int | None = None,
    revision_token: str = "",
    expected_generation_proposal_digest: str = "",
    settings_id: int | None = None,
    settings_permission_epoch: int | None = None,
    publication=None,
    now=None,
) -> PrizeCaseResult:
    """Group validated evidence and preferences into one open business case."""
    if not isinstance(programme, PrizeProgramme):
        return PrizeCaseResult(reason="programme_missing")
    source_id = getattr(source_message, "pk", source_message)
    now = now or timezone.now()
    source_identity = (
        InstagramBotMessage.objects.filter(pk=source_id)
        .values("client_id")
        .first()
    )
    if not source_identity or not source_identity.get("client_id"):
        return PrizeCaseResult(reason="source_missing")
    revision_mode = any((
        revision_id is not None,
        bool(revision_token),
        bool(expected_generation_proposal_digest),
        settings_id is not None,
        settings_permission_epoch is not None,
        publication is not None,
    ))
    if revision_mode:
        from management.services.ig_revision_outbox import PublicationBinding

        if (
            isinstance(revision_id, bool)
            or not isinstance(revision_id, int)
            or revision_id <= 0
            or not revision_token
            or not isinstance(expected_generation_proposal_digest, str)
            or len(expected_generation_proposal_digest) != 64
            or isinstance(settings_id, bool)
            or not isinstance(settings_id, int)
            or settings_id <= 0
            or isinstance(settings_permission_epoch, bool)
            or not isinstance(settings_permission_epoch, int)
            or not isinstance(publication, PublicationBinding)
        ):
            return PrizeCaseResult(reason="revision_context_invalid")
    with transaction.atomic():
        settings_obj = None
        revision = None
        revision_source = None
        if revision_mode:
            settings_obj = (
                InstagramBotSettings.objects.select_for_update()
                .select_related("active_instruction_publication")
                .filter(pk=settings_id)
                .first()
            )
            if settings_obj is None:
                return PrizeCaseResult(reason="settings_missing")
        client = (
            IgClient.objects.select_for_update()
            .filter(pk=source_identity["client_id"])
            .first()
        )
        if client is None:
            return PrizeCaseResult(reason="source_missing")
        if revision_mode:
            revision = (
                IgCustomerTurnRevision.objects.select_for_update()
                .filter(pk=revision_id, client_id=client.pk)
                .first()
            )
            if revision is None:
                return PrizeCaseResult(reason="revision_missing")
            proposal = revision.generation_proposal
            if (
                revision.generation_proposal_digest
                != expected_generation_proposal_digest
                or not isinstance(proposal, Mapping)
                or _canonical_digest(proposal)
                != expected_generation_proposal_digest
            ):
                return PrizeCaseResult(reason="generation_proposal_changed")
            authority = proposal.get("authority")
            if (
                not isinstance(authority, Mapping)
                or "prize_review_case_create"
                not in (authority.get("allowed_actions") or ())
            ):
                return PrizeCaseResult(reason="action_not_authorized")
            from management.services.ig_revision_authority import (
                check_fact_bindings,
                check_offer_bindings,
            )
            from management.services.ig_revision_outbox import (
                pre_winner_readiness,
            )

            readiness = pre_winner_readiness(
                revision.pk,
                revision_token,
                settings_id=settings_obj.pk,
                settings_permission_epoch=settings_permission_epoch,
                publication=publication,
                fact_bindings=authority.get("fact_bindings") or (),
                offer_bindings=authority.get("offer_bindings") or (),
                fact_checker=check_fact_bindings,
                offer_checker=check_offer_bindings,
                now=now,
            )
            if not readiness.ready:
                return PrizeCaseResult(
                    reason=readiness.reasons[0] if readiness.reasons else "revision_not_current"
                )
            try:
                from management.services.ig_policy_publication import (
                    load_active_policy_snapshot,
                )
                from management.services.ig_prize_programme import (
                    active_shooting_prize_programme,
                )

                current_programme = active_shooting_prize_programme(
                    publication_snapshot=load_active_policy_snapshot(settings_obj)
                )
            except Exception:
                current_programme = None
            if (
                current_programme is None
                or current_programme.programme_id != programme.programme_id
                or current_programme.version != programme.version
                or current_programme.cue_codes != programme.cue_codes
                or current_programme.manager_required is not True
            ):
                return PrizeCaseResult(reason="programme_changed")
            revision_source = (
                revision.sources.select_for_update()
                .filter(message_id=source_id)
                .first()
            )
        if expected_permission_epoch is not None:
            if isinstance(expected_permission_epoch, bool):
                return PrizeCaseResult(reason="permission_epoch_invalid")
            try:
                expected_epoch = int(expected_permission_epoch)
            except (TypeError, ValueError):
                return PrizeCaseResult(reason="permission_epoch_invalid")
            if expected_epoch != int(client.reply_permission_epoch or 0):
                return PrizeCaseResult(reason="permission_epoch_changed")
        if client.privacy_erasure_started_at is not None:
            return PrizeCaseResult(reason="client_erasure_active")
        if client.hidden_at is not None:
            return PrizeCaseResult(reason="client_hidden")
        if client.is_blocked:
            return PrizeCaseResult(reason="client_blocked")
        if client.bot_paused:
            return PrizeCaseResult(reason="client_paused")
        if client.manager_takeover:
            return PrizeCaseResult(reason="manager_takeover")
        if client.opted_out_at and (
            not client.opted_in_at or client.opted_out_at > client.opted_in_at
        ):
            return PrizeCaseResult(reason="client_opted_out")
        source = (
            InstagramBotMessage.objects.select_for_update()
            .filter(
                pk=source_id,
                client_id=client.pk,
                role=InstagramBotMessage.Role.USER,
            )
            .first()
        )
        if source is None:
            return PrizeCaseResult(reason="source_owner_mismatch")
        if revision_mode:
            if revision_source is None:
                return PrizeCaseResult(reason="source_not_in_revision")
            source_entry = []
            for item in proposal.get("sources") or ():
                if not isinstance(item, Mapping):
                    continue
                try:
                    proposal_source_id = int(item.get("message_id") or 0)
                except (TypeError, ValueError):
                    continue
                if proposal_source_id == source.pk:
                    source_entry.append(item)
            if (
                len(source_entry) != 1
                or str(source_entry[0].get("source_digest") or "")
                != revision_source.source_digest
            ):
                return PrizeCaseResult(reason="source_identity_changed")
            evidence, has_candidate_payload, evidence_reason = (
                _validated_revision_candidate_evidence(
                    revision, source, current_programme
                )
            )
            if evidence_reason:
                return PrizeCaseResult(reason=evidence_reason)
            effective_preference = _revision_preference(revision)
        else:
            candidate_artifact = (
                source.turn_intelligence_artifact
                if isinstance(source.turn_intelligence_artifact, dict)
                else {}
            )
            has_candidate_payload = any(
                isinstance(item, Mapping)
                and item.get("prize_certificate") is not None
                for item in candidate_artifact.get("image_observations") or []
            )
            evidence = []
            effective_preference = preference
        if source.sender_id != client.igsid:
            return PrizeCaseResult(reason="source_owner_mismatch")
        source_reviewable = bool(
            source.source == "webhook"
            and source.media_capture_eligible is True
            and source.private_media_state
            == InstagramBotMessage.PrivateMediaState.ACTIVE
        )
        if has_candidate_payload and not source_reviewable:
            return PrizeCaseResult(reason="source_not_reviewable")
        if not revision_mode:
            evidence = (
                _validated_candidate_evidence(source, programme)
                if source_reviewable
                else []
            )
        elif not source_reviewable:
            evidence = []
        preference_entry = _preference_entry(source, effective_preference)
        task = (
            IgFollowUpTask.objects.select_for_update()
            .filter(
                client=client,
                kind=IgFollowUpTask.Kind.MANAGER_TASK,
                reason=_case_reason(programme),
                manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
            )
            .exclude(status__in=[
                IgFollowUpTask.Status.COMPLETED,
                IgFollowUpTask.Status.CANCELLED,
            ])
            .order_by("id")
            .first()
        )
        if task is None and not evidence:
            return PrizeCaseResult(reason="candidate_not_validated")
        created = task is None
        if task is None:
            notification_key = (
                f"ig-prize-review:{client.pk}:{programme.programme_id}:{source.pk}"
            )[:255]
            task = IgFollowUpTask.objects.create(
                client=client,
                due_at=now,
                status=IgFollowUpTask.Status.SKIPPED,
                kind=IgFollowUpTask.Kind.MANAGER_TASK,
                reason=_case_reason(programme),
                manager_approval_status=(
                    IgFollowUpTask.ManagerApprovalStatus.PENDING
                ),
                manager_approval_requested_at=now,
                message_text=(
                    "Потребує перевірки: кандидат сертифіката стрілецького призу."
                ),
                manager_context=None,
                event_key=(
                    f"ig-prize-case:{client.pk}:{programme.programme_id}:{source.pk}"
                )[:180],
                trigger=IgFollowUpTask.Trigger.EVENT,
                event_occurred_at=(
                    source.provider_created_at or source.created_at or now
                ),
                event_payload={
                    "schema_version": CASE_SCHEMA_VERSION,
                    "case_kind": "prize_review",
                    "programme_id": programme.programme_id,
                    "programme_version": programme.version,
                    "initial_source_message_id": source.pk,
                    "manager_required": True,
                    "notification_dedupe_key": notification_key,
                },
                policy_started_at=now,
                policy_version=CASE_POLICY_VERSION,
                skip_reason="human_business_decision_required",
                last_error="",
            )
        document = _case_document(task)
        document.update({
            "programme_id": programme.programme_id,
            "programme_version": programme.version,
        })
        document["evidence"], evidence_added = _append_distinct(
            list(document.get("evidence") or []),
            evidence,
            identity=lambda item: (
                item.get("source_message_id"),
                item.get("source_part_id"),
                item.get("content_hash"),
                item.get("request_id"),
            ),
        )
        document["evidence"] = document["evidence"][-MAX_CASE_EVIDENCE:]
        preference_added = False
        if preference_entry is not None:
            preferences, count = _append_distinct(
                list(document.get("preferences") or []),
                [preference_entry],
                identity=lambda item: (
                    item.get("source_message_id"),
                    item.get("kind"),
                    item.get("product_id"),
                ),
            )
            document["preferences"] = preferences[-MAX_CASE_PREFERENCES:]
            preference_added = bool(count)
        task.manager_context = document
        task.message_text = _operator_summary(document)
        task.save(update_fields=["manager_context", "message_text", "updated_at"])

        notification_key = str(
            (task.event_payload or {}).get("notification_dedupe_key") or ""
        ) or f"ig-prize-review:task:{task.pk}"
        notification_payload = _notification_payload(task, document)
        notification, notification_created = IgBotNotification.objects.get_or_create(
            dedupe_key=notification_key,
            defaults={
                "client": client,
                "event_type": NOTIFICATION_EVENT_TYPE,
                "payload": notification_payload,
                "status": IgBotNotification.Status.PENDING,
            },
        )
        if not notification_created:
            notification_payload = _merge_notification_payload(
                notification.payload,
                notification_payload,
            )
        if not notification_created and notification.payload != notification_payload:
            notification.payload = notification_payload
            notification.save(update_fields=["payload", "updated_at"])
        return PrizeCaseResult(
            task_id=task.pk,
            notification_id=notification.pk,
            created=created,
            evidence_added=evidence_added,
            preference_added=preference_added,
            reason="created" if created else "updated",
        )


__all__ = [
    "CASE_POLICY_VERSION",
    "CASE_SCHEMA_VERSION",
    "PrizeCaseResult",
    "upsert_prize_review_case",
]
