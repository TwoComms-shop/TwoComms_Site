"""Deterministic, provenance-bound assessment of branded Instagram UGC.

Vision/model output is treated as evidence only.  The policy below owns the
decision and deliberately fails closed on missing provider provenance, missing
owned bytes, ambiguous apparel, ads, and duplicate provider objects.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone


POLICY_VERSION = "ugc-v1"
AUTO_BRAND_THRESHOLD = Decimal("0.97")
AUTO_GARMENT_THRESHOLD = Decimal("0.95")
AUTO_CUSTOMER_CONTENT_THRESHOLD = Decimal("0.95")
LIVE_PROVENANCE = "live_webhook"
OWNED_STATUS = "owned"
BRAND_TARGET_USERNAME = "twocomms"
PROVIDER_MEDIA_TYPES = frozenset({
    "story_mention",
    "story",
    "share",
    "ig_post",
    "ig_reel",
    "reel",
})
MAX_OWNED_MEDIA_BYTES = 6 * 1024 * 1024
UGC_MEDIA_RECONCILE_LEASE_SECONDS = 120
UGC_MEDIA_CAPTURE_MAX_ATTEMPTS = 2


class UgcProvenanceError(ValueError):
    """The durable source evidence cannot authorize a UGC reward."""


@dataclass(frozen=True)
class UgcProvenance:
    message: object
    media: dict
    provider_key: str
    provider_object_digest: str
    evidence_fingerprint: str
    perceptual_fingerprint: str
    content_hash: str


def _decimal(value, default=Decimal("0")) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if Decimal("0") <= parsed <= Decimal("1") else default


def _media_items(message) -> list[dict]:
    result = []
    for item in (getattr(message, "attachment_media", None) or []):
        if isinstance(item, dict):
            result.append(dict(item))
    return result[:8]


def _first_media(message, facts: dict) -> dict:
    items = _media_items(message)
    del facts  # Provider identity is never selected by model output.
    if not items:
        return {}

    def rank(item):
        return (
            int(item.get("provenance") == LIVE_PROVENANCE),
            int(item.get("provider_native_mention") is True),
            int(item.get("status") == OWNED_STATUS),
            int(bool(item.get("storage_name"))),
            int(str(item.get("media_type") or "").casefold() in PROVIDER_MEDIA_TYPES),
        )

    return max(items, key=rank)


def _provider_key(message, facts: dict, media: dict) -> str:
    del message, facts
    return str(media.get("provider_object_key") or "").strip()[:255]


def _provider_object_digest(provider_key: str) -> str:
    value = str(provider_key or "").strip()
    if not value:
        return ""
    return hashlib.sha256(
        f"instagram-ugc-provider-object-v1:{value}".encode("utf-8")
    ).hexdigest()


def _owned_media_bytes(
    media: dict,
    *,
    message_id: int | None = None,
) -> tuple[bytes, str] | None:
    storage_name = str(media.get("storage_name") or "").strip()
    mime = str(media.get("mime") or "").strip().casefold()
    if (
        media.get("provenance") != LIVE_PROVENANCE
        or media.get("status") != OWNED_STATUS
        or not storage_name
        or not mime.startswith("image/")
    ):
        return None
    try:
        lease = None
        if media.get("private_storage"):
            from management.services.ig_private_media import (
                acquire_blob_use,
                private_media_storage,
                release_blob_use,
            )

            lease = acquire_blob_use(int(message_id or 0), seconds=120)
            if not lease:
                return None
            storage = private_media_storage()
        else:
            from django.core.files.storage import default_storage

            storage = default_storage
        try:
            with storage.open(storage_name, "rb") as handle:
                raw = handle.read(MAX_OWNED_MEDIA_BYTES + 1)
        finally:
            if lease:
                release_blob_use(int(message_id or 0), lease)
    except Exception:
        return None
    if not raw or len(raw) > MAX_OWNED_MEDIA_BYTES:
        return None
    expected_size = media.get("bytes")
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != len(raw):
                return None
        except (TypeError, ValueError):
            return None
    expected_hash = str(media.get("content_hash") or "").strip().casefold()
    content_hash = hashlib.sha256(raw).hexdigest()
    if expected_hash and expected_hash != content_hash:
        return None
    if not expected_hash:
        return None
    return raw, content_hash


def _perceptual_fingerprint(raw: bytes) -> str:
    try:
        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            image = image.convert("L").resize((9, 8))
            pixels = list(image.getdata())
        bits = [pixels[index] > pixels[index + 1] for index in range(0, 72, 9) for index in range(index, index + 8)]
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:016x}"
    except Exception:
        return ""


def _evidence_fingerprint(message, provider_digest: str, content_hash: str) -> str:
    source = str(getattr(message, "mid", "") or "").strip()
    return hashlib.sha256(
        f"{provider_digest}\x1f{source}\x1f{content_hash}".encode("utf-8")
    ).hexdigest()


def _catalog_candidates(facts: dict) -> list[dict]:
    raw = facts.get("catalog_matches")
    if not isinstance(raw, list):
        raw = facts.get("catalog_candidates")
    result = []
    for fallback_index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        try:
            garment_index = int(item.get("garment_index", fallback_index))
            product_id = int(item.get("product_id"))
        except (TypeError, ValueError):
            continue
        confidence = _decimal(item.get("confidence"))
        if product_id <= 0 or garment_index < 0 or garment_index >= 8:
            continue
        result.append({
            "garment_index": garment_index,
            "product_id": product_id,
            "product_name": " ".join(
                str(item.get("product_name") or "").split()
            )[:160],
            "confidence": float(confidence),
        })
    return result[:8]


def _safe_reasons(values) -> list[str]:
    result = []
    for value in values or []:
        code = str(value or "").strip().lower().replace(" ", "_")
        if code and code not in result:
            result.append(code[:64])
    return result[:20]


def _inspect_provenance(message, media: dict) -> dict:
    source_id = str(getattr(message, "mid", "") or "").strip()
    origin_ok = bool(
        getattr(message, "role", "") == "user"
        and str(getattr(message, "source", "") or "") == "webhook"
        and getattr(message, "media_capture_eligible", False)
        and getattr(message, "client_id", None)
        and source_id
        and str(getattr(message, "sender_id", "") or "").strip()
    )
    provider_key = _provider_key(message, {}, media)
    provider_digest = _provider_object_digest(provider_key)
    owned = _owned_media_bytes(media, message_id=getattr(message, "pk", None))
    target = str(media.get("target_username") or "").strip().lstrip("@").casefold()
    native = bool(media.get("provider_native_mention"))
    media_type = str(media.get("media_type") or "").strip().casefold()
    provider_event_id = str(media.get("provider_event_id") or "").strip()
    provider_media_id = str(media.get("provider_media_id") or "").strip()
    content_hash = owned[1] if owned else ""
    return {
        "origin_ok": origin_ok,
        "provider_key": provider_key,
        "provider_object_digest": provider_digest,
        "owned": owned is not None,
        "content_hash": content_hash,
        "perceptual_fingerprint": _perceptual_fingerprint(owned[0]) if owned else "",
        "target": target,
        "provider_native": native,
        "media_type": media_type,
        "provider_event_id": provider_event_id,
        "provider_media_id": provider_media_id,
        "provider_identity_ok": bool(
            provider_event_id
            and provider_event_id == source_id
            and provider_media_id
        ),
    }


def _decision(
    *,
    facts: dict,
    provenance: dict,
    exact_duplicate: bool,
    near_duplicate: bool,
    already_rewarded: bool,
    auto_award_mode: str,
) -> tuple[str, list[str]]:
    if exact_duplicate:
        return "rejected", ["duplicate_provider_object"]
    if already_rewarded:
        return "rejected", ["already_rewarded"]

    reasons = _safe_reasons(facts.get("risk_flags"))
    if not provenance["origin_ok"]:
        return "rejected", _safe_reasons([*reasons, "untrusted_message_origin"])
    target = provenance["target"]
    provider_native = provenance["provider_native"]
    live_owned = provenance["owned"]
    media_type = provenance["media_type"]
    if not provenance["provider_key"]:
        return "needs_manager_review", _safe_reasons([*reasons, "provider_object_missing"])
    if not provenance["provider_identity_ok"]:
        return "needs_manager_review", _safe_reasons([*reasons, "provider_mention_incomplete"])
    if media_type not in PROVIDER_MEDIA_TYPES or not provider_native:
        return "needs_manager_review", _safe_reasons([*reasons, "provider_mention_incomplete"])
    if not live_owned:
        return "needs_manager_review", _safe_reasons([*reasons, "media_not_owned"])
    personal_worn = facts.get("personal_worn_apparel") is True
    if not personal_worn:
        return "rejected", _safe_reasons([*reasons, "ad_or_no_garment"])
    if target != BRAND_TARGET_USERNAME:
        reasons.append("brand_tag_missing_or_wrong")
    if reasons or target != BRAND_TARGET_USERNAME:
        return "rejected", _safe_reasons(reasons or ["policy_gate"])

    customer_created = facts.get("customer_created_content") is True
    customer_confidence = _decimal(facts.get("customer_content_confidence"))
    if not customer_created or customer_confidence < AUTO_CUSTOMER_CONTENT_THRESHOLD:
        return "needs_manager_review", ["customer_origin_unproven"]

    brand_confidence = _decimal(facts.get("brand_match_confidence"))
    candidates = _catalog_candidates(facts)
    if not candidates:
        return "needs_manager_review", ["catalog_match_missing"]
    try:
        people_count = max(0, min(8, int(facts.get("people_count"))))
        garment_count = max(0, min(8, int(facts.get("garment_count"))))
    except (TypeError, ValueError):
        people_count = 0
        garment_count = 0
    if people_count < 1 or garment_count < 1:
        return "needs_manager_review", ["model_fact_inconsistent"]
    mapped = {
        int(item["garment_index"]): item
        for item in candidates
        if 0 <= int(item["garment_index"]) < garment_count
    }
    required = [mapped.get(index) for index in range(garment_count)]
    if (
        any(item is None for item in required)
        or len({item["product_id"] for item in required if item is not None}) < garment_count
    ):
        return "needs_manager_review", ["catalog_coverage_insufficient"]
    if brand_confidence < AUTO_BRAND_THRESHOLD:
        return "needs_manager_review", ["brand_match_ambiguous"]
    if any(
        _decimal(item.get("confidence")) < AUTO_GARMENT_THRESHOLD
        for item in required
        if item is not None
    ):
        return "needs_manager_review", ["catalog_match_ambiguous"]
    if near_duplicate:
        return "needs_manager_review", ["perceptual_duplicate_review"]
    if auto_award_mode != "auto":
        reason = "auto_award_shadow" if auto_award_mode == "shadow" else "auto_award_disabled"
        return "needs_manager_review", [reason]
    return "qualified_auto", ["provider_mention", "owned_media", "worn_apparel", "catalog_match"]


@transaction.atomic
def assess_ugc_evidence(*, message, facts: dict | None = None, now=None):
    """Persist one bounded assessment and return it.

    ``message`` must be a provider-owned inbound message.  A caller may pass
    structured vision facts, but they never grant ownership or bypass the
    provenance gates derived from ``attachment_media``.
    """
    from management.ig_bot_models import IgUgcEvidenceAssessment

    facts = dict(facts or {})
    now = now or timezone.now()
    media = _first_media(message, facts)
    provenance = _inspect_provenance(message, media)
    provider_key = provenance["provider_key"]
    provider_digest = provenance["provider_object_digest"]
    source_id = str(getattr(message, "mid", "") or f"local:{getattr(message, 'pk', '')}")[:255]
    fingerprint = _evidence_fingerprint(
        message,
        provider_digest or hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
        provenance["content_hash"] or "unowned",
    )
    perceptual = provenance["perceptual_fingerprint"]
    existing_source = (
        IgUgcEvidenceAssessment.objects.select_for_update()
        .filter(client_id=message.client_id, source_message_id=source_id)
        .first()
    )
    if (
        existing_source is not None
        and existing_source.decision != IgUgcEvidenceAssessment.Decision.PENDING
    ):
        return existing_source
    existing_provider = (
        IgUgcEvidenceAssessment.objects.select_for_update()
        .filter(provider_object_digest=provider_digest)
        .exclude(pk=getattr(existing_source, "pk", None))
        .order_by("-id")
        .first()
        if provider_digest
        else None
    )
    existing_perceptual = (
        IgUgcEvidenceAssessment.objects.filter(
            perceptual_fingerprint=perceptual,
        ).exclude(provider_object_key=provider_key).exists()
        if perceptual
        else False
    )
    from management.services.ig_ugc_rewards import ugc_identity_already_rewarded

    auto_award_mode = str(
        getattr(settings, "IG_UGC_AUTO_AWARD_MODE", "shadow") or "shadow"
    ).strip().casefold()
    if auto_award_mode not in {"auto", "shadow", "disabled"}:
        auto_award_mode = "shadow"
    decision, reasons = _decision(
        facts=facts,
        provenance=provenance,
        exact_duplicate=existing_provider is not None,
        near_duplicate=existing_perceptual,
        already_rewarded=ugc_identity_already_rewarded(message.client),
        auto_award_mode=auto_award_mode,
    )
    from management.services.ig_ugc_rewards import ugc_service_case_reason

    service_reason = ugc_service_case_reason(message.client)
    if service_reason and decision != IgUgcEvidenceAssessment.Decision.REJECTED:
        decision = IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW
        reasons = _safe_reasons([*reasons, service_reason])
    candidates = _catalog_candidates(facts)
    confidence = max(
        [_decimal(facts.get("brand_match_confidence"))]
        + [_decimal(item.get("confidence")) for item in candidates]
    )
    values = {
        "provider_media_id": str(media.get("provider_media_id") or "")[:255],
        "provider_event_id": str(media.get("provider_event_id") or "")[:255],
        "target_username": provenance["target"][:80],
        "provider_object_digest": None if existing_provider is not None else (provider_digest or None),
        "evidence_fingerprint": fingerprint,
        "perceptual_fingerprint": perceptual,
        "decision": decision,
        "decision_source": "auto" if decision == "qualified_auto" else "policy",
        "policy_version": POLICY_VERSION,
        "reason_codes": reasons,
        "catalog_candidates": candidates,
        "confidence": confidence,
        "people_count": max(0, int(facts.get("people_count") or 0)),
        "garment_count": max(0, int(facts.get("garment_count") or 0)),
        "reward_owner_client_id": message.client_id,
        "updated_at": now,
    }
    if (
        existing_source is not None
        and existing_source.decision == IgUgcEvidenceAssessment.Decision.PENDING
    ):
        for field, value in values.items():
            setattr(existing_source, field, value)
        existing_source.save(update_fields=[*values.keys()])
        assessment = existing_source
    else:
        try:
            with transaction.atomic():
                assessment = IgUgcEvidenceAssessment.objects.create(
                    client=message.client,
                    source_message_id=source_id,
                    provider_object_key=provider_key,
                    **values,
                    generation=1,
                )
        except IntegrityError:
            assessment = IgUgcEvidenceAssessment.objects.filter(
                client_id=message.client_id,
                source_message_id=source_id,
            ).first()
            if assessment is None and provider_digest:
                # A concurrent worker won the nullable-unique provider slot.
                # Keep this source visible for audit, but never let it retain
                # the digest or qualify for a reward.
                values.update({
                    "provider_object_digest": None,
                    "decision": IgUgcEvidenceAssessment.Decision.REJECTED,
                    "decision_source": "policy",
                    "reason_codes": ["duplicate_provider_object"],
                })
                try:
                    with transaction.atomic():
                        assessment = IgUgcEvidenceAssessment.objects.create(
                            client=message.client,
                            source_message_id=source_id,
                            provider_object_key=provider_key,
                            **values,
                            generation=1,
                        )
                except IntegrityError:
                    assessment = IgUgcEvidenceAssessment.objects.get(
                        client_id=message.client_id,
                        source_message_id=source_id,
                    )
    return assessment


def validate_ugc_provenance(*, assessment, client, lock=False) -> UgcProvenance:
    """Re-read the original webhook evidence immediately before reward issuance."""
    from management.models import InstagramBotMessage

    if str(getattr(assessment, "policy_version", "")) != POLICY_VERSION:
        raise UgcProvenanceError("UGC policy version is stale.")
    source_id = str(getattr(assessment, "source_message_id", "") or "").strip()
    if not source_id or not getattr(client, "pk", None):
        raise UgcProvenanceError("UGC source message is missing.")
    queryset = InstagramBotMessage.objects
    if lock:
        queryset = queryset.select_for_update()
    message = queryset.filter(
        client_id=client.pk,
        mid=source_id,
    ).first()
    if message is None:
        raise UgcProvenanceError("Original UGC webhook message is missing.")
    if message.role != InstagramBotMessage.Role.USER or message.source != "webhook":
        raise UgcProvenanceError("UGC source is not an inbound webhook message.")
    if message.sender_id != getattr(client, "igsid", ""):
        raise UgcProvenanceError("UGC source identity does not match the client.")
    media = _first_media(message, {})
    provenance = _inspect_provenance(message, media)
    if not provenance["origin_ok"]:
        raise UgcProvenanceError("UGC source webhook is not capture-authoritative.")
    if (
        not provenance["provider_key"]
        or not provenance["provider_object_digest"]
        or not provenance["provider_media_id"]
    ):
        raise UgcProvenanceError("UGC provider object is missing.")
    if provenance["media_type"] not in PROVIDER_MEDIA_TYPES or not provenance["provider_native"]:
        raise UgcProvenanceError("UGC provider mention metadata is incomplete.")
    if not provenance["owned"]:
        raise UgcProvenanceError("UGC owned media is missing or changed.")
    if provenance["target"] != BRAND_TARGET_USERNAME:
        raise UgcProvenanceError("UGC target is not the configured brand account.")
    if provenance["provider_event_id"] != source_id:
        raise UgcProvenanceError("UGC provider event identity does not match the source.")
    if str(getattr(assessment, "provider_object_key", "") or "") != provenance["provider_key"]:
        raise UgcProvenanceError("UGC provider object identity changed.")
    if str(getattr(assessment, "provider_object_digest", "") or "") != provenance["provider_object_digest"]:
        raise UgcProvenanceError("UGC provider object digest is missing or changed.")
    expected_fingerprint = _evidence_fingerprint(
        message,
        provenance["provider_object_digest"],
        provenance["content_hash"],
    )
    if str(getattr(assessment, "evidence_fingerprint", "") or "") != expected_fingerprint:
        raise UgcProvenanceError("UGC evidence fingerprint is missing or changed.")
    if getattr(assessment, "reward_owner_client_id", None) not in (None, client.pk):
        raise UgcProvenanceError("UGC reward owner does not match the posting identity.")
    return UgcProvenance(
        message=message,
        media=media,
        provider_key=provenance["provider_key"],
        provider_object_digest=provenance["provider_object_digest"],
        evidence_fingerprint=expected_fingerprint,
        perceptual_fingerprint=provenance["perceptual_fingerprint"],
        content_hash=provenance["content_hash"],
    )


def commerce_suppressed_for_ugc(assessment) -> bool:
    """A recognized or pending UGC turn cannot be turned into a sales pitch."""
    return str(getattr(assessment, "decision", "") or "") in {
        "pending",
        "qualified_auto",
        "needs_manager_review",
        "manager_approved",
    }


def potential_ugc_message(message) -> bool:
    """Cheap ingress gate used before classifier/commerce reduction."""
    if getattr(message, "role", "") != "user":
        return False
    if str(getattr(message, "source", "") or "") != "webhook":
        return False
    if not str(getattr(message, "mid", "") or "").strip():
        return False
    if getattr(message, "media_capture_eligible", False) is not True:
        return False
    for item in _media_items(message):
        if (
            item.get("provenance") != LIVE_PROVENANCE
            or item.get("provider_native_mention") is not True
            or str(item.get("target_username") or "").strip().lstrip("@").casefold()
            != BRAND_TARGET_USERNAME
        ):
            continue

        media_type = str(item.get("media_type") or "").casefold()
        status = str(item.get("status") or "").casefold()
        if (
            media_type in PROVIDER_MEDIA_TYPES
            and status == OWNED_STATUS
            and bool(str(item.get("storage_name") or "").strip())
        ):
            return True

        # A provider-native story mention remains a non-commercial turn when
        # its download is still pending or unavailable.  It can receive only
        # the neutral receipt path below; qualification still requires owned
        # bytes in ``_decision`` and therefore cannot issue a reward here.
        if media_type in PROVIDER_MEDIA_TYPES and status in {"pending", "unavailable"}:
            return True
    return False


@transaction.atomic
def ensure_pending_ugc_assessment(message):
    """Create one pending assessment at ingress before any commerce reducer."""
    from management.ig_bot_models import IgUgcEvidenceAssessment

    source_id = str(getattr(message, "mid", "") or f"local:{getattr(message, 'pk', '')}")[:255]
    existing = (
        IgUgcEvidenceAssessment.objects.filter(
            client_id=message.client_id,
            source_message_id=source_id,
        ).order_by("-id").first()
    )
    if existing is not None:
        return existing
    media = _first_media(message, {})
    provenance = _inspect_provenance(message, media)
    provider_key = provenance["provider_key"]
    provider_digest = provenance["provider_object_digest"]
    duplicate = bool(
        provider_digest
        and IgUgcEvidenceAssessment.objects.filter(
            provider_object_digest=provider_digest,
        ).exists()
    )
    pending_decision = (
        IgUgcEvidenceAssessment.Decision.REJECTED
        if duplicate or not provenance["origin_ok"]
        else IgUgcEvidenceAssessment.Decision.PENDING
    )
    pending_reasons = (
        ["duplicate_provider_object"]
        if duplicate
        else (["untrusted_message_origin"] if not provenance["origin_ok"] else ["vision_pending"])
    )
    try:
        with transaction.atomic():
            return IgUgcEvidenceAssessment.objects.create(
                client_id=message.client_id,
                source_message_id=source_id,
                provider_object_key=provider_key,
                provider_object_digest=None if duplicate else (provider_digest or None),
                provider_media_id=str(media.get("provider_media_id") or "")[:255],
                provider_event_id=str(media.get("provider_event_id") or "")[:255],
                target_username=provenance["target"][:80],
                evidence_fingerprint=_evidence_fingerprint(
                    message,
                    provider_digest or hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
                    provenance["content_hash"] or "unowned",
                ),
                perceptual_fingerprint=provenance["perceptual_fingerprint"],
                decision=pending_decision,
                decision_source="ingress",
                policy_version=POLICY_VERSION,
                reason_codes=pending_reasons,
                reward_owner_client_id=message.client_id,
            )
    except IntegrityError:
        source_winner = (
            IgUgcEvidenceAssessment.objects.select_for_update()
            .filter(
                client_id=message.client_id,
                source_message_id=source_id,
            )
            .first()
        )
        if source_winner is not None:
            return source_winner
        digest_winner = (
            IgUgcEvidenceAssessment.objects.select_for_update()
            .filter(provider_object_digest=provider_digest)
            .first()
            if provider_digest
            else None
        )
        if digest_winner is None:
            raise
        try:
            with transaction.atomic():
                return IgUgcEvidenceAssessment.objects.create(
                    client_id=message.client_id,
                    source_message_id=source_id,
                    provider_object_key=provider_key,
                    provider_object_digest=None,
                    provider_media_id=str(media.get("provider_media_id") or "")[:255],
                    provider_event_id=str(media.get("provider_event_id") or "")[:255],
                    target_username=provenance["target"][:80],
                    evidence_fingerprint=_evidence_fingerprint(
                        message,
                        provider_digest,
                        provenance["content_hash"] or "unowned",
                    ),
                    perceptual_fingerprint=provenance["perceptual_fingerprint"],
                    decision=IgUgcEvidenceAssessment.Decision.REJECTED,
                    decision_source="ingress",
                    policy_version=POLICY_VERSION,
                    reason_codes=["duplicate_provider_object"],
                    reward_owner_client_id=message.client_id,
                )
        except IntegrityError:
            return IgUgcEvidenceAssessment.objects.select_for_update().get(
                client_id=message.client_id,
                source_message_id=source_id,
            )


def _parse_media_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _ugc_media_retry_due(item: dict, *, now) -> bool:
    """Return whether one provider-native item may be retried now.

    The item lives in ``InstagramBotMessage.attachment_media`` so this queue
    remains durable without a schema migration.  Provider identity is checked
    again here; a pending assessment can never turn an arbitrary URL into a
    network fetch candidate.
    """
    if not isinstance(item, dict):
        return False
    if (
        item.get("provenance") != LIVE_PROVENANCE
        or item.get("provider_native_mention") is not True
        or str(item.get("target_username") or "").strip().lstrip("@").casefold()
        != BRAND_TARGET_USERNAME
        or str(item.get("media_type") or "").strip().casefold()
        not in PROVIDER_MEDIA_TYPES
    ):
        return False
    status = str(item.get("status") or "").strip().casefold()
    try:
        attempts = max(0, int(item.get("capture_attempts") or 0))
    except (TypeError, ValueError):
        attempts = 0
    if status == "owned" and item.get("storage_name"):
        return False
    if item.get("capture_terminal") is True:
        return False
    if "capture_retryable" in item:
        if item.get("capture_retryable") is not True:
            return False
        from management.services.ig_media_recovery import retry_due

        return retry_due(item, now=now)
    if attempts >= UGC_MEDIA_CAPTURE_MAX_ATTEMPTS:
        return False
    if status in {"acquiring", "storing"}:
        started = _parse_media_datetime(item.get("capture_started_at"))
        return bool(
            started is None
            or started <= now - timedelta(seconds=60)
        )
    if status not in {"pending", "unavailable"}:
        return False
    retry_at = _parse_media_datetime(item.get("capture_next_attempt_at"))
    return retry_at is None or retry_at <= now


def _ugc_media_capture_exhausted(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if (
        item.get("provenance") != LIVE_PROVENANCE
        or item.get("provider_native_mention") is not True
        or str(item.get("target_username") or "").strip().lstrip("@").casefold()
        != BRAND_TARGET_USERNAME
        or str(item.get("media_type") or "").strip().casefold()
        not in PROVIDER_MEDIA_TYPES
    ):
        return False
    status = str(item.get("status") or "").strip().casefold()
    if (
        item.get("capture_terminal") is True
        and item.get("resolution_required") is True
        and status in {"unavailable", "expired", "blocked"}
    ):
        return True
    if status != "unavailable":
        return False
    try:
        attempts = max(0, int(item.get("capture_attempts") or 0))
    except (TypeError, ValueError):
        return False
    return attempts >= UGC_MEDIA_CAPTURE_MAX_ATTEMPTS


def _claim_pending_ugc_assessment(assessment_id: int, *, now):
    """Claim one pending assessment before any CDN or Gemini work."""
    from management.ig_bot_models import IgUgcEvidenceAssessment

    with transaction.atomic():
        assessment = (
            IgUgcEvidenceAssessment.objects.select_for_update()
            .filter(pk=assessment_id)
            .first()
        )
        if assessment is None or assessment.decision != IgUgcEvidenceAssessment.Decision.PENDING:
            return None
        if (
            assessment.lease_token
            and assessment.lease_expires_at
            and assessment.lease_expires_at > now
        ):
            return None
        token = secrets.token_hex(16)
        assessment.lease_token = token
        assessment.lease_expires_at = now + timedelta(seconds=UGC_MEDIA_RECONCILE_LEASE_SECONDS)
        assessment.save(update_fields=["lease_token", "lease_expires_at", "updated_at"])
        return assessment, token


def _release_pending_ugc_assessment(assessment_id: int, token: str) -> None:
    from management.ig_bot_models import IgUgcEvidenceAssessment

    IgUgcEvidenceAssessment.objects.filter(
        pk=assessment_id,
        lease_token=token,
    ).update(lease_token="", lease_expires_at=None, updated_at=timezone.now())


def queue_ugc_manager_review(assessment) -> bool:
    """Queue one authenticated 5/10/reject Telegram decision card."""
    from management.services.instagram_bot import notify_manager

    client = getattr(assessment, "client", None)
    generation = int(getattr(assessment, "generation", 0) or 0)
    assessment_id = int(getattr(assessment, "pk", 0) or 0)
    if not client or not assessment_id:
        return False
    from management.models import InstagramBotMessage

    source = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        mid=str(getattr(assessment, "source_message_id", "") or ""),
        role=InstagramBotMessage.Role.USER,
    ).first()
    owned_evidence = []
    for item in getattr(source, "attachment_media", None) or []:
        if not isinstance(item, dict) or item.get("status") != OWNED_STATUS:
            continue
        if item.get("private_storage") and item.get("storage_name"):
            owned_evidence.append({
                "role": "ugc_evidence",
                "private_storage_name": str(item.get("storage_name") or "")[:1200],
                "mime": str(item.get("mime") or "")[:64],
                "content_hash": str(item.get("content_hash") or "")[:64],
                "message_id": str(getattr(source, "pk", "") or ""),
                "source_part_id": str(item.get("source_part_id") or "")[:40],
            })
    product_context = [
        {
            "product_id": int(item.get("product_id") or 0),
            "product_name": str(item.get("product_name") or "")[:160],
            "confidence": float(item.get("confidence") or 0),
        }
        for item in (getattr(assessment, "catalog_candidates", None) or [])[:8]
        if isinstance(item, dict) and item.get("product_id")
    ]
    return notify_manager(
        "\n".join((
            "📸 IG UGC — потрібне рішення менеджера",
            f"Клієнт #{client.pk}; assessment #{assessment_id}.",
            "Provider provenance і візуальні факти перевірено. Оберіть 5%, 10% або відмову; перед видачею eligibility перевіряється повторно.",
        )),
        dedupe_key=f"ugc_review:{assessment_id}:{generation}",
        event_type="ugc_reward_review",
        client=client,
        reply_markup={
            "inline_keyboard": [[
                {
                    "text": "5%",
                    "callback_data": f"igugc:5:{assessment_id}:{generation}",
                },
                {
                    "text": "10%",
                    "callback_data": f"igugc:10:{assessment_id}:{generation}",
                },
                {
                    "text": "Відхилити",
                    "callback_data": f"igugc:reject:{assessment_id}:{generation}",
                },
            ]],
        },
        metadata={
            "assessment_id": assessment_id,
            "assessment_generation": generation,
            "requires_human_review": True,
            "catalog_candidates": product_context,
        },
        media=owned_evidence,
        deliver_immediately=False,
    )


def pending_ugc_review_notifications():
    """The exact indexed anti-join, with explicit MariaDB expression collation."""
    from django.db import connections
    from django.db.models import CharField, Exists, OuterRef, Value
    from django.db.models.functions import Cast, Collate, Concat
    from management.models import IgBotNotification, IgUgcEvidenceAssessment

    rows = IgUgcEvidenceAssessment.objects.filter(
        decision=IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
    )
    key = Concat(
        Value("ugc_review:"), Cast("pk", output_field=CharField()),
        Value(":"), Cast("generation", output_field=CharField()),
        output_field=CharField(),
    )
    if connections[rows.db].vendor == "mysql":
        key = Collate(key, "utf8mb4_unicode_ci")
    return rows.annotate(_expected_notification_key=key).annotate(
        _notification_exists=Exists(IgBotNotification.objects.filter(
            dedupe_key=OuterRef("_expected_notification_key"),
        )),
    ).filter(_notification_exists=False).order_by("updated_at", "id")


def reconcile_pending_ugc_media(*, limit: int = 20, now=None) -> dict[str, int]:
    """Retry event-scoped UGC capture and resume its existing vision path.

    Selection starts from durable, ingress-created pending assessments rather
    than scanning provider media or synthesizing a customer turn.  Per-item
    capture tokens and the assessment lease make retries safe across workers;
    terminal manager decisions and issued rewards are never revisited.
    """
    from management.ig_bot_models import IgUgcEvidenceAssessment
    from management.models import InstagramBotMessage

    now = now or timezone.now()
    bounded = max(0, min(50, int(limit or 0)))
    counts = {
        "selected": 0,
        "retried": 0,
        "owned": 0,
        "assessed": 0,
        "awarded": 0,
        "review_queued": 0,
        "waiting": 0,
        "terminalized": 0,
        "skipped": 0,
        "failed": 0,
        "collation_deferred": 0,
    }
    if bounded == 0:
        return counts

    # A transition to NEEDS_MANAGER_REVIEW and notification creation are
    # separate durable boundaries. If the outbox insert failed, the assessment
    # must remain selectable until its unique dedupe row exists.
    from django.core.cache import cache
    from django.db import DatabaseError

    # A SQL-shape fault is not a connection outage: isolate this selector and
    # leave unrelated follow/payment work running. Never reconnect-and-retry it.
    cooldown_key = "ig:ugc_review_selector:collation:v1"
    if cache.get(cooldown_key):
        counts["collation_deferred"] = 1
        return counts
    try:
        review_rows = list(pending_ugc_review_notifications()[:bounded])
    except DatabaseError as exc:
        if not exc.args or exc.args[0] != 1267:
            raise
        if cache.add(cooldown_key, True, timeout=900):
            import logging

            logging.getLogger(__name__).error(
                "UGC review selector SQL 1267; lane deferred for 900 seconds",
            )
        counts["collation_deferred"] = 1
        return counts
    for review in review_rows:
        if counts["selected"] >= bounded:
            break
        counts["selected"] += 1
        if queue_ugc_manager_review(review):
            counts["review_queued"] += 1

    candidates = list(
        IgUgcEvidenceAssessment.objects.filter(
            decision=IgUgcEvidenceAssessment.Decision.PENDING,
        )
        .order_by("updated_at", "id")[: bounded * 4]
    )
    for candidate in candidates:
        if counts["selected"] >= bounded:
            break
        message = (
            InstagramBotMessage.objects.filter(
                client_id=candidate.client_id,
                mid=candidate.source_message_id,
                role=InstagramBotMessage.Role.USER,
                source="webhook",
                media_capture_eligible=True,
            )
            .first()
        )
        if message is None:
            counts["skipped"] += 1
            continue
        media = [item for item in (message.attachment_media or []) if isinstance(item, dict)]
        if not any(_ugc_media_retry_due(item, now=now) for item in media):
            if any(_ugc_media_capture_exhausted(item) for item in media):
                claimed = _claim_pending_ugc_assessment(candidate.pk, now=now)
                if claimed is not None:
                    _locked_assessment, lease_token = claimed
                    counts["selected"] += 1
                    try:
                        updated = IgUgcEvidenceAssessment.objects.filter(
                            pk=candidate.pk,
                            decision=IgUgcEvidenceAssessment.Decision.PENDING,
                        ).update(
                            decision=IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
                            decision_source="policy",
                            reason_codes=["media_capture_exhausted"],
                            lease_token="",
                            lease_expires_at=None,
                            generation=(candidate.generation or 0) + 1,
                            updated_at=now,
                        )
                        if updated:
                            counts["terminalized"] += 1
                            candidate.refresh_from_db()
                            if queue_ugc_manager_review(candidate):
                                counts["review_queued"] += 1
                    finally:
                        _release_pending_ugc_assessment(candidate.pk, lease_token)
                    continue
            counts["waiting"] += 1
            continue
        claimed = _claim_pending_ugc_assessment(candidate.pk, now=now)
        if claimed is None:
            counts["skipped"] += 1
            continue
        _locked_assessment, lease_token = claimed
        counts["selected"] += 1
        try:
            from management.services.instagram_bot import (
                _capture_message_media,
                _collect_media_images,
            )

            counts["retried"] += 1
            _capture_message_media(message, limit=8)
            message.refresh_from_db(fields=["attachment_media"])
            owned_items = [
                item for item in (message.attachment_media or [])
                if isinstance(item, dict)
                and item.get("provenance") == LIVE_PROVENANCE
                and item.get("provider_native_mention") is True
                and item.get("status") == "owned"
                and item.get("storage_name")
            ]
            if not owned_items:
                counts["waiting"] += 1
                continue
            counts["owned"] += 1
            current_assessment = IgUgcEvidenceAssessment.objects.filter(
                pk=candidate.pk,
                decision=IgUgcEvidenceAssessment.Decision.PENDING,
            ).first()
            if current_assessment is None:
                counts["skipped"] += 1
                continue
            from management.services import bot_vision
            from management.services.ig_private_media import (
                acquire_blob_use,
                release_blob_use,
            )

            blob_lease = acquire_blob_use(message.pk, seconds=180)
            if not blob_lease:
                counts["waiting"] += 1
                continue
            try:
                images = _collect_media_images(
                    owned_items,
                    message_id=message.pk,
                    lease_already_held=True,
                )
                if not images:
                    counts["waiting"] += 1
                    continue
                facts = bot_vision.assess_ugc(
                    images,
                    candidates=bot_vision.build_match_candidates(),
                )
            finally:
                release_blob_use(message.pk, blob_lease)
            if not isinstance(facts, dict) or not facts:
                # A provider/model outage is retryable; do not turn absent
                # facts into a terminal rejection.
                counts["waiting"] += 1
                continue
            first_owned = _first_media(message, {})
            facts.update({
                "provider_native_mention": bool(first_owned.get("provider_native_mention")),
                "target_username": first_owned.get("target_username", ""),
                "owned_media": first_owned.get("status") == "owned",
            })
            assessment = assess_ugc_evidence(
                message=message,
                facts=facts,
                now=now,
            )
            counts["assessed"] += 1
            if assessment.decision == IgUgcEvidenceAssessment.Decision.QUALIFIED_AUTO:
                updated = IgUgcEvidenceAssessment.objects.filter(
                    pk=assessment.pk,
                    decision=IgUgcEvidenceAssessment.Decision.QUALIFIED_AUTO,
                    generation=assessment.generation,
                ).update(
                    decision=IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
                    decision_source="policy",
                    reason_codes=[
                        *(assessment.reason_codes or []),
                        "manager_discount_decision_required",
                    ],
                    generation=assessment.generation + 1,
                    updated_at=now,
                )
                if updated:
                    assessment.refresh_from_db()
            if (
                assessment.decision
                == IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW
                and queue_ugc_manager_review(assessment)
            ):
                counts["review_queued"] += 1
        except Exception:
            # Leave pending evidence untouched.  The next bounded pass can
            # reclaim the lease and retry capture/vision without duplicating a
            # message or reward.
            counts["failed"] += 1
        finally:
            _release_pending_ugc_assessment(candidate.pk, lease_token)
    return counts


def safe_ugc_acknowledgement(client, generated: str, *, assessment=None, media_analysis=None,
    social_only=True) -> str:
    """Compatible UGC adapter; assessment decisions never prove media content.

    Mixed caption/commerce callers explicitly opt out of the social-only lane.
    Action receipts and reward decisions remain with their existing owners.
    """
    from management.services.ig_media_response import normalize_media_reply
    return normalize_media_reply(client, generated, media_analysis=media_analysis,
        social_only=social_only)
