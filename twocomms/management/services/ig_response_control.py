"""Typed, fail-closed boundary for model replies and operational controls.

The worker can keep consuming the historical ``control`` mapping, but only a
validated immutable response may produce that mapping.  This module deliberately
has no provider or database side effects.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from management.services.ig_media_manifest import (
    IMAGE_EVIDENCE_CODES,
    IMAGE_OBSERVATION_OUTCOMES,
    IMAGE_TYPE_CODES,
)
from management.services.ig_prize_programme import (
    PRIZE_CUE_CODES,
    PRIZE_REASON_CODES,
    PRIZE_STATUSES,
    PrizeCertificateObservation,
    PrizeProgramme,
    validate_prize_observation,
)


CONTROL_KINDS = frozenset(
    {
        "manager",
        "spam",
        "stage",
        "paylink",
        "payment",
        "product",
        "item",
        "option",
        "qty",
        "size",
        "fit",
        "color_variant_id",
        "price",
        "price_quoted",
        "order",
        "show_products",
        "catalog_link",
        "objhandle",
    }
)
PROVIDER_CONTROL_KINDS = CONTROL_KINDS - {"price"}
_KIND_ALIASES = {"opt": "option", "variant": "color_variant_id"}
_REPEATED_KINDS = frozenset({"item", "option"})
_BOOLEAN_KINDS = frozenset({"manager", "spam", "order", "catalog_link"})
_HARD_STAGES = frozenset({"paid", "order_created", "done"})
_STAGES = frozenset(
    {
        "new",
        "qualifying",
        "product_matched",
        "checkout",
        "payment_pending",
        "paid",
        "order_created",
        "done",
        "lead_manager",
        "spam",
        "cold",
    }
)

# These broad, unbounded-by-length matchers are intentional: an ASCII bracket
# token which looks like a command must never reach a customer, even if its
# name is misspelled or a malformed provider response makes it very long.  The
# negated character classes keep both passes linear in the reply length.
# Permit only invisible/spacing characters between ``[`` and the command's
# first ASCII letter. This catches obfuscated or truncated legacy controls
# without treating ordinary Cyrillic bracket text as a command.
_CONTROL_PREFIX_GAP = (
    r"(?:[^\S\r\n]|\u200b|\u200c|\u200d|\u200e|\u200f|"
    r"\u202a|\u202b|\u202c|\u202d|\u202e|\u2060|\u2066|\u2067|\u2068|\u2069|\ufeff)*"
)
_ASCII_CLOSED_CONTROL_TOKEN_RE = re.compile(
    rf"\[{_CONTROL_PREFIX_GAP}[A-Za-z][^\]\r\n]*\]"
)
_ASCII_CONTROL_SHAPED_RE = re.compile(
    rf"\[{_CONTROL_PREFIX_GAP}[A-Za-z][^\]\r\n]*(?:\]|\r?\n|$)"
)
_FOLLOW_URL_RE = re.compile(
    r"(?:https?://|www\.|(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"[a-z]{2,24}(?::\d{1,5})?(?:[/?:#][^\s]*)?)",
    re.IGNORECASE,
)
_FOLLOW_MARKDOWN_RE = re.compile(r"[`*_#\[\]{}<>]|\]\(")
_FOLLOW_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_FOLLOW_PERCENT_RE = re.compile(r"\d+\s*%")
_FOLLOW_PROMO_TOKEN_RE = re.compile(
    r"\b(?=[a-z0-9_-]{5,}\b)(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)"
    r"[a-z0-9_-]+\b",
    re.IGNORECASE,
)
_FOLLOW_DISCOUNT_RE = re.compile(
    r"(?:зниж\w*|скид\w*|промо\s*код\w*|промокод\w*|promo(?:\s*code)?|"
    r"coupon|discount|stack\w*|відсот\w*|процент\w*|"
    r"\bкод(?:ом|у)?\s+[A-Z0-9][A-Z0-9_-]{3,}\b)",
    re.IGNORECASE,
)
_FOLLOW_URGENCY_RE = re.compile(
    r"(?:терміново|поспіш\w*|сьогодні|зараз|лише|тільки|останн\w*|"
    r"last\s+chance|не\s+втрач\w*)",
    re.IGNORECASE,
)
_FOLLOW_SURVEILLANCE_RE = re.compile(
    r"(?:\b(?:я|ми)\s+(?:бач\w*|вид\w*|поміт\w*|замет\w*|зна\w*|"
    r"відстеж\w*|отслеж\w*)|\bви\s+(?:ще\s+)?не\s+(?:підпис|подпис)\w*|"
    r"\bстатус\b[^.!?]{0,40}\b(?:підпис|подпис)\w*|"
    r"\b(?:підпис|подпис)\w*\b[^.!?]{0,30}\b(?:не\s+актив\w*|"
    r"не\s+підтвердж\w*|не\s+подтвержд\w*)|перевір\w*|провер\w*|"
    r"контролю\w*|контролир\w*|стежимо|следим|відслідков\w*|отслеж\w*)",
    re.IGNORECASE,
)
_FOLLOW_FALSE_PROMISE_RE = re.compile(
    r"(?:гарант\w*|обіця\w*|обещ\w*|безкоштов\w*|free\s+gift)",
    re.IGNORECASE,
)
_FOLLOW_IMPERATIVE_RE = re.compile(
    r"\b(?:підпис(?:уйтесь|іться|ись)|підпиш(?:іться|ись)|"
    r"подпис(?:ывайтесь|итесь|ывайся)|"
    r"follow(?:\s+us)?|стежте|следите|долучайтеся)\b",
    re.IGNORECASE,
)
_FOLLOW_MIN_LENGTH = 24
_FOLLOW_MAX_LENGTH = 220
_KNOWN_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_QTY_RE = re.compile(r"^[1-9][0-9]{0,3}$")
_SIZE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,15}$")
_FIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,49}$")
_OPTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,48}=[^=;\[\]\r\n]{1,80}$", re.I)
_OBJHANDLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}:[a-z][a-z0-9_]{0,47}$")

# The provider schema excludes legacy negotiated ``price`` while retaining its
# parser compatibility for trusted historical inputs. ``value`` remains broad
# because different approved kinds use different JSON types; the validator
# below is still the authorization boundary.
STRUCTURED_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
        },
        "controls": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(PROVIDER_CONTROL_KINDS),
                    },
                    "value": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "boolean"},
                            {"type": "integer"},
                            {"type": "number"},
                            {"type": "array", "items": {"type": "integer"}},
                        ]
                    },
                },
                "required": ["kind", "value"],
            },
        },
        "follow_cta": {
            "type": "object",
            "properties": {
                "include": {"type": "boolean"},
                "text": {
                    "type": "string",
                    "minLength": _FOLLOW_MIN_LENGTH,
                    "maxLength": _FOLLOW_MAX_LENGTH,
                },
            },
            "required": ["include", "text"],
        },
        "turn_intelligence": {
            "type": "object",
            "properties": {
                "catalog_candidates": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer", "minimum": 1},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "evidence": {"type": "string", "maxLength": 240},
                        },
                        "required": ["product_id", "confidence", "evidence"],
                    },
                },
                "transcript": {"type": "string", "maxLength": 4000},
                "intent": {"type": "string", "maxLength": 64},
                "audio_status": {
                    "type": "string",
                    "enum": ["not_applicable", "transcribed", "unintelligible"],
                },
                "image_observations": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_image_index": {"type": "integer", "minimum": 0},
                            "outcome": {
                                "type": "string",
                                "enum": sorted(IMAGE_OBSERVATION_OUTCOMES),
                            },
                            "evidence_code": {
                                "type": "string",
                                "enum": sorted(IMAGE_EVIDENCE_CODES),
                            },
                            "type_code": {
                                "type": "string",
                                "enum": sorted(IMAGE_TYPE_CODES),
                            },
                            "prize_certificate": {
                                "type": "object",
                                "properties": {
                                    "programme_id": {"type": "string", "maxLength": 64},
                                    "programme_version": {"type": "string", "maxLength": 64},
                                    "status": {"type": "string", "enum": sorted(PRIZE_STATUSES)},
                                    "cue_codes": {
                                        "type": "array", "maxItems": len(PRIZE_CUE_CODES),
                                        "items": {"type": "string", "enum": sorted(PRIZE_CUE_CODES)},
                                    },
                                    "reason_code": {"type": "string", "enum": sorted(PRIZE_REASON_CODES)},
                                    "manager_required": {"type": "boolean", "const": True},
                                },
                                "required": [
                                    "programme_id", "programme_version", "status", "cue_codes",
                                    "reason_code", "manager_required",
                                ],
                            },
                        },
                        "required": ["source_image_index", "outcome", "evidence_code", "type_code"],
                    },
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["catalog_candidates", "transcript", "intent", "confidence"],
        },
    },
    "required": ["reply_text", "controls"],
}


def structured_response_schema(
    *,
    prize_programme: PrizeProgramme | None = None,
) -> dict[str, Any]:
    """Return a caller-owned schema with prize output explicitly opted in."""
    schema = deepcopy(STRUCTURED_RESPONSE_SCHEMA)
    if not isinstance(prize_programme, PrizeProgramme):
        observation_properties = (
            schema["properties"]["turn_intelligence"]["properties"]
            ["image_observations"]["items"]["properties"]
        )
        observation_properties.pop("prize_certificate", None)
    return schema


def structured_response_instruction() -> str:
    """Bounded semantic contract for MIME-only JSON generation."""
    return (
        "[RESPONSE JSON CONTRACT — REQUIRED]\n"
        "Return exactly one JSON object, without Markdown or surrounding text. "
        "Allowed root keys: reply_text, controls, follow_cta, turn_intelligence. "
        "reply_text is a non-empty customer reply of at most 4000 characters. "
        "controls is required and is an array of at most 32 objects; every object "
        "has exactly kind and value. Allowed kind values: "
        + ", ".join(sorted(PROVIDER_CONTROL_KINDS))
        + '. If no authorized action is needed, use controls: [], for example '
        '{"reply_text":"Дякую за повідомлення.","controls":[]}. '
        "Ordinary questions, vacancy inquiries and thanks do not require a control. "
        "Never put bracket commands such as [MANAGER] or [STAGE:qualifying] in reply_text; "
        "represent an authorized action only in controls. "
        "manager, spam, order and catalog_link accept only JSON true. Omit an action "
        "that does not apply; never use false, null or a string boolean for it. "
        "Do not repeat a kind, except item and option. stage accepts only: "
        + ", ".join(sorted(_STAGES - _HARD_STAGES))
        + ". paid, order_created and done are server-owned stages. "
        "paylink accepts only full or prepay. product and color_variant_id accept a "
        "positive integer ID (up to 10 digits) from supplied facts; qty is a positive "
        "integer of at most 4 digits. Integer IDs and qty may also be digit strings. "
        "payment and price_quoted accept positive decimal amounts (JSON number or "
        "decimal string, at most 9 integer digits and 2 decimal places), only when "
        "authorized by supplied facts. size and fit are strings matching respectively "
        + _SIZE_RE.pattern + " and " + _FIT_RE.pattern
        + ". option is a string matching " + _OPTION_RE.pattern
        + ". item is a string product_id|qty|size|fit, optionally followed by "
        "|color_variant_id and then |option1;option2, using the same value rules; "
        "size is required, fit may be empty. show_products is a non-empty array "
        "of at most 12 positive integer catalog IDs (or their comma-separated string). "
        "objhandle is a lowercase string matching " + _OBJHANDLE_RE.pattern + ". "
        "Never emit the legacy price control. For an exact current catalog quote, "
        "use price_quoted and include product plus every known size, fit, "
        "color_variant_id, and option value that determines that configuration. "
        "A price from an existing published offer remains server-owned; do not "
        "restate it as price. "
        "follow_cta is optional; when included it has exactly include:boolean and "
        f"text:string ({_FOLLOW_MIN_LENGTH}-{_FOLLOW_MAX_LENGTH} characters). "
        "Omit optional objects when unused; do not return null, false or an empty "
        "object in their place. turn_intelligence is optional unless this turn explicitly requires it. "
        "When present it has catalog_candidates, transcript, intent, confidence, "
        "and optional audio_status and image_observations. catalog_candidates is an "
        "array of at most 8 objects with exactly product_id, confidence, evidence. "
        "Every confidence is a number from 0 to 1; product_id is a positive integer "
        "from the supplied catalog, evidence is at most 240 characters. transcript "
        "is a string of at most 4000 characters, empty when there is no audio. "
        "intent is a lowercase ASCII identifier matching [a-z][a-z0-9_]{0,63}, "
        "for example media_review. audio_status is not_applicable, transcribed, "
        "or unintelligible. "
        "image_observations is an array of at most 8 objects with source_image_index "
        "(unique zero-based integer for an attached image), outcome, evidence_code "
        "and type_code; all four fields are required. Allowed outcome: "
        + ", ".join(sorted(IMAGE_OBSERVATION_OUTCOMES))
        + ". Allowed evidence_code: "
        + ", ".join(sorted(IMAGE_EVIDENCE_CODES))
        + ". Allowed type_code: "
        + ", ".join(sorted(IMAGE_TYPE_CODES))
        + ". Do not return prize_certificate unless a separate programme contract "
        "explicitly requires it. The server strictly validates every field and "
        "rejects unknown keys or unauthorized actions."
    )


@dataclass(frozen=True)
class ResponseControl:
    """One normalized control emitted by the model."""

    kind: str
    value: str | bool


@dataclass(frozen=True)
class FollowCtaCandidate:
    """Optional growth sentence proposed by the model, separate from controls."""

    text: str


@dataclass(frozen=True)
class TurnCatalogCandidate:
    product_id: int
    confidence: float
    evidence: str


@dataclass(frozen=True)
class TurnImageObservation:
    """A provider image index, deliberately free of URLs, OCR, and raw IDs."""

    source_image_index: int
    outcome: str
    evidence_code: str = ""
    type_code: str = ""
    prize_certificate: PrizeCertificateObservation | None = None


@dataclass(frozen=True)
class TurnIntelligenceArtifact:
    catalog_candidates: tuple[TurnCatalogCandidate, ...] = ()
    transcript: str = ""
    intent: str = ""
    audio_status: str = ""
    confidence: float = 0.0
    image_observations: tuple[TurnImageObservation, ...] = ()


@dataclass(frozen=True)
class ValidatedResponse:
    """Immutable parser result.

    ``control`` is a fresh compatibility projection.  Mutating that projection
    cannot mutate the validated tuple or alter a later projection.
    """

    reply_text: str
    controls: tuple[ResponseControl, ...] = ()
    valid: bool = True
    error: str = ""
    follow_cta: FollowCtaCandidate | None = None
    turn_intelligence: TurnIntelligenceArtifact | None = None

    @property
    def control(self) -> dict[str, Any]:
        if not self.valid:
            return {}
        projected: dict[str, Any] = {}
        for entry in self.controls:
            if entry.kind in _REPEATED_KINDS:
                projected.setdefault(f"{entry.kind}s", []).append(entry.value)
            else:
                projected[entry.kind] = entry.value
        return projected

    @property
    def downstream_control(self) -> dict[str, Any]:
        """Explicit name for callers migrating from the legacy tuple parser."""
        return self.control


def _clean_text(text: object) -> str:
    value = str(text or "")
    value = _ASCII_CONTROL_SHAPED_RE.sub("", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _failure(text: object, reason: str) -> ValidatedResponse:
    # Invalid provider text must not be retained in a compatibility result:
    # stringifying an object can expose fields that were never customer text.
    clean = "" if reason == "invalid_reply_text" else _clean_text(text)
    return ValidatedResponse(reply_text=clean, valid=False, error=reason)


def _positive_integer(value: object, *, quantity: bool = False) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (re.fullmatch(_QTY_RE, value) if quantity else re.fullmatch(_ID_RE, value)):
        return None
    return value


def _positive_decimal(value: object) -> str | None:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple)):
        return None
    if isinstance(value, (int, float, Decimal)):
        value = str(value)
    if not isinstance(value, str):
        return None
    raw = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", raw):
        return None
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _normalize_item(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    parts = raw.split("|")
    if len(parts) not in (4, 5, 6):
        return None
    product_id = _positive_integer(parts[0])
    qty = _positive_integer(parts[1], quantity=True)
    size = parts[2].strip()
    fit = parts[3].strip()
    if not product_id or not qty or not size or not _SIZE_RE.fullmatch(size):
        return None
    if fit and not _FIT_RE.fullmatch(fit):
        return None
    if len(parts) >= 5 and parts[4].strip() and not _positive_integer(parts[4].strip()):
        return None
    if len(parts) == 6 and parts[5].strip():
        for option in parts[5].split(";"):
            if not _OPTION_RE.fullmatch(option.strip()):
                return None
    return raw


def _normalize_show_products(value: object) -> str | None:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return None
    if not values or len(values) > 12:
        return None
    normalized = [_positive_integer(item) for item in values]
    if any(item is None for item in normalized):
        return None
    return ",".join(normalized)


def _normalize_control(kind: object, value: object, *, legacy: bool, has_value: bool) -> ResponseControl | None:
    if not isinstance(kind, str):
        return None
    normalized_kind = kind.lower()
    canonical = _KIND_ALIASES.get(normalized_kind, normalized_kind) if legacy else normalized_kind
    if canonical not in CONTROL_KINDS:
        return None

    if canonical in _BOOLEAN_KINDS:
        if legacy:
            if has_value:
                return None
            return ResponseControl(canonical, True)
        if value is not True:
            return None
        return ResponseControl(canonical, True)
    if canonical == "stage":
        if not isinstance(value, str):
            return None
        stage = value.strip().lower()
        if stage not in _STAGES or stage in _HARD_STAGES:
            return None
        return ResponseControl(canonical, stage)
    if canonical == "paylink":
        if not isinstance(value, str) or value.strip().lower() not in {"full", "prepay"}:
            return None
        return ResponseControl(canonical, value.strip().lower())
    if canonical in {"product", "color_variant_id"}:
        normalized = _positive_integer(value)
        return ResponseControl(canonical, normalized) if normalized else None
    if canonical == "qty":
        normalized = _positive_integer(value, quantity=True)
        return ResponseControl(canonical, normalized) if normalized else None
    if canonical in {"price", "price_quoted", "payment"}:
        normalized = _positive_decimal(value)
        return ResponseControl(canonical, normalized) if normalized else None
    if canonical == "size":
        if not isinstance(value, str) or not _SIZE_RE.fullmatch(value.strip()):
            return None
        return ResponseControl(canonical, value.strip().upper())
    if canonical == "fit":
        if not isinstance(value, str) or not _FIT_RE.fullmatch(value.strip()):
            return None
        return ResponseControl(canonical, value.strip().lower())
    if canonical == "item":
        normalized = _normalize_item(value)
        return ResponseControl(canonical, normalized) if normalized else None
    if canonical == "option":
        if not isinstance(value, str) or not _OPTION_RE.fullmatch(value.strip()):
            return None
        return ResponseControl(canonical, value.strip())
    if canonical == "show_products":
        normalized = _normalize_show_products(value)
        return ResponseControl(canonical, normalized) if normalized else None
    if canonical == "objhandle":
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return ResponseControl(canonical, normalized) if _OBJHANDLE_RE.fullmatch(normalized) else None
    return None


def _append_control(controls: list[ResponseControl], entry: ResponseControl) -> bool:
    if entry.kind in _REPEATED_KINDS:
        controls.append(entry)
        return True
    for previous in controls:
        if previous.kind == entry.kind:
            # A singleton is an assertion, not an ordered list.  Repeating it
            # is ambiguous even when the values happen to be equal: duplicate
            # model output must fail closed instead of being silently accepted.
            return False
    controls.append(entry)
    return True


def _follow_emoji_count(text: str) -> int:
    return sum(
        1
        for char in text
        if (0x1F000 <= ord(char) <= 0x1FAFF)
        or (0x2600 <= ord(char) <= 0x27BF)
    )


def follow_cta_static_error(text: object) -> str:
    """Return the shared lexical rejection reason for model-authored CTA copy."""
    candidate = str(text or "").strip()
    if not (_FOLLOW_MIN_LENGTH <= len(candidate) <= _FOLLOW_MAX_LENGTH):
        return "candidate_length"
    if "\n" in candidate or "\r" in candidate:
        return "candidate_format"
    lexical_candidate = unicodedata.normalize("NFKC", candidate)
    lexical_candidate = _FOLLOW_INVISIBLE_RE.sub("", lexical_candidate)
    if _FOLLOW_URL_RE.search(lexical_candidate):
        return "candidate_url"
    if _FOLLOW_INVISIBLE_RE.search(candidate):
        return "candidate_format"
    if _FOLLOW_MARKDOWN_RE.search(lexical_candidate) or _ASCII_CONTROL_SHAPED_RE.search(lexical_candidate):
        return "candidate_control"
    if (
        _FOLLOW_PERCENT_RE.search(lexical_candidate)
        or _FOLLOW_DISCOUNT_RE.search(lexical_candidate)
        or _FOLLOW_PROMO_TOKEN_RE.search(lexical_candidate)
    ):
        return "candidate_discount"
    if _FOLLOW_URGENCY_RE.search(lexical_candidate):
        return "candidate_urgency"
    if _FOLLOW_SURVEILLANCE_RE.search(lexical_candidate):
        return "candidate_surveillance"
    if _FOLLOW_FALSE_PROMISE_RE.search(lexical_candidate):
        return "candidate_false_promise"
    if _FOLLOW_IMPERATIVE_RE.search(lexical_candidate):
        return "candidate_imperative"
    if "?" in candidate or "？" in candidate:
        return "candidate_question"
    punctuation = re.findall(r"[.!?。！？]", candidate)
    if len(punctuation) > 1 or candidate.count("?") > 1 or candidate.count("？") > 1:
        return "candidate_sentence_count"
    if _follow_emoji_count(candidate) > 1:
        return "candidate_emoji"
    return ""


def _parse_follow_candidate(payload: object) -> FollowCtaCandidate | None:
    """Parse optional CTA syntax without invalidating the base response."""
    if not isinstance(payload, dict) or set(payload) != {"include", "text"}:
        return None
    if payload.get("include") is not True:
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if follow_cta_static_error(text):
        return None
    return FollowCtaCandidate(text=text)


def _parse_turn_intelligence(
    payload: object,
    *,
    prize_programme: PrizeProgramme | None = None,
) -> TurnIntelligenceArtifact | None:
    required = {"catalog_candidates", "transcript", "intent", "confidence"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or not set(payload).issubset(
            required | {"audio_status", "image_observations"}
        )
    ):
        return None
    candidates = payload.get("catalog_candidates")
    if not isinstance(candidates, list) or len(candidates) > 8:
        return None
    parsed_candidates = []
    seen = set()
    for raw in candidates:
        if not isinstance(raw, dict) or set(raw) != {
            "product_id", "confidence", "evidence"
        }:
            return None
        product_id = _positive_integer(raw.get("product_id"))
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            return None
        evidence = _clean_text(raw.get("evidence"))[:240]
        if (
            product_id is None
            or not 0 <= confidence <= 1
            or int(product_id) in seen
        ):
            return None
        seen.add(int(product_id))
        parsed_candidates.append(TurnCatalogCandidate(
            product_id=int(product_id),
            confidence=confidence,
            evidence=evidence,
        ))
    observations = payload.get("image_observations", [])
    if not isinstance(observations, list) or len(observations) > 8:
        return None
    parsed_observations = []
    seen_image_indexes = set()
    for raw in observations:
        if not isinstance(raw, dict) or not set(raw).issubset({
            "source_image_index", "outcome", "evidence_code", "type_code", "prize_certificate",
        }) or not {"source_image_index", "outcome"}.issubset(raw):
            return None
        source_image_index = raw.get("source_image_index")
        # Index zero is valid, but JSON strings, booleans, and floats are not.
        if isinstance(source_image_index, bool) or not isinstance(source_image_index, int):
            return None
        outcome = str(raw.get("outcome") or "").strip().lower()
        evidence_code = str(raw.get("evidence_code") or "").strip().lower()
        type_code = str(raw.get("type_code") or "").strip().lower()
        if (
            source_image_index < 0
            or source_image_index in seen_image_indexes
            or outcome not in IMAGE_OBSERVATION_OUTCOMES
            or evidence_code not in IMAGE_EVIDENCE_CODES
            or type_code not in IMAGE_TYPE_CODES
        ):
            return None
        prize_certificate = None
        if "prize_certificate" in raw:
            if type_code != "certificate":
                return None
            prize_certificate = validate_prize_observation(
                raw.get("prize_certificate"), programme=prize_programme,
            )
            if prize_certificate is None:
                return None
        seen_image_indexes.add(source_image_index)
        parsed_observations.append(TurnImageObservation(
            source_image_index=source_image_index,
            outcome=outcome,
            evidence_code=evidence_code,
            type_code=type_code,
            prize_certificate=prize_certificate,
        ))
    transcript = payload.get("transcript")
    intent = payload.get("intent")
    audio_status = str(payload.get("audio_status") or "").strip().casefold()
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(transcript, str)
        or len(transcript) > 4000
        or not isinstance(intent, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", intent.strip().lower())
        or audio_status not in {"", "not_applicable", "transcribed", "unintelligible"}
        or not 0 <= confidence <= 1
    ):
        return None
    return TurnIntelligenceArtifact(
        catalog_candidates=tuple(parsed_candidates),
        transcript=_clean_text(transcript)[:4000],
        intent=intent.strip().lower(),
        audio_status=audio_status,
        confidence=confidence,
        image_observations=tuple(parsed_observations),
    )


def parse_structured_response(
    payload: object,
    *,
    prize_programme: PrizeProgramme | None = None,
) -> ValidatedResponse:
    """Validate a structured model response without performing side effects."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _failure("", "invalid_json")
    if (
        not isinstance(payload, dict)
        or not {"reply_text", "controls"}.issubset(payload)
        or not set(payload).issubset({
            "reply_text", "controls", "follow_cta", "turn_intelligence"
        })
    ):
        return _failure(payload.get("reply_text", "") if isinstance(payload, dict) else "", "malformed_payload")
    reply_text = payload.get("reply_text")
    controls_payload = payload.get("controls")
    if not isinstance(reply_text, str):
        return _failure("", "invalid_reply_text")
    if not isinstance(controls_payload, list):
        return _failure(reply_text, "malformed_payload")
    if not reply_text.strip() or len(reply_text) > 4000:
        return _failure(reply_text, "invalid_reply_text")
    if len(controls_payload) > 32:
        return _failure(reply_text, "too_many_controls")
    if _ASCII_CONTROL_SHAPED_RE.search(reply_text):
        return _failure(reply_text, "control_token_in_reply_text")

    controls: list[ResponseControl] = []
    for raw in controls_payload:
        if not isinstance(raw, dict) or set(raw) != {"kind", "value"}:
            return _failure(reply_text, "malformed_control")
        entry = _normalize_control(raw.get("kind"), raw.get("value"), legacy=False, has_value=True)
        if entry is None:
            return _failure(reply_text, "invalid_control")
        if not _append_control(controls, entry):
            return _failure(reply_text, "conflicting_control")
    follow_cta = (
        _parse_follow_candidate(payload.get("follow_cta"))
        if "follow_cta" in payload
        else None
    )
    turn_intelligence = (
        _parse_turn_intelligence(
            payload.get("turn_intelligence"), prize_programme=prize_programme,
        )
        if "turn_intelligence" in payload
        else None
    )
    if "turn_intelligence" in payload and turn_intelligence is None:
        return _failure(reply_text, "invalid_turn_intelligence")
    return ValidatedResponse(
        reply_text=reply_text.strip(),
        controls=tuple(controls),
        follow_cta=follow_cta,
        turn_intelligence=turn_intelligence,
    )


def parse_legacy_response(text: object) -> ValidatedResponse:
    """Parse the historical uppercase bracket protocol with fail-closed output."""
    if not isinstance(text, str):
        return _failure("", "malformed_text")
    shaped_tokens = list(_ASCII_CONTROL_SHAPED_RE.finditer(text))
    tokens = list(_ASCII_CLOSED_CONTROL_TOKEN_RE.finditer(text))
    if any(not match.group(0).endswith("]") for match in shaped_tokens):
        return _failure(text, "malformed_token")
    controls: list[ResponseControl] = []
    invalid_reason = ""
    for token in tokens:
        body = token.group(0)[1:-1]
        kind, separator, raw_value = body.partition(":")
        if not _KNOWN_KIND_RE.fullmatch(kind):
            invalid_reason = "unknown_or_case_control"
            continue
        canonical = _KIND_ALIASES.get(kind.lower(), kind.lower())
        has_value = bool(separator)
        value: object = raw_value.strip() if separator else None
        entry = _normalize_control(canonical, value, legacy=True, has_value=has_value)
        if entry is None:
            invalid_reason = invalid_reason or "invalid_control"
            continue
        if not _append_control(controls, entry):
            invalid_reason = invalid_reason or "conflicting_control"
    clean = _clean_text(text)
    if invalid_reason:
        return ValidatedResponse(reply_text=clean, valid=False, error=invalid_reason)
    return ValidatedResponse(reply_text=clean, controls=tuple(controls))


# Names useful to callers during migration from the old tuple parser.
validate_structured_response = parse_structured_response
parse_model_response = parse_structured_response
parse_legacy_tags = parse_legacy_response


__all__ = [
    "CONTROL_KINDS",
    "PROVIDER_CONTROL_KINDS",
    "FollowCtaCandidate",
    "ResponseControl",
    "STRUCTURED_RESPONSE_SCHEMA",
    "ValidatedResponse",
    "follow_cta_static_error",
    "parse_legacy_response",
    "parse_legacy_tags",
    "parse_model_response",
    "parse_structured_response",
    "structured_response_instruction",
    "structured_response_schema",
    "validate_structured_response",
]
