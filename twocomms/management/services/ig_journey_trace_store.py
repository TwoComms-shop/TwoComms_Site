"""Persist immutable transcript interpretations after source and scope checks."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

from django.db import transaction
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgJourneyTraceSnapshot, InstagramBotMessage
from management.services.ig_journey_trace_contract import validate_normalized_journey_trace


SCHEMA_VERSION = "journey-trace.v1"
PRODUCER_VERSION = "journey-trace-store.v1"
MAX_SOURCE_MESSAGES = 160
MAX_SOURCE_TEXT_CHARS = 1_000_000
_ROLES = {"user", "manager", "model"}


class JourneyTraceStoreRejected(ValueError):
    """A supplied interpretation cannot be bound to the current owned sources."""


class JourneyTraceStoreConflict(JourneyTraceStoreRejected):
    """This exact input and prompt already have a different immutable result."""


def _positive(value):
    return type(value) is int and value > 0


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def _text_digest(text):
    try:
        return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise JourneyTraceStoreRejected("source_encoding_invalid") from exc


def _source_manifest(by_id, watermark):
    if not isinstance(by_id, dict) or not 1 <= len(by_id) <= MAX_SOURCE_MESSAGES:
        raise JourneyTraceStoreRejected("source_bounds")
    manifest, chars = [], 0
    for message_id, source in by_id.items():
        if (
            not _positive(message_id) or message_id > watermark
            or not isinstance(source, dict) or type(source.get("message_id")) is not int
            or source["message_id"] != message_id or not isinstance(source.get("role"), str)
            or source["role"] not in _ROLES
            or not isinstance(source.get("text"), str)
        ):
            raise JourneyTraceStoreRejected("source_schema")
        chars += len(source["text"])
        if chars > MAX_SOURCE_TEXT_CHARS:
            raise JourneyTraceStoreRejected("source_text_bounds")
        manifest.append({"message_id": message_id, "role": source["role"],
                         "source_text_sha256": _text_digest(source["text"])})
    return sorted(manifest, key=lambda row: row["message_id"])


def record_journey_trace(*, client_id, episode_id, watermark, normalized_trace, by_id,
                         prompt_version, analysis_model, analyzed_at):
    """Store only a current, source-bound interpretation; no normal job writes.

    Identity is input + prompt, independently of provider output. Identical
    retries return the first result; conflicting outputs cannot rewrite it.
    The caller owns provider admission and explicitly selects the trace scope.
    """
    if not _positive(client_id) or not _positive(watermark) or (episode_id is not None and not _positive(episode_id)):
        raise JourneyTraceStoreRejected("scope_invalid")
    for value, maximum in ((prompt_version, 32), (analysis_model, 80)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0," + str(maximum - 1) + r"}", value):
            raise JourneyTraceStoreRejected("producer_metadata_invalid")
    if not isinstance(analyzed_at, datetime) or timezone.is_naive(analyzed_at):
        raise JourneyTraceStoreRejected("analyzed_at_invalid")
    trace = deepcopy(normalized_trace)
    if (not validate_normalized_journey_trace(trace) or trace["watermark"] != watermark
            or trace["status"] not in {"interpretation_only", "partial"} or not trace["steps"]):
        raise JourneyTraceStoreRejected("trace_schema_invalid")
    manifest = _source_manifest(by_id, watermark)
    by_message = {row["message_id"]: row for row in manifest}
    for step in trace["steps"]:
        for evidence in step["evidence"]:
            if by_message.get(evidence["message_id"]) != evidence:
                raise JourneyTraceStoreRejected("trace_source_mismatch")
    source_digest = _digest(manifest)
    trace_digest = _digest(trace)
    snapshot_key = _digest({
        "client_id": client_id, "episode_id": episode_id, "watermark": watermark,
        "source_digest": source_digest, "prompt_version": prompt_version, "schema_version": SCHEMA_VERSION,
    })
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).values(
            "id", "hidden_at", "privacy_erasure_started_at",
        ).first()
        if client is None or client["hidden_at"] is not None or client["privacy_erasure_started_at"] is not None:
            raise JourneyTraceStoreRejected("client_unavailable")
        if episode_id is not None and not IgCommercialEpisode.objects.select_for_update().filter(
            pk=episode_id, client_id=client_id,
        ).exists():
            raise JourneyTraceStoreRejected("episode_owner_mismatch")
        # Full text is read once for all sources, never truncated and never saved.
        rows = list(InstagramBotMessage.objects.filter(pk__in=by_message, client_id=client_id).values("id", "role", "text"))
        if len(rows) != len(manifest) or any(
            row["role"] != by_message[row["id"]]["role"]
            or _text_digest(row["text"]) != by_message[row["id"]]["source_text_sha256"]
            for row in rows
        ):
            raise JourneyTraceStoreRejected("persisted_source_mismatch")
        latest_id = InstagramBotMessage.objects.filter(client_id=client_id, role__in=_ROLES).order_by("-id").values_list("id", flat=True).first()
        if latest_id != watermark:
            raise JourneyTraceStoreRejected("watermark_stale")
        existing = IgJourneyTraceSnapshot.objects.filter(snapshot_key=snapshot_key).first()
        if existing:
            if existing.trace_digest != trace_digest or existing.trace != trace or existing.source_digest != source_digest:
                raise JourneyTraceStoreConflict("snapshot_input_conflict")
            return existing
        return IgJourneyTraceSnapshot.objects.create(
            snapshot_key=snapshot_key, client_id=client_id, commercial_episode_id=episode_id,
            watermark_message_id=watermark, source_digest=source_digest, trace=trace, trace_digest=trace_digest,
            schema_version=SCHEMA_VERSION, prompt_version=prompt_version, producer_version=PRODUCER_VERSION,
            analysis_model=analysis_model, analyzed_at=analyzed_at,
        )
