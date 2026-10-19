"""Source-bound advertising evidence. Attribution never selects merchandise.

All provider strings are quoted evidence, including titles and CTA. This module
performs no network I/O; media URLs are candidates for the existing media gates.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from urllib.parse import urlsplit

MAX_TOUCHES = 8
MAX_CANDIDATES = 12
IDENTITY_FIELDS = ("ad_id", "ref", "post_id")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _text(value, limit=255):
    return str(value or "").strip()[:limit]


def _context(value):
    return value.get("ads_context_data") if isinstance(value.get("ads_context_data"), Mapping) else {}


def referral_identity(value):
    value = value if isinstance(value, Mapping) else {}
    return {key: _text(_context(value).get(key) if key == "post_id" else value.get(key))
        for key in IDENTITY_FIELDS}


def _url(value):
    raw = _text(value, 2048)
    try:
        parsed = urlsplit(raw)
        return raw if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def _bounded_referral(value):
    output = {key: _text(value.get(key)) for key in ("ref", "ad_id", "source", "type")
        if _text(value.get(key))}
    context = _context(value)
    quoted = {key: _text(context.get(key), 512 if key == "ad_title" else 255)
        for key in ("post_id", "ad_title", "creative_id") if _text(context.get(key))}
    for key in ("photo_url", "video_url"):
        if context.get(key) or context.get(key + "_availability") == "invalid_url":
            # A syntactically usable URL is not an admitted/fetched asset.
            quoted[key] = _url(context.get(key))
            quoted[key + "_availability"] = "unverified_url" if quoted[key] else "invalid_url"
    if quoted:
        output["ads_context_data"] = quoted
    return output


def sanitize_referral(value):
    if not isinstance(value, Mapping):
        return {}
    output = _bounded_referral(value)
    conflicts = value.get("_conflicting_referrals")
    if isinstance(conflicts, list):
        output["_conflicting_referrals"] = [dict(
            reason=_text(item.get("reason"), 64), origin=_text(item.get("origin"), 32),
            evidence=_bounded_referral(item.get("evidence") or {}))
            for item in conflicts[:3] if isinstance(item, Mapping)]
    output["source_digest"] = digest(dict(value))
    return output


def merge_referral_candidates(*candidates):
    """Nested identity wins; a shared agreeing identity is required to enrich.

    Missing identity is not proof of compatibility. Conflicting locations stay
    separate so an image from another ad can never acquire the primary ad ID.
    """
    merged = {}
    conflicts = []
    for index, candidate in enumerate(candidates[:4]):
        if not isinstance(candidate, Mapping) or not candidate:
            continue
        incoming = _bounded_referral(candidate)
        if not incoming:
            continue
        if not merged:
            merged = deepcopy(incoming)
            continue
        left, right = referral_identity(merged), referral_identity(incoming)
        shared = [key for key in IDENTITY_FIELDS if left[key] and right[key]]
        conflict = any(left[key] != right[key] for key in shared)
        if conflict or not shared:
            conflicts.append(dict(origin=f"location:{index}",
                reason="identity_conflict" if conflict else "identity_unproven", evidence=incoming))
            continue
        for key, value in incoming.items():
            if key == "ads_context_data":
                existing = merged.setdefault(key, {})
                for name, nested in value.items():
                    if not existing.get(name):
                        existing[name] = deepcopy(nested)
            elif not merged.get(key):
                merged[key] = deepcopy(value)
    if conflicts:
        merged["_conflicting_referrals"] = conflicts[:3]
    return merged


def campaign_editor_context(value, *, product_id=None, theme=""):
    """Validate the supported JSON authoring format without inventing prices."""
    if value in (None, "", {}):
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("ad_context_must_be_object")
    kind = _text(value.get("kind"), 32) or ("single_product" if product_id else "theme" if _text(theme) else "unknown")
    if kind not in {"single_product", "product_set", "theme", "unknown"}:
        raise ValueError("invalid_ad_context_kind")
    rows = value.get("candidates", [])
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise ValueError("ad_candidate_limit")
    if not rows and product_id and kind == "single_product":
        rows = [{"product_id": product_id, "position": 1}]
    candidates, seen = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError("invalid_ad_candidate")
        try:
            raw_pid, raw_position = row.get("product_id"), row.get("position", index + 1)
            if isinstance(raw_pid, (bool, float)) or isinstance(raw_position, (bool, float)):
                raise ValueError("invalid_ad_candidate")
            pid = int(raw_pid)
            position = int(raw_position)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_ad_candidate") from exc
        if pid < 1 or position < 1 or position > MAX_CANDIDATES or pid in seen:
            raise ValueError("invalid_or_duplicate_ad_candidate")
        seen.add(pid)
        candidates.append(dict(product_id=pid, position=position, label=_text(row.get("label"), 120)))
    if len({row["position"] for row in candidates}) != len(candidates):
        raise ValueError("duplicate_ad_position")
    if kind == "single_product" and len(candidates) != 1:
        raise ValueError("single_product_requires_one_candidate")
    if kind == "product_set" and not candidates:
        raise ValueError("product_set_requires_candidates")
    if kind in {"theme", "unknown"} and candidates:
        raise ValueError("ad_kind_candidate_conflict")
    if kind == "theme" and not _text(theme):
        raise ValueError("ad_theme_required")
    creative = value.get("creative") if isinstance(value.get("creative"), Mapping) else {}
    review = value.get("review") if isinstance(value.get("review"), Mapping) else {}
    return dict(schema_version=1, kind=kind, candidates=candidates,
        creative={key: _text(creative.get(key), 255) for key in ("post_id", "creative_id") if _text(creative.get(key))},
        review={key: _text(review.get(key), 128) for key in ("reviewed_by", "reviewed_at") if _text(review.get(key))})


def _aware_stamp(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _provider_stamp(value):
    try:
        if isinstance(value, bool):
            return None
        from datetime import timezone as utc_timezone
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, utc_timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _standalone_referrals_for_capture(sources, scope, *, client, boundary):
    """Bind a click receipt only to its next real owned customer source.

    Both provider and local observation clocks must precede the anchor. A late
    webhook with an old provider timestamp cannot rewrite an already sealed
    customer turn. There is no invented TTL or account-level last-ad fallback.
    """
    if client is None or not isinstance(boundary, Mapping) or not sources:
        return []
    if (scope.get("client_id") != getattr(client, "pk", None) or scope.get("erasure_epoch")
        or getattr(client, "privacy_erasure_started_at", None)):
        return []
    namespace = str(scope.get("source_namespace") or "")
    if (not namespace or boundary.get("source_namespace") != namespace
        or boundary.get("client_id") != client.pk
        or boundary.get("reset_id") != scope.get("reset_id")
        or boundary.get("reset_floor") != scope.get("reset_floor")):
        return []
    watermark = _aware_stamp((boundary.get("watermark") or {}).get("event_at"))
    if watermark is None:
        return []
    from management.models import IgClient, IgFunnelResetAudit, IgWebhookInboxEvent, InstagramBotMessage
    from management.services.ig_webhook_inbox import _row_event
    from django.db.models import Q, Exists, OuterRef, Subquery

    try:
        # The capture adapter already validated these immutable observation
        # clocks against the owned messages. Absence needs one bounded read;
        # expensive revalidation is reserved for an actual click candidate.
        observed = [_aware_stamp(source.get("observed_created_at")) for source in sources]
        candidate_rows = None
        if all(observed):
            first_observed = min(observed)
            source_ids = [source["message_id"] for source in sources]
            first_id = min(source["message_id"] for source in sources
                if _aware_stamp(source.get("observed_created_at")) == first_observed)
            # An older click must not add five revalidation reads to every
            # subsequent turn. The same next-message rule lives in this one
            # correlated absence read, then is checked again on actual hits.
            intervening = InstagramBotMessage.objects.filter(client_id=client.pk,
                sender_id=client.igsid, provider_namespace=namespace, role="user")
            intervening = intervening.exclude(pk__in=source_ids).filter(
                Q(created_at__lt=first_observed) | Q(created_at=first_observed, pk__lt=first_id),
                created_at__gte=OuterRef("received_at"))
            candidates = IgWebhookInboxEvent.objects.filter(namespace=namespace,
                customer_igsid=client.igsid, decision="accepted", reason="attribution_only",
                processed_at__isnull=False, received_at__lte=max(observed))
            if scope.get("reset_id"):
                candidates = candidates.filter(received_at__gt=Subquery(IgFunnelResetAudit.objects.filter(
                    pk=scope["reset_id"], client_id=client.pk).values("created_at")[:1]))
            candidate_rows = list(candidates.annotate(_intervening_customer_source=Exists(intervening))
                .filter(_intervening_customer_source=False).order_by("-received_at", "-pk")[:MAX_TOUCHES + 1])
            if not candidate_rows:
                return []
        if not IgClient.objects.filter(pk=client.pk, igsid=client.igsid,
            privacy_erasure_started_at__isnull=True, hidden_at__isnull=True, is_blocked=False).exists():
            return []
        floor = int(scope.get("reset_floor") or 0)
        ids = [source["message_id"] for source in sources]
        if floor < 1 or any(type(mid) is not int or mid < floor for mid in ids):
            return []
        reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").first()
        if (scope.get("reset_id") != (reset.pk if reset else None)
            or floor != (int(reset.reset_after_message_id or 0) + 1 if reset else 1)):
            return []
        actual = list(InstagramBotMessage.objects.filter(pk__in=ids, client_id=client.pk,
            sender_id=client.igsid, provider_namespace=namespace, role="user", source__in=["webhook", "poll"])
            .exclude(status="failed").order_by("created_at", "pk"))
        if len(actual) != len(set(ids)):
            return []
        by_id = {source["message_id"]: source for source in sources}
        for message in actual:
            source = by_id[message.pk]
            stamp = _aware_stamp(source.get("provider_created_at") or source.get("observed_created_at"))
            if (source.get("source_namespace") != namespace or stamp is None or stamp > watermark
                or source.get("provider_message_id", "") != str(message.mid or "")
                or stamp != (message.provider_created_at or message.created_at)):
                return []
        first, last = actual[0], actual[-1]
        previous = (InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
            provider_namespace=namespace, role="user").exclude(pk__in=ids)
            .filter(Q(created_at__lt=first.created_at) | Q(created_at=first.created_at, pk__lt=first.pk))
            .order_by("-created_at", "-pk").first())
        rows = IgWebhookInboxEvent.objects.filter(namespace=namespace, customer_igsid=client.igsid,
            decision="accepted", reason="attribution_only", processed_at__isnull=False,
            received_at__lte=last.created_at)
        if previous:
            rows = rows.filter(received_at__gt=previous.created_at)
        if reset:
            rows = rows.filter(received_at__gt=reset.created_at)
        owner = namespace.split(":", 1)[1] if ":" in namespace else ""
        output = []
        selected_rows = candidate_rows if candidate_rows is not None else list(rows.order_by("-received_at", "-pk")[:MAX_TOUCHES + 1])
        for row in reversed(selected_rows):
            if (row.received_at > last.created_at or (previous and row.received_at <= previous.created_at)
                or (reset and row.received_at <= reset.created_at)):
                continue
            event = _row_event(row.payload)
            provider_at = _provider_stamp(event.get("timestamp"))
            if (not owner or row.owner_id != owner or digest(row.payload) != row.payload_digest
                or str((event.get("sender") or {}).get("id") or "") != client.igsid
                or str((event.get("recipient") or {}).get("id") or "") != owner
                or event.get("message") or event.get("postback")
                or not isinstance(event.get("referral"), Mapping) or not event["referral"]
                or provider_at is None or provider_at > watermark
                or (previous and provider_at <= (previous.provider_created_at or previous.created_at))
                or (reset and provider_at <= reset.created_at)):
                continue
            anchors = [message for message in actual if row.received_at <= message.created_at
                and provider_at <= (message.provider_created_at or message.created_at)]
            if not anchors:
                continue
            anchor = dict(by_id[anchors[0].pk])
            anchor["_referral_event"] = dict(referral_event_id=row.pk, referral_event_key=row.event_key,
                referral_event_digest=row.payload_digest, provider_event_at=provider_at.isoformat(),
                received_at=row.received_at.isoformat())
            output.append((anchor, event["referral"]))
        return output
    except Exception:
        # This optional attribution read cannot erase a current nested source.
        return []


def capture_ad_context(sources, scope, *, client=None, boundary=None):
    """Capture ordered touches only from this caller's sealed source scope."""
    from management.services.ig_ad_referral import resolve_referral_evidence

    # Only the DB reader below may create an attribution-event envelope.
    sources = [{key: value for key, value in source.items() if key != "_referral_event"}
        for source in sources]
    refs = [(source, source.get("referral")) for source in sources
        if isinstance(source.get("referral"), Mapping) and
        (any(referral_identity(source["referral"]).values()) or
         any(source["referral"].get(key) for key in ("source", "type", "ads_context_data")))]
    standalone = _standalone_referrals_for_capture(sources, scope, client=client, boundary=boundary)
    ordinal = {source["message_id"]: index for index, source in enumerate(sources)}
    refs = sorted([*standalone, *refs], key=lambda item: (
        ordinal[item[0]["message_id"]], 0 if item[0].get("_referral_event") else 1,
        (item[0].get("_referral_event") or {}).get("provider_event_at", "")))
    result = dict(scope=scope, status="unknown", present=False, touches=[], candidates=[],
        intent_status="not_inferred", selection_status="not_selected")
    if not refs:
        return result
    if len(refs) > MAX_TOUCHES:
        result.update(present=True, status="ambiguous", reason_codes=["touch_count_budget"],
            source_message_id=refs[-1][0]["message_id"], omitted_touches=len(refs) - MAX_TOUCHES)
    # Preserve the current explicit source when older touches exceed budget.
    for source, ref in refs[-MAX_TOUCHES:]:
        resolution = resolve_referral_evidence(ref, namespace=source.get("source_namespace", ""))
        snapshot = resolution.snapshot()
        metadata = snapshot.get("metadata") or {}
        if metadata.get("snapshot_digest"):
            try:
                observed = datetime.fromisoformat(metadata["fetched_at"].replace("Z", "+00:00"))
                raw_event = (source.get("_referral_event") or {}).get("provider_event_at") or source.get("provider_created_at")
                event_at = raw_event if isinstance(raw_event, datetime) else datetime.fromisoformat(str(raw_event).replace("Z", "+00:00"))
                if not observed.tzinfo or not event_at.tzinfo:
                    raise ValueError("naive event clock")
                metadata["observation"] = "current_observation_after_event" if observed > event_at else "saved_observation_before_event_not_historical_proof"
            except (KeyError, TypeError, ValueError):
                metadata["observation"] = "observation_event_time_relation_unknown"
        touch = dict(source_message_id=source["message_id"],
            provider_message_id=_text(source.get("provider_message_id")),
            source_namespace=_text(source.get("source_namespace"), 128),
            event_at=_text(source.get("provider_created_at"), 64),
            referral=sanitize_referral(ref), source_referral_digest=ref.get("source_digest") or digest(ref), **snapshot)
        event = source.get("_referral_event")
        touch["evidence_kind"] = "standalone_referral" if event else "message_referral"
        if event:
            touch.update({key: event[key] for key in ("referral_event_id", "referral_event_key", "referral_event_digest")})
            touch.update(anchor_provider_message_id=touch["provider_message_id"],
                anchor_event_at=touch["event_at"], provider_message_id="", event_at=event["provider_event_at"],
                referral_received_at=event["received_at"])
        creative = _context(touch["referral"])
        touch["creative_media_availability"] = {key: (
            "requires_media_admission" if creative.get(key) else
            "invalid_url" if creative.get(key + "_availability") == "invalid_url" else "not_provided")
            for key in ("photo_url", "video_url")}
        if event:
            touch["creative_media_availability"] = {key: "standalone_metadata_only" if creative.get(key) else value
                for key, value in touch["creative_media_availability"].items()}
        touch["touch_digest"] = digest(touch)
        result["touches"].append(touch)
    last = result["touches"][-1]
    result.update(present=True, source_message_id=last["source_message_id"],
        mapping_active=all(touch.get("mapping_active") for touch in result["touches"]),
        mapping_unique=all(touch.get("mapping_unique") for touch in result["touches"]))
    if len(refs) <= MAX_TOUCHES:
        targets = {digest(dict(candidates=t.get("candidates"), theme=t.get("theme"))) for t in result["touches"]}
        statuses = {t["status"] for t in result["touches"]}
        status = "resolved" if statuses == {"resolved"} and len(targets) == 1 else "ambiguous" if len(targets) > 1 or "ambiguous" in statuses else "unavailable"
        result.update(status=status, reason_codes=["multiple_ad_targets"] if len(targets) > 1 else list(last["reason_codes"]))
    if len(result["touches"]) == 1:
        for key in ("mapping_id", "product_id", "theme", "revision", "mapping_digest"):
            if key in last:
                result[key] = deepcopy(last[key])
    seen = set()
    for touch in result["touches"]:
        for row in touch.get("candidates", []):
            key = (row["product_id"], row["position"], touch["touch_digest"])
            if key not in seen:
                result["candidates"].append(dict(row, source_message_id=touch["source_message_id"], touch_digest=touch["touch_digest"]))
                seen.add(key)
    result["context_digest"] = digest(result)
    return result


def ad_media_candidates(context):
    """Return URL evidence for admission; never imply the asset was fetched.

    Meta's video_url in referral examples is a thumbnail, not a full video.
    Caller MUST pass these through the same ownership/URL/size/budget gates as
    ordinary media before constructing provider parts.
    """
    output = []
    for touch in (context.get("touches") or [])[:MAX_TOUCHES]:
        if touch.get("evidence_kind") == "standalone_referral":
            continue
        creative = _context(touch.get("referral") or {})
        for key in ("photo_url", "video_url"):
            if _url(creative.get(key)):
                output.append(dict(source_message_id=touch["source_message_id"],
                    source_namespace=touch.get("source_namespace", ""),
                    provider_message_id=touch.get("provider_message_id", ""),
                    source_url=creative[key], type="image", role="ad_photo" if key == "photo_url" else "ad_video_thumbnail",
                    post_id=creative.get("post_id", ""), touch_digest=touch["touch_digest"],
                    availability="requires_media_admission"))
    return output


def ad_context_manifest(context):
    """Content-free IDs/digests of the actual captured advertising context."""
    return dict(version="ig-ad-context-manifest.v1", status=context.get("status", "unknown"),
        context_digest=context.get("context_digest") or digest(context), touches=[dict(
            source_message_id=touch["source_message_id"], status=touch["status"],
            reason_codes=list(touch.get("reason_codes") or [])[:8],
            mapping_id=touch.get("mapping_id"), revision=touch.get("revision") or "",
            mapping_digest=touch.get("mapping_digest") or "",
            candidates=[dict(product_id=row["product_id"], position=row.get("position"))
                for row in (touch.get("candidates") or [])[:MAX_CANDIDATES]],
            metadata_digest=(touch.get("metadata") or {}).get("snapshot_digest") or "",
            **({"referral_event_id": touch["referral_event_id"]} if touch.get("referral_event_id") else {}))
            for touch in (context.get("touches") or [])[:MAX_TOUCHES]])


def ad_referral_media_metadata(referral, *, first_index=0, provider_message_id=""):
    """Ingress descriptors, captured later by the ordinary private media lane.

    Call only for a real owned customer message/postback, never a standalone
    referral. No URL is fetched here and no account-level prior ad is used.
    """
    safe = sanitize_referral(referral)
    context = _context(safe)
    output = []
    for field in ("photo_url", "video_url"):
        url = _url(context.get(field))
        if not url:
            continue
        output.append(dict(url=url, original_index=int(first_index) + len(output),
            type="image", media_type="image", role="ad_photo" if field == "photo_url" else "ad_video_thumbnail",
            ad_context_only=True, provenance="live_webhook", status="pending",
            provider_event_id=_text(provider_message_id), provider_object_id=_text(context.get("post_id")),
            provider_context_kind="ad_referral", capture_eligible=True,
            catalog_match_allowed=False, actionable=False, payment_evidence=False))
    return output[:max(0, 8 - int(first_index))]
