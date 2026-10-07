"""Pure per-part interpretation of one existing, admitted media request.

Interpretations never authorize a reward, payment, sponsorship or manager action.
The existing request/proposal and attachment inspection remain their only owners.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re

from management.services.ig_media_manifest import inline_part_evidence, MediaManifestError

ANALYSIS_VERSION = "ig-turn-media-analysis.v1"
CONTENT_KINDS = frozenset({"unboxing", "wearing", "product_photo", "custom_design",
    "review_video", "sponsorship", "neutral_mention", "other", "unknown"})
SENTIMENTS = frozenset({"positive", "neutral", "negative", "mixed", "unknown"})
OBSERVATION_OUTCOMES = frozenset({"understood", "unreadable", "uncertain"})
EVIDENCE_CODES = frozenset({"visual_content", "text_visible", "text_unreadable",
    "insufficient_detail", "audio_speech", "video_content", "sponsorship_disclosure"})
COMPLAINT_CODES = frozenset({"none", "current_customer_complaint"})
MAX_PARTS = 8
MAX_TRANSCRIPT_CHARS = 4000
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PART = re.compile(r"mp1_[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")
_MIME = re.compile(r"(?:image|audio|video)/[a-z0-9][a-z0-9.+-]{0,63}")


class MediaAnalysisError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TurnMediaObservation:
    source_inline_index: int
    outcome: str
    content_kind: str = "unknown"
    sentiment: str = "unknown"
    confidence: float = 0.0
    evidence_code: str = "insufficient_detail"
    evidence: str = ""
    complaint_code: str = "none"
    transcript: str = ""
    audio_status: str = "not_applicable"


def _value(value):
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, TurnMediaObservation):
        return {key: getattr(value, key) for key in TurnMediaObservation.__dataclass_fields__}
    raise MediaAnalysisError("media_observation_invalid")


def parse_media_observations(values):
    """Strict optional provider schema; absent fields stay explicitly unknown."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_PARTS:
        raise MediaAnalysisError("media_observations_invalid")
    parsed, seen, transcript_chars = [], set(), 0
    for value in values:
        raw = _value(value)
        if not {"source_inline_index", "outcome"}.issubset(raw) or not set(raw).issubset(TurnMediaObservation.__dataclass_fields__):
            raise MediaAnalysisError("media_observation_invalid")
        index = raw["source_inline_index"]
        if type(index) is not int or not 0 <= index < MAX_PARTS or index in seen:
            raise MediaAnalysisError("media_observation_index_invalid")
        normalized = {}
        for key, allowed, default in (
            ("outcome", OBSERVATION_OUTCOMES, ""), ("content_kind", CONTENT_KINDS, "unknown"),
            ("sentiment", SENTIMENTS, "unknown"), ("evidence_code", EVIDENCE_CODES, "insufficient_detail"),
            ("complaint_code", COMPLAINT_CODES, "none"),
            ("audio_status", {"not_applicable", "transcribed", "unintelligible"}, "not_applicable"),
        ):
            text = raw.get(key, default)
            if not isinstance(text, str) or text not in allowed:
                raise MediaAnalysisError("media_observation_value_invalid")
            normalized[key] = text
        confidence = raw.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise MediaAnalysisError("media_observation_confidence_invalid")
        evidence, transcript = raw.get("evidence", ""), raw.get("transcript", "")
        if not isinstance(evidence, str) or len(evidence) > 240 or not isinstance(transcript, str):
            raise MediaAnalysisError("media_observation_text_invalid")
        transcript_chars += len(transcript)
        if transcript_chars > MAX_TRANSCRIPT_CHARS:
            raise MediaAnalysisError("media_transcript_budget_exceeded")
        if normalized["outcome"] != "understood" and (
            normalized["content_kind"] != "unknown" or normalized["sentiment"] != "unknown"
            or normalized["complaint_code"] != "none" or transcript.strip()
        ):
            raise MediaAnalysisError("media_observation_not_understood")
        if (normalized["content_kind"] != "unknown" or normalized["sentiment"] != "unknown") and (
            not evidence.strip() or normalized["evidence_code"] in {"insufficient_detail", "text_unreadable"}
        ):
            raise MediaAnalysisError("media_observation_evidence_missing")
        if normalized["complaint_code"] != "none" and (
            normalized["sentiment"] not in {"negative", "mixed"} or not evidence.strip()
        ):
            raise MediaAnalysisError("media_complaint_evidence_missing")
        if normalized["content_kind"] == "sponsorship" and (
            normalized["evidence_code"] != "sponsorship_disclosure" or not evidence.strip()
        ):
            raise MediaAnalysisError("media_sponsorship_evidence_missing")
        if normalized["audio_status"] == "transcribed":
            if not transcript.strip() or normalized["outcome"] != "understood":
                raise MediaAnalysisError("media_audio_transcript_missing")
        elif transcript.strip():
            raise MediaAnalysisError("media_audio_status_mismatch")
        seen.add(index)
        parsed.append(TurnMediaObservation(index, confidence=float(confidence),
            evidence=evidence.strip(), transcript=transcript.strip(), **normalized))
    return tuple(parsed)


def normalized_capture_outcomes(values):
    """Keep failed/unavailable collector evidence separate from model analysis."""
    if not isinstance(values, (list, tuple)) or len(values) > 1024:
        raise MediaAnalysisError("media_capture_outcomes_invalid")
    result, seen, positions = [], set(), set()
    for value in values:
        if not isinstance(value, Mapping):
            raise MediaAnalysisError("media_capture_outcomes_invalid")
        pk, part_id, index = value.get("source_message_id"), value.get("source_part_id"), value.get("original_index")
        state, reason = value.get("collection_outcome"), value.get("reason", "")
        if (type(pk) is not int or pk <= 0 or not isinstance(part_id, str) or not _PART.fullmatch(part_id)
            or type(index) is not int or index < 0 or not isinstance(state, str) or state not in {"admitted", "omitted", "unavailable"}
            or not isinstance(reason, str) or (reason and not _CODE.fullmatch(reason)) or (pk, part_id) in seen
            or (pk, index) in positions):
            raise MediaAnalysisError("media_capture_outcomes_invalid")
        seen.add((pk, part_id))
        positions.add((pk, index))
        result.append({"source_message_id": pk, "source_part_id": part_id, "original_index": index,
            "collection_outcome": state, "reason": reason})
    return result


def bound_media_parts(*, parts, observations=(), actual_inline_count,
    actual_content_hashes, legacy_images=()):
    """Bind model indexes to the actual submitted prefix, never prepared tails.

    Backend request identity and source IDs are derived here, never model fields.
    Unavailable capture siblings remain in the existing collector's outcomes;
    they are not claimed to have reached or been understood by the provider.
    """
    if not isinstance(parts, (list, tuple)) or len(parts) > MAX_PARTS:
        raise MediaAnalysisError("media_analysis_parts_invalid")
    if type(actual_inline_count) is not int or not 0 <= actual_inline_count <= len(parts):
        raise MediaAnalysisError("media_analysis_admission_unknown")
    if (not isinstance(actual_content_hashes, (list, tuple))
        or len(actual_content_hashes) != actual_inline_count
        or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in actual_content_hashes)):
        raise MediaAnalysisError("media_analysis_binding_invalid")
    identities, positions = set(), set()
    for part in parts:
        if (not isinstance(part, Mapping) or not isinstance(part.get("source_part_id"), str)
            or not _PART.fullmatch(part["source_part_id"])
            or not isinstance(part.get("content_hash"), str) or not _HASH.fullmatch(part["content_hash"])
            or type(part.get("original_index")) is not int or part["original_index"] < 0):
            raise MediaAnalysisError("media_analysis_parts_invalid")
        message_id = part.get("source_message_id")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise MediaAnalysisError("media_analysis_source_invalid")
        if not isinstance(part.get("mime"), str) or not _MIME.fullmatch(part["mime"]):
            raise MediaAnalysisError("media_analysis_mime_unsupported")
        key = (part.get("source_message_id"), part["source_part_id"])
        position = (message_id, part["original_index"])
        if key in identities or position in positions:
            raise MediaAnalysisError("media_analysis_duplicate_part")
        identities.add(key)
        positions.add(position)
    try:
        inline_part_evidence(parts, image_count=len(parts), actual_inline_count=actual_inline_count,
            actual_content_hashes=actual_content_hashes)
    except MediaManifestError as exc:
        raise MediaAnalysisError("media_analysis_binding_invalid") from exc
    parsed = parse_media_observations(observations)
    by_index = {item.source_inline_index: item for item in parsed}
    legacy = {}
    if not isinstance(legacy_images, (list, tuple)) or len(legacy_images) > MAX_PARTS:
        raise MediaAnalysisError("media_analysis_legacy_index_invalid")
    for item in legacy_images or ():
        raw = dict(item) if isinstance(item, Mapping) else {
            "source_image_index": getattr(item, "source_image_index", None),
            "outcome": getattr(item, "outcome", None),
        }
        index = raw.get("source_image_index")
        if (type(index) is not int or index in legacy or not 0 <= index < actual_inline_count
            or not str(parts[index].get("mime") or "").startswith("image/")
            or not isinstance(raw.get("outcome"), str) or raw["outcome"] not in OBSERVATION_OUTCOMES):
            raise MediaAnalysisError("media_analysis_legacy_index_invalid")
        legacy[index] = raw
    if any(index >= actual_inline_count for index in by_index):
        raise MediaAnalysisError("media_analysis_part_not_admitted")
    result = []
    for index, part in enumerate(parts):
        mime = str(part.get("mime") or "")
        kind = mime.split("/", 1)[0]
        observation = by_index.get(index)
        old = legacy.get(index)
        if observation and kind not in {"image", "audio", "video"}:
            raise MediaAnalysisError("media_analysis_mime_unsupported")
        if observation and old and observation.outcome != old.get("outcome"):
            raise MediaAnalysisError("media_analysis_legacy_conflict")
        if observation and kind != "audio" and (observation.transcript or observation.audio_status != "not_applicable"):
            raise MediaAnalysisError("media_analysis_audio_mime_mismatch")
        if observation and kind == "audio" and observation.audio_status == "not_applicable":
            raise MediaAnalysisError("media_analysis_audio_status_missing")
        if observation and (
            (kind == "image" and observation.evidence_code in {"audio_speech", "video_content"})
            or (kind == "audio" and observation.evidence_code not in {
                "audio_speech", "sponsorship_disclosure", "insufficient_detail"})
            or (kind == "audio" and observation.audio_status == "unintelligible"
                and observation.outcome == "understood")
        ):
            raise MediaAnalysisError("media_analysis_evidence_mime_mismatch")
        raw = _value(observation) if observation else _value(TurnMediaObservation(index,
            outcome=str((old or {}).get("outcome") or "uncertain")))
        if observation:
            state, origin = observation.outcome, "provider_observation"
        elif index >= actual_inline_count:
            state, origin = "omitted", "not_admitted"
            raw["outcome"] = "provider_omitted"
        else:
            state, origin = "legacy_unknown", "legacy_observation" if old else "observation_missing"
        raw.update({"analysis_state": state, "origin": origin,
            "source_part_id": part["source_part_id"], "content_hash": part["content_hash"],
            "original_index": part["original_index"], "mime": mime})
        message_id = part.get("source_message_id")
        if message_id is not None:
            if type(message_id) is not int or message_id <= 0:
                raise MediaAnalysisError("media_analysis_source_invalid")
            raw["source_message_id"] = message_id
        result.append(raw)
    return result


def bind_media_analysis(*, parts, observations=(), actual_inline_count,
    actual_content_hashes, request_id, provider_model, legacy_images=(), capture_outcomes=()):
    if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        for value, limit in ((request_id, 40), (provider_model, 80))):
        raise MediaAnalysisError("media_analysis_request_invalid")
    result = bound_media_parts(parts=parts, observations=observations,
        actual_inline_count=actual_inline_count, actual_content_hashes=actual_content_hashes,
        legacy_images=legacy_images)
    captured = normalized_capture_outcomes(capture_outcomes)
    by_identity = {(row.get("source_message_id"), row["source_part_id"]): row for row in result}
    for outcome in captured:
        part = by_identity.get((outcome["source_message_id"], outcome["source_part_id"]))
        if ((part is not None and (part["original_index"] != outcome["original_index"]
            or outcome["collection_outcome"] != "admitted"))
            or (part is None and outcome["collection_outcome"] == "admitted")):
            raise MediaAnalysisError("media_capture_outcome_binding_mismatch")
    return {"schema_version": ANALYSIS_VERSION, "request_id": request_id,
        "provider_model": provider_model, "parts": result, "capture_outcomes": captured}


def bind_turn_media_analysis(artifact, media_binding):
    """Adapter for the existing provider result; performs no I/O or effects."""
    if not isinstance(media_binding, Mapping):
        raise MediaAnalysisError("media_analysis_binding_invalid")
    get = artifact.get if isinstance(artifact, Mapping) else lambda key, default=None: getattr(artifact, key, default)
    return bind_media_analysis(parts=media_binding.get("items") or [],
        observations=get("media_observations", ()) or (),
        legacy_images=get("image_observations", ()) or (),
        actual_inline_count=media_binding.get("actual_inline_count"),
        actual_content_hashes=media_binding.get("actual_content_hashes"),
        request_id=media_binding.get("request_id"), provider_model=media_binding.get("provider_model"),
        capture_outcomes=media_binding.get("outcomes") or ())


def validate_bound_media_analysis(value, *, media, request_id, provider_model, legacy_images=()):
    """Reconstruct every binding at proposal admission; reject extra authority."""
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "request_id", "provider_model", "parts", "capture_outcomes"} or value.get("schema_version") != ANALYSIS_VERSION:
        raise MediaAnalysisError("media_analysis_schema_invalid")
    if (not isinstance(media, Mapping) or not isinstance(media.get("items"), (list, tuple))
        or not {"actual_inline_count", "actual_content_hashes"}.issubset(media)):
        raise MediaAnalysisError("media_analysis_binding_invalid")
    rows = value.get("parts")
    if not isinstance(rows, list) or len(rows) != len(media["items"]):
        raise MediaAnalysisError("media_analysis_coverage_invalid")
    observations = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MediaAnalysisError("media_analysis_schema_invalid")
        if row.get("origin") == "provider_observation":
            observations.append({key: row.get(key) for key in TurnMediaObservation.__dataclass_fields__})
    expected = bind_media_analysis(parts=media["items"], observations=observations,
        actual_inline_count=media["actual_inline_count"], actual_content_hashes=media["actual_content_hashes"],
        request_id=request_id, provider_model=provider_model, legacy_images=legacy_images,
        capture_outcomes=media.get("outcomes") or ())
    # JSON distinguishes booleans from integer identities, unlike dict equality.
    try:
        matches = json.dumps(dict(value), sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise MediaAnalysisError("media_analysis_proof_mismatch")
    return expected


def media_reaction(analysis):
    """Conservative reply guidance; no action authority or delivery assertion."""
    rows = analysis.get("parts", ()) if isinstance(analysis, Mapping) else ()
    known = [row for row in rows if isinstance(row, Mapping)
        and row.get("analysis_state") == "understood" and row.get("origin") == "provider_observation"]
    sentiments = sorted({row.get("sentiment", "unknown") for row in known})
    complaints = [row for row in known if row.get("complaint_code") == "current_customer_complaint"
        and row.get("sentiment") in {"negative", "mixed"}]
    incomplete = (len(known) != len(rows) or any(row.get("sentiment") == "unknown" for row in known)
        or any(row.get("collection_outcome") != "admitted" for row in analysis.get("capture_outcomes", ()))) if isinstance(analysis, Mapping) else True
    if complaints:
        mode = "service"
    elif {"negative", "mixed"}.intersection(sentiments) or incomplete or not known:
        mode = "neutral"
    elif "positive" in sentiments:
        mode = "social"
    else:
        mode = "neutral"
    return {"mode": mode, "sentiments": sentiments, "incomplete": incomplete,
        "complaint_parts": [{key: row[key] for key in
            ("source_part_id", "content_hash", "source_inline_index", "complaint_code")}
            | ({"source_message_id": row["source_message_id"]} if "source_message_id" in row else {})
            for row in complaints]}


def inspection_analysis_fields(row):
    """Only validated interpretation fields; source/request proof stays outside."""
    keys = ("analysis_state", "origin", "content_kind", "sentiment",
        "confidence", "evidence", "complaint_code", "transcript", "audio_status")
    return {key: row[key] for key in keys} | (
        {"evidence_code": row["evidence_code"]} if row["origin"] == "provider_observation" else {})
