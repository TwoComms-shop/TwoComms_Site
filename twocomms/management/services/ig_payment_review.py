"""Evidence-bound manual payment review for Instagram conversations."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone


logger = logging.getLogger(__name__)


_PAYMENT_EVIDENCE_RE = re.compile(
    r"(?:\bоплат\w*\b|\bпередоплат\w*\b|\bплатіж\w*\b|\bплатеж\w*\b|"
    r"\bоплач\w*\b|\bсплач\w*\b|\bпереказ\w*\b|\bперевод\w*\b|\bоплатила\b|"
    r"\bоплатив\b|\bчек(?:а|у|ом)?\b|\bквитанц\w*\b|\breceipt\b|\bpaid\b)",
    re.IGNORECASE,
)
_NON_EVIDENCE_RE = re.compile(
    r"(?:посилання|ссылка|лінк|линк|як оплатити|как оплатить|оплата доступна|payment link)",
    re.IGNORECASE,
)
_AFFIRMATION_RE = re.compile(
    r"(?:я\s+(?:вже\s+)?оплат\w*|(?:оплат\w*|сплач\w*|переказ\w*|перевод\w*)\s+"
    r"(?:вже\s+)?(?:зроб\w*|викон\w*|готов\w*)|\bчек(?:а|у|ом)?\b|"
    r"\bквитанц\w*\b|receipt|paid)",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)\s*(?:грн|uah|₴)", re.IGNORECASE)
_FIT_RE = re.compile(
    r"(?P<fit>базов\w*|класич\w*|classic|basic|оверсайз\w*|oversize)"
    r"(?:\s+(?:розмір|size))?\s*(?P<size>2xl|xxl|xl|l|m|s|xs|2xs)\b",
    re.IGNORECASE,
)
_QTY_RE = re.compile(r"\b(\d+)\s+(?:футбол\w*|шт\.?|штук\w*)\b", re.IGNORECASE)
_CUSTOMER_ROLES = {"user", "customer", "client"}
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?380|0)\d{9}(?!\d)")
_OFFICE_RE = re.compile(r"(?P<kind>поштомат|відділен\w*|відд\.?|office)\s*№?\s*(?P<number>\d{1,8})", re.IGNORECASE)
_NAME_STOPWORDS = {
    "в різні",
    "по повній передоплаті",
    "по повній оплаті",
    "потрібна оплата",
}

# f34d54c8 stopped historical inbox recovery from creating operational payment
# reviews on 2026-07-31 01:14:12 EEST. Receipt-only repair is deliberately
# limited to the rows produced before that boundary.
LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF = datetime(
    2026, 7, 30, 22, 14, 12, tzinfo=datetime_timezone.utc,
)


def is_legacy_historical_payment_review(review) -> bool:
    created_at = getattr(review, "created_at", None)
    if not isinstance(created_at, datetime):
        return False
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at, timezone.get_default_timezone())
    return created_at < LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF


_MEDIA_PRODUCT_RE = re.compile(
    r"\b(товар\w*|футболк\w*|худі|худи|одяг\w*|одежд\w*|модель\w*|"
    r"розмір\w*|размер\w*|колір\w*|цвет\w*|ціна\w*|цена\w*|"
    r"наявн\w*|налич\w*|хочу|беру|давайте|замовл\w*|заказ\w*|куп\w*)\b",
    re.IGNORECASE,
)
_MEDIA_PURCHASE_RE = re.compile(
    r"\b(хочу|беру|забираю|оформл\w*|замовл\w*|заказ\w*|купл\w*|куп\w*|"
    r"давайте|підтверджую|подтверждаю)\b",
    re.IGNORECASE,
)
_MEDIA_CUSTOM_RE = re.compile(
    r"\b(кастом\w*|custom|dtf|дтф|принт\w*|друк\w*|печать\w*)\b",
    re.IGNORECASE,
)
_MEDIA_REFERENCE_RE = re.compile(
    r"(?:\b(?:ось|вот)\s+(?:цей|ця|це|цей|этот|эта|это|таку|такой|такий)\b|"
    r"\b(?:цей|ця|це|этот|эта|это|таку|такой|такий)\s+(?:принт|фото|варіант|вариант)\b|"
    r"\bяк\s+на\s+(?:фото|зображенн\w*)\b)",
    re.IGNORECASE,
)
_CUSTOMER_PAYMENT_COMMITMENT_RE = re.compile(
    r"\b(?:по\s+)?повн\w*\s+(?:перед)?оплат\w*\b|"
    r"\b(?:обираю|вибираю|выбираю)\s+(?:повн\w*\s+)?(?:перед)?оплат\w*\b",
    re.IGNORECASE,
)
_PRICE_ACCEPTANCE_RE = re.compile(
    r"\b(так|да|ок|добре|хорошо|домов\w*|погодж\w*|соглас\w*|оформл\w*|"
    r"замовл\w*|заказ\w*|беру|забираю)\b",
    re.IGNORECASE,
)


def _amount_evidence_kind(text: str) -> str:
    low = " ".join(str(text or "").split()).casefold()
    if re.search(
        r"\b(передоплат\w*|аванс\w*|оплатив\w*|оплатила\w*|оплачено\w*|"
        r"сплатив\w*|сплатила\w*|сплачено\w*|чек\w*|квитанц\w*|receipt|paid)\b",
        low,
    ) or re.search(
        r"\b(переказ\w*|перевод\w*)\b.{0,24}\b(зроб\w*|викон\w*|готов\w*)\b",
        low,
    ):
        return "payment_evidence"
    if re.search(r"\b(сума|сумма|разом|итого|всього|всего|total)\b", low):
        return "order_total"
    if re.search(r"\b(ціна|цена|вартість|стоимость)\b", low) or re.search(
        r"\bза\s+\d{2,6}(?:[.,]\d{1,2})?\s*(?:грн|uah|₴)", low
    ):
        return "unit_price"
    return "unknown"


def _amount_match_evidence_kind(text: str, amount_match) -> str:
    """Classify one amount by its closest semantic cue in the message.

    A manager can write both the full order total and a prepayment in one
    message. Classifying the whole message would assign the same meaning to
    both values, so keep each amount bound to the nearest explicit label.
    """
    previous_amounts = [match for match in _AMOUNT_RE.finditer(text) if match.end() <= amount_match.start()]
    local_start = previous_amounts[-1].end() if previous_amounts else 0
    local_kind = _amount_evidence_kind(text[local_start:amount_match.end()])
    if local_kind != "unknown":
        return local_kind

    cue_patterns = (
        (
            "payment_evidence",
            re.compile(
                r"\b(передоплат\w*|аванс\w*|оплатив\w*|оплатила\w*|оплачено\w*|"
                r"сплатив\w*|сплатила\w*|сплачено\w*|чек\w*|квитанц\w*|receipt|paid|"
                r"переказ\w*|перевод\w*)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "order_total",
            re.compile(
                r"\b(сума(?:\s+замовлення)?|сумма(?:\s+заказа)?|разом|итого|всього|всего|total)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "unit_price",
            re.compile(r"\b(ціна|цена|вартість|стоимость)\b", re.IGNORECASE),
        ),
    )
    candidates = []
    for kind, pattern in cue_patterns:
        for cue in pattern.finditer(text):
            if cue.end() <= amount_match.start():
                distance = amount_match.start() - cue.end()
                direction_penalty = 0
            elif cue.start() >= amount_match.end():
                distance = cue.start() - amount_match.end()
                direction_penalty = 20
            else:
                distance = 0
                direction_penalty = 0
            candidates.append((distance + direction_penalty, -cue.start(), kind))
    if candidates:
        distance, _position, kind = min(candidates)
        if distance <= 80:
            return kind
    return _amount_evidence_kind(text)

_PRODUCT_MEDIA_TYPES = {"ig_post", "share", "ig_reel", "reel", "story_mention", "story"}
_HISTORICAL_MEDIA_PROVENANCE = "historical_import"
_LIVE_MEDIA_PROVENANCE = "live_webhook"


def _row_allows_live_media(item: dict | None) -> bool:
    """Only explicit post-migration eligibility can promote raw bytes to live media."""
    return bool(isinstance(item, dict) and item.get("media_capture_eligible") is True)


def _historical_media(item: dict | None) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("provenance") == _HISTORICAL_MEDIA_PROVENANCE
    )


def _live_owned_media(item: dict | None) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("provenance") == _LIVE_MEDIA_PROVENANCE
        and item.get("status") == "owned"
        and item.get("storage_name")
    )


def _safe_local_media_url(item: dict | None) -> str:
    if not isinstance(item, dict):
        return ""
    value = str(item.get("local_url") or "").strip()
    return value if value.startswith("/media/") else ""


def _raw_media_by_mid(client) -> dict[str, list[dict]]:
    """Recover media that Meta kept only in the raw webhook event.

    Instagram sometimes sends an ``ig_post`` in a separate event with the same
    message id while the normalized message row has an empty ``attachments``
    field. Raw events are the source evidence; this helper only reads them.
    """
    if not client or not getattr(client, "igsid", ""):
        return {}
    try:
        from management.models import InstagramBotRawEvent
        from management.services.instagram_bot import _iter_events
    except Exception:
        return {}
    recovered: dict[str, list[dict]] = {}
    selected_event_by_mid: dict[str, int] = {}
    rows = InstagramBotRawEvent.objects.filter(sender_id=client.igsid).order_by("-id")[:240]
    for event in rows:
        try:
            payload = json.loads(event.payload or "{}")
        except (TypeError, ValueError):
            continue
        try:
            events = _iter_events(payload)
            for sender_id, _recipient_id, message, _referral in events:
                if sender_id != client.igsid:
                    continue
                mid = str(message.get("mid") or "").strip()
                if not mid:
                    continue
                selected_event = selected_event_by_mid.setdefault(mid, event.pk)
                if selected_event != event.pk:
                    continue
                for original_index, attachment in enumerate(
                    message.get("attachments") or []
                ):
                    if not isinstance(attachment, dict):
                        continue
                    payload_data = attachment.get("payload")
                    if not isinstance(payload_data, dict):
                        continue
                    url = str(payload_data.get("url") or "").strip()
                    if not url or not url.startswith(("https://", "http://")):
                        continue
                    item = {
                        "url": url[:1200],
                        "type": str(attachment.get("type") or "image")[:32],
                        "title": str(payload_data.get("title") or "")[:700],
                        "ig_post_media_id": str(payload_data.get("ig_post_media_id") or "")[:80],
                        "raw_event_id": event.pk,
                        "provenance": _HISTORICAL_MEDIA_PROVENANCE,
                        "status": "metadata_only",
                        "original_index": original_index,
                    }
                    event_at = message.get("_event_created_at")
                    if event_at is not None:
                        item["event_at"] = event_at.isoformat() if hasattr(event_at, "isoformat") else str(event_at)
                    existing = recovered.setdefault(mid, [])
                    existing.append(item)
        except Exception:
            continue
    from management.services.ig_media_manifest import normalize_attachment_media

    for mid, items in list(recovered.items()):
        recovered[mid] = normalize_attachment_media(
            items[:8],
            message_scope=f"raw-message:{mid}",
            identity_origin="ingress",
        )
    # Keep media whose provider mid has no normalized row available for the
    # timestamp-based fallback in _augment_messages_with_raw_media.
    known_mids = set()
    try:
        from management.models import InstagramBotMessage
        known_mids = set(
            InstagramBotMessage.objects.filter(client=client).exclude(mid__isnull=True).values_list("mid", flat=True)
        )
    except Exception:
        pass
    unmatched = []
    for mid, items in list(recovered.items()):
        if mid not in known_mids:
            unmatched.extend(items)
    if unmatched:
        recovered["__unmatched__"] = unmatched
    return recovered


def _existing_media(
    raw_attachments: str,
    *,
    provenance: str | None = None,
) -> list[dict]:
    try:
        urls = json.loads(raw_attachments or "[]")
    except (TypeError, ValueError):
        urls = []
    if not isinstance(urls, list):
        urls = []
        for candidate in re.findall(r"https?://[^\s\"'\]]+", raw_attachments or ""):
            urls.append(candidate)
    result = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        item = {
            "url": str(url)[:1200],
            "type": "image",
            "title": "",
            "raw_event_id": None,
        }
        if provenance:
            item["provenance"] = provenance
            item["status"] = (
                "metadata_only"
                if provenance == _HISTORICAL_MEDIA_PROVENANCE
                else "pending"
            )
        result.append(item)
    return result


def _media_intent(
    text: str,
    *,
    payment_context: bool,
    explicit_claim: bool,
    purchase_context: bool = False,
) -> str:
    low = " ".join(str(text or "").split()).casefold()
    if explicit_claim:
        return "payment_evidence"
    # A customer often sends the product screenshot in a follow-up message:
    # "Принт ось цей" after the actual purchase lines. Bind only an explicit
    # reference within the bounded purchase window; a later generic image
    # remains unresolved.
    if purchase_context and (
        _MEDIA_REFERENCE_RE.search(low)
        or _FIT_RE.search(low)
        or _MEDIA_PURCHASE_RE.search(low)
    ):
        return "purchase_candidate"
    if _MEDIA_CUSTOM_RE.search(low) and (
        re.search(
            r"(можн\w*|можете|можна|зроб\w*|сдел\w*|виготов\w*|нанес\w*|надрук\w*)",
            low,
        )
        or _MEDIA_PURCHASE_RE.search(low)
    ):
        return "custom_print_request"
    if _MEDIA_PURCHASE_RE.search(low):
        return "purchase_candidate"
    if _MEDIA_PRODUCT_RE.search(low):
        return "question" if re.search(
            r"(який|яка|яке|какой|какая|скільки|сколько|чи є|есть ли|можна|можно|розмір|размер|ціна|цена)",
            low,
        ) else "interest"
    if payment_context:
        return "payment_evidence"
    return "unknown"


def classify_media_items(
    text: str,
    media: list[dict] | None,
    *,
    payment_context: bool = False,
    explicit_claim: bool | None = None,
    purchase_context: bool = False,
) -> list[dict]:
    """Attach conservative role/intent semantics to current-turn media.

    This is deliberately deterministic. Gemini may enrich a product match, but
    receipts are never sent to catalog matching and unknown images remain
    reviewable instead of becoming invented products.
    """
    normalized_text = " ".join(str(text or "").split())
    if explicit_claim is None:
        explicit_claim = bool(_AFFIRMATION_RE.search(normalized_text)) and not _NON_EVIDENCE_RE.search(normalized_text)
    intent = _media_intent(
        normalized_text,
        payment_context=payment_context,
        explicit_claim=bool(explicit_claim),
        purchase_context=purchase_context,
    )
    result = []
    for raw in media or []:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        item = dict(raw)
        media_type = str(item.get("type") or "image").casefold()
        if media_type in _PRODUCT_MEDIA_TYPES:
            # Meta's explicit post/share type is stronger than surrounding
            # payment text: a product post accompanying a receipt is still a
            # product reference, while the generic image is the receipt.
            role = "product"
            item_intent = "purchase_candidate" if intent == "purchase_candidate" else "interest"
        elif intent == "payment_evidence":
            role = "receipt" if explicit_claim else "payment_candidate"
            item_intent = intent
        elif intent == "custom_print_request":
            role = "custom_reference"
            item_intent = intent
        elif intent in {"question", "interest", "purchase_candidate"}:
            role = "product"
            item_intent = intent
        else:
            role = "other"
            item_intent = intent
        item.update({
            "role": role,
            "intent": item_intent,
            "actionable": item_intent == "purchase_candidate" and role == "product",
            "payment_evidence": role in {"receipt", "payment_candidate"},
            "catalog_match_allowed": role == "product",
            "purchase_context": bool(purchase_context),
            "uncertain": role in {"other", "custom_reference", "payment_candidate"} or item_intent in {"unknown", "question", "interest"},
        })
        result.append(item)
    return result


def _role_for_media(
    item: dict,
    *,
    payment_context: bool,
    explicit_claim: bool,
    text: str = "",
) -> str:
    """Return the durable media role used by the payment-review audit."""
    classified = classify_media_items(
        text,
        [item],
        payment_context=payment_context,
        explicit_claim=explicit_claim,
    )
    return classified[0]["role"] if classified else "other"


def _augment_messages_with_raw_media(client, messages) -> list[dict]:
    raw_by_mid = _raw_media_by_mid(client)
    result = []
    for raw in list(messages or ()):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        media = list(item.get("media") or []) if isinstance(item.get("media"), list) else []
        attachment_media = item.get("attachment_media")
        if isinstance(attachment_media, list) and attachment_media:
            media.extend(
                dict(row)
                for row in attachment_media
                if isinstance(row, dict) and row.get("url")
            )
        else:
            media.extend(_existing_media(
                str(item.get("attachments") or ""),
                provenance=_HISTORICAL_MEDIA_PROVENANCE,
            ))
        mid = str(item.get("mid") or "").strip()
        for attachment in raw_by_mid.get(mid, []):
            attachment = dict(attachment)
            if _row_allows_live_media(item):
                attachment["provenance"] = _LIVE_MEDIA_PROVENANCE
                attachment["status"] = "pending"
            else:
                attachment["provenance"] = _HISTORICAL_MEDIA_PROVENANCE
                attachment["status"] = "metadata_only"
            source_part_id = str(attachment.get("source_part_id") or "")
            if source_part_id:
                if any(
                    str(row.get("source_part_id") or "") == source_part_id
                    for row in media
                ):
                    continue
                media.append(attachment)
            elif not any(row.get("url") == attachment.get("url") for row in media):
                # Positional identity was never recorded for this historical
                # row, so URL is only an honest legacy compatibility fallback.
                media.append(attachment)
        # Keep the old attachments contract intact for callers that only know
        # how to consume a JSON list of URLs, while exposing structured media
        # evidence to the review UI and catalog matcher.
        item["media"] = media[:8]
        if media and not item.get("attachments"):
            item["attachments"] = json.dumps(
                [row["url"] for row in media if row.get("url")], ensure_ascii=False
            )
        result.append(item)
    unmatched = list(raw_by_mid.get("__unmatched__") or [])
    if unmatched and result:
        # Meta may emit the attachment as a follow-up event with a different
        # mid. Bind it only to the nearest customer message in a bounded time
        # window; text or row order alone must never promote historical media.
        for attachment in unmatched:
            try:
                event_at = attachment.get("event_at")
                if not isinstance(event_at, datetime):
                    event_at = datetime.fromisoformat(
                        str(event_at or "").replace("Z", "+00:00")
                    )
                if timezone.is_naive(event_at):
                    event_at = timezone.make_aware(
                        event_at, timezone.get_default_timezone()
                    )
            except (TypeError, ValueError):
                event_at = None
            candidates = []
            if event_at is not None:
                for row in result:
                    if str(row.get("role") or "").casefold() not in _CUSTOMER_ROLES:
                        continue
                    try:
                        row_at = row.get("created_at")
                        if not isinstance(row_at, datetime):
                            row_at = datetime.fromisoformat(
                                str(row_at or "").replace("Z", "+00:00")
                            )
                        if timezone.is_naive(row_at):
                            row_at = timezone.make_aware(
                                row_at, timezone.get_default_timezone()
                            )
                        distance = abs((row_at - event_at).total_seconds())
                    except (TypeError, ValueError):
                        continue
                    if distance <= 300:
                        candidates.append((distance, row))
            target = min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None
            if target is not None:
                attachment = dict(attachment)
                if _row_allows_live_media(target):
                    attachment["provenance"] = _LIVE_MEDIA_PROVENANCE
                    attachment["status"] = "pending"
                else:
                    attachment["provenance"] = _HISTORICAL_MEDIA_PROVENANCE
                    attachment["status"] = "metadata_only"
                target.setdefault("media", [])
                if not any(row.get("url") == attachment.get("url") for row in target["media"]):
                    target["media"].append(attachment)
                target["media"] = target["media"][:8]
                if not target.get("attachments"):
                    target["attachments"] = json.dumps(
                        [row["url"] for row in target["media"] if row.get("url")], ensure_ascii=False
                    )
    return result


def _persist_review_media(media: list[dict]) -> list[dict]:
    """Keep customer evidence in its owned private blob, never a public copy.

    Existing first-party catalog URLs remain distinct metadata. Live customer
    media is read through ``_owned_media_bytes`` by the analysis callers below;
    this projection stage must not duplicate those bytes into ``default_storage``.
    """
    result = []
    for item in media[:8]:
        row = dict(item) if isinstance(item, dict) else item
        if isinstance(row, dict) and _live_owned_media(row):
            # Older review rows may carry a public copy from the removed path.
            # It is never an admissible substitute for the owned private blob.
            row.pop("local_url", None)
        result.append(row)
    return result


def _resolve_payment_media_candidates(media: list[dict]) -> list[dict]:
    """Use bounded vision only for images whose receipt role is contextual."""
    result = [dict(item) for item in (media or []) if isinstance(item, dict)]
    candidate_indexes = [
        index for index, item in enumerate(result)
        if (
            item.get("role") == "payment_candidate"
            and item.get("url")
            and not _historical_media(item)
            and (_live_owned_media(item) or _safe_local_media_url(item))
        )
    ]
    if not candidate_indexes:
        return result
    try:
        from management.services.instagram_bot import _owned_media_bytes, download_image
        from management.services import bot_vision

        images = []
        source_indexes = []
        for source_index in candidate_indexes[:8]:
            source = result[source_index]
            image = _owned_media_bytes(
                source,
                message_id=source.get("message_id") or source.get("source_message_id"),
            )
            if image is None and _safe_local_media_url(source):
                base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/") + "/"
                image = download_image(
                    urljoin(base, _safe_local_media_url(source).lstrip("/")),
                    profile="own_origin",
                )
            if image:
                images.append(image)
                source_indexes.append(source_index)
        matches = bot_vision.classify_media_roles(images) if images else []
    except Exception:
        return result
    for match in matches:
        try:
            image_index = int(match.get("source_image_index"))
            confidence = float(match.get("confidence") or 0)
            source_index = source_indexes[image_index]
        except (IndexError, TypeError, ValueError):
            continue
        if confidence < 0.75:
            result[source_index]["uncertain"] = True
            continue
        role = str(match.get("role") or "other")
        semantics = {
            "receipt": ("payment_evidence", True, False, False),
            "product": ("interest", False, False, True),
            "custom_reference": ("custom_print_request", False, False, True),
            "other": ("unknown", False, False, True),
        }.get(role)
        if semantics is None:
            continue
        intent, payment_evidence, catalog_match_allowed, uncertain = semantics
        result[source_index].update({
            "role": role,
            "intent": intent,
            "actionable": False,
            "payment_evidence": payment_evidence,
            "catalog_match_allowed": catalog_match_allowed,
            "uncertain": uncertain,
            "vision_role": role,
            "vision_confidence": confidence,
            "vision_reason": str(match.get("reason") or "")[:300],
        })
    return result


def _reconcile_payment_evidence_after_media_resolution(extracted: dict, media: list[dict]) -> dict:
    """Rebuild payment evidence after vision has resolved provisional images.

    A contextual ``payment_candidate`` is not proof by itself. Once vision says
    that the image is a product/custom/reference/other image, it must leave the
    financial workflow unless the same customer message contains an explicit
    payment statement.
    """
    if not isinstance(extracted, dict):
        return extracted
    resolved = [item for item in (media or []) if isinstance(item, dict)]
    roles_by_message: dict[int, set[str]] = {}
    for item in resolved:
        try:
            message_id = int(item.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        if message_id:
            roles_by_message.setdefault(message_id, set()).add(str(item.get("role") or "other"))
    kept = []
    for entry in extracted.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        try:
            message_id = int(entry.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        quote = str(entry.get("quote") or "")
        explicit_statement = bool(_AFFIRMATION_RE.search(quote) and not _NON_EVIDENCE_RE.search(quote))
        payment_role = bool(roles_by_message.get(message_id, set()).intersection({"receipt", "payment_candidate"}))
        if explicit_statement or payment_role:
            kept.append(entry)
    extracted["evidence"] = kept[-20:]
    extracted["message_ids"] = [entry.get("message_id") for entry in kept if entry.get("message_id")]
    extracted["needs_review"] = bool(kept)
    return extracted


def _is_review_deal_compatible(deal, product_ids: set[int] | None = None) -> bool:
    """Allow only the current unpaid/open deal for the matched product set."""
    if not deal:
        return False
    if getattr(deal, "order_id", None):
        return False
    if str(getattr(deal, "status", "") or "") not in {"draft", "quoted", "awaiting_payment"}:
        return False
    provider_truth = str(getattr(deal, "payment_truth", "") or "")
    deal_id = getattr(deal, "pk", None)
    if deal_id:
        try:
            from management.ig_bot_models import IgPaymentProjection

            projection_truth = IgPaymentProjection.objects.filter(
                deal_id=deal_id
            ).values_list("truth", flat=True).first()
            if projection_truth:
                provider_truth = str(projection_truth)
        except Exception:
            return False
    if provider_truth not in {"", "unverified", "pending"}:
        return False
    if str(getattr(deal, "payment_status", "") or "") in {"paid", "prepaid", "confirmed"}:
        return False
    wanted = {int(value) for value in (product_ids or set()) if str(value).isdigit()}
    if not wanted:
        return False
    known = getattr(deal, "product_ids", None)
    if known is None:
        try:
            known = set(deal.items.values_list("product_id", flat=True))
        except Exception:
            known = set()
    return bool(known) and set(known) == wanted


def _select_review_deal(client, catalog_matches: list[dict]):
    """Select a same-product open deal; never fall back to latest historical deal."""
    product_ids = {
        int(match.get("product_id"))
        for match in (catalog_matches or [])
        if isinstance(match, dict) and str(match.get("product_id") or "").isdigit()
    }
    if not client or not product_ids:
        return None
    try:
        from management.ig_bot_models import IgCommercialEpisode

        current_episode = (
            IgCommercialEpisode.objects.select_related("deal")
            .filter(client_id=client.pk, open_slot=1)
            .order_by("-sequence", "-id")
            .first()
        )
        if current_episode is not None:
            current_deal = current_episode.deal
            return (
                current_deal
                if _is_review_deal_compatible(current_deal, product_ids)
                else None
            )
        candidates = (
            client.deals.filter(commercial_episode__isnull=True)
            .order_by("-id")[:20]
        )
        for deal in candidates:
            if _is_review_deal_compatible(deal, product_ids):
                return deal
    except Exception:
        return None
    return None


def _apply_validated_conversation_price_to_draft(draft: dict, messages, catalog_matches: list[dict]) -> dict:
    """Apply only a human-authorized, product-bound price to a review draft."""
    if not isinstance(draft, dict):
        return draft
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    matches = [match for match in (catalog_matches or []) if isinstance(match, dict) and match.get("status") == "matched"]
    product = None
    if len(matches) == 1:
        product = type("ReviewProduct", (), {
            "pk": matches[0].get("product_id"),
            "title": matches[0].get("title") or "",
            "slug": matches[0].get("slug") or "",
        })()
    from management.services.bot_orders import _conversation_price_evidence, _message_role

    decision = _conversation_price_evidence(list(messages or ()), qty=int(items[0].get("qty") or 1) if len(items) == 1 else 1, product=product)
    if decision.get("status") == "accepted" and decision.get("price") is not None and len(items) == 1 and product:
        price = Decimal(str(decision["price"])).quantize(Decimal("0.01"))
        items[0]["unit_price"] = str(price)
        draft["quoted_total"] = str(price * Decimal(str(items[0].get("qty") or 1))).rstrip("0").rstrip(".")
        draft["amount_source_message_id"] = decision.get("source_message_id")
        return draft
    commercial_amount_seen = False
    manager_amount_accepted = False
    rows = list(messages or ())
    for index, message in enumerate(rows):
        text = message.get("text") if isinstance(message, dict) else getattr(message, "text", "")
        role = _message_role(message)
        if _AMOUNT_RE.search(str(text or "")) and _amount_evidence_kind(str(text or "")) in {"order_total", "unit_price"}:
            commercial_amount_seen = True
            next_message = rows[index + 1] if index + 1 < len(rows) else None
            next_role = _message_role(next_message) if next_message is not None else ""
            next_text = next_message.get("text") if isinstance(next_message, dict) else getattr(next_message, "text", "") if next_message is not None else ""
            if role == "manager" and next_role in {"user", "customer", "client"} and _PRICE_ACCEPTANCE_RE.search(str(next_text or "")):
                manager_amount_accepted = True
    if commercial_amount_seen and len(items) > 1:
        # Keep the negotiated conversation total visible for the operator, but
        # never invent a per-line split for a multi-product order.
        for item in items:
            item["unit_price"] = None
        if not manager_amount_accepted:
            reasons = list(draft.get("uncertainty_reasons") or [])
            if "conversation_price_allocation_required" not in reasons:
                reasons.append("conversation_price_allocation_required")
            draft["uncertainty_reasons"] = reasons
    elif commercial_amount_seen:
        draft["quoted_total"] = ""
        for item in items:
            item["unit_price"] = None
        reasons = list(draft.get("uncertainty_reasons") or [])
        if "conversation_price_not_authorized" not in reasons:
            reasons.append("conversation_price_not_authorized")
        draft["uncertainty_reasons"] = reasons
    return draft


def _hydrate_catalog_match(match: dict, source_rows: list[dict], source_indexes: list[int]) -> dict:
    product_id = match.get("product_id")
    selected_rows = [source_rows[index] for index in source_indexes if 0 <= index < len(source_rows)]
    result = {
        "status": "matched" if product_id else "unresolved",
        "product_id": product_id,
        "confidence": match.get("confidence", 0),
        "reason": match.get("reason", ""),
        "source_media_indexes": source_indexes,
        "source_message_ids": sorted({
            int(row.get("message_id"))
            for row in selected_rows
            if str(row.get("message_id") or "").isdigit()
        }),
    }
    if not product_id:
        return result
    try:
        from storefront.models import Product
        from productcolors.models import ProductColorVariant

        product = Product.objects.filter(pk=product_id).first()
        if not product:
            result.update({"status": "unresolved", "product_id": None, "reason": "catalog_product_missing"})
            return result
        result.update({
            "title": product.title,
            "slug": product.slug,
            "url": f"https://twocomms.shop/product/{product.slug}/",
        })
        variant_rows = list(
            ProductColorVariant.objects.filter(product=product)
            .select_related("color")[:20]
        )
        variants = []
        for variant in variant_rows:
            variants.append({
                "id": variant.pk,
                "color": getattr(variant.color, "name", "") or "",
                "sku": variant.sku or "",
            })
        result["variant_candidates"] = variants
        if len(variants) == 1:
            result["color_variant_id"] = variants[0]["id"]
        from management.services.ig_catalog_pricing import resolve_product_pricing

        pricing = resolve_product_pricing(
            product,
            variants=variant_rows,
            selected_variant_id=result.get("color_variant_id"),
        )
        result["catalog_price"] = (
            pricing["display"] or "залежить від конфігурації"
        )
    except Exception:
        result.update({"status": "error", "reason": "catalog_hydration_failed"})
    return result


def _catalog_order_media(media: list[dict]) -> list[dict]:
    """Return only images carrying an explicit purchase commitment."""
    return [
        row for row in (media or [])
        if isinstance(row, dict)
        and not _historical_media(row)
        and row.get("url")
        and row.get("role") == "product"
        and row.get("intent") == "purchase_candidate"
        and row.get("actionable") is True
        and row.get("catalog_match_allowed") is True
    ]


def _catalog_media_url_candidates(item: dict) -> list[str]:
    """Prefer durable local evidence, then fall back to the original URL."""
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/") + "/"
    candidates = []
    for raw in (item.get("local_url"), item.get("url")):
        value = str(raw or "").strip()
        if not value:
            continue
        if value.startswith("/"):
            value = urljoin(base, value.lstrip("/"))
        if value.startswith(("https://", "http://")) and value not in candidates:
            candidates.append(value)
    return candidates


def _catalog_matches_for_media(media: list[dict]) -> list[dict]:
    product_media = _catalog_order_media(media)
    if not product_media:
        return []
    try:
        from management.services.instagram_bot import _owned_media_bytes, download_image
        from management.services import bot_vision

        images = []
        # A forwarded post can arrive more than once with different signed
        # URLs. Keep every source media index for audit/order-line binding,
        # but send identical bytes to vision only once.
        downloaded_source_indexes = []
        image_digests = {}
        for index, row in enumerate(product_media[:8]):
            known_digest = str(row.get("content_hash") or "").strip()
            if known_digest and known_digest in image_digests:
                downloaded_source_indexes[image_digests[known_digest]].append(index)
                continue
            owned_image = _owned_media_bytes(
                row,
                message_id=row.get("message_id") or row.get("source_message_id"),
            )
            candidates = [None] if owned_image else (
                _catalog_media_url_candidates({"local_url": _safe_local_media_url(row)})
                if _safe_local_media_url(row)
                else []
            )
            for media_url in candidates:
                image = owned_image
                if image is None and media_url:
                    image = download_image(media_url, profile="own_origin")
                if image:
                    try:
                        digest = hashlib.sha256(image[1]).hexdigest()
                    except (TypeError, ValueError, IndexError):
                        digest = None
                    if digest and digest in image_digests:
                        downloaded_source_indexes[image_digests[digest]].append(index)
                    else:
                        if digest:
                            image_digests[digest] = len(images)
                        images.append(image)
                        downloaded_source_indexes.append([index])
                    break
        raw_matches = bot_vision.match_many(images) if images else []
    except Exception as exc:
        return [{"status": "error", "reason": str(exc)[:180], "source_media_indexes": []}]
    if not raw_matches:
        reason = "image_download_failed" if not images else "catalog_match_unresolved"
        return [_hydrate_catalog_match(
            {"product_id": None, "confidence": 0, "reason": reason},
            product_media,
            list(range(len(product_media[:8]))),
        )]
    results = []
    for match in raw_matches:
        image_indexes = match.get("source_image_indexes") or list(range(len(images)))
        source_indexes = sorted({
            source_index
            for index in image_indexes
            if 0 <= index < len(downloaded_source_indexes)
            for source_index in downloaded_source_indexes[index]
        })
        result = _hydrate_catalog_match(match, product_media, source_indexes)
        results.append(result)
        if result.get("status") == "matched":
            for source_index in source_indexes:
                product_media[source_index].setdefault("catalog_matches", []).append({
                    key: result.get(key)
                    for key in ("product_id", "title", "url", "confidence")
                    if result.get(key) not in (None, "")
                })
    return results


def _catalog_match_for_media(media: list[dict]) -> dict:
    """Compatibility accessor for callers that only understand one match."""
    matches = _catalog_matches_for_media(media)
    return matches[0] if matches else {}


def _apply_catalog_matches_to_draft(draft: dict, matches: list[dict]) -> None:
    """Bind validated catalog matches to draft lines without collapsing SKUs."""
    if not isinstance(draft, dict):
        return
    items = [item for item in (draft.get("items") or []) if isinstance(item, dict)]
    confirmed = [
        match for match in (matches or [])
        if isinstance(match, dict) and match.get("status") == "matched" and match.get("product_id")
    ]
    if not confirmed:
        return

    def bind(item, match):
        item["product_id"] = match.get("product_id")
        item["color_variant_id"] = match.get("color_variant_id")
        item["catalog"] = {
            key: match.get(key)
            for key in (
                "product_id", "title", "slug", "url", "catalog_price",
                "color_variant_id", "variant_candidates", "confidence",
            )
            if match.get(key) not in (None, "")
        }
        item.setdefault("title", match.get("title") or "Товар з каталогу")
        item.setdefault("qty", 1)
        item.setdefault("size", "")
        item.setdefault("fit", "")
        item.setdefault("unit_price", None)

    if not items:
        for match in confirmed:
            item = {}
            bind(item, match)
            items.append(item)
    else:
        # A single catalog product may cover several fit lines from one
        # customer message (for example classic S plus oversize XS with one
        # shared print). Only reserve a match when multiple distinct products
        # must be mapped; otherwise reusing the sole match is intentional.
        reserve_match = len(confirmed) > 1
        used_product_ids = set()
        for item in items:
            source_message_id = item.get("source_message_id")
            candidates = [
                match for match in confirmed
                if source_message_id
                and source_message_id in (match.get("source_message_ids") or [])
                and (not reserve_match or match.get("product_id") not in used_product_ids)
            ]
            if len(candidates) == 1:
                bind(item, candidates[0])
                if reserve_match:
                    used_product_ids.add(candidates[0].get("product_id"))
        unresolved_matches = [
            match for match in confirmed
            if match.get("product_id") not in {
                item.get("product_id") for item in items if item.get("product_id")
            }
        ]
        if unresolved_matches:
            draft["catalog_candidates"] = unresolved_matches
            reasons = list(draft.get("uncertainty_reasons") or [])
            if "catalog_line_mapping_required" not in reasons:
                reasons.append("catalog_line_mapping_required")
            draft["uncertainty_reasons"] = reasons
    draft["items"] = items
    if items and all(item.get("product_id") for item in items):
        draft["uncertainty_reasons"] = [
            reason for reason in (draft.get("uncertainty_reasons") or [])
            if reason != "catalog_product_not_identified"
        ]


def next_review_status(status: str, action: str) -> str:
    """Apply the monotonic manager decision state machine."""
    if status == "pending" and action == "confirm":
        return "confirmed"
    if status == "confirmed" and action == "cancel":
        return "cancelled"
    if status in {"confirmed", "cancelled"}:
        return status
    if status == "pending" and action == "cancel":
        return "cancelled"
    return status


def extract_payment_review_evidence(messages) -> dict:
    """Classify customer payment evidence and preserve negotiated order facts.

    Manager payment instructions are context only. They can contribute a quoted
    conversation amount, but never create a payment review by themselves.
    """
    evidence = []
    amount_evidence = []
    order_items = []
    customer_messages = []
    context_messages = []
    raw_messages = list(messages or ())
    customer_order_seen = False
    last_customer_purchase_index = None
    last_manager_payment_index = None
    last_customer_payment_commitment_index = None
    for message_index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "unknown").strip().lower()
        raw_text = str(raw.get("text") or "")
        text = " ".join(raw_text.split())
        attachments = str(raw.get("attachments") or "").strip()
        raw_media = raw.get("media") if isinstance(raw.get("media"), list) else _existing_media(attachments)
        if not text and not attachments and not raw_media:
            continue
        try:
            message_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        media = [dict(item) for item in raw_media if isinstance(item, dict) and item.get("url")]
        explicit_claim = bool(_AFFIRMATION_RE.search(text)) and not _NON_EVIDENCE_RE.search(text)
        payment_text = bool(text and _PAYMENT_EVIDENCE_RE.search(text) and not _NON_EVIDENCE_RE.search(text))
        payment_context = bool(
            customer_order_seen
            and (
                (
                    last_manager_payment_index is not None
                    and message_index - last_manager_payment_index == 1
                )
                or (
                    last_customer_payment_commitment_index is not None
                    and message_index - last_customer_payment_commitment_index == 1
                )
            )
        )
        purchase_context = bool(
            role in _CUSTOMER_ROLES
            and (
                (
                    last_customer_purchase_index is not None
                    and message_index - last_customer_purchase_index <= 4
                    and _MEDIA_REFERENCE_RE.search(text)
                )
                or _FIT_RE.search(text)
                or _MEDIA_PURCHASE_RE.search(text)
            )
        )
        media = classify_media_items(
            text,
            media,
            payment_context=payment_context,
            explicit_claim=explicit_claim,
            purchase_context=purchase_context,
        )
        for media_item in media:
            media_item["message_id"] = message_id
        context_messages.append({
            "message_id": message_id,
            "role": role,
            "quote": raw_text[:500],
            "attachments": attachments[:500],
            "media": media[:8],
        })
        for amount_match in _AMOUNT_RE.finditer(text):
            amount = amount_match.group(1)
            amount_kind = _amount_match_evidence_kind(text, amount_match)
            try:
                normalized = Decimal(amount.replace(",", ".")).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                continue
            amount_evidence.append({
                "message_id": message_id,
                "role": role,
                "amount": str(normalized).rstrip("0").rstrip("."),
                "kind": amount_kind,
                "quote": text[:300],
            })
        if role in _CUSTOMER_ROLES:
            customer_messages.append(text)
            for match in _FIT_RE.finditer(text):
                fit_raw = match.group("fit").lower()
                fit = "oversize" if ("оверсайз" in fit_raw or fit_raw == "oversize") else "classic"
                order_items.append({
                    "title": "Оверсайз" if fit == "oversize" else "Базова футболка",
                    "fit": fit,
                    "size": match.group("size").upper(),
                    "qty": 1,
                    "product_id": None,
                    "color_variant_id": None,
                    "unit_price": None,
                    "source_message_id": message_id,
                })
            is_receipt_attachment = bool(attachments) and (
                payment_context
                or explicit_claim
                or (payment_text and not _NON_EVIDENCE_RE.search(text))
            ) and not any(item.get("role") == "product" for item in media)
            is_receipt_attachment = is_receipt_attachment or any(
                item.get("role") in {"receipt", "payment_candidate"} for item in media
            )
            if explicit_claim or is_receipt_attachment:
                evidence.append({
                    "message_id": message_id,
                    "role": role,
                    "quote": text[:300],
                    "attachments": attachments[:500],
                    "media": media[:8],
                })
            if (
                payment_text
                or _MEDIA_PURCHASE_RE.search(text)
                or _FIT_RE.search(text)
                or any(item.get("role") == "product" for item in media)
            ):
                customer_order_seen = True
            if _MEDIA_PURCHASE_RE.search(text) or _FIT_RE.search(text):
                last_customer_purchase_index = message_index
            if _CUSTOMER_PAYMENT_COMMITMENT_RE.search(text):
                last_customer_payment_commitment_index = message_index
        elif payment_text:
            last_manager_payment_index = message_index

    # A single explicit quantity describes the only extracted line; numbered
    # lines remain independent so classic and oversize are never collapsed.
    if len(order_items) == 1 and customer_messages:
        quantity_match = _QTY_RE.search(" ".join(customer_messages))
        if quantity_match:
            order_items[0]["qty"] = int(quantity_match.group(1))
    uncertainty_reasons = []
    if order_items:
        uncertainty_reasons.append("catalog_product_not_identified")
    commercial_amounts = [
        item for item in amount_evidence if item.get("kind") in {"order_total", "unit_price"}
    ]
    if order_items and not commercial_amounts:
        uncertainty_reasons.append("conversation_price_not_found")
    selected_amount = commercial_amounts[-1] if commercial_amounts else None
    quoted_total = selected_amount["amount"] if selected_amount else ""
    if len(order_items) == 1 and quoted_total:
        try:
            order_items[0]["unit_price"] = str(
                (Decimal(quoted_total) / Decimal(str(order_items[0].get("qty") or 1))).quantize(Decimal("0.01"))
            )
        except (InvalidOperation, ValueError, ZeroDivisionError):
            order_items[0]["unit_price"] = None
    elif len(order_items) > 1 and quoted_total:
        # A total for multiple lines is evidence, but it cannot safely be
        # allocated to each SKU without an explicit per-line price.
        uncertainty_reasons.append("conversation_price_allocation_required")
    manager_package_context = any(
        context["role"] not in _CUSTOMER_ROLES
        and re.search(r"пакет|zip|зіп", context["quote"], re.IGNORECASE)
        for context in context_messages
    )
    packaging_preference = ""
    if manager_package_context:
        customer_packaging_text = " ".join(
            context["quote"] for context in context_messages if context["role"] in _CUSTOMER_ROLES
        ).casefold()
        if "різн" in customer_packaging_text:
            packaging_preference = "Окремі пакети"
        elif "один" in customer_packaging_text:
            packaging_preference = "Один пакет"
    delivery = {"full_name": "", "phone": "", "city": "", "office": ""}
    for context in context_messages:
        if context["role"] not in _CUSTOMER_ROLES:
            continue
        quote = context["quote"]
        phone_match = _PHONE_RE.search(quote.replace(" ", ""))
        if phone_match and not delivery["phone"]:
            delivery["phone"] = phone_match.group(0)
        office_match = _OFFICE_RE.search(quote)
        if office_match and not delivery["office"]:
            delivery["office"] = f"{office_match.group('kind').capitalize()} {office_match.group('number')}"
        if "," in quote and not delivery["city"]:
            candidate = quote.split(",", 1)[0].strip()
            if 2 <= len(candidate) <= 100 and not any(char.isdigit() for char in candidate):
                delivery["city"] = candidate
        # Names are accepted only from the same customer message as a phone
        # number. This prevents short follow-ups such as "В різні" from
        # overwriting an already extracted recipient name. Slash-separated
        # delivery details are common in Instagram messages.
        segments = re.split(r"[/\n]+", quote) if phone_match else []
        for line in segments:
            candidate = " ".join(line.split()).strip(" .,:;()")
            if (
                not delivery["full_name"]
                and len(candidate.split()) in {2, 3}
                and not _PHONE_RE.search(candidate.replace(" ", ""))
                and not _FIT_RE.search(candidate)
                and not _AMOUNT_RE.search(candidate)
                and not _OFFICE_RE.search(candidate)
                and candidate.casefold() not in _NAME_STOPWORDS
                and not any(word in candidate.lower() for word in ("принт", "футбол", "передоплат", "оплат"))
            ):
                delivery["full_name"] = candidate
    media_audit = [
        media_item
        for context in context_messages
        for media_item in context.get("media", [])
    ]
    order_draft = {
        "items": order_items,
        "quoted_total": quoted_total,
        "currency": "UAH",
        "amount_source_message_id": selected_amount["message_id"] if selected_amount else None,
        "uncertainty_reasons": uncertainty_reasons,
        "packaging_preference": packaging_preference,
        "delivery": delivery,
        "context_messages": context_messages[-80:],
        "media": media_audit[:40],
    }
    return {
        "needs_review": bool(evidence),
        "provider_confirmed": False,
        "message_ids": [item["message_id"] for item in evidence if item["message_id"]],
        "evidence": evidence[-20:],
        "amount_evidence": amount_evidence[-20:],
        "order_draft": order_draft,
        "media": media_audit[:40],
    }


def _deal_payload(deal) -> dict:
    if not deal:
        return {"deal_id": None, "items": [], "amount": "0", "delivery": {}}
    return {
        "deal_id": deal.pk,
        "amount": str(deal.amount or 0),
        "currency": deal.currency or "UAH",
        "items": [
            {
                "product_id": item.product_id,
                "color_variant_id": item.color_variant_id,
                "title": item.title,
                "size": item.size,
                "qty": item.qty,
                "unit_price": str(item.unit_price or 0),
            }
            for item in deal.items.select_related("product", "color_variant").all()
        ],
        "delivery": {
            "full_name": deal.np_full_name or "",
            "phone": deal.np_phone or "",
            "city": deal.np_city or "",
            "office": deal.np_office or "",
        },
    }


def _alert_text(review, client) -> str:
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
    amount = draft.get("quoted_total") or "не вказано"
    media = evidence.get("media") if isinstance(evidence.get("media"), list) else []
    receipts = [item for item in media if item.get("role") == "receipt"]
    payment_candidates = [item for item in media if item.get("role") == "payment_candidate"]
    from management.services.ig_alerts import (
        format_operator_alert,
    )

    return format_operator_alert(
        "⚠️ Instagram: потрібна перевірка заяви про оплату",
        event_type="payment_review",
        client_id=getattr(client, "pk", None),
        review_id=review.pk,
        amount=amount,
        status="provider_unconfirmed",
        counts={"receipt_evidence": len(receipts), "unresolved_media": len(payment_candidates)},
        instruction_code="payment_review",
    )


def _review_keyboard(review) -> dict:
    base = getattr(settings, "MANAGEMENT_BASE_URL", "https://management.twocomms.shop").rstrip("/")
    try:
        amount_contract = resolve_review_payment_amount(review)
    except ValueError:
        amount_contract = None
    if amount_contract:
        amount = amount_contract["amount"]
        scope = amount_contract["scope"]
        action_label = "передоплату" if scope == "prepayment" else "оплату"
        rows = [[
            {
                "text": f"Підтвердити {action_label} {amount:.2f} грн",
                "callback_data": f"igpay:confirm:{review.pk}",
            },
            {"text": "Відхилити", "callback_data": f"igpay:cancel:{review.pk}"},
        ], [
            {"text": "Відкрити перевірку", "url": f"{base}/bot/?payment_review={review.pk}"},
        ]]
    else:
        rows = [[
            {"text": "Відкрити перевірку", "url": f"{base}/bot/?payment_review={review.pk}"},
            {"text": "Відхилити", "callback_data": f"igpay:cancel:{review.pk}"},
        ]]
    evidence = review.evidence if isinstance(getattr(review, "evidence", None), dict) else {}
    matches = evidence.get("catalog_matches") if isinstance(evidence.get("catalog_matches"), list) else []
    if not matches:
        legacy_match = evidence.get("catalog_match") if isinstance(evidence.get("catalog_match"), dict) else {}
        matches = [legacy_match] if legacy_match else []
    for match in matches[:4]:
        if not isinstance(match, dict) or match.get("status") != "matched" or not match.get("url"):
            continue
        rows.append([{
            "text": f"Товар: {str(match.get('title') or 'відкрити картку')[:48]}",
            "url": str(match["url"]),
        }])
    return {"inline_keyboard": rows}


def payment_review_order_url(review_id: int) -> str:
    """Return the storefront manual-order URL from any subdomain URLConf."""
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    path = reverse("manual_order_create", urlconf="twocomms.urls")
    return f"{base}{path}?ig_payment_review={int(review_id)}"


def _review_evidence_needs_refresh(status: str, current: dict, extracted: dict) -> bool:
    """Refresh material evidence only while the manager decision is pending."""
    if str(status or "") != "pending":
        return False
    current = current if isinstance(current, dict) else {}
    extracted = extracted if isinstance(extracted, dict) else {}
    if not current.get("media_audit_v3"):
        return True
    incoming = {
        "messages": extracted.get("messages", extracted.get("evidence")),
        "amount_evidence": extracted.get("amount_evidence"),
        "order_draft": extracted.get("order_draft"),
        "media": extracted.get("media"),
        "catalog_match": extracted.get("catalog_match"),
        "catalog_matches": extracted.get("catalog_matches"),
    }
    return any(
        current.get(key) != incoming.get(key)
        for key in incoming
    )


def _fingerprint_text(value) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def payment_review_fingerprint(review_or_evidence) -> str:
    """Return a strict, evidence-bound identity for one payment claim.

    Watermarks and the full conversation transcript are intentionally excluded:
    a later analysis pass may see more messages while still describing the same
    receipt.  Receipt/payment evidence, negotiated money, commercial lines and
    delivery identity remain part of the fingerprint so a real repeat purchase
    cannot be collapsed merely because its amount matches.
    """
    evidence = getattr(review_or_evidence, "evidence", review_or_evidence)
    if not isinstance(evidence, dict):
        return ""
    draft = evidence.get("order_draft")
    if not isinstance(draft, dict):
        draft = {}

    amount_rows = []
    for raw in evidence.get("amount_evidence") or []:
        if not isinstance(raw, dict):
            continue
        amount = _positive_money(raw.get("amount"))
        message_id = raw.get("message_id")
        if amount is None or not str(message_id).isdigit():
            continue
        # ``kind`` is analyzer metadata, not payment identity. Legacy reviews
        # stored the same amount/message as null before classifying it as
        # ``order_total`` on a later pass.
        amount_rows.append((str(amount), int(message_id)))

    receipt_rows = []
    for raw in evidence.get("media") or []:
        if not isinstance(raw, dict) or raw.get("role") not in {"receipt", "payment_candidate"}:
            continue
        message_id = raw.get("message_id") or raw.get("source_message_id")
        url = _fingerprint_text(raw.get("url") or raw.get("local_url"))
        if str(message_id).isdigit() or url:
            receipt_rows.append((int(message_id) if str(message_id).isdigit() else 0, url))

    item_rows = []
    for raw in draft.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item_rows.append({
            "product_id": str(raw.get("product_id") or ""),
            "variant_id": str(raw.get("variant_id") or raw.get("color_variant_id") or ""),
            "title": _fingerprint_text(raw.get("title") or raw.get("name")),
            "fit": _fingerprint_text(raw.get("fit") or raw.get("fit_option_code")),
            "size": _fingerprint_text(raw.get("size")),
            "qty": int(raw.get("qty") or 0) if str(raw.get("qty") or "").isdigit() else 0,
            "unit_price": str(_positive_money(raw.get("unit_price")) or ""),
            "line_total": str(_positive_money(raw.get("line_total")) or ""),
        })

    delivery = draft.get("delivery") if isinstance(draft.get("delivery"), dict) else {}
    payload = {
        "amount_evidence": sorted(amount_rows),
        "receipts": sorted(receipt_rows),
        "quoted_total": str(_positive_money(draft.get("quoted_total")) or ""),
        "currency": _fingerprint_text(draft.get("currency") or "UAH").upper(),
        "items": sorted(item_rows, key=lambda row: json.dumps(row, sort_keys=True)),
        "delivery": {
            key: _fingerprint_text(delivery.get(key))
            for key in ("full_name", "phone", "city", "office")
        },
    }
    # A receipt or exact amount is mandatory; otherwise two unrelated drafts
    # with the same catalog lines could be mistaken for one payment.
    if not payload["amount_evidence"] and not payload["receipts"]:
        return ""
    if (
        not payload["items"]
        and not payload["quoted_total"]
        and not payload["receipts"]
    ):
        return ""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _claim_evidence_rows(extracted_or_evidence) -> list[dict]:
    """Extract immutable customer-payment rows without transcript context."""
    evidence = getattr(extracted_or_evidence, "evidence", extracted_or_evidence)
    if not isinstance(evidence, dict):
        return []
    rows = evidence.get("evidence")
    if not isinstance(rows, list):
        rows = evidence.get("messages")
    if not isinstance(rows, list):
        return []
    result = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role and role not in _CUSTOMER_ROLES:
            continue
        message_id = raw.get("source_message_id") or raw.get("message_id") or raw.get("id")
        try:
            message_id = int(message_id or 0)
        except (TypeError, ValueError):
            message_id = 0
        media_rows = []
        for media in raw.get("media") or []:
            if not isinstance(media, dict):
                continue
            source_id = media.get("source_message_id") or media.get("message_id") or message_id
            try:
                source_id = int(source_id or 0)
            except (TypeError, ValueError):
                source_id = 0
            # Signed CDN URLs rotate. Once Instagram supplied a source message
            # identity, it is the durable receipt reference; URL is fallback.
            identity = "" if source_id else _fingerprint_text(
                media.get("url") or media.get("local_url")
            )
            if source_id or identity:
                media_rows.append({"source_message_id": source_id, "identity": identity})
        quote = _fingerprint_text(raw.get("quote") or raw.get("text"))
        # Raw attachment payloads commonly contain expiring Meta CDN URLs.
        # The durable source message id already identifies the evidence, so a
        # URL refresh must not change the payment-claim identity.
        attachment = "" if message_id else _fingerprint_text(raw.get("attachments"))
        if message_id or quote or attachment or media_rows:
            result.append({
                "message_id": message_id,
                "quote": quote,
                "attachments": attachment,
                "media": sorted(media_rows, key=lambda item: json.dumps(item, sort_keys=True)),
            })
    return sorted(result, key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True))


def payment_review_claim_anchor(extracted_or_evidence) -> str:
    """Hash only the customer's immutable payment assertion and receipt."""
    evidence = getattr(extracted_or_evidence, "evidence", extracted_or_evidence)
    if isinstance(evidence, dict) and isinstance(evidence.get("claim_anchor"), str):
        return evidence["claim_anchor"][:64]
    rows = _claim_evidence_rows(evidence)
    if not rows:
        return ""
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _receipt_source_identities(review_or_evidence) -> tuple[str, ...]:
    """Return source-bound receipt identities for conservative legacy repair."""
    evidence = getattr(review_or_evidence, "evidence", review_or_evidence)
    if not isinstance(evidence, dict):
        return ()
    media_rows = list(evidence.get("media") or [])
    for row in evidence.get("messages") or evidence.get("evidence") or []:
        if isinstance(row, dict):
            media_rows.extend(row.get("media") or [])
    identities = set()
    for media in media_rows:
        if not isinstance(media, dict):
            continue
        if media.get("role") not in {"receipt", "payment_candidate"}:
            continue
        source_id = media.get("source_message_id") or media.get("message_id")
        if str(source_id).isdigit():
            identities.add(f"source:{int(source_id)}")
            continue
        identity = _fingerprint_text(media.get("url") or media.get("local_url"))
        if identity:
            identities.add("media:" + hashlib.sha256(identity.encode("utf-8")).hexdigest())
    return tuple(sorted(identities))


def _persisted_payment_review_claim_identity(review_or_evidence) -> str:
    """Return a v2 identity only when it was saved with the review."""
    evidence = getattr(review_or_evidence, "evidence", review_or_evidence)
    if isinstance(evidence, dict):
        anchor = evidence.get("claim_anchor")
        if isinstance(anchor, str) and re.fullmatch(r"[0-9a-f]{64}", anchor):
            return f"claim:{anchor}"
    return ""


def _legacy_receipt_source_identity(review) -> str:
    """Return the one proven receipt source eligible for legacy repair only."""
    if not review or _persisted_payment_review_claim_identity(review):
        return ""
    created_at = getattr(review, "created_at", None)
    if not created_at or created_at >= LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF:
        return ""
    sources = _receipt_source_identities(review)
    if len(sources) != 1 or not sources[0].startswith("source:"):
        return ""
    return f"legacy-receipt:{sources[0]}"


def _payment_review_reconciliation_identity(review, *, allow_legacy_receipt: bool) -> str:
    """Choose the one identity explicitly authorized for a write repair."""
    claim_identity = _persisted_payment_review_claim_identity(review)
    if claim_identity:
        return claim_identity
    return _legacy_receipt_source_identity(review) if allow_legacy_receipt else ""


def payment_review_duplicate_identity(review_or_evidence) -> str:
    """Read-only grouping identity for the management workspace.

    Display grouping follows the same conservative identities as the repair,
    but never mutates records. Fingerprint- or URL-only similarity is not a
    purchase identity and remains visible as separate manager work.
    """
    return (
        _persisted_payment_review_claim_identity(review_or_evidence)
        or _legacy_receipt_source_identity(review_or_evidence)
    )


def payment_review_canonical_sort_key(review) -> tuple:
    """Prefer materialized payment truth, then retain the oldest audit row."""
    from management.ig_bot_models import IgPaymentConfirmationReview

    return (
        0 if review.order_id else 1,
        0 if review.status == IgPaymentConfirmationReview.Status.CONFIRMED else 1,
        review.pk,
    )


def _resolve_duplicate_notification(review, *, now) -> bool:
    from management.ig_bot_models import IgBotNotification, IgBotNotificationAudit

    notification = IgBotNotification.objects.select_for_update().filter(
        dedupe_key=review.dedupe_key,
    ).first()
    if not notification or notification.status not in {
        IgBotNotification.Status.PENDING,
        IgBotNotification.Status.FAILED,
        IgBotNotification.Status.SENT,
    }:
        return False
    previous_status = notification.status
    notification.status = IgBotNotification.Status.RESOLVED
    notification.next_attempt_at = None
    notification.failure_kind = "payment_review_superseded"
    notification.payload = {
        **(notification.payload if isinstance(notification.payload, dict) else {}),
        "review_status": review.Status.SUPERSEDED,
        "superseded_by_review_id": review.superseded_by_id,
        "resolved_at": now.isoformat(),
    }
    notification.save(update_fields=[
        "status", "next_attempt_at", "failure_kind", "payload", "updated_at",
    ])
    IgBotNotificationAudit.objects.create(
        notification=notification,
        action="supersede_duplicate",
        from_status=previous_status,
        to_status=IgBotNotification.Status.RESOLVED,
        note=f"canonical payment review {review.superseded_by_id}",
    )
    return True


def _payment_review_notification_is_sending(review) -> bool:
    """Do not hide a review while its manager alert is crossing Telegram."""
    from management.ig_bot_models import IgBotNotification

    return IgBotNotification.objects.filter(
        dedupe_key=review.dedupe_key,
        status=IgBotNotification.Status.SENDING,
    ).exists()


def reconcile_duplicate_payment_review(
    review,
    *,
    dry_run: bool = False,
    allow_legacy_receipt: bool = False,
):
    """Supersede only evidence-bound duplicate payment-review tasks.

    This is deliberately a write service, never a read-path helper. Default
    behavior permits only a saved v2 claim anchor. The bounded maintenance
    command opts into the one-source legacy rule for rows before the known
    historical-refresh fix boundary.
    """
    from management.ig_bot_models import IgPaymentConfirmationReview

    if not review:
        return None
    with transaction.atomic():
        review = IgPaymentConfirmationReview.objects.select_for_update().filter(
            pk=review.pk,
        ).select_related("order").first()
        if not review or review.status in {
            IgPaymentConfirmationReview.Status.CANCELLED,
            IgPaymentConfirmationReview.Status.SUPERSEDED,
        }:
            return getattr(review, "superseded_by", None)
        if _payment_review_notification_is_sending(review):
            return None
        identity = _payment_review_reconciliation_identity(
            review,
            allow_legacy_receipt=allow_legacy_receipt,
        )
        if not identity:
            return None
        candidate_query = IgPaymentConfirmationReview.objects.select_for_update().filter(
            client_id=review.client_id,
        ).exclude(
            status__in=[
                IgPaymentConfirmationReview.Status.CANCELLED,
                IgPaymentConfirmationReview.Status.SUPERSEDED,
            ]
        )
        if identity.startswith("legacy-receipt:"):
            candidate_query = candidate_query.filter(
                created_at__lt=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF,
            ).filter(
                Q(
                    status=IgPaymentConfirmationReview.Status.PENDING,
                    deal_id__isnull=True,
                    order_id__isnull=True,
                )
                | Q(
                    status=IgPaymentConfirmationReview.Status.CONFIRMED,
                    order_id__isnull=False,
                )
            )
        candidates = list(candidate_query.select_related("order"))
        matches = [
            row for row in candidates
            if _payment_review_reconciliation_identity(
                row,
                allow_legacy_receipt=allow_legacy_receipt,
            ) == identity
            and not (review.order_id and row.order_id and review.order_id != row.order_id)
        ]
        if len(matches) < 2:
            return None
        canonical = min(matches, key=payment_review_canonical_sort_key)
        if canonical.pk == review.pk:
            return None
        if dry_run:
            return canonical
        now = timezone.now()
        update_fields = ["status", "superseded_by", "superseded_at", "supersede_reason", "updated_at"]
        review.status = IgPaymentConfirmationReview.Status.SUPERSEDED
        review.superseded_by = canonical
        review.superseded_at = now
        review.supersede_reason = (
            "legacy_single_receipt_source"
            if identity.startswith("legacy-receipt:")
            else "same_payment_claim_anchor"
        )
        if canonical.order_id and not review.order_id:
            review.order = canonical.order
            update_fields.insert(0, "order")
        review.save(update_fields=update_fields)
        _resolve_duplicate_notification(review, now=now)

        # The orphan episode created by the old watermark is historical
        # duplicate work, not a second purchase cycle. Retain its timeline.
        try:
            from management.ig_bot_models import IgClient, IgCommercialEpisode
            from management.services.ig_commercial_episodes import append_episode_event

            episode = IgCommercialEpisode.objects.select_for_update().filter(
                primary_payment_review_id=review.pk,
            ).first()
            canonical_episode = IgCommercialEpisode.objects.select_for_update().filter(
                primary_payment_review_id=canonical.pk,
            ).first()
            if episode and episode.pk != getattr(canonical_episode, "pk", None):
                episode.open_slot = None
                episode.state = IgCommercialEpisode.State.LOST
                episode.outcome = "superseded_duplicate_payment_review"
                episode.closed_at = now
                episode.save(update_fields=["open_slot", "state", "outcome", "closed_at", "updated_at"])
                IgClient.objects.filter(
                    pk=review.client_id,
                    current_commercial_episode_id=episode.pk,
                ).update(
                    current_commercial_episode_id=None,
                    updated_at=now,
                )
                append_episode_event(
                    episode,
                    dedupe_key=f"episode:{episode.pk}:superseded-by-review:{canonical.pk}",
                    event_type="review_superseded",
                    source="payment_review_reconciliation",
                    evidence={"canonical_review_id": canonical.pk, "identity": identity},
                )
        except Exception:
            logger.exception("Could not close duplicate payment-review episode review=%s", review.pk)
        return canonical


def reconcile_legacy_payment_reviews(*, client_id: int | None = None, limit: int = 100, dry_run: bool = True) -> dict:
    """Bounded, no-network repair for duplicate pending historical reviews."""
    from management.ig_bot_models import IgPaymentConfirmationReview

    bounded_limit = max(1, min(int(limit or 100), 1000))
    reviews = IgPaymentConfirmationReview.objects.filter(
        status=IgPaymentConfirmationReview.Status.PENDING,
        deal_id__isnull=True,
        order_id__isnull=True,
        created_at__lt=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF,
    )
    if client_id:
        reviews = reviews.filter(client_id=int(client_id))
    rows = list(reviews.order_by("client_id", "created_at", "id")[:bounded_limit])
    result = {
        "dry_run": bool(dry_run),
        "scanned": len(rows),
        "would_supersede": 0,
        "superseded": 0,
        "skipped_unsafe": 0,
        "skipped_sending_notification": 0,
    }
    for review in rows:
        if not _legacy_receipt_source_identity(review):
            result["skipped_unsafe"] += 1
            continue
        if _payment_review_notification_is_sending(review):
            result["skipped_sending_notification"] += 1
            continue
        canonical = reconcile_duplicate_payment_review(
            review,
            dry_run=dry_run,
            allow_legacy_receipt=True,
        )
        if not canonical:
            continue
        if dry_run:
            result["would_supersede"] += 1
        else:
            result["superseded"] += 1
    return result


def _claim_review_context(extracted: dict, *, claim_anchor: str) -> dict:
    return {
        "claim_anchor": claim_anchor,
        "messages": extracted.get("evidence", []),
        "amount_evidence": extracted.get("amount_evidence", []),
        "order_draft": extracted.get("order_draft", {}),
        "media": extracted.get("media", []),
        "media_audit_v3": False,
    }


def _pending_review_matches_payment_evidence(review, extracted: dict) -> bool:
    """Return whether new context continues the pending payment claim."""
    existing_sources = set(_receipt_source_identities(review))
    incoming_sources = set(_receipt_source_identities(extracted))
    if not existing_sources or not incoming_sources:
        return True
    incoming_message_sources = sorted(
        int(identity.split(":", 1)[1])
        for identity in incoming_sources
        if identity.startswith("source:") and identity.split(":", 1)[1].isdigit()
    )
    if incoming_message_sources:
        return f"source:{incoming_message_sources[-1]}" in existing_sources
    return incoming_sources.issubset(existing_sources)


def _claim_payment_review(client, *, extracted: dict, watermark: int, claim_anchor: str):
    """Create one durable claim inside the client's current commercial episode."""
    from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
    from management.services.ig_commercial_episodes import ensure_open_episode_for_locked_client

    with transaction.atomic():
        locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
        episode = ensure_open_episode_for_locked_client(
            locked_client,
            materialization_prefix="ig-payment-review-v2",
        )
        dedupe_key = f"ig-payment-review:v2:{locked_client.pk}:{episode.pk}:{claim_anchor}"
        exact = IgPaymentConfirmationReview.objects.select_for_update().filter(
            dedupe_key=dedupe_key,
        ).first()
        if exact:
            return exact, False
        # A customer can first state that payment was made and only then add a
        # receipt image. Those are progressively stronger evidence for one
        # claim. A genuinely newer receipt source remains a separate review.
        primary = IgPaymentConfirmationReview.objects.select_for_update().filter(
            pk=episode.primary_payment_review_id,
            client_id=locked_client.pk,
            status=IgPaymentConfirmationReview.Status.PENDING,
        ).first()
        if primary and _pending_review_matches_payment_evidence(primary, extracted):
            return primary, False
        review, created = IgPaymentConfirmationReview.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "client": locked_client,
                "evidence": _claim_review_context(extracted, claim_anchor=claim_anchor),
                "watermark_message_id": watermark,
            },
        )
        if created:
            episode.primary_payment_review = review
            episode.save(update_fields=["primary_payment_review", "updated_at"])
    return review, created


def _pending_review_has_new_receipt_evidence(review, extracted: dict) -> bool:
    """Allow one enrichment pass when a receipt arrives after a text claim."""
    incoming_sources = set(_receipt_source_identities(extracted))
    if not incoming_sources:
        return False
    return bool(incoming_sources.difference(_receipt_source_identities(review)))


def _review_media_needs_owned_retry(evidence: dict) -> bool:
    media = evidence.get("media") if isinstance(evidence, dict) else []
    return any(
        isinstance(item, dict)
        and item.get("provenance") == _LIVE_MEDIA_PROVENANCE
        and (
            item.get("status") in {"pending", "acquiring"}
            or (
                item.get("status") == "unavailable"
                and max(0, int(item.get("capture_attempts") or 0)) < 2
            )
        )
        for item in (media or [])
    )


def _merge_review_media(current: list[dict], incoming: list[dict]) -> list[dict]:
    merged = []
    positions = {}
    for raw in [*(current or []), *(incoming or [])]:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        item = dict(raw)
        url = str(item.get("url") or "")
        position = positions.get(url)
        if position is None:
            positions[url] = len(merged)
            merged.append(item)
            continue
        stored = merged[position]
        stored_owned = _live_owned_media(stored) or bool(_safe_local_media_url(stored))
        incoming_owned = _live_owned_media(item) or bool(_safe_local_media_url(item))
        if incoming_owned and not stored_owned:
            merged[position] = {**stored, **item}
        elif incoming_owned == stored_owned:
            merged[position] = {**stored, **item}
    return merged


def _refresh_pending_review_context(review, extracted: dict, *, watermark: int) -> None:
    if review.status != review.Status.PENDING:
        return
    current = review.evidence if isinstance(review.evidence, dict) else {}
    current_draft = current.get("order_draft") if isinstance(current.get("order_draft"), dict) else {}
    incoming_draft = extracted.get("order_draft") if isinstance(extracted.get("order_draft"), dict) else {}
    merged_delivery = dict(current_draft.get("delivery") or {})
    merged_delivery.update({
        key: value for key, value in (incoming_draft.get("delivery") or {}).items() if value
    })
    incoming_media = extracted.get("media") or incoming_draft.get("media") or []
    merged_media = _merge_review_media(
        current.get("media", current_draft.get("media", [])),
        incoming_media,
    )
    merged_draft = {
        **current_draft,
        **incoming_draft,
        "delivery": merged_delivery,
        "media": merged_media,
    }
    refreshed = {
        **current,
        "messages": extracted.get("evidence", []),
        "amount_evidence": extracted.get("amount_evidence", []),
        "order_draft": merged_draft,
        "media": merged_media,
    }
    update_fields = []
    if refreshed != current:
        review.evidence = refreshed
        update_fields.append("evidence")
    if int(watermark or 0) > int(review.watermark_message_id or 0):
        review.watermark_message_id = int(watermark)
        update_fields.append("watermark_message_id")
    if update_fields:
        update_fields.append("updated_at")
        review.save(update_fields=update_fields)


def create_payment_review(client, *, watermark: int = 0, messages=None):
    """Persist one review per payment claim before any costly media enrichment."""
    if not client or client.hidden_at:
        return None
    from management.ig_bot_models import IgCommercialEpisode
    from management.models import InstagramBotMessage

    episode_floor = int(
        IgCommercialEpisode.objects.filter(
            client_id=client.pk,
            open_slot=1,
        ).values_list("opened_watermark_message_id", flat=True).first()
        or 0
    )
    if messages is None:
        message_query = InstagramBotMessage.objects.filter(client_id=client.pk)
        if episode_floor:
            message_query = message_query.filter(pk__gte=episode_floor)
        rows = list(message_query.order_by("-id")[:80])
        rows.reverse()
        messages = [{
            "id": row.pk,
            "mid": row.mid,
            "role": row.role,
            "text": row.text,
            "attachments": row.attachments,
            "attachment_media": row.attachment_media,
            "source": row.source,
            "media_capture_eligible": bool(row.media_capture_eligible),
            "created_at": row.created_at.isoformat(),
        } for row in rows]
    elif episode_floor:
        scoped_messages = []
        for message in messages:
            raw_id = (
                message.get("id")
                if isinstance(message, dict)
                else getattr(message, "pk", None) or getattr(message, "id", None)
            )
            try:
                message_id = int(raw_id or 0)
            except (TypeError, ValueError):
                message_id = 0
            if message_id >= episode_floor:
                scoped_messages.append(message)
        messages = scoped_messages
    messages = _augment_messages_with_raw_media(client, messages)
    extracted = extract_payment_review_evidence(messages)
    if not extracted["needs_review"]:
        return None
    watermark = int(watermark or max(extracted["message_ids"] or [0]))
    claim_anchor = payment_review_claim_anchor(extracted)
    if not claim_anchor:
        return None

    # This lock serializes concurrent webhook/reconcile workers for one
    # customer, so only the claimant below may cross the vision boundary.
    from management.services.ig_commercial_episodes import commercial_episode_client_lock

    with commercial_episode_client_lock(client.pk):
        review, created = _claim_payment_review(
            client,
            extracted=extracted,
            watermark=watermark,
            claim_anchor=claim_anchor,
        )
        current_evidence = review.evidence if isinstance(review.evidence, dict) else {}
        if not created:
            # A terminal review is an audited manager decision about this exact
            # claim. A real resubmission has a new source message and therefore
            # a new claim anchor; replaying this anchor must not reopen it or
            # cross the media/vision boundary again.
            if (
                review.status != review.Status.PENDING
                or (
                    current_evidence.get("media_audit_v3")
                    and not _pending_review_has_new_receipt_evidence(review, extracted)
                    and not _review_media_needs_owned_retry(current_evidence)
                )
            ):
                _refresh_pending_review_context(review, extracted, watermark=watermark)
                return review

        resolved_media = _resolve_payment_media_candidates(extracted.get("media") or [])
        enriched_media = _persist_review_media(resolved_media)
        for item in enriched_media:
            item["message_id"] = item.get("message_id") or None
        for container in [
            *(extracted.get("order_draft", {}).get("context_messages", []) or []),
            *(extracted.get("evidence", []) or []),
        ]:
            context_media = container.get("media") or [] if isinstance(container, dict) else []
            for context_item in context_media:
                for item in enriched_media:
                    if item.get("url") == context_item.get("url"):
                        context_item.update(item)
        _reconcile_payment_evidence_after_media_resolution(extracted, enriched_media)
        if not extracted["needs_review"]:
            review.status = review.Status.CANCELLED
            review.cancellation_reason = "automated_media_not_payment"
            review.cancelled_at = timezone.now()
            review.save(update_fields=["status", "cancellation_reason", "cancelled_at", "updated_at"])
            return None
        extracted["media"] = enriched_media
        extracted["order_draft"]["media"] = enriched_media
        catalog_matches = _catalog_matches_for_media(enriched_media)
        for media_item in enriched_media:
            bound_matches = media_item.get("catalog_matches") if isinstance(media_item.get("catalog_matches"), list) else []
            if bound_matches:
                media_item["product_id"] = ",".join(str(match.get("product_id")) for match in bound_matches if match.get("product_id"))
                media_item["product_title"] = " / ".join(str(match.get("title")) for match in bound_matches if match.get("title"))
                media_item["product_url"] = "\n".join(str(match.get("url")) for match in bound_matches if match.get("url"))
                media_item["confidence"] = ",".join(str(match.get("confidence")) for match in bound_matches if match.get("confidence") is not None)
        extracted["catalog_matches"] = catalog_matches
        extracted["catalog_match"] = catalog_matches[0] if catalog_matches else {}
        _apply_catalog_matches_to_draft(extracted["order_draft"], catalog_matches)
        _apply_validated_conversation_price_to_draft(extracted["order_draft"], messages, catalog_matches)
        deal = _select_review_deal(client, catalog_matches)
        review.evidence = {
            # Keep the original claim identity: later receipt evidence belongs
            # to this pending episode review and must not mutate its key.
            "claim_anchor": current_evidence.get("claim_anchor") or claim_anchor,
            "messages": extracted["evidence"],
            "amount_evidence": extracted["amount_evidence"],
            "order_draft": extracted["order_draft"],
            "media": enriched_media,
            "catalog_match": extracted.get("catalog_match", {}),
            "catalog_matches": extracted.get("catalog_matches", []),
            "media_audit_v3": True,
            "deal": _deal_payload(deal),
        }
        review.deal = deal
        review.watermark_message_id = max(int(review.watermark_message_id or 0), watermark)
        review.save(update_fields=["evidence", "deal", "watermark_message_id", "updated_at"])

        from management.services.instagram_bot import notify_manager

        notify_manager(
            _alert_text(review, client),
            dedupe_key=review.dedupe_key,
            event_type="payment_review",
            client=client,
            reply_markup=_review_keyboard(review),
            metadata={
                "payment_candidate": payment_confirmation_candidate(review),
                "requires_human_review": True,
            },
        )
        return review


def _decision_stage_after(client, decision: str, verification_scope: str = "") -> str:
    from management.ig_bot_models import IgClient, IgPaymentReviewDecision

    if (
        decision == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED
        and verification_scope in {
            IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
            IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
        }
    ):
        return IgClient.Stage.PAID
    if decision == IgPaymentReviewDecision.Decision.MANAGER_REJECTED:
        return IgClient.Stage.CHECKOUT
    return getattr(client, "stage", "") or IgClient.Stage.CHECKOUT


def _positive_money(value) -> Decimal | None:
    try:
        raw = Decimal(str(value))
        if not raw.is_finite() or raw.as_tuple().exponent < -2:
            return None
        amount = raw.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if Decimal("0.00") < amount <= Decimal("9999999999.99") else None


def resolve_review_payment_amount(
    review,
    *,
    verification_scope: str = "",
    confirmed_amount=None,
    order_total_amount=None,
) -> dict:
    """Resolve exact payment money from this review/deal only.

    The function never reads another client or a previous episode. Explicit
    manager input wins, while server evidence remains attached for audit.
    """
    from management.ig_bot_models import IgPaymentReviewDecision

    evidence = review.evidence if isinstance(getattr(review, "evidence", None), dict) else {}
    draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
    deal = getattr(review, "deal", None)
    deal_total = _positive_money(getattr(deal, "amount", None))
    review_total = _positive_money(draft.get("quoted_total"))
    if deal_total is not None and review_total is not None and deal_total != review_total:
        raise ValueError(
            "Сума угоди та узгоджена сума з переписки суперечать одна одній; потрібна ручна перевірка."
        )
    negotiated_total = review_total or deal_total
    supplied_total = None
    if order_total_amount not in (None, ""):
        supplied_total = _positive_money(order_total_amount)
        if supplied_total is None:
            raise ValueError("Повна вартість замовлення має бути додатним числом до 2 знаків.")
        if negotiated_total is not None and supplied_total != negotiated_total:
            raise ValueError(
                "Вказана повна вартість замовлення не збігається з узгодженою сумою."
            )
    if negotiated_total is None and supplied_total is not None:
        negotiated_total = supplied_total
        negotiated_total_source = "manager_input"
    else:
        negotiated_total_source = (
            "conversation_quoted_total" if review_total is not None
            else "deal_negotiated_total" if deal_total is not None
            else "unknown"
        )
    currency = str(
        getattr(deal, "currency", "") or draft.get("currency") or "UAH"
    ).strip().upper()[:8] or "UAH"

    inferred_scope = str(verification_scope or "").strip()
    evidence_amount = None
    evidence_source = ""
    evidence_ids = []
    if deal is not None:
        if deal.pay_type in {deal.PayType.PREPAYMENT, deal.PayType.PREPAY_200}:
            inferred_scope = inferred_scope or IgPaymentReviewDecision.VerificationScope.PREPAYMENT
        elif deal.pay_type == deal.PayType.ONLINE_FULL:
            inferred_scope = inferred_scope or IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT
        evidence_amount = _positive_money(deal.payable_amount())
        if evidence_amount:
            evidence_source = "deal_requested_amount"
            evidence_ids = [
                int(value)
                for value in (getattr(deal, "requested_payment_evidence_ids", None) or [])
                if str(value).isdigit()
            ]

    if evidence_amount is None:
        payment_rows = []
        for row in evidence.get("amount_evidence") or []:
            if not isinstance(row, dict) or row.get("kind") != "payment_evidence":
                continue
            amount = _positive_money(row.get("amount"))
            if amount:
                payment_rows.append((amount, row.get("message_id")))
        unique_amounts = {amount for amount, _message_id in payment_rows}
        if len(unique_amounts) == 1:
            evidence_amount = next(iter(unique_amounts))
            evidence_source = "review_payment_evidence"
            evidence_ids = sorted({
                int(message_id)
                for _amount, message_id in payment_rows
                if str(message_id).isdigit()
            })

    if (
        evidence_amount is None
        and negotiated_total is not None
        and inferred_scope not in {
            IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
            IgPaymentReviewDecision.VerificationScope.PAYMENT_CLAIM,
        }
    ):
        evidence_amount = negotiated_total
        evidence_source = "review_quoted_total"
        message_id = draft.get("amount_source_message_id")
        evidence_ids = [int(message_id)] if str(message_id).isdigit() else []

    exact_amount = _positive_money(confirmed_amount) if confirmed_amount not in (None, "") else evidence_amount
    if exact_amount is None:
        raise ValueError("Сума підтвердженого платежу не визначена; відкрийте перевірку та вкажіть її вручну.")
    if not inferred_scope:
        if negotiated_total is not None and exact_amount == negotiated_total:
            inferred_scope = IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT
        elif negotiated_total is not None and exact_amount < negotiated_total:
            inferred_scope = IgPaymentReviewDecision.VerificationScope.PREPAYMENT
        else:
            inferred_scope = IgPaymentReviewDecision.VerificationScope.PAYMENT_CLAIM
    if (
        inferred_scope in {
            IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
            IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
        }
        and negotiated_total is None
    ):
        raise ValueError(
            "Повна вартість замовлення не визначена; підтвердження не може авторизувати виконання."
        )
    if (
        inferred_scope == IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT
        and negotiated_total is not None
        and exact_amount != negotiated_total
    ):
        raise ValueError("Підтверджена сума повної оплати не збігається з повною вартістю замовлення.")
    if negotiated_total is not None and exact_amount > negotiated_total:
        raise ValueError("Підтверджена сума не може перевищувати повну вартість замовлення.")
    if (
        inferred_scope == IgPaymentReviewDecision.VerificationScope.PREPAYMENT
        and negotiated_total is not None
        and exact_amount >= negotiated_total
    ):
        raise ValueError("Передоплата має бути меншою за повну вартість замовлення.")
    if confirmed_amount not in (None, ""):
        # Explicit manager input may differ from the requested amount. Attach
        # only messages that contain this exact value; a receipt watermark or
        # a different payment request must never become amount evidence.
        exact_evidence_ids = set()
        if negotiated_total is not None and exact_amount == negotiated_total:
            source_message_id = draft.get("amount_source_message_id")
            if str(source_message_id).isdigit():
                exact_evidence_ids.add(int(source_message_id))
        if deal is not None and _positive_money(getattr(deal, "requested_payment_amount", None)) == exact_amount:
            exact_evidence_ids.update(
                int(value)
                for value in (getattr(deal, "requested_payment_evidence_ids", None) or [])
                if str(value).isdigit()
            )
        for row in evidence.get("amount_evidence") or []:
            if not isinstance(row, dict) or _positive_money(row.get("amount")) != exact_amount:
                continue
            message_id = row.get("message_id")
            if str(message_id).isdigit():
                exact_evidence_ids.add(int(message_id))
        evidence_ids = sorted(exact_evidence_ids)
    contract = {
        "amount": exact_amount,
        "currency": currency,
        "scope": inferred_scope,
        "source": "manager_input" if confirmed_amount not in (None, "") else evidence_source,
        "evidence_message_ids": evidence_ids,
        "order_total": negotiated_total,
        "order_total_source": negotiated_total_source,
        "requested_amount": evidence_amount,
    }
    digest_payload = {
        "review_id": int(getattr(review, "pk", 0) or 0),
        "watermark": int(getattr(review, "watermark_message_id", 0) or 0),
        "amount": f"{exact_amount:.2f}",
        "currency": currency,
        "scope": inferred_scope,
        "source": contract["source"],
        "evidence_message_ids": evidence_ids,
        "order_total": f"{negotiated_total:.2f}" if negotiated_total is not None else "",
        "order_total_source": negotiated_total_source,
        "requested_amount": f"{evidence_amount:.2f}" if evidence_amount is not None else "",
    }
    contract["digest"] = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return contract


def payment_confirmation_candidate(review) -> dict:
    """JSON-safe immutable candidate shared by web UI and Telegram."""
    try:
        contract = resolve_review_payment_amount(review)
    except ValueError:
        return {}
    return {
        "amount": f"{contract['amount']:.2f}",
        "currency": contract["currency"],
        "scope": contract["scope"],
        "source": contract["source"],
        "evidence_message_ids": contract["evidence_message_ids"],
        "order_total": (
            f"{contract['order_total']:.2f}"
            if contract["order_total"] is not None
            else ""
        ),
        "requested_amount": (
            f"{contract['requested_amount']:.2f}"
            if contract["requested_amount"] is not None
            else ""
        ),
        "digest": contract["digest"],
    }


def record_review_decision(
    review,
    *,
    actor,
    decision: str,
    verification_scope: str = "",
    confirmed_amount=None,
    order_total_amount=None,
    reason_code: str = "",
    reason_text: str = "",
    telegram_decision: dict | None = None,
    allow_amount_clarification: bool = False,
):
    """Atomically record one manager decision without mutating provider truth."""
    from management.ig_bot_models import (
        IgClientStageEvent,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )

    decision = str(decision or "").strip()
    verification_scope = str(verification_scope or "").strip()
    reason_code = str(reason_code or "").strip()
    reason_text = str(reason_text or "").strip()
    telegram_decision = telegram_decision if isinstance(telegram_decision, dict) else {}
    allowed = {choice for choice, _label in IgPaymentReviewDecision.Decision.choices}
    if decision not in allowed:
        raise ValueError("Невідоме рішення щодо перевірки оплати.")
    if decision == IgPaymentReviewDecision.Decision.MANAGER_REJECTED and not reason_code:
        raise ValueError("Код причини відхилення обов'язковий")
    allowed_scopes = {
        choice for choice, _label in IgPaymentReviewDecision.VerificationScope.choices
    }
    if verification_scope and verification_scope not in allowed_scopes:
        raise ValueError("Обсяг перевірки оплати не підтримується")

    telegram_actor_id = str(telegram_decision.get("telegram_user_id") or "").strip()
    if telegram_actor_id:
        actor_source = IgPaymentReviewDecision.ActorSource.TELEGRAM_USER
        actor_external_id = telegram_actor_id
        actor_label = str(
            telegram_decision.get("telegram_username") or actor_external_id
        ).strip()
    elif actor is not None and getattr(actor, "pk", None):
        actor_source = IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER
        actor_external_id = str(actor.pk)
        actor_label = str(getattr(actor, "get_username", lambda: "")() or actor.pk)
    else:
        raise ValueError("Автор рішення не визначений")

    with transaction.atomic():
        locked = (
            IgPaymentConfirmationReview.objects.select_for_update()
            .select_related("client", "deal")
            .get(pk=review.pk)
        )
        locked._transitioned = False
        if locked.client.hidden_at:
            raise ValueError("Прихований клієнт виключений з операцій.")
        clarification = False
        if locked.status != IgPaymentConfirmationReview.Status.PENDING:
            if not IgPaymentReviewDecision.objects.filter(review=locked).exists():
                raise ValueError("Завершена перевірка не має журналу рішення")
            if allow_amount_clarification and locked.order_id:
                raise ValueError(
                    "Суму не можна уточнити: до перевірки замовлення вже прив’язано."
                )
            latest = IgPaymentReviewDecision.objects.filter(review=locked).order_by("-id").first()
            has_authoritative_amount = IgPaymentReviewDecision.objects.filter(
                review=locked,
                decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
                verification_source="manager",
                confirmed_amount__gt=0,
                verification_scope__in={
                    IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
                    IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
                },
            ).exists()
            clarification = bool(
                allow_amount_clarification
                and locked.status == IgPaymentConfirmationReview.Status.CONFIRMED
                and decision == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED
                and latest
                and latest.decision == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED
                and not has_authoritative_amount
            )
            if not clarification:
                if allow_amount_clarification and has_authoritative_amount:
                    raise ValueError("Перевірка вже містить точну суму підтвердженої оплати.")
                return locked

        amount_contract = None
        if decision == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED:
            amount_contract = resolve_review_payment_amount(
                locked,
                verification_scope=verification_scope,
                confirmed_amount=confirmed_amount,
                order_total_amount=order_total_amount,
            )
            verification_scope = amount_contract["scope"]
        if not verification_scope:
            if locked.deal_id and locked.deal.pay_type in {
                locked.deal.PayType.PREPAYMENT,
                locked.deal.PayType.PREPAY_200,
            }:
                verification_scope = IgPaymentReviewDecision.VerificationScope.PREPAYMENT
            elif locked.deal_id and locked.deal.pay_type == locked.deal.PayType.ONLINE_FULL:
                verification_scope = IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT
            else:
                verification_scope = IgPaymentReviewDecision.VerificationScope.PAYMENT_CLAIM

        stage_before = locked.client.stage
        stage_after = _decision_stage_after(
            locked.client,
            decision,
            verification_scope,
        )
        review_status_before = locked.status
        now = timezone.now()
        update_fields = ["updated_at"]
        if decision == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED and not clarification:
            locked.status = IgPaymentConfirmationReview.Status.CONFIRMED
            locked.confirmed_by = actor
            locked.confirmed_at = now
            update_fields.extend(["status", "confirmed_by", "confirmed_at"])
        elif decision == IgPaymentReviewDecision.Decision.MANAGER_REJECTED:
            locked.status = IgPaymentConfirmationReview.Status.CANCELLED
            locked.cancelled_by = actor
            locked.cancelled_at = now
            locked.cancellation_reason = (reason_text or reason_code or "")[:500]
            update_fields.extend(["status", "cancelled_by", "cancelled_at", "cancellation_reason"])
        if telegram_decision:
            evidence = locked.evidence if isinstance(locked.evidence, dict) else {}
            locked.evidence = {**evidence, "telegram_decision": telegram_decision}
            update_fields.append("evidence")
        locked.save(update_fields=update_fields)
        decision_row = IgPaymentReviewDecision.objects.create(
            review=locked,
            client=locked.client,
            decision=decision,
            verification_source="manager",
            verification_scope=verification_scope,
            confirmed_amount=(amount_contract or {}).get("amount"),
            order_total_amount=(amount_contract or {}).get("order_total"),
            order_total_source=(amount_contract or {}).get("order_total_source", ""),
            currency=(amount_contract or {}).get("currency", "UAH"),
            amount_source=(amount_contract or {}).get("source", ""),
            amount_evidence_message_ids=(amount_contract or {}).get("evidence_message_ids", []),
            reason_code=reason_code[:64],
            reason_text=(reason_text or reason_code)[:500],
            evidence_watermark_message_id=locked.watermark_message_id or 0,
            review_status_before=review_status_before,
            review_status_after=locked.status,
            stage_before=stage_before or "",
            stage_after=stage_after or "",
            actor=actor,
            actor_source=actor_source,
            actor_external_id=actor_external_id[:128],
            actor_label=actor_label[:150],
            telegram_decision=telegram_decision,
        )
        if stage_after and stage_after != stage_before:
            locked.client.stage = stage_after
            locked.client.stage_updated_at = now
            locked.client.save(update_fields=["stage", "stage_updated_at", "updated_at"])
            IgClientStageEvent.objects.create(
                client=locked.client,
                from_stage=stage_before or "",
                to_stage=stage_after,
                reason=f"payment_review_{decision}",
            )
        from management.services.ig_commercial_episodes import sync_episode_payment

        sync_episode_payment(review=locked, deal=locked.deal if locked.deal_id else None)
        from management.services.bot_conversation_analysis import schedule_client_truth_analysis

        def _schedule_truth_analysis():
            try:
                schedule_client_truth_analysis(locked.client, trigger="manager_payment_decision")
            except Exception:
                logger.exception(
                    "manager_payment_truth_analysis_enqueue_failed review_id=%s client_id=%s",
                    locked.pk,
                    locked.client_id,
                )

        transaction.on_commit(_schedule_truth_analysis)
        locked._transitioned = True
    return locked


@transaction.atomic
def archive_historical_paid_review(
    review,
    *,
    actor,
    reason: str,
    outcome: str | None = None,
    transition_to_done: bool = False,
    preserve_client_stage: bool = False,
):
    """Close a verified legacy sale without fabricating a local Order."""
    from management.ig_bot_models import (
        IgBotNotification,
        IgClient,
        IgCommercialEpisode,
        IgDeal,
        IgClientStageEvent,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )
    from management.services.ig_commercial_episodes import (
        append_episode_event,
        ensure_episode_for_review,
    )

    note = str(reason or "").strip()
    if not note:
        raise ValueError("Причина архівування старого продажу обов'язкова.")
    if not actor or not getattr(actor, "pk", None) or not (
        getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)
    ):
        raise ValueError("Архівувати старий продаж може лише менеджер.")
    resolved_outcome = str(outcome or "").strip()
    if resolved_outcome:
        allowed_outcomes = {
            choice for choice, _label in IgPaymentConfirmationReview.ResolutionOutcome.choices
        }
        if resolved_outcome not in allowed_outcomes:
            raise ValueError("Результат історичного завершення не підтримується.")

    locked = (
        IgPaymentConfirmationReview.objects.select_for_update()
        .select_related("client")
        .get(pk=review.pk)
    )
    client = IgClient.objects.select_for_update().get(pk=locked.client_id)
    if client.hidden_at:
        raise ValueError("Прихований клієнт виключений з операцій.")
    if locked.resolution_kind == locked.ResolutionKind.HISTORICAL_PAID_ARCHIVED:
        if not resolved_outcome or locked.resolution_outcome == resolved_outcome:
            return locked
        raise ValueError("Історичний продаж уже завершено іншим результатом.")
    if locked.deal_id and IgDeal.objects.select_for_update().filter(
        pk=locked.deal_id,
        active_checkout_proposal__isnull=False,
    ).exists():
        raise ValueError(
            "Не можна архівувати продаж, поки угода має активну checkout-пропозицію."
        )
    if locked.status != locked.Status.CONFIRMED:
        raise ValueError("Спочатку підтвердьте оплату менеджерським рішенням.")
    if locked.order_id:
        raise ValueError("Перевірка вже має пов'язане замовлення і не є історичною.")

    decision_scopes = [IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT]
    if resolved_outcome:
        decision_scopes = [IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED]
    decision = (
        IgPaymentReviewDecision.objects.filter(
            review=locked,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_source="manager",
            verification_scope__in=decision_scopes,
            actor_source__in=(
                IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
                IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
            ),
        )
        .order_by("-id")
        .first()
    )
    if decision is None or (not resolved_outcome and decision.confirmed_amount is None):
        raise ValueError(
            "Потрібне точне manager-verified рішення про повну оплату."
        )

    now = timezone.now()
    locked.resolution_kind = locked.ResolutionKind.HISTORICAL_PAID_ARCHIVED
    if resolved_outcome:
        locked.resolution_outcome = resolved_outcome
    locked.resolution_note = note[:2000]
    locked.resolved_at = now
    locked.resolved_by = actor
    locked.save(
        update_fields=[
            "resolution_kind",
            "resolution_outcome",
            "resolution_note",
            "resolved_at",
            "resolved_by",
            "updated_at",
        ]
    )

    episode = ensure_episode_for_review(locked)
    episode = IgCommercialEpisode.objects.select_for_update().get(pk=episode.pk)
    previous_state = episode.state
    episode.state = IgCommercialEpisode.State.FULFILLED
    episode.outcome = "historical_paid_archived"
    episode.open_slot = None
    episode.closed_at = now
    episode.save(
        update_fields=["state", "outcome", "open_slot", "closed_at", "updated_at"]
    )
    append_episode_event(
        episode,
        dedupe_key=f"episode:{episode.pk}:historical-paid-archived:{locked.pk}",
        event_type="historical_paid_archived",
        from_state=previous_state,
        to_state=episode.state,
        stage=IgClient.Stage.PAID,
        source="manager_resolution",
        evidence={
            "review_id": locked.pk,
            "decision_id": decision.pk,
            "confirmed_amount": (
                str(decision.confirmed_amount)
                if decision.confirmed_amount is not None
                else None
            ),
            "resolution_outcome": resolved_outcome,
        },
    )

    preserve_client_stage = preserve_client_stage or (
        IgCommercialEpisode.objects.select_for_update()
        .filter(client_id=client.pk, open_slot=1)
        .exclude(pk=episode.pk)
        .exists()
    )
    if not preserve_client_stage:
        stage_before = client.stage
        client.stage = IgClient.Stage.DONE if transition_to_done else IgClient.Stage.PAID
        client.stage_updated_at = now
        if client.current_commercial_episode_id == episode.pk:
            client.current_commercial_episode = None
        client.save(
            update_fields=[
                "stage",
                "stage_updated_at",
                "current_commercial_episode",
                "updated_at",
            ]
        )
        if stage_before != client.stage:
            # Э3.2: событие пишется при ЛЮБОМ изменении стадии, а не только при
            # переходе в DONE. Раньше переход в PAID оставался без evidence.
            stage_event = IgClientStageEvent.objects.create(
                client=client,
                from_stage=stage_before or "",
                to_stage=client.stage,
                reason=(
                    "historical_payment_review_completed"
                    if transition_to_done
                    else "historical_payment_review_paid"
                ),
            )
            from management.services.ig_commercial_episodes import append_client_stage_transition_event
            append_client_stage_transition_event(
                client, stage_event_id=stage_event.pk, from_stage=stage_before or "",
                to_stage=client.stage,
                reason=(
                    "historical_payment_review_completed"
                    if transition_to_done else "historical_payment_review_paid"
                ),
                episode_id=episode.pk,
            )
    IgBotNotification.objects.filter(
        dedupe_key=locked.dedupe_key,
        event_type="payment_review",
    ).exclude(
        status__in=[
            IgBotNotification.Status.UNKNOWN,
            IgBotNotification.Status.DEAD_LETTER,
        ]
    ).update(
        status=IgBotNotification.Status.RESOLVED,
        next_attempt_at=None,
        last_error="",
        failure_kind="",
        updated_at=now,
    )
    return locked


@transaction.atomic
def correct_false_historical_purchase(review, *, actor, reason: str):
    """Auditably retract a false legacy-purchase classification.

    The original manager decision and episode event remain append-only audit
    evidence.  Only the current projection is superseded; no order, payment,
    provider event, customer message, or active funnel is created.
    """
    from management.ig_bot_models import (
        IgClient,
        IgCommercialEpisode,
        IgCommercialEpisodeEvent,
        IgClientStageEvent,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )
    from management.services.bot_payment_truth import (
        recalculate_client_payment_aggregates,
        verified_payment_deals,
    )
    from management.services.ig_commercial_episodes import (
        FALSE_HISTORICAL_PURCHASE_REASON,
        append_episode_event,
        derive_current_episode_stage,
        ensure_open_episode_for_locked_client,
        payment_truth_snapshot,
    )

    note = str(reason or "").strip()
    if not note:
        raise ValueError("Причина виправлення історичної покупки обов'язкова.")
    if not actor or not getattr(actor, "pk", None) or not (
        getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)
    ):
        raise ValueError("Виправити історичну покупку може лише менеджер.")

    locked = (
        IgPaymentConfirmationReview.objects.select_for_update()
        .select_related("client", "deal", "order")
        .get(pk=review.pk)
    )
    client = IgClient.objects.select_for_update().get(pk=locked.client_id)
    previous_retraction = (
        IgPaymentReviewDecision.objects.filter(
            review=locked,
            decision=IgPaymentReviewDecision.Decision.MANAGER_REJECTED,
            reason_code=FALSE_HISTORICAL_PURCHASE_REASON,
        )
        .order_by("-id")
        .first()
    )
    already_corrected = bool(
        locked.status == IgPaymentConfirmationReview.Status.SUPERSEDED
        and previous_retraction is not None
    )
    if not already_corrected and (
        locked.status != IgPaymentConfirmationReview.Status.CONFIRMED
        or locked.resolution_kind
        != IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
    ):
        raise ValueError("Виправлення доступне лише для підтвердженої історичної покупки.")
    if locked.order_id:
        raise ValueError("Не можна виправити історичну покупку з прив'язаним замовленням.")
    if locked.deal_id and verified_payment_deals(
        locked.client.deals.filter(pk=locked.deal_id)
    ).exists():
        raise ValueError("Не можна скасувати історичну покупку з provider-підтвердженою оплатою.")

    episode_ids = set(
        IgCommercialEpisode.objects.select_for_update()
        .filter(client_id=client.pk, primary_payment_review_id=locked.pk)
        .values_list("pk", flat=True)
    )
    correction_events = list(
        IgCommercialEpisodeEvent.objects.filter(
            episode__client_id=client.pk,
            event_type="historical_purchase_corrected",
        ).values("episode_id", "evidence")
    )
    for row in correction_events:
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            continue
        try:
            event_review_id = int(evidence.get("review_id") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if event_review_id == locked.pk:
            episode_ids.add(int(row["episode_id"]))
    episodes = list(
        IgCommercialEpisode.objects.select_for_update()
        .select_related("deal", "intended_order")
        .filter(client_id=client.pk, pk__in=episode_ids)
        .order_by("id")
    )
    for episode in episodes:
        if episode.intended_order_id:
            raise ValueError("Не можна виправити історичну покупку з епізодом замовлення.")
        if episode.deal_id and verified_payment_deals(
            client.deals.filter(pk=episode.deal_id)
        ).exists():
            raise ValueError("Епізод має provider-підтверджену оплату.")

    now = timezone.now()
    if already_corrected:
        decision = previous_retraction
        correction_at = locked.superseded_at or decision.created_at or now
    else:
        decision = IgPaymentReviewDecision.objects.create(
            review=locked,
            client=client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_REJECTED,
            verification_source="manager",
            verification_scope=IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED,
            confirmed_amount=None,
            order_total_amount=None,
            order_total_source="",
            currency=str(getattr(locked.deal, "currency", "") or "UAH")[:8],
            amount_source="owner_correction",
            amount_evidence_message_ids=[],
            reason_code=FALSE_HISTORICAL_PURCHASE_REASON,
            reason_text=note[:500],
            evidence_watermark_message_id=locked.watermark_message_id or 0,
            review_status_before=IgPaymentConfirmationReview.Status.CONFIRMED,
            review_status_after=IgPaymentConfirmationReview.Status.SUPERSEDED,
            stage_before=client.stage or "",
            stage_after=client.stage or "",
            actor=actor,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(actor.pk)[:128],
            actor_label=str(
                getattr(actor, "get_username", lambda: "")() or actor.pk
            )[:150],
        )
        correction_at = now
        locked.status = IgPaymentConfirmationReview.Status.SUPERSEDED
        locked.superseded_at = correction_at
        locked.supersede_reason = FALSE_HISTORICAL_PURCHASE_REASON
        locked.resolution_kind = IgPaymentConfirmationReview.ResolutionKind.NONE
        locked.resolution_outcome = None
        locked.resolution_note = f"Correction: {note}"[:2000]
        locked.resolved_at = correction_at
        locked.resolved_by = actor
        locked.save(
            update_fields=[
                "status",
                "superseded_at",
                "supersede_reason",
                "resolution_kind",
                "resolution_outcome",
                "resolution_note",
                "resolved_at",
                "resolved_by",
                "updated_at",
            ]
        )

    for episode in episodes:
        previous_state = episode.state
        episode.primary_payment_review = None
        episode.state = IgCommercialEpisode.State.CANCELLED
        episode.outcome = "historical_purchase_corrected"
        episode.open_slot = None
        episode.closed_at = correction_at
        episode.payment_snapshot = payment_truth_snapshot(
            episode=episode,
            review=None,
            allow_deal_review_fallback=False,
        )
        episode.save(
            update_fields=[
                "primary_payment_review",
                "state",
                "outcome",
                "open_slot",
                "closed_at",
                "payment_snapshot",
                "updated_at",
            ]
        )
        append_episode_event(
            episode,
            dedupe_key=f"episode:{episode.pk}:historical-purchase-corrected:{locked.pk}",
            event_type="historical_purchase_corrected",
            from_state=previous_state,
            to_state=episode.state,
            stage=client.stage,
            source="manager_correction",
            evidence={
                "review_id": locked.pk,
                "decision_id": decision.pk,
                "reason_code": FALSE_HISTORICAL_PURCHASE_REASON,
            },
        )

    recalculate_client_payment_aggregates(client)
    client.refresh_from_db()

    corrected_ids = {episode.pk for episode in episodes}
    current = (
        IgCommercialEpisode.objects.select_for_update()
        .select_related("deal", "deal__order", "intended_order")
        .filter(
            client_id=client.pk,
            open_slot=1,
            state=IgCommercialEpisode.State.ACTIVE,
        )
        .exclude(pk__in=corrected_ids)
        .order_by("-sequence", "-id")
        .first()
    )
    if current is None and client.current_commercial_episode_id not in corrected_ids:
        current = (
            IgCommercialEpisode.objects.select_for_update()
            .select_related("deal", "deal__order", "intended_order")
            .filter(
                pk=client.current_commercial_episode_id,
                client_id=client.pk,
                state=IgCommercialEpisode.State.ACTIVE,
            )
            .first()
        )

    latest_stage_event = (
        IgClientStageEvent.objects.select_for_update()
        .filter(client_id=client.pk)
        .order_by("-id")
        .first()
    )
    poisoned_stage_reasons = {
        "historical_payment_review_completed",
        "historical_payment_review_paid",
        FALSE_HISTORICAL_PURCHASE_REASON,
    }
    stage_was_poisoned = bool(
        latest_stage_event
        and latest_stage_event.reason in poisoned_stage_reasons
        and latest_stage_event.to_stage == client.stage
    )

    if current is None and client.current_commercial_episode_id in corrected_ids:
        client.current_commercial_episode = None
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        if stage_was_poisoned and client.stage != IgClient.Stage.NEW:
            client.set_stage(
                IgClient.Stage.NEW,
                reason=FALSE_HISTORICAL_PURCHASE_REASON,
            )
        current = ensure_open_episode_for_locked_client(
            client,
            materialization_prefix="ig-false-history-correction",
        )
    elif current is not None and client.current_commercial_episode_id != current.pk:
        client.current_commercial_episode = current
        client.save(update_fields=["current_commercial_episode", "updated_at"])

    if current is not None and stage_was_poisoned:
        target_stage = derive_current_episode_stage(client, current)
        if target_stage != client.stage:
            client.set_stage(
                target_stage,
                reason=FALSE_HISTORICAL_PURCHASE_REASON,
            )
    return locked


@transaction.atomic
def resolve_historical_paid_review(
    review,
    *,
    actor,
    outcome: str,
    reason: str,
    confirmed_amount=None,
    amount_unrecoverable: bool = False,
):
    """Audit a legacy sale already completed outside the current order flow.

    This operation intentionally has no order, provider, customer-message, or
    purchase side effects. A historical amount is evidence of payment only;
    it never becomes an invented negotiated order total.
    """
    from management.ig_bot_models import (
        IgClient,
        IgCommercialEpisode,
        IgDeal,
        IgPaymentConfirmationReview,
        IgPaymentProjection,
        IgPaymentReviewDecision,
    )
    from management.services.ig_commercial_episodes import sync_episode_payment

    note = str(reason or "").strip()
    if not note:
        raise ValueError("Причина завершення старого продажу обов'язкова.")
    if not actor or not getattr(actor, "pk", None) or not (
        getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)
    ):
        raise ValueError("Завершити старий продаж може лише менеджер.")
    resolved_outcome = str(outcome or "").strip()
    allowed_outcomes = {
        choice for choice, _label in IgPaymentConfirmationReview.ResolutionOutcome.choices
    }
    if resolved_outcome not in allowed_outcomes:
        raise ValueError("Результат історичного завершення не підтримується.")

    exact_amount = _positive_money(confirmed_amount)
    if confirmed_amount not in (None, "") and exact_amount is None:
        raise ValueError("Підтверджена історична сума має бути додатним числом до 2 знаків.")
    if exact_amount is None and not amount_unrecoverable:
        raise ValueError("Вкажіть точну суму або позначте, що її неможливо відновити.")
    if exact_amount is not None and amount_unrecoverable:
        raise ValueError("Оберіть точну суму або неможливість її відновити, але не обидва варіанти.")

    locked = (
        IgPaymentConfirmationReview.objects.select_for_update()
        .select_related("client", "deal")
        .get(pk=review.pk)
    )
    client = IgClient.objects.select_for_update().get(pk=locked.client_id)
    if client.hidden_at:
        raise ValueError("Прихований клієнт виключений з операцій.")
    if locked.resolution_kind == locked.ResolutionKind.HISTORICAL_PAID_ARCHIVED:
        existing = (
            IgPaymentReviewDecision.objects.filter(
                review=locked,
                decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
                verification_scope=IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED,
            )
            .order_by("-id")
            .first()
        )
        if (
            existing is not None
            and locked.resolution_outcome == resolved_outcome
            and locked.resolution_note == note[:2000]
            and existing.confirmed_amount == exact_amount
        ):
            return locked
        raise ValueError("Історичний продаж уже завершено іншим результатом або даними.")
    if locked.order_id:
        raise ValueError("Перевірка вже має пов'язане замовлення і не є історичною.")
    if locked.deal_id and IgDeal.objects.select_for_update().filter(
        pk=locked.deal_id,
        active_checkout_proposal__isnull=False,
    ).exists():
        raise ValueError(
            "Не можна архівувати продаж, поки угода має активну checkout-пропозицію."
        )
    if locked.deal_id and IgPaymentProjection.objects.select_for_update().filter(
        deal_id=locked.deal_id,
        truth__in={
            IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
            IgDeal.PaymentTruth.REFUNDED,
            IgDeal.PaymentTruth.REVERSED,
            IgDeal.PaymentTruth.FAILED,
            IgDeal.PaymentTruth.CANCELLED,
        },
    ).exists():
        raise ValueError("Історичне завершення заблоковано конфліктом або скасуванням provider-оплати.")
    if locked.status != IgPaymentConfirmationReview.Status.PENDING:
        raise ValueError("Історичне завершення доступне лише для незавершеної перевірки.")
    if not is_legacy_historical_payment_review(locked):
        raise ValueError(
            "Історичне завершення доступне лише для старих перевірок, "
            "створених до межі legacy-імпорту."
        )

    current_episode = IgCommercialEpisode.objects.select_for_update().filter(
        client_id=client.pk,
        open_slot=1,
    ).first()
    current_watermark = int(getattr(current_episode, "opened_watermark_message_id", 0) or 0)
    review_watermark = int(locked.watermark_message_id or 0)
    current_has_newer_context = bool(
        current_episode
        and current_watermark > 0
        and (review_watermark <= 0 or current_watermark > review_watermark)
    )
    current_matches_review = bool(
        current_episode
        and not current_has_newer_context
        and (
            current_episode.primary_payment_review_id == locked.pk
            or (locked.deal_id and current_episode.deal_id == locked.deal_id)
            or (locked.order_id and current_episode.intended_order_id == locked.order_id)
        )
    )
    preserve_client_funnel = bool(current_episode and not current_matches_review)
    now = timezone.now()
    locked.status = IgPaymentConfirmationReview.Status.CONFIRMED
    locked.confirmed_by = actor
    locked.confirmed_at = now
    locked.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])
    evidence = locked.evidence if isinstance(locked.evidence, dict) else {}
    draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
    decision = IgPaymentReviewDecision.objects.create(
        review=locked,
        client=client,
        decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        verification_source="manager",
        verification_scope=IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED,
        confirmed_amount=exact_amount,
        order_total_amount=None,
        order_total_source="",
        currency=str(getattr(locked.deal, "currency", "") or draft.get("currency") or "UAH")[:8],
        amount_source=(
            "historical_manager_verified"
            if exact_amount is not None
            else "historical_amount_unrecoverable"
        ),
        amount_evidence_message_ids=[],
        reason_code="historical_paid_fulfilled",
        reason_text=note[:500],
        evidence_watermark_message_id=locked.watermark_message_id or 0,
        review_status_before=IgPaymentConfirmationReview.Status.PENDING,
        review_status_after=IgPaymentConfirmationReview.Status.CONFIRMED,
        stage_before=client.stage or "",
        stage_after=(client.stage if preserve_client_funnel else IgClient.Stage.DONE),
        actor=actor,
        actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
        actor_external_id=str(actor.pk)[:128],
        actor_label=str(getattr(actor, "get_username", lambda: "")() or actor.pk)[:150],
    )
    sync_episode_payment(
        review=locked,
        deal=locked.deal if locked.deal_id else None,
        isolate_from_current=preserve_client_funnel,
    )
    resolved = archive_historical_paid_review(
        locked,
        actor=actor,
        reason=note,
        outcome=resolved_outcome,
        transition_to_done=not preserve_client_funnel,
        preserve_client_stage=preserve_client_funnel,
    )
    resolved._transitioned = True
    return resolved


def confirm_review(
    review,
    *,
    actor,
    verification_scope="",
    confirmed_amount=None,
    order_total_amount=None,
    telegram_decision=None,
):
    from management.ig_bot_models import IgPaymentConfirmationReview

    if isinstance(review, IgPaymentConfirmationReview):
        return record_review_decision(
            review,
            actor=actor,
            decision="manager_verified",
            verification_scope=verification_scope,
            confirmed_amount=confirmed_amount,
            order_total_amount=order_total_amount,
            telegram_decision=telegram_decision,
        )

    with transaction.atomic():
        locked = IgPaymentConfirmationReview.objects.select_for_update().get(pk=review.pk)
        locked._transitioned = False
        if locked.status == IgPaymentConfirmationReview.Status.PENDING:
            locked.status = IgPaymentConfirmationReview.Status.CONFIRMED
            locked.confirmed_by = actor
            locked.confirmed_at = timezone.now()
            update_fields = ["status", "confirmed_by", "confirmed_at", "updated_at"]
            if isinstance(telegram_decision, dict):
                evidence = locked.evidence if isinstance(locked.evidence, dict) else {}
                locked.evidence = {**evidence, "telegram_decision": telegram_decision}
                update_fields.append("evidence")
            locked.save(update_fields=update_fields)
            locked._transitioned = True
        return locked


def cancel_review(
    review,
    *,
    actor,
    reason="",
    reason_code="manager_rejected",
    verification_scope="",
    telegram_decision=None,
):
    from management.ig_bot_models import IgPaymentConfirmationReview

    if isinstance(review, IgPaymentConfirmationReview):
        return record_review_decision(
            review,
            actor=actor,
            decision="manager_rejected",
            verification_scope=verification_scope,
            reason_code=reason_code,
            reason_text=reason,
            telegram_decision=telegram_decision,
        )

    with transaction.atomic():
        locked = IgPaymentConfirmationReview.objects.select_for_update().get(pk=review.pk)
        locked._transitioned = False
        if locked.status == IgPaymentConfirmationReview.Status.PENDING:
            locked.status = IgPaymentConfirmationReview.Status.CANCELLED
            locked.cancelled_by = actor
            locked.cancelled_at = timezone.now()
            locked.cancellation_reason = (reason or "")[:500]
            update_fields = ["status", "cancelled_by", "cancelled_at", "cancellation_reason", "updated_at"]
            if isinstance(telegram_decision, dict):
                evidence = locked.evidence if isinstance(locked.evidence, dict) else {}
                locked.evidence = {**evidence, "telegram_decision": telegram_decision}
                update_fields.append("evidence")
            locked.save(update_fields=update_fields)
            locked._transitioned = True
        return locked
