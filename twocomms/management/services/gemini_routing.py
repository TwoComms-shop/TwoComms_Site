"""Versioned, evidence-based routing for Instagram Gemini work.

The router deliberately consumes *structured facts* produced by the ingress,
commerce and media layers.  It does not promote a turn merely because a word
such as ``price`` or ``size`` appeared in free text.  The resulting immutable
decision is safe to persist before any provider request and can therefore be
explained after a fallback, deadline or worker restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from management.services import gemini_model_registry


POLICY_VERSION = "gemini-routing-v2.1"
AUTHORITY_SNAPSHOT_VERSION = "ig-authority-v1"


class TaskClass(StrEnum):
    NO_MODEL = "no_model"
    ORDINARY_LIVE = "ordinary_live"
    COMPLEX_LIVE = "complex_live"
    DURABLE_ANALYSIS = "durable_analysis"


class RoutingMode(StrEnum):
    ADAPTIVE = "adaptive"
    PINNED = "pinned"


ORDINARY_CHAIN = (
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)
COMPLEX_CHAIN = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
ANALYSIS_CHAIN = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
ANALYSIS_ESCALATION_CHAIN = ("gemini-3.8-flash", "gemini-3.7-flash")

_ALLOWED_LIVE_MODELS = frozenset(
    model for model in gemini_model_registry.DISPLAY_MODELS
    if model in (*ORDINARY_CHAIN, *COMPLEX_CHAIN)
)
_NO_MODEL_REASONS = frozenset({
    "authoritative_reply",
    "duplicate_reply",
    "explicit_no_buy",
    "manager_takeover",
    "opt_out",
    "postback",
    "provider_native_ugc",
    "rate_limited",
    "reaction_only",
    "repeat_guard",
    "spam_abuse",
    "media_unavailable",
})

# Public, operator-facing definitions live beside the executable classifier so
# the read API and the router cannot silently drift into two different policy
# documents.  They intentionally describe decision boundaries, not prompt
# wording or customer data.
PUBLIC_TASK_CLASS_DEFINITIONS = {
    TaskClass.NO_MODEL: {
        "title": "No model",
        "definition": (
            "Backend truth fully determines the response or action; no Gemini "
            "generation is required."
        ),
    },
    TaskClass.ORDINARY_LIVE: {
        "title": "Ordinary live",
        "definition": (
            "Backend facts are complete and Gemini only formulates a short "
            "customer-facing response."
        ),
    },
    TaskClass.COMPLEX_LIVE: {
        "title": "Complex live",
        "definition": (
            "Ambiguous or multimodal understanding materially affects a product, "
            "configuration, or funnel branch."
        ),
    },
    TaskClass.DURABLE_ANALYSIS: {
        "title": "Durable analysis",
        "definition": (
            "Background CRM analysis records structured proposals without delaying "
            "the customer reply."
        ),
    },
}


@dataclass(frozen=True)
class TurnFacts:
    """Bounded facts known before the customer-facing provider call."""

    deterministic_action: str = ""
    has_image: bool = False
    has_audio: bool = False
    unresolved_catalog_candidates: int = 0
    personalized_fit_required: bool = False
    product_or_recipient_switch: bool = False
    custom_print_brief: bool = False
    conflicting_intent: bool = False
    ambiguous_ad_referral: bool = False
    comparison_required: bool = False
    objection_present: bool = False
    commercial_risk: str = "low"
    reasoning_task_hint: str = ""


@dataclass(frozen=True)
class RoutingDecision:
    lane: str
    task_class: TaskClass
    reason_codes: tuple[str, ...]
    authority_snapshot_version: str
    requires_media_reasoning: bool
    commercial_risk: str
    model_chain: tuple[str, ...]
    deadline_ms: int
    policy_version: str = POLICY_VERSION
    reasoning_task: str = "customer_chat"
    routing_mode: RoutingMode = RoutingMode.ADAPTIVE

    def as_dict(self) -> dict:
        return {
            "lane": self.lane,
            "task_class": self.task_class.value,
            "reason_codes": list(self.reason_codes),
            "authority_snapshot_version": self.authority_snapshot_version,
            "requires_media_reasoning": self.requires_media_reasoning,
            "commercial_risk": self.commercial_risk,
            "model_chain": list(self.model_chain),
            "deadline_ms": self.deadline_ms,
            "policy_version": self.policy_version,
            "reasoning_task": self.reasoning_task,
            "routing_mode": self.routing_mode.value,
        }


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def active_pin(settings_obj, *, now=None) -> str:
    """Return a valid, unexpired emergency chat pin without mutating settings."""
    if settings_obj is None:
        return ""
    if str(getattr(settings_obj, "gemini_routing_mode", "") or "") != RoutingMode.PINNED:
        return ""
    pinned_until = getattr(settings_obj, "pinned_until", None)
    now = now or timezone.now()
    if pinned_until is None or pinned_until <= now:
        return ""
    model = str(getattr(settings_obj, "pinned_chat_model", "") or "").strip()
    return model if model in _ALLOWED_LIVE_MODELS else ""


def _apply_pin(chain: tuple[str, ...], settings_obj, *, now=None) -> tuple[tuple[str, ...], RoutingMode]:
    pinned = active_pin(settings_obj, now=now)
    if not pinned:
        return chain, RoutingMode.ADAPTIVE
    return _unique((pinned, *chain)), RoutingMode.PINNED


def classify_live_turn(facts: TurnFacts, *, settings_obj=None, now=None) -> RoutingDecision:
    """Classify one live turn from structured evidence, never raw keywords."""
    deterministic = str(facts.deterministic_action or "").strip()
    if deterministic:
        reason = deterministic if deterministic in _NO_MODEL_REASONS else "authoritative_reply"
        return RoutingDecision(
            lane="live",
            task_class=TaskClass.NO_MODEL,
            reason_codes=(reason,),
            authority_snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
            requires_media_reasoning=False,
            commercial_risk=str(facts.commercial_risk or "low"),
            model_chain=(),
            deadline_ms=0,
            reasoning_task="no_model",
        )

    complex_reasons: list[str] = []
    if facts.has_image:
        complex_reasons.append("image_reasoning")
    if facts.has_audio:
        complex_reasons.append("audio_reasoning")
    if int(facts.unresolved_catalog_candidates or 0) > 1:
        complex_reasons.append("ambiguous_catalog")
    for enabled, reason in (
        (facts.personalized_fit_required, "personalized_fit"),
        (facts.product_or_recipient_switch, "branch_switch"),
        (facts.custom_print_brief, "custom_print"),
        (facts.conflicting_intent, "conflicting_intent"),
        (facts.ambiguous_ad_referral, "ambiguous_referral"),
        (facts.comparison_required, "comparison"),
        (facts.objection_present, "objection_resolution"),
    ):
        if enabled:
            complex_reasons.append(reason)

    if complex_reasons:
        chain, mode = _apply_pin(COMPLEX_CHAIN, settings_obj, now=now)
        task = str(facts.reasoning_task_hint or "").strip()
        if task not in {
            "media_analysis", "catalog_match", "product_decision",
            "size_fit_decision", "payment_decision", "order_decision",
        }:
            task = "media_analysis" if facts.has_image or facts.has_audio else "product_decision"
        return RoutingDecision(
            lane="live",
            task_class=TaskClass.COMPLEX_LIVE,
            reason_codes=_unique(complex_reasons),
            authority_snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
            requires_media_reasoning=bool(facts.has_image or facts.has_audio),
            commercial_risk=str(facts.commercial_risk or "medium"),
            model_chain=chain,
            deadline_ms=45_000,
            reasoning_task=task,
            routing_mode=mode,
        )

    chain, mode = _apply_pin(ORDINARY_CHAIN, settings_obj, now=now)
    return RoutingDecision(
        lane="live",
        task_class=TaskClass.ORDINARY_LIVE,
        reason_codes=("backend_facts_ready",),
        authority_snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
        requires_media_reasoning=False,
        commercial_risk=str(facts.commercial_risk or "low"),
        model_chain=chain,
        deadline_ms=35_000,
        reasoning_task="customer_chat",
        routing_mode=mode,
    )


def durable_analysis_decision(*, reason_codes: Iterable[str] = ()) -> RoutingDecision:
    return RoutingDecision(
        lane="analysis",
        task_class=TaskClass.DURABLE_ANALYSIS,
        reason_codes=_unique(reason_codes) or ("material_change",),
        authority_snapshot_version=AUTHORITY_SNAPSHOT_VERSION,
        requires_media_reasoning=False,
        commercial_risk="medium",
        model_chain=ANALYSIS_CHAIN,
        deadline_ms=75_000,
        reasoning_task="customer_intelligence",
    )


def analysis_escalation_chain(
    *,
    schema_valid: bool,
    low_confidence: bool,
    high_value: bool,
    conflict_or_missing_fact: bool,
    already_escalated: bool,
    capacity_available: bool,
) -> tuple[str, ...]:
    """Return the one-pass strong-model escalation hook for durable analysis.

    A 3.6 outage is intentionally absent from the inputs: unavailability alone
    must never spend scarce strong-model analysis quota. The caller still owns
    the one-pass budget and must record any skipped 3.8 candidate.
    """
    if all((
        schema_valid,
        low_confidence,
        high_value,
        conflict_or_missing_fact,
        not already_escalated,
        capacity_available,
    )):
        return ANALYSIS_ESCALATION_CHAIN
    return ()


def recovery_decision_for(message, settings_obj, *, has_image=False, has_audio=False):
    """Revalidate an original live route against the current routing policy."""
    reasons = set(getattr(message, "gemini_routing_reason_codes", None) or [])
    original_class = str(getattr(message, "gemini_task_class", "") or "")
    facts = TurnFacts(
        has_image=bool(has_image),
        has_audio=bool(has_audio),
        unresolved_catalog_candidates=(
            2 if "ambiguous_catalog" in reasons else 0
        ),
        personalized_fit_required="personalized_fit" in reasons,
        product_or_recipient_switch="branch_switch" in reasons,
        custom_print_brief="custom_print" in reasons,
        conflicting_intent="conflicting_intent" in reasons,
        ambiguous_ad_referral="ambiguous_referral" in reasons,
        comparison_required="comparison" in reasons,
        objection_present="objection_resolution" in reasons,
        commercial_risk=str(
            getattr(message, "gemini_routing_commercial_risk", "") or "low"
        ),
        reasoning_task_hint=(
            "media_analysis"
            if has_image or has_audio
            else "size_fit_decision"
            if "personalized_fit" in reasons
            else "product_decision"
        ),
    )
    if (
        original_class == TaskClass.COMPLEX_LIVE
        and not any((
            facts.has_image,
            facts.has_audio,
            facts.unresolved_catalog_candidates,
            facts.personalized_fit_required,
            facts.product_or_recipient_switch,
            facts.custom_print_brief,
            facts.conflicting_intent,
            facts.ambiguous_ad_referral,
            facts.comparison_required,
            facts.objection_present,
        ))
    ):
        facts = TurnFacts(
            unresolved_catalog_candidates=2,
            commercial_risk=facts.commercial_risk,
            reasoning_task_hint="product_decision",
        )
    decision = classify_live_turn(facts, settings_obj=settings_obj)
    from dataclasses import replace

    return replace(decision, lane="recovery")


def persist_decision(message, decision: RoutingDecision) -> None:
    """Persist the immutable route before provider I/O; telemetry is fail-soft."""
    if message is None or not getattr(message, "pk", None):
        return
    payload = decision.as_dict()
    fields = {
        "gemini_task_class": payload["task_class"],
        "gemini_routing_reason_codes": payload["reason_codes"],
        "gemini_routing_policy_version": payload["policy_version"],
        "gemini_routing_model_chain": payload["model_chain"],
        "gemini_routing_deadline_ms": payload["deadline_ms"],
        "gemini_routing_lane": payload["lane"],
        "gemini_routing_authority_version": payload["authority_snapshot_version"],
        "gemini_routing_requires_media": payload["requires_media_reasoning"],
        "gemini_routing_commercial_risk": payload["commercial_risk"],
        "gemini_routing_mode": payload["routing_mode"],
    }
    try:
        with transaction.atomic():
            locked = type(message).objects.select_for_update().get(pk=message.pk)
            if str(locked.gemini_routing_policy_version or ""):
                return

            if decision.task_class == TaskClass.NO_MODEL:
                from management.models import GeminiRequest, GeminiRequestAttempt

                # A provider-backed graph is durable ownership of this
                # source/lane even when a crash left the message route blank.
                # Locking the inbound row serializes this check with V2 graph
                # creation, so duplicate suppression cannot win a stale CAS
                # between graph creation and provider_started settlement.
                provider_attempt = GeminiRequestAttempt.objects.filter(
                    request_graph_id=OuterRef("pk"),
                ).filter(
                    Q(provider_started_at__isnull=False)
                    | Q(fsm_state__in=(
                        GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                        GeminiRequestAttempt.FsmState.SUCCEEDED,
                        GeminiRequestAttempt.FsmState.FAILED,
                        GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
                        GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
                    ))
                    | Q(outcome__in=(
                        "provider_started",
                        "succeeded",
                        "failed",
                        "timeout_ambiguous",
                        "succeeded_late",
                    ))
                )
                unlinked_provider_evidence = (
                    GeminiRequestAttempt.objects.select_for_update()
                    .filter(
                        source_message_id=message.pk,
                        lane=payload["lane"],
                        request_graph_id__isnull=True,
                    )
                    .filter(
                        Q(provider_started_at__isnull=False)
                        | Q(fsm_state__in=(
                            GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                            GeminiRequestAttempt.FsmState.SUCCEEDED,
                            GeminiRequestAttempt.FsmState.FAILED,
                            GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
                            GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
                        ))
                        | Q(outcome__in=(
                            "provider_started",
                            "succeeded",
                            "failed",
                            "timeout_ambiguous",
                            "succeeded_late",
                        ))
                    )
                    .exists()
                )
                provider_owner = (
                    GeminiRequest.objects.select_for_update()
                    .filter(
                        source_message_id=message.pk,
                        lane=payload["lane"],
                    )
                    .annotate(has_provider_attempt=Exists(provider_attempt))
                    .filter(
                        Q(provider_phase_started_at__isnull=False)
                        | Q(has_provider_attempt=True)
                        | ~Q(task_class=TaskClass.NO_MODEL.value)
                        | ~Q(candidate_plan=[])
                    )
                    .order_by("-provider_phase_started_at", "-id")
                    .first()
                )
                if provider_owner is None and unlinked_provider_evidence:
                    # A legacy/orphan attempt proves provider I/O but cannot
                    # safely reconstruct the missing parent route.  Leave the
                    # message blank for reconciliation and never assert
                    # NO_MODEL over that evidence.
                    return
                if provider_owner is not None:
                    if provider_owner.task_class == TaskClass.NO_MODEL.value:
                        # Historical/corrupt evidence cannot be projected as a
                        # deterministic route once a provider boundary exists.
                        # Preserve the graph and leave the blank message route
                        # fail-closed for operator reconciliation.
                        return
                    model_chain: list[str] = []
                    for item in provider_owner.candidate_plan or ():
                        model = str(
                            item.get("model") if isinstance(item, dict) else ""
                        ).strip()
                        if model and model not in model_chain:
                            model_chain.append(model)
                    provider_fields = {
                        "gemini_task_class": str(provider_owner.task_class or ""),
                        "gemini_routing_reason_codes": list(
                            locked.gemini_routing_reason_codes or []
                        ),
                        "gemini_routing_policy_version": str(
                            provider_owner.routing_policy_version or ""
                        ),
                        "gemini_routing_model_chain": model_chain,
                        "gemini_routing_deadline_ms": int(
                            provider_owner.deadline_ms or 0
                        ),
                        "gemini_routing_lane": str(provider_owner.lane or ""),
                        "gemini_routing_authority_version": str(
                            provider_owner.authority_snapshot_version or ""
                        ),
                        "gemini_routing_requires_media": bool(
                            provider_owner.requires_media_reasoning
                        ),
                        "gemini_routing_commercial_risk": str(
                            provider_owner.commercial_risk or ""
                        ),
                        "gemini_routing_mode": str(
                            provider_owner.routing_mode or ""
                        ),
                    }
                    # Some pre-V2 rows may not carry a policy version.  The
                    # task class still preserves provider ownership; do not
                    # substitute the incoming NO_MODEL policy.
                    type(message).objects.filter(pk=locked.pk).update(
                        **provider_fields
                    )
                    for field, value in provider_fields.items():
                        setattr(message, field, value)
                    return

            updated = type(message).objects.filter(
                pk=locked.pk,
                gemini_routing_policy_version="",
            ).update(**fields)
            if not updated:
                return
            for field, value in fields.items():
                setattr(message, field, value)
            if decision.task_class == TaskClass.NO_MODEL:
                from management.services.gemini_accounting_runtime import (
                    record_no_model_decision,
                )

                record_no_model_decision(message, decision)
    except Exception:
        # Routing/business behavior remains fail-soft if optional evidence
        # persistence is unavailable.
        return
