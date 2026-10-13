"""PII-free Analysis V2 normalization and fail-soft shadow persistence."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.signing import salted_hmac
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.models import (
    IgAnalysisProposal,
    IgClient,
    IgConversationAnalysisResult,
    IgConversationAnalysisSnapshot,
    IgObjection,
)


RESULT_SCHEMA_VERSION = "analysis-v2.2"
NORMALIZER_VERSION = "analysis-v2-normalizer.1"
EXTENDED_PROMPT_VERSION = "2026-08-31.analysis-v2-lang.v2"
MAX_EVIDENCE = 40
MAX_PROPOSALS = 12
MAX_TOKEN_COUNT = 2**63 - 1
MAX_LATENCY_MS = 24 * 60 * 60 * 1000

_SAFE_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,96}$")
_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|reveal\s+(?:the\s+)?(?:system\s+)?prompt|"
    r"system\s*:\s*|developer\s*:\s*|jailbreak|"
    r"забудь\s+(?:усі|всі|все|предыдущ)|ігноруй\s+(?:попередн|інструкц)|"
    r"раскрой\s+(?:системн|промпт))",
    re.IGNORECASE,
)
_LANGUAGES = frozenset({"uk", "ru", "en", "mixed", "unknown"})
_CONFLICT_CODES = frozenset({
    "product_conflict",
    "recipient_conflict",
    "line_conflict",
    "payment_claim_conflict",
    "manager_customer_conflict",
    "artifact_conflict",
    "live_agent_inconsistency",
})
_UNCERTAINTY_CODES = frozenset({
    "analysis_v2_missing",
    "custom_print_user_evidence_missing",
    "deferred_evidence_missing",
    "evidence_unverified",
    "injection_signal",
    "manager_evidence_not_customer_intent",
    "payment_unverified",
    "probability_evidence_missing",
    "product",
    "size",
})
_CLAIM_CODES = frozenset({
    "interaction",
    "purchase_intent",
    "objection",
    "deferred_intent",
    "repeat_intent",
    "injection_risk",
    "conflict",
    "explicit_no_buy",
    "opt_out",
})
_DEFERRED_CONDITIONS = frozenset({
    "customer_date",
    "after_event",
    "payday",
    "indefinite",
})
_REPEAT_KINDS = frozenset({
    "explicit_more", "reorder", "gift", "another_recipient",
})
_FUNNEL_PROPOSALS = frozenset({
    IgAnalysisProposal.ProposalType.CLOSE_NODE,
    IgAnalysisProposal.ProposalType.INVALIDATE_NODE,
    IgAnalysisProposal.ProposalType.OPEN_SUBFUNNEL,
    IgAnalysisProposal.ProposalType.SWITCH_ACTIVE_LINE,
})
_RESULT_DIGEST_FIELDS = (
    "legacy_snapshot_id", "client_id", "commercial_episode_id", "line_id",
    "watermark_message_id", "job_revision", "materiality_event_highwater",
    "materiality_digest", "authority_digest", "artifact_digest",
    "state_correlation", "result_schema_version", "normalizer_version",
    "source_kind", "interaction_type", "score_band", "detected_language",
    "purchase_probability", "purchase_confidence", "probability_basis",
    "evidence_manifest", "customer_evidence_count", "manager_evidence_count",
    "authority_evidence_count", "active_objection_type",
    "active_objection_confidence", "deferred_kind", "deferred_until",
    "deferred_condition_code", "repeat_intent_kind",
    "repeat_intent_confidence", "prior_purchase_count", "ltv_signal",
    "injection_risk", "injection_evidence_message_ids", "has_conflicts",
    "conflict_codes", "uncertainty_codes", "analysis_model", "prompt_version",
    "routing_policy_version", "reasoning_policy_version", "project_slot",
    "gemini_request_ref", "usage_status", "prompt_tokens", "thoughts_tokens",
    "candidates_tokens", "total_tokens", "analysis_latency_ms", "analyzed_at",
)
_RESULT_DIGEST_FIELDS_V22 = (
    *_RESULT_DIGEST_FIELDS,
    "language_evidence_message_ids",
)
_PROPOSAL_DIGEST_FIELDS = (
    "proposal_type", "target_scope", "target_definition_key",
    "target_definition_version", "target_key", "typed_value",
    "evidence_message_ids", "confidence", "source_result_digest",
    "expected_materiality_digest", "expected_authority_digest",
    "expected_state_correlation",
)


@dataclass(frozen=True, slots=True)
class NormalizedAnalysisV2:
    result_values: dict
    proposals: tuple[dict, ...]


def analysis_v2_mode() -> str:
    value = str(getattr(settings, "IG_ANALYSIS_V2_MODE", "off") or "off")
    value = value.strip().casefold()
    return value if value in {"off", "shadow"} else "off"


def shadow_enabled() -> bool:
    if analysis_v2_mode() != "shadow":
        return False
    from management.services.ig_analysis_materiality import materiality_mode

    return materiality_mode() == "shadow"


def state_correlation(required_state_fingerprint: str) -> str:
    """Keyed correlation for a content-derived legacy fingerprint."""
    value = str(required_state_fingerprint or "")
    if not value:
        return ""
    return salted_hmac(
        "management.analysis-v2.state-correlation",
        value,
        algorithm="sha256",
    ).hexdigest()[:64]


def _sha(payload) -> str:
    def encode(value):
        if isinstance(value, datetime):
            if timezone.is_naive(value):
                value = timezone.make_aware(value)
            return value.astimezone(datetime_timezone.utc).isoformat()
        if isinstance(value, Decimal):
            return str(value)
        return str(value)

    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=encode,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def result_digest_for_values(values: dict) -> str:
    fields = (
        _RESULT_DIGEST_FIELDS_V22
        if values.get("result_schema_version") == "analysis-v2.2"
        else _RESULT_DIGEST_FIELDS
    )
    return _sha({field: values.get(field) for field in fields})


def result_digest_for_instance(result: IgConversationAnalysisResult) -> str:
    return result_digest_for_values({
        field: getattr(result, field)
        for field in _RESULT_DIGEST_FIELDS_V22
    })


def proposal_key_for_values(*, result_key: str, ordinal: int, values: dict) -> str:
    payload = {
        "result_key": str(result_key),
        "ordinal": int(ordinal),
        **{field: values.get(field) for field in _PROPOSAL_DIGEST_FIELDS},
    }
    return f"analysis-proposal:{_sha(payload)}"


def proposal_key_for_instance(proposal: IgAnalysisProposal) -> str:
    return proposal_key_for_values(
        result_key=proposal.analysis_result.result_key,
        ordinal=proposal.ordinal,
        values={field: getattr(proposal, field) for field in _PROPOSAL_DIGEST_FIELDS},
    )


def _decimal_01(value, *, nullable: bool = False):
    if value in (None, "") and nullable:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None if nullable else Decimal("0.0000")
    return max(Decimal("0"), min(Decimal("1"), parsed)).quantize(
        Decimal("0.0001")
    )


def _bounded_int(value, *, maximum=MAX_TOKEN_COUNT) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, maximum))


def _message_ids(values, by_id: dict[int, dict], *, roles=None) -> list[int]:
    allowed_roles = set(roles or ())
    result = []
    for value in values or ():
        try:
            message_id = int(value)
        except (TypeError, ValueError):
            continue
        source = by_id.get(message_id)
        if not isinstance(source, dict):
            continue
        if allowed_roles and source.get("role") not in allowed_roles:
            continue
        if message_id not in result:
            result.append(message_id)
        if len(result) >= MAX_EVIDENCE:
            break
    return result


def _safe_code(value: object, *, maximum: int = 96) -> str:
    normalized = str(value or "").strip().casefold()[:maximum]
    return normalized if _SAFE_CODE_RE.fullmatch(normalized) else ""


def _build_evidence_manifest(
    *,
    legacy_normalized: dict,
    analysis_v2: dict,
    by_id: dict[int, dict],
) -> tuple[list[dict], dict[str, list[int]]]:
    claims: dict[int, set[str]] = {}

    def add(values, claim_code, roles=None, predicate=None):
        if claim_code not in _CLAIM_CODES:
            return
        for message_id in _message_ids(values, by_id, roles=roles):
            if predicate and not predicate(str(by_id[message_id].get("text") or "")):
                continue
            claims.setdefault(message_id, set()).add(claim_code)

    legacy_ids = [
        row.get("message_id")
        for row in (legacy_normalized.get("evidence") or [])
        if isinstance(row, dict)
    ]
    add(legacy_ids, "interaction")
    legacy_repeat = legacy_normalized.get("repeat_intent")
    if isinstance(legacy_repeat, dict):
        add(
            legacy_repeat.get("evidence_message_ids"),
            "repeat_intent",
            {"user"},
        )
    purchase = analysis_v2.get("purchase_intent")
    if isinstance(purchase, dict):
        from management.services.bot_sales_classifier import (
            PAYMENT_RE,
            PRICE_RE,
            PRODUCT_RE,
            PURCHASE_DECISION_RE,
            SIZE_RE,
        )

        add(
            purchase.get("evidence_message_ids"),
            "purchase_intent",
            {"user"},
            lambda text: any(pattern.search(text) for pattern in (
                PURCHASE_DECISION_RE, PAYMENT_RE, PRODUCT_RE, PRICE_RE, SIZE_RE,
            )),
        )
    objection = analysis_v2.get("active_objection")
    if isinstance(objection, dict):
        add(objection.get("evidence_message_ids"), "objection", {"user"})
    deferred = analysis_v2.get("deferred_intent")
    if isinstance(deferred, dict):
        add(deferred.get("evidence_message_ids"), "deferred_intent", {"user"})
    ltv = analysis_v2.get("ltv_signals")
    if isinstance(ltv, dict):
        add(ltv.get("evidence_message_ids"), "repeat_intent", {"user"})
    risk = analysis_v2.get("adversarial_risk")
    if isinstance(risk, dict):
        add(risk.get("evidence_message_ids"), "injection_risk")
    for conflict in analysis_v2.get("conflicts") or []:
        if isinstance(conflict, dict):
            add(conflict.get("evidence_message_ids"), "conflict")
    from management.services.bot_sales_classifier import NO_BUY_RE, is_explicit_opt_out

    user_ids = [
        message_id
        for message_id, source in by_id.items()
        if isinstance(source, dict) and source.get("role") == "user"
    ]
    add(user_ids, "explicit_no_buy", {"user"}, lambda text: bool(NO_BUY_RE.search(text)))
    add(user_ids, "opt_out", {"user"}, is_explicit_opt_out)
    add(
        list(by_id),
        "injection_risk",
        {"user", "manager", "model"},
        lambda text: bool(_INJECTION_RE.search(text)),
    )

    manifest = []
    by_claim = {claim: [] for claim in _CLAIM_CODES}
    for message_id in sorted(claims)[:MAX_EVIDENCE]:
        source = by_id[message_id]
        claim_codes = sorted(claims[message_id])
        manifest.append({
            "message_id": message_id,
            "source_role": str(source.get("role") or "system")[:16],
            "claim_codes": claim_codes,
        })
        for claim_code in claim_codes:
            by_claim[claim_code].append(message_id)
    return manifest, by_claim


def _injection_signal(analysis_v2: dict, by_id: dict[int, dict]):
    raw = analysis_v2.get("adversarial_risk")
    raw = raw if isinstance(raw, dict) else {}
    requested_level = str(raw.get("level") or "none").strip().casefold()
    requested_ids = _message_ids(raw.get("evidence_message_ids"), by_id)
    deterministic_ids = [
        message_id
        for message_id, source in by_id.items()
        if isinstance(source, dict)
        and source.get("role") in {"user", "manager", "model"}
        and _INJECTION_RE.search(str(source.get("text") or ""))
    ][:MAX_EVIDENCE]
    evidence_ids = sorted(set(requested_ids + deterministic_ids))[:MAX_EVIDENCE]
    if not evidence_ids:
        return IgConversationAnalysisResult.InjectionRisk.NONE, []
    if requested_level == "high" and requested_ids:
        return IgConversationAnalysisResult.InjectionRisk.HIGH, evidence_ids
    return IgConversationAnalysisResult.InjectionRisk.SUSPECTED, evidence_ids


def _deferred_intent(analysis_v2: dict, by_id: dict[int, dict], analyzed_at):
    raw = analysis_v2.get("deferred_intent")
    raw = raw if isinstance(raw, dict) else {}
    kind = str(raw.get("kind") or "none").strip().casefold()
    valid_kinds = set(IgConversationAnalysisResult.DeferredKind.values)
    evidence_ids = _message_ids(
        raw.get("evidence_message_ids"), by_id, roles={"user"}
    )
    if kind not in valid_kinds or kind == "none" or not evidence_ids:
        return "none", None, "", []
    condition = _safe_code(raw.get("condition_code"), maximum=32)
    expected_condition = {
        IgConversationAnalysisResult.DeferredKind.DATE: "customer_date",
        IgConversationAnalysisResult.DeferredKind.EVENT: "after_event",
        IgConversationAnalysisResult.DeferredKind.PAYDAY: "payday",
        IgConversationAnalysisResult.DeferredKind.INDEFINITE: "indefinite",
    }.get(kind)
    if condition not in _DEFERRED_CONDITIONS or condition != expected_condition:
        return "none", None, "", []
    deferred_until = parse_datetime(str(raw.get("deferred_until") or ""))
    if deferred_until is not None and timezone.is_naive(deferred_until):
        deferred_until = timezone.make_aware(deferred_until)
    if deferred_until is not None and not (
        analyzed_at < deferred_until <= analyzed_at + timedelta(days=366)
    ):
        deferred_until = None
    if kind != IgConversationAnalysisResult.DeferredKind.DATE:
        deferred_until = None
    if kind == IgConversationAnalysisResult.DeferredKind.DATE and deferred_until is None:
        return "none", None, "", []
    return kind, deferred_until, condition, evidence_ids


def _provider_funnel_proposals(analysis_v2: dict, by_id: dict[int, dict]):
    proposals = []
    for raw in analysis_v2.get("proposals") or []:
        if not isinstance(raw, dict):
            continue
        proposal_type = str(raw.get("type") or "").strip().casefold()
        if proposal_type not in _FUNNEL_PROPOSALS:
            continue
        target_scope = str(raw.get("target_scope") or "funnel_node").strip().casefold()
        if target_scope not in IgAnalysisProposal.TargetScope.values:
            continue
        evidence_ids = _message_ids(
            raw.get("evidence_message_ids"), by_id, roles={"user"}
        )
        if not evidence_ids:
            continue
        definition_key = _safe_code(raw.get("definition_key"))
        target_key = _safe_code(raw.get("target_key"))
        if not target_key:
            continue
        proposals.append({
            "proposal_type": proposal_type,
            "target_scope": target_scope,
            "target_definition_key": definition_key,
            "target_definition_version": _safe_code(
                raw.get("definition_version"), maximum=32
            ),
            "target_key": target_key,
            "typed_value": {},
            "evidence_message_ids": evidence_ids,
            "confidence": _decimal_01(raw.get("confidence")),
        })
        if len(proposals) >= MAX_PROPOSALS:
            break
    return proposals


def _prior_purchase_count(client) -> int:
    """Count confirmed purchases preceding this cycle, not its own settlement."""
    from management.models import IgCommercialEpisode
    from management.services.bot_payment_truth import confirmed_purchase_units

    units = confirmed_purchase_units(client)
    episodes = IgCommercialEpisode.objects.filter(client_id=client.pk).exclude(
        primary_payment_review__status="superseded",
    ).select_related(
        "primary_payment_review", "deal",
    )
    episode = episodes.filter(pk=getattr(client, "current_commercial_episode_id", None),
        open_slot=1, state__in=("active", "order_created")).first()
    episode = episode or episodes.filter(open_slot=1, state__in=("active", "order_created")).first()
    if episode is None:
        episode = episodes.filter(state="fulfilled", intended_order_id__isnull=False).order_by("-sequence", "-id").first()
    own_order_id = None
    own_keys = set()
    if episode:
        own_order_id = episode.intended_order_id
        if episode.primary_payment_review_id:
            own_keys.add(("review", episode.primary_payment_review_id))
        if episode.deal_id:
            own_keys.add(("deal", episode.deal_id))
        if not own_order_id and episode.primary_payment_review_id:
            review = episode.primary_payment_review
            if review.client_id == client.pk and review.status != "superseded":
                own_order_id = review.order_id
        if not own_order_id and episode.deal_id:
            own_order_id = episode.deal.order_id
    if own_order_id:
        own_keys.add(("order", own_order_id))
    return sum(1 for unit in units if tuple(unit.get("purchase_key") or ()) not in own_keys
        and not (own_order_id and unit["order_id"] == own_order_id))


def normalize_analysis_v2(
    *,
    parsed: dict,
    legacy_normalized: dict,
    by_id: dict[int, dict],
    client: IgClient,
    truth_state: dict,
    analyzed_at,
) -> NormalizedAnalysisV2:
    parsed = parsed if isinstance(parsed, dict) else {}
    analysis_v2 = parsed.get("analysis_v2")
    analysis_v2 = analysis_v2 if isinstance(analysis_v2, dict) else {}
    uncertainties = {
        str(value).strip().casefold()
        for value in legacy_normalized.get("uncertainties") or []
        if str(value).strip().casefold() in _UNCERTAINTY_CODES
    }
    if not analysis_v2:
        uncertainties.add("analysis_v2_missing")

    manifest, by_claim = _build_evidence_manifest(
        legacy_normalized=legacy_normalized,
        analysis_v2=analysis_v2,
        by_id=by_id,
    )
    # Э3.5 / NEW-ANALYSIS-002. Межа ролі (доказ менеджера ≠ намір клієнта) тепер
    # спільна для legacy і V2 normalizer: обидва застосовують одні й ті самі
    # правила, тому вони більше не можуть розійтися в трактуванні ролі автора.
    from management.services import ig_analysis_roles as analysis_roles

    coverage = analysis_roles.manifest_role_coverage(manifest)
    customer_ids = sorted(coverage.customer_message_ids)
    manager_ids = sorted(coverage.manager_message_ids)
    purchase_raw = analysis_v2.get("purchase_intent")
    purchase_raw = purchase_raw if isinstance(purchase_raw, dict) else {}
    probability_ids = by_claim.get("purchase_intent") or []
    no_buy_ids = by_claim.get("explicit_no_buy") or []
    opt_out_ids = by_claim.get("opt_out") or []
    interaction_type = str(
        legacy_normalized.get("interaction_type")
        or IgConversationAnalysisSnapshot.InteractionType.UNKNOWN
    )
    score_band = str(
        legacy_normalized.get("score_band")
        or IgConversationAnalysisSnapshot.Band.COLD
    )
    window = analysis_roles.window_roles(by_id)
    boundary = analysis_roles.apply_source_role_boundary(
        interaction_type=interaction_type,
        score_band=score_band,
        purchase_probability=legacy_normalized.get("purchase_probability"),
        confidence=legacy_normalized.get("confidence"),
        uncertainties=legacy_normalized.get("uncertainties") or [],
        coverage=coverage,
        window=window,
        verified_payment=bool(truth_state.get("verified_payment")),
    )
    if boundary.changed:
        interaction_type = boundary.interaction_type
        score_band = boundary.score_band

    if (
        opt_out_ids
        and interaction_type == IgConversationAnalysisSnapshot.InteractionType.OPT_OUT
    ):
        interaction_type = IgConversationAnalysisSnapshot.InteractionType.OPT_OUT
        score_band = IgConversationAnalysisSnapshot.Band.OPTED_OUT
        probability = Decimal("0.0000")
        probability_confidence = Decimal("1.0000")
        probability_basis = IgConversationAnalysisResult.ProbabilityBasis.DETERMINISTIC_OPT_OUT
    elif (
        no_buy_ids
        and interaction_type == IgConversationAnalysisSnapshot.InteractionType.EXPLICIT_NO_BUY
    ):
        interaction_type = IgConversationAnalysisSnapshot.InteractionType.EXPLICIT_NO_BUY
        score_band = IgConversationAnalysisSnapshot.Band.LOST
        probability = Decimal("0.0000")
        probability_confidence = Decimal("1.0000")
        probability_basis = IgConversationAnalysisResult.ProbabilityBasis.DETERMINISTIC_NO_BUY
    elif probability_ids:
        probability = _decimal_01(
            purchase_raw.get("probability", legacy_normalized.get("purchase_probability")),
            nullable=True,
        )
        probability_confidence = _decimal_01(
            purchase_raw.get("confidence", legacy_normalized.get("confidence")),
            nullable=True,
        )
        probability_basis = (
            IgConversationAnalysisResult.ProbabilityBasis.CUSTOMER_EVIDENCE
            if probability is not None
            else IgConversationAnalysisResult.ProbabilityBasis.INSUFFICIENT_EVIDENCE
        )
        if probability is None:
            probability_confidence = None
            uncertainties.add("probability_evidence_missing")
    else:
        probability = None
        probability_confidence = None
        probability_basis = IgConversationAnalysisResult.ProbabilityBasis.INSUFFICIENT_EVIDENCE
        uncertainties.add("probability_evidence_missing")

    objection_raw = analysis_v2.get("active_objection")
    objection_raw = objection_raw if isinstance(objection_raw, dict) else {}
    objection_type = str(objection_raw.get("type") or "").strip().casefold()
    objection_ids = by_claim.get("objection") or []
    if objection_type not in IgObjection.Type.values or not objection_ids:
        objection_type = ""
        objection_confidence = None
    else:
        objection_confidence = _decimal_01(
            objection_raw.get("confidence"), nullable=True
        )

    deferred_kind, deferred_until, deferred_condition, deferred_ids = (
        _deferred_intent(analysis_v2, by_id, analyzed_at)
    )
    repeat = legacy_normalized.get("repeat_intent")
    repeat = repeat if isinstance(repeat, dict) else {}
    repeat_kind = str(repeat.get("kind") or "")
    repeat_ids = _message_ids(
        repeat.get("evidence_message_ids"), by_id, roles={"user"}
    )
    if repeat_kind not in _REPEAT_KINDS or not repeat_ids:
        repeat_kind = ""
        repeat_confidence = None
    else:
        repeat_confidence = _decimal_01(repeat.get("confidence"), nullable=True)

    prior_purchase_count = _prior_purchase_count(client)
    if prior_purchase_count > 0:
        ltv_signal = IgConversationAnalysisResult.LtvSignal.REPEAT_CUSTOMER
    elif repeat_kind:
        ltv_signal = IgConversationAnalysisResult.LtvSignal.REACTIVATION
    else:
        ltv_signal = IgConversationAnalysisResult.LtvSignal.FIRST_PURCHASE

    injection_risk, injection_ids = _injection_signal(analysis_v2, by_id)
    if injection_ids:
        uncertainties.add("injection_signal")
    conflict_codes = sorted({
        str(row.get("code") or "").strip().casefold()
        for row in (analysis_v2.get("conflicts") or [])
        if isinstance(row, dict)
        and str(row.get("code") or "").strip().casefold() in _CONFLICT_CODES
    })
    language_ids = _message_ids(
        analysis_v2.get("language_evidence_message_ids"),
        by_id,
        roles={"user"},
    )
    if analysis_v2.get("schema_version") != 2:
        language_ids = []
    language = str(analysis_v2.get("detected_language") or "").strip().casefold()
    if language not in _LANGUAGES or not language_ids:
        language = ""
        language_ids = []

    proposals = []
    if probability is not None and probability_basis == (
        IgConversationAnalysisResult.ProbabilityBasis.CUSTOMER_EVIDENCE
    ):
        proposals.append({
            "proposal_type": IgAnalysisProposal.ProposalType.UPDATE_PROBABILITY,
            "target_scope": IgAnalysisProposal.TargetScope.CLIENT,
            "typed_value": {
                "probability": str(probability),
                "basis": str(probability_basis),
            },
            "evidence_message_ids": probability_ids,
            "confidence": probability_confidence or Decimal("0.0000"),
        })
    if objection_type:
        proposals.append({
            "proposal_type": IgAnalysisProposal.ProposalType.RECORD_OBJECTION,
            "target_scope": IgAnalysisProposal.TargetScope.EPISODE,
            "typed_value": {"objection_type": objection_type},
            "evidence_message_ids": objection_ids,
            "confidence": objection_confidence or Decimal("0.0000"),
        })
    if deferred_kind != IgConversationAnalysisResult.DeferredKind.NONE:
        proposals.append({
            "proposal_type": IgAnalysisProposal.ProposalType.RECORD_DEFERRED_INTENT,
            "target_scope": IgAnalysisProposal.TargetScope.EPISODE,
            "typed_value": {
                "kind": deferred_kind,
                "condition_code": deferred_condition,
                "deferred_until": deferred_until.isoformat() if deferred_until else "",
            },
            "evidence_message_ids": deferred_ids,
            "confidence": Decimal("1.0000"),
        })
    if repeat_kind:
        proposals.append({
            "proposal_type": IgAnalysisProposal.ProposalType.START_REPEAT_EPISODE,
            "target_scope": IgAnalysisProposal.TargetScope.EPISODE,
            "typed_value": {"repeat_kind": repeat_kind},
            "evidence_message_ids": repeat_ids,
            "confidence": repeat_confidence or Decimal("0.0000"),
        })
    if conflict_codes:
        proposals.append({
            "proposal_type": IgAnalysisProposal.ProposalType.REQUEST_CLARIFICATION,
            "target_scope": IgAnalysisProposal.TargetScope.EPISODE,
            "typed_value": {"reason_codes": conflict_codes},
            "evidence_message_ids": customer_ids,
            "confidence": Decimal("1.0000"),
        })
    proposals.extend(_provider_funnel_proposals(analysis_v2, by_id))

    result_values = {
        "interaction_type": interaction_type,
        "score_band": score_band,
        "detected_language": language,
        "language_evidence_message_ids": language_ids,
        "purchase_probability": probability,
        "purchase_confidence": probability_confidence,
        "probability_basis": probability_basis,
        "evidence_manifest": manifest,
        "customer_evidence_count": len(customer_ids),
        "manager_evidence_count": len(manager_ids),
        "authority_evidence_count": 1 if truth_state else 0,
        "active_objection_type": objection_type,
        "active_objection_confidence": objection_confidence,
        "deferred_kind": deferred_kind,
        "deferred_until": deferred_until,
        "deferred_condition_code": deferred_condition,
        "repeat_intent_kind": repeat_kind,
        "repeat_intent_confidence": repeat_confidence,
        "prior_purchase_count": prior_purchase_count,
        "ltv_signal": ltv_signal,
        "injection_risk": injection_risk,
        "injection_evidence_message_ids": injection_ids,
        "has_conflicts": bool(conflict_codes),
        "conflict_codes": conflict_codes,
        "uncertainty_codes": sorted(uncertainties),
    }
    return NormalizedAnalysisV2(
        result_values=result_values,
        proposals=tuple(proposals[:MAX_PROPOSALS]),
    )


def _usage_values(result: dict, meta: dict) -> dict:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    prompt = _bounded_int(
        usage.get("promptTokenCount") or usage.get("prompt_token_count")
    )
    thoughts = _bounded_int(
        usage.get("thoughtsTokenCount")
        or usage.get("thoughts_token_count")
        or meta.get("thoughts_tokens")
    )
    candidates = _bounded_int(
        usage.get("candidatesTokenCount")
        or usage.get("candidates_token_count")
        or meta.get("candidates_tokens")
    )
    total = _bounded_int(
        usage.get("totalTokenCount") or usage.get("total_token_count")
    )
    if not total and any((prompt, thoughts, candidates)):
        total = _bounded_int(prompt + thoughts + candidates)
    status = (
        IgConversationAnalysisResult.UsageStatus.PROVIDER_REPORTED
        if any((prompt, thoughts, candidates, total))
        else IgConversationAnalysisResult.UsageStatus.ACCOUNTING_UNKNOWN
    )
    return {
        "usage_status": status,
        "prompt_tokens": prompt,
        "thoughts_tokens": thoughts,
        "candidates_tokens": candidates,
        "total_tokens": total,
        "analysis_latency_ms": _bounded_int(
            meta.get("latency_ms"), maximum=MAX_LATENCY_MS
        ),
    }


def _proposal_defaults(*, analysis_result, ordinal: int, row: dict) -> dict:
    return {
        "analysis_result": analysis_result,
        "ordinal": ordinal,
        "client_id": analysis_result.client_id,
        "commercial_episode_id": analysis_result.commercial_episode_id,
        "line_id": analysis_result.line_id,
        "proposal_type": row["proposal_type"],
        "target_scope": row["target_scope"],
        "target_definition_key": str(row.get("target_definition_key") or "")[:96],
        "target_definition_version": str(row.get("target_definition_version") or "")[:32],
        "target_key": str(row.get("target_key") or "")[:96],
        "typed_value": row.get("typed_value") if isinstance(row.get("typed_value"), dict) else {},
        "evidence_message_ids": list(row.get("evidence_message_ids") or [])[:MAX_EVIDENCE],
        "confidence": _decimal_01(row.get("confidence")),
        "source_result_digest": analysis_result.result_digest,
        "expected_materiality_digest": analysis_result.materiality_digest,
        "expected_authority_digest": analysis_result.authority_digest,
        "expected_state_correlation": analysis_result.state_correlation,
    }


def persist_shadow_result(
    *,
    client: IgClient,
    legacy_snapshot: IgConversationAnalysisSnapshot,
    parsed: dict,
    legacy_normalized: dict,
    by_id: dict[int, dict],
    truth_state: dict,
    materiality_cursor,
    watermark: int,
    job_revision: int,
    line_id: str,
    provider_result: dict,
    analyzed_at,
):
    """Persist shadow result/proposals without changing legacy behavior."""
    if not shadow_enabled():
        return None
    cursor_digest = str(getattr(materiality_cursor, "digest", "") or "")[:64]
    cursor_highwater = max(
        0, int(getattr(materiality_cursor, "event_highwater", 0) or 0)
    )
    if not cursor_digest or cursor_highwater <= 0:
        return None
    normalized = normalize_analysis_v2(
        parsed=parsed,
        legacy_normalized=legacy_normalized,
        by_id=by_id,
        client=client,
        truth_state=truth_state,
        analyzed_at=analyzed_at,
    )
    meta = (
        provider_result.get("meta")
        if isinstance(provider_result.get("meta"), dict)
        else {}
    )
    from management.services import gemini_health

    project_slot = gemini_health.SLOT_BY_ALIAS.get(str(meta.get("key") or ""), "")
    request_ref = gemini_health.public_request_reference(
        str(meta.get("request_id") or "")
    )
    correlation = state_correlation(legacy_snapshot.required_state_fingerprint)
    if not correlation:
        return None
    identity = {
        "snapshot": legacy_snapshot.dedupe_key,
        "watermark": int(watermark or 0),
        "revision": int(job_revision or 0),
        "materiality_event_highwater": cursor_highwater,
        "materiality_digest": cursor_digest,
        "schema": RESULT_SCHEMA_VERSION,
    }
    result_key = f"analysis-v2:{_sha(identity)}"
    result_payload = {
        **normalized.result_values,
        "client_id": client.pk,
        "commercial_episode_id": legacy_snapshot.commercial_episode_id,
        "line_id": str(line_id or "")[:96],
        "watermark_message_id": int(watermark or 0),
        "job_revision": int(job_revision or 0),
        "materiality_event_highwater": cursor_highwater,
        "materiality_digest": cursor_digest,
        "authority_digest": str(
            getattr(materiality_cursor, "authority_digest", "") or ""
        )[:64],
        "artifact_digest": str(
            getattr(materiality_cursor, "artifact_digest", "") or ""
        )[:64],
        "state_correlation": correlation,
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "source_kind": IgConversationAnalysisResult.SourceKind.AI,
        "analysis_model": str(provider_result.get("model") or "")[:80],
        "prompt_version": (
            EXTENDED_PROMPT_VERSION
            if bool(getattr(settings, "IG_ANALYSIS_V2_EXTENDED_PROMPT", False))
            else str(legacy_snapshot.analysis_prompt_version or "")[:40]
        ),
        "routing_policy_version": str(meta.get("routing_policy_version") or "")[:32],
        "reasoning_policy_version": str(meta.get("reasoning_policy_version") or "")[:32],
        "project_slot": project_slot,
        "gemini_request_ref": request_ref,
        **_usage_values(provider_result, meta),
        "analyzed_at": analyzed_at,
    }
    defaults = {
        "legacy_snapshot": legacy_snapshot,
        **result_payload,
    }
    digest_values = {
        **defaults,
        "legacy_snapshot_id": legacy_snapshot.pk,
    }
    result_digest = result_digest_for_values(digest_values)
    defaults["result_digest"] = result_digest
    try:
        with transaction.atomic():
            analysis_result, created = IgConversationAnalysisResult.objects.get_or_create(
                result_key=result_key,
                defaults=defaults,
            )
            if not created and (
                analysis_result.result_digest != result_digest
                or result_digest_for_instance(analysis_result) != result_digest
            ):
                raise ValueError("Analysis V2 result identity conflict")
            proposal_ids = []
            for ordinal, row in enumerate(normalized.proposals, start=1):
                proposal_values = _proposal_defaults(
                    analysis_result=analysis_result,
                    ordinal=ordinal,
                    row=row,
                )
                proposal_key = proposal_key_for_values(
                    result_key=result_key,
                    ordinal=ordinal,
                    values=proposal_values,
                )
                proposal, proposal_created = IgAnalysisProposal.objects.get_or_create(
                    proposal_key=proposal_key,
                    defaults=proposal_values,
                )
                if not proposal_created:
                    if proposal.proposal_key != proposal_key_for_instance(proposal):
                        raise ValueError("Analysis V2 proposal key conflict")
                    expected_identity = {
                        "analysis_result_id": analysis_result.pk,
                        "ordinal": ordinal,
                        "client_id": analysis_result.client_id,
                        "commercial_episode_id": analysis_result.commercial_episode_id,
                        "line_id": analysis_result.line_id,
                        **{
                            field: proposal_values[field]
                            for field in _PROPOSAL_DIGEST_FIELDS
                        },
                    }
                    for field, expected in expected_identity.items():
                        if getattr(proposal, field) != expected:
                            raise ValueError("Analysis V2 proposal identity conflict")
                proposal_ids.append(proposal.pk)
            from management.services.ig_analysis_v2_projector import (
                project_shadow_proposals,
            )

            project_shadow_proposals(proposal_ids, now=analyzed_at)
            return analysis_result
    except Exception:
        return None


def current_analysis_result(client_or_id):
    """Diagnostics-only exact current Result selector; no consumer switch."""
    if not shadow_enabled():
        return None
    client = client_or_id if hasattr(client_or_id, "pk") else None
    client_id = getattr(client, "pk", client_or_id)
    if not client_id:
        return None
    if client is None:
        client = (
            IgClient.objects.select_related(
                "current_commercial_episode", "analysis_job"
            )
            .filter(pk=client_id)
            .first()
        )
    if client is None:
        return None
    try:
        job = client.analysis_job
    except Exception:
        return None
    if (
        job.status != job.Status.DONE
        or not job.materiality_digest
        or job.analyzed_materiality_digest != job.materiality_digest
        or int(job.analyzed_materiality_event_highwater or 0)
        < int(job.materiality_event_highwater or 0)
    ):
        return None
    result = (
        IgConversationAnalysisResult.objects.filter(
            client_id=client.pk,
            commercial_episode_id=client.current_commercial_episode_id,
            line_id=str(job.materiality_line_id or ""),
            watermark_message_id=job.analyzed_watermark_message_id,
            job_revision=job.analyzed_revision,
            materiality_event_highwater=job.analyzed_materiality_event_highwater,
            materiality_digest=job.analyzed_materiality_digest,
            authority_digest=job.authority_digest,
            artifact_digest=job.artifact_digest,
            state_correlation=state_correlation(job.required_state_fingerprint),
            result_schema_version=RESULT_SCHEMA_VERSION,
        )
        .order_by("-id")
        .first()
    )
    if result is None or result.result_digest != result_digest_for_instance(result):
        return None
    from management.services.ig_funnel_reset import current_message_floor

    return result if result.watermark_message_id >= current_message_floor(client) else None
