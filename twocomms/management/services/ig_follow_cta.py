"""Deterministic, fail-closed policy for the optional Instagram follow CTA.

The service deliberately has no provider I/O.  It owns the local policy
boundary, immutable decision snapshots, the database-backed reservation, and
the receipt/ambiguous outcome accounting used by later delivery workers.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.models import (
    IgClient,
    IgCommercialEpisode,
    IgConversationAnalysisSnapshot,
    IgFollowCtaDecision,
    IgFollowState,
    IgLifecycleEvent,
    IgPaymentFollowPreparation,
    IgPostSaleCase,
    InstagramBotMessage,
)
from management.services.ig_follow_state import effective_follow_state
from management.services.ig_response_control import follow_cta_static_error


POLICY_VERSION = "follow-v1"
COOLDOWN = timedelta(days=90)
ROLLING_YEAR = timedelta(days=365)
RESERVATION_LEASE = timedelta(minutes=5)
PAYMENT_PREPARATION_WINDOW = timedelta(seconds=7)
PAYMENT_PREPARATION_LEASE = timedelta(seconds=10)
PAYMENT_LOCAL_POLICY_VERSION = "payment-follow-local-v1"
_PREPARED_OPPORTUNITY_PRIORITY = {
    IgFollowCtaDecision.Opportunity.PAYMENT: 0,
    IgFollowCtaDecision.Opportunity.HESITATION: 1,
    IgFollowCtaDecision.Opportunity.POST_DELIVERY: 2,
}
_PAYMENT_LOCAL_CANDIDATES = {
    "uk": "Якщо вам близький наш підхід, будемо раді бачити вас серед підписників.",
    "ru": "Если вам близок наш подход, будем рады видеть вас среди подписчиков.",
    "en": "If our approach resonates with you, we would be glad to have you among our followers.",
}
_SOFT_HESITATION = re.compile(
    r"\b(подума|думаю|подум|ще подума|порад|не впевн|maybe|think|consider|размыш)",
    re.IGNORECASE,
)
_PAYMENT_LINK = re.compile(r"(?:paylink|payment\s+link|посилання\s+на\s+оплат|ссылк\s+на\s+оплат)", re.I)
_UGC_OR_REVIEW = re.compile(
    r"(?:відгук|відміт|познач|репост|репостн|сторі|фото\s+в\s+одяз|ugc|review)", re.I
)
_POSITIVE_POST_DELIVERY = re.compile(
    r"(?:все\s+(?:добре|супер|чудово)|подоба(?:є|ється)|сподоб|класно|"
    r"дуже\s+(?:гарн|крут|якіс)|очень\s+(?:крут|хорош|качествен)|love\s+it)",
    re.I,
)
_RISK_WORDS = re.compile(
    r"(?:refund|refunded|повернен|обмін|обмен|exchange|скасув|отмен|reversal|chargeback|скарг|жалоб)",
    re.I,
)
_CURRENT_TURN_RISK = re.compile(
    r"(?:скарг|жалоб|претенз|брак|дефект|пошкод|поврежд|не\s+підійш|"
    r"не\s+подош|не\s+той|не\s+та|не\s+те|помилк|ошибк|повернен|возврат|"
    r"обмін|обмен|refund|return|exchange|скасув|отмен|cancel|chargeback|"
    r"потріск\w*|потреск\w*|пран\w*|стир\w*|як\s+(?:прат|стир)|"
    r"догляд\w*|уход\w*)",
    re.I,
)
_FOLLOW_REFUSAL = re.compile(
    r"(?:\b(?:не\s+хочу|не\s+буду|не\s+треба|не\s+потрібно|не\s+нужно|"
    r"відмовляюсь|отказываюсь|don['’]?t\s+want|won['’]?t)\b[^.!?\n]{0,48}"
    r"\b(?:підпис\w*|подпис\w*|follow(?:ing)?)\b|"
    r"\b(?:підпис\w*|подпис\w*|follow(?:ing)?)\b[^.!?\n]{0,48}"
    r"\b(?:не\s+хочу|не\s+буду|не\s+треба|не\s+потрібно|не\s+нужно|"
    r"відмовляюсь|отказываюсь|don['’]?t\s+want|won['’]?t)\b)",
    re.I,
)
_MANAGER_HANDOFF = re.compile(
    r"(?:переда\w*|підключ\w*|позов\w*|поклич\w*|запрош\w*)[^.!?\n]{0,48}"
    r"(?:менеджер|фахів|специалист)|(?:менеджер|фахів|специалист)"
    r"[^.!?\n]{0,48}(?:зв['’]?яж|ответ|відпов|підключ)",
    re.I,
)
_CUSTOMER_ACTION = re.compile(
    r"(?:оформ\w*|створ\w*|созд\w*|place)\s+(?:ваше\s+|ваш\s+|the\s+)?"
    r"(?:замовлен\w*|заказ\w*|order)|(?:оплат\w*|сплат\w*|pay\w*)"
    r"[^.!?\n]{0,40}(?:посилання|ссылк|link)|https?://|www\.",
    re.I,
)


@dataclass(frozen=True)
class FollowOpportunity:
    allowed: bool
    client_id: int
    opportunity: str
    episode_id: int | None
    source_message_id: int | None
    order_id: int | None
    lifecycle_event_id: int | None
    follow_state: str
    follow_state_revision: int
    conversation_watermark: int
    context_fingerprint: str
    base_text: str
    trigger_key: str
    reason_codes: tuple[str, ...] = ()
    analysis_id: int | None = None

    @property
    def suppression_reason(self) -> str:
        return self.reason_codes[0] if self.reason_codes else ""


@dataclass(frozen=True)
class AuthorizedFollowCta:
    decision_id: int
    text: str
    base_text: str
    final_text: str
    lease_token: str

    @property
    def candidate_text(self) -> str:
        return self.text


def _enum_value(value) -> str:
    return str(getattr(value, "value", value) or "")


def _fingerprint(*parts) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trigger_key(*, client, opportunity, episode=None, source_message=None, lifecycle_event=None) -> str:
    anchor = (
        f"message:{getattr(source_message, 'pk', '')}"
        if getattr(source_message, "pk", None)
        else f"lifecycle:{getattr(lifecycle_event, 'pk', '')}"
        if getattr(lifecycle_event, "pk", None)
        else f"episode:{getattr(episode, 'pk', '')}"
    )
    return f"{_enum_value(opportunity)}:{getattr(client, 'pk', 0)}:{anchor}"[:180]


def _source_text(source_message) -> str:
    return str(getattr(source_message, "text", "") or "")


def _latest_user_id(client_id: int) -> int:
    return int(
        InstagramBotMessage.objects.filter(
            client_id=client_id, role=InstagramBotMessage.Role.USER
        ).order_by("-pk").values_list("pk", flat=True).first()
        or 0
    )


def _timestamp_for_message(message):
    return getattr(message, "provider_created_at", None) or getattr(message, "created_at", None)


def record_follow_refusal_from_inbound(message, *, now=None) -> bool:
    """Persist an explicit follow-specific refusal without pausing service replies."""
    if (
        message is None
        or getattr(message, "role", "") != InstagramBotMessage.Role.USER
        or not getattr(message, "client_id", None)
        or not _FOLLOW_REFUSAL.search(_source_text(message))
    ):
        return False
    now = now or timezone.now()
    refused_at = _timestamp_for_message(message) or now
    with transaction.atomic():
        projection, _created = IgFollowState.objects.select_for_update().get_or_create(
            client_id=message.client_id
        )
        if projection.cta_refused_at is None:
            projection.cta_refused_at = refused_at
            projection.cta_refusal_message_id = int(message.pk or 0)
            projection.save(
                update_fields=[
                    "cta_refused_at",
                    "cta_refusal_message_id",
                    "updated_at",
                ]
            )
    return True


def _post_delivery_truth(*, episode, order, source_message) -> bool:
    candidate_order = order or getattr(episode, "intended_order", None)
    if candidate_order is None or source_message is None:
        return False
    from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at

    delivered_at = nova_poshta_delivery_confirmed_at(candidate_order)
    source_at = _timestamp_for_message(source_message)
    return bool(
        delivered_at
        and source_at
        and source_at > delivered_at
        and _POSITIVE_POST_DELIVERY.search(_source_text(source_message))
    )


def _window_is_open(client, *, source_message=None, now) -> bool:
    if getattr(client, "delivery_status", "") == IgClient.DeliveryStatus.WINDOW_CLOSED:
        return False
    latest = getattr(client, "meta_window_anchor", None)
    source_timestamp = _timestamp_for_message(source_message) if source_message is not None else None
    if latest is None or (source_timestamp is not None and source_timestamp > latest):
        latest = source_timestamp
    if latest is None:
        return False
    return latest >= now - timedelta(hours=24)


def _has_post_sale_risk(client, *, order=None, lifecycle_event=None) -> bool:
    from management.services.ig_service_complaints import promotion_service_hold_reason

    if promotion_service_hold_reason(client):
        return True
    active_statuses = {
        IgPostSaleCase.Status.NEEDS_DETAILS,
        IgPostSaleCase.Status.OPEN,
        IgPostSaleCase.Status.APPROVED,
        IgPostSaleCase.Status.IN_TRANSIT,
        IgPostSaleCase.Status.RECEIVED,
    }
    if IgPostSaleCase.objects.filter(client_id=client.pk, status__in=active_statuses).exists():
        return True
    payload = {}
    if lifecycle_event is not None:
        kind = _enum_value(getattr(lifecycle_event, "kind", ""))
        payload = getattr(lifecycle_event, "payload", {}) or {}
        if kind in {"payment_recovery", "refund", "reversal", "cancellation", "return", "exchange"}:
            return True
    if _enum_value(getattr(client, "intent", "")) in {
        IgClient.Intent.SUPPORT,
        IgClient.Intent.SPAM,
    }:
        return True
    flags = getattr(client, "conversion_flags", {}) or {}
    if isinstance(flags, dict) and any(
        bool(flags.get(key)) for key in ("complaint", "support_complaint", "return", "exchange", "refund", "reversal", "cancelled")
    ):
        return True
    if isinstance(payload, dict) and any(
        str(payload.get(key) or "").lower() in {"return", "exchange", "refund", "reversal", "cancelled", "cancellation"}
        for key in ("kind", "event", "flow", "status", "case_type")
    ):
        return True
    for obj in (order, getattr(client, "current_commercial_episode", None)):
        values = " ".join(
            str(getattr(obj, name, "") or "")
            for name in ("status", "state", "cancellation_reason", "refund_status", "return_status", "exchange_status")
        )
        if _RISK_WORDS.search(values):
            return True
    return False


def _payment_recovery_active(client, *, source_message=None, lifecycle_event=None, base_text="") -> bool:
    context = getattr(client, "sales_context", {}) or {}
    if isinstance(context, dict) and any(
        bool(context.get(key)) for key in ("paylink_active", "payment_recovery", "payment_recovery_pending", "recovery_pending")
    ):
        return True
    event_payload = getattr(lifecycle_event, "payload", {}) or {}
    if isinstance(event_payload, dict) and any(
        bool(event_payload.get(key)) for key in ("paylink", "payment_recovery", "recovery")
    ):
        return True
    return bool(_PAYMENT_LINK.search(" ".join((_source_text(source_message), str(base_text or "")))))


def _growth_cta_present(text: str) -> bool:
    text = str(text or "")
    return bool(re.search(r"(?:підпис|подпис|follow|відгук|відміт|репост|промокод|зниж|скид)", text, re.I))


def _latest_hesitation_analysis(*, client, episode, source_message, now):
    if source_message is None or not _SOFT_HESITATION.search(_source_text(source_message)):
        return None
    allowed_bands = {
        IgConversationAnalysisSnapshot.Band.QUALIFIED,
        IgConversationAnalysisSnapshot.Band.HIGH_INTENT,
        IgConversationAnalysisSnapshot.Band.CHECKOUT,
    }
    from management.services.ig_analysis_materiality import (
        current_analysis_snapshot,
        selector_enforced,
    )

    if selector_enforced():
        analysis = current_analysis_snapshot(client)
    else:
        # Exact legacy behavior: select the newest snapshot *among qualifying
        # rows*. Selecting newest overall and filtering afterwards suppresses
        # a valid older hesitation signal and changes operational CTA behavior.
        analysis = (
            IgConversationAnalysisSnapshot.objects.filter(
                client_id=client.pk,
                score_band__in=allowed_bands,
                confidence__gte=Decimal("0.70"),
                analyzed_at__gte=now - timedelta(hours=24),
                analyzed_at__lte=now,
            )
            .order_by("-id")
            .first()
        )
    if analysis is None:
        return None
    if (
        analysis.score_band not in allowed_bands
        or analysis.confidence < Decimal("0.70")
        or analysis.analyzed_at < now - timedelta(hours=24)
        or analysis.analyzed_at > now
    ):
        return None
    if analysis.commercial_episode_id != getattr(episode, "pk", None):
        return None
    if not analysis.last_analyzed_message_id or analysis.last_analyzed_message_id < source_message.pk:
        return None
    if analysis.interaction_type in {
        IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT,
        IgConversationAnalysisSnapshot.InteractionType.OPT_OUT,
        IgConversationAnalysisSnapshot.InteractionType.SPAM_ABUSE,
        IgConversationAnalysisSnapshot.InteractionType.EXPLICIT_NO_BUY,
    }:
        return None
    return analysis


def _suppression_codes(
    *,
    client,
    opportunity,
    episode,
    source_message,
    order,
    lifecycle_event,
    base_text,
    now,
    follow_view,
    conversation_watermark,
    include_history=True,
):
    reasons: list[str] = []
    if not follow_view.fresh or follow_view.state != IgFollowState.State.NOT_FOLLOWING:
        reasons.append("follow_state")
    if getattr(client, "hidden_at", None):
        reasons.append("hidden")
    if getattr(client, "is_blocked", False):
        reasons.append("blocked")
    if _enum_value(getattr(client, "stage", "")) == IgClient.Stage.SPAM:
        reasons.append("spam")
    if int(getattr(client, "spam_strikes", 0) or 0) > 0:
        reasons.append("spam")
    if IgFollowState.objects.filter(
        client_id=client.pk,
        cta_refused_at__isnull=False,
    ).exists():
        reasons.append("follow_refused")
    opted_out_at = getattr(client, "opted_out_at", None)
    opted_in_at = getattr(client, "opted_in_at", None)
    if opted_out_at and (not opted_in_at or opted_in_at <= opted_out_at):
        reasons.append("opted_out")
    if getattr(client, "bot_paused", False):
        reasons.append("paused")
    if getattr(client, "manager_takeover", False):
        reasons.append("takeover")
    if not _window_is_open(client, source_message=source_message, now=now):
        reasons.append("closed_window")
    if episode is None or getattr(episode, "state", "") in {
        IgCommercialEpisode.State.CANCELLED,
        IgCommercialEpisode.State.LOST,
    } or getattr(episode, "closed_at", None):
        reasons.append("stale_episode")
    current_episode_id = getattr(client, "current_commercial_episode_id", None)
    if current_episode_id and current_episode_id != getattr(episode, "pk", None):
        reasons.append("stale_episode")
    latest_user = _latest_user_id(client.pk)
    source_id = getattr(source_message, "pk", None)
    if latest_user > conversation_watermark and latest_user != source_id:
        reasons.append("new_inbound")
    if _CURRENT_TURN_RISK.search(_source_text(source_message)):
        reasons.append("current_turn_risk")
    if "?" in _source_text(source_message) or "？" in _source_text(source_message):
        reasons.append("inbound_question")
    if _has_post_sale_risk(client, order=order, lifecycle_event=lifecycle_event):
        reasons.append("post_sale_risk")
    if _payment_recovery_active(
        client, source_message=source_message, lifecycle_event=lifecycle_event, base_text=base_text
    ):
        reasons.append("payment_recovery")
    lifecycle_kind = _enum_value(getattr(lifecycle_event, "kind", ""))
    if (
        lifecycle_kind == IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED
        or _UGC_OR_REVIEW.search(
        _source_text(source_message)
        )
    ):
        reasons.append("delivered_review_or_ugc")
    if _growth_cta_present(base_text):
        reasons.append("existing_cta")
    if "?" in str(base_text or ""):
        reasons.append("existing_question")
    if _MANAGER_HANDOFF.search(str(base_text or "")):
        reasons.append("manager_handoff")
    if _CUSTOMER_ACTION.search(str(base_text or "")):
        reasons.append("customer_action")
    if (
        _enum_value(opportunity) == IgFollowCtaDecision.Opportunity.POST_DELIVERY
        and not _post_delivery_truth(
            episode=episode,
            order=order,
            source_message=source_message,
        )
    ):
        reasons.append("delivery_not_confirmed")
    if _enum_value(opportunity) == IgFollowCtaDecision.Opportunity.HESITATION:
        analysis = _latest_hesitation_analysis(
            client=client, episode=episode, source_message=source_message, now=now
        )
        if analysis is None:
            reasons.append("analysis")
    else:
        analysis = None
    if include_history and episode is not None:
        active_states = {
            IgFollowCtaDecision.State.PREPARED,
            IgFollowCtaDecision.State.RESERVED,
            IgFollowCtaDecision.State.SENT,
            IgFollowCtaDecision.State.AMBIGUOUS,
        }
        if IgFollowCtaDecision.objects.filter(
            client_id=client.pk, commercial_episode_id=episode.pk, state__in=active_states
        ).exists():
            reasons.append("another_cta")
        happened = _delivery_timestamps(client.pk, now=now)
        if happened and now - max(happened) < COOLDOWN:
            reasons.append("cooldown")
        if len([stamp for stamp in happened if now - stamp < ROLLING_YEAR]) >= 2:
            reasons.append("annual_cap")
    return tuple(dict.fromkeys(reasons)), analysis


def _delivery_timestamps(client_id: int, *, now) -> list:
    rows = IgFollowCtaDecision.objects.filter(
        client_id=client_id,
        state__in=(IgFollowCtaDecision.State.SENT, IgFollowCtaDecision.State.AMBIGUOUS),
    ).only("completed_at", "provider_io_started_at", "updated_at", "created_at")
    result = []
    for row in rows:
        stamp = row.completed_at or row.provider_io_started_at or row.updated_at or row.created_at
        if stamp and stamp <= now:
            result.append(stamp)
    return result


def evaluate_follow_opportunity(
    *,
    client,
    opportunity,
    episode,
    source_message=None,
    order=None,
    lifecycle_event=None,
    base_text="",
    now=None,
) -> FollowOpportunity:
    now = now or timezone.now()
    opportunity_value = _enum_value(opportunity)
    follow_view = effective_follow_state(client, now=now)
    watermark = max(
        int(getattr(episode, "opened_watermark_message_id", 0) or 0),
        int(getattr(source_message, "pk", 0) or 0),
    )
    trigger_key = _trigger_key(
        client=client,
        opportunity=opportunity_value,
        episode=episode,
        source_message=source_message,
        lifecycle_event=lifecycle_event,
    )
    context_fingerprint = _fingerprint(
        POLICY_VERSION,
        getattr(client, "pk", None),
        getattr(episode, "pk", None),
        opportunity_value,
        getattr(source_message, "pk", None),
        getattr(order, "pk", None),
        getattr(lifecycle_event, "pk", None),
        follow_view.revision,
        watermark,
        str(base_text or "").strip(),
    )
    reasons, analysis = _suppression_codes(
        client=client,
        opportunity=opportunity_value,
        episode=episode,
        source_message=source_message,
        order=order,
        lifecycle_event=lifecycle_event,
        base_text=base_text,
        now=now,
        follow_view=follow_view,
        conversation_watermark=watermark,
    )
    return FollowOpportunity(
        allowed=not reasons,
        client_id=int(client.pk),
        opportunity=opportunity_value,
        episode_id=getattr(episode, "pk", None),
        source_message_id=getattr(source_message, "pk", None),
        order_id=getattr(order, "pk", None),
        lifecycle_event_id=getattr(lifecycle_event, "pk", None),
        follow_state=follow_view.state,
        follow_state_revision=int(follow_view.revision or 0),
        conversation_watermark=watermark,
        context_fingerprint=context_fingerprint,
        base_text=str(base_text or "").strip(),
        trigger_key=trigger_key,
        reason_codes=reasons,
        analysis_id=getattr(analysis, "pk", None),
    )


def live_follow_opportunity(*, client, source_message, base_text="", now=None):
    """Build a local-only opportunity envelope for an inbound live reply.

    This helper deliberately chooses only opportunities that can be inferred
    from the current turn. It never refreshes Meta state; the final policy call
    and send boundary remain the authorization points.
    """
    if client is None or source_message is None:
        return None
    episode = getattr(client, "current_commercial_episode", None)
    if episode is None:
        return None
    text = _source_text(source_message)
    if _SOFT_HESITATION.search(text):
        kind = IgFollowCtaDecision.Opportunity.HESITATION
    elif (
        _POSITIVE_POST_DELIVERY.search(text)
        and _post_delivery_truth(
            episode=episode,
            order=getattr(episode, "intended_order", None),
            source_message=source_message,
        )
    ):
        kind = IgFollowCtaDecision.Opportunity.POST_DELIVERY
    else:
        return None
    return evaluate_follow_opportunity(
        client=client,
        opportunity=kind,
        episode=episode,
        source_message=source_message,
        order=(
            getattr(episode, "intended_order", None)
            if kind == IgFollowCtaDecision.Opportunity.POST_DELIVERY
            else None
        ),
        base_text=base_text,
        now=now,
    )


def follow_opportunity_prompt_note(opportunity: FollowOpportunity | None) -> str:
    """Render only safe, bounded policy facts for the model prompt."""
    if opportunity is None or not opportunity.allowed:
        return ""
    return (
        "[OPTIONAL FOLLOW CTA]\n"
        "Локальна політика дозволяє запропонувати одну необов'язкову коротку "
        "фразу про підписку, якщо це природно для поточного діалогу. "
        "Можна повернути follow_cta.include=false і нічого не додавати. "
        "Не згадуй перевірку підписки, знижки, промокоди, відсотки, URL, "
        "терміновість або тиск; не додавай CTA до сервісної/UGC-відповіді."
    )


def payment_follow_preparation_due_at(client, *, now=None):
    """Return the bounded payment delay only when follow truth is already fresh."""
    now = now or timezone.now()
    try:
        view = effective_follow_state(client, now=now)
    except Exception:
        return now
    if (
        view.fresh
        and view.state == IgFollowState.State.NOT_FOLLOWING
    ):
        return now + PAYMENT_PREPARATION_WINDOW
    try:
        from management.services.ig_follow_state import request_follow_refresh

        request_follow_refresh(client, trigger="payment", now=now)
    except Exception:
        # Optional observation cannot interfere with verified-payment truth.
        pass
    return now


def prepare_local_payment_follow_snapshot(event_id: int, *, now=None):
    """Prepare a safe payment CTA from already-fresh local follow truth.

    This path is deliberately synchronous and provider-free.  It is only a
    fast opportunity probe: the full deterministic policy still owns every
    suppression gate and the lifecycle provider boundary remains the final
    authorization point.
    """
    now = now or timezone.now()
    event = (
        IgLifecycleEvent.objects.select_related(
            "client",
            "commercial_episode",
            "order",
        )
        .filter(
            pk=event_id,
            kind=IgLifecycleEvent.Kind.PAYMENT_VERIFIED,
        )
        .first()
    )
    if event is None:
        return None
    try:
        view = effective_follow_state(event.client, now=now)
    except Exception:
        return None
    if not (
        view.fresh
        and view.state == IgFollowState.State.NOT_FOLLOWING
    ):
        return None
    language = _enum_value(getattr(event.client, "language", ""))
    language = language.lower().replace("_", "-").split("-", 1)[0]
    candidate = _PAYMENT_LOCAL_CANDIDATES.get(
        language,
        _PAYMENT_LOCAL_CANDIDATES["uk"],
    )
    return prepare_payment_follow_snapshot(
        event.pk,
        candidate_text=candidate,
        model_meta={
            "model": "local_template",
            "prompt_version": PAYMENT_LOCAL_POLICY_VERSION,
        },
        now=now,
    )


def queue_payment_follow_preparation(event_id, *, deadline_at=None):
    """Persist one local preparation request; this function never performs I/O."""
    event = (
        IgLifecycleEvent.objects.select_related("client")
        .filter(pk=event_id, kind=IgLifecycleEvent.Kind.PAYMENT_VERIFIED)
        .first()
    )
    if event is None:
        return None
    preparation, _created = IgPaymentFollowPreparation.objects.get_or_create(
        lifecycle_event_id=event.pk,
        defaults={
            "client_id": event.client_id,
            "deadline_at": deadline_at or event.due_at,
            "state": IgPaymentFollowPreparation.State.PENDING,
        },
    )
    return preparation


def _payment_follow_copy_prompt(event) -> str:
    """Provide Gemini only the bounded, non-identifying payment context."""
    language = _enum_value(getattr(getattr(event, "client", None), "language", ""))
    return json.dumps(
        {
            "opportunity": "verified_payment",
            "language": language or "uk",
            "instruction": (
                "The mandatory payment confirmation is sent separately. Return one "
                "optional warm follow invitation only when it reads naturally."
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


_PAYMENT_FOLLOW_COPY_SYSTEM = """You write one optional clause for an Instagram Direct reply.
Return JSON only: {\"include\": boolean, \"text\": string}. If it is not natural,
set include to false and text to an empty string. When include is true, text must be
one short sentence in the requested customer language. It may invite a person to
follow the brand, but must never say that their follow state was checked or known.
Never mention a discount, promo code, percentage, URL, urgency, scarcity, surveillance,
guilt, reward, purchase requirement, or a command to follow. Do not add any question,
emoji, markdown, or another customer action."""


def _payment_follow_model_meta(result) -> dict[str, str]:
    result = result if isinstance(result, dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    try:
        from management.services.call_ai_analysis import reasoning_policy

        prompt_version = str(reasoning_policy("follow_cta_copy").get("policy_version") or "")
    except Exception:
        prompt_version = ""
    return {
        "model": str(result.get("model") or meta.get("used_model") or "")[:80],
        "key_alias": str(meta.get("key") or meta.get("key_alias") or "")[:80],
        "prompt_version": prompt_version[:40],
    }


def _payment_follow_candidate(result) -> tuple[bool, str]:
    """Accept only the tiny structured contract; every ambiguity omits CTA."""
    payload = result.get("parsed") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return False, ""
    include = payload.get("include")
    text = payload.get("text", "")
    if include is False and (text is None or isinstance(text, str)):
        return False, ""
    if include is not True or not isinstance(text, str):
        return False, ""
    return True, text.strip()


def _finish_payment_follow_preparation(
    preparation_id: int,
    lease_token: str,
    *,
    result: dict | None = None,
    error_kind: str = "",
    now=None,
) -> str:
    """Publish one background result only while its payment window is current."""
    now = now or timezone.now()
    with transaction.atomic():
        preparation = (
            IgPaymentFollowPreparation.objects.select_for_update()
            .select_related("lifecycle_event")
            .filter(pk=preparation_id)
            .first()
        )
        if (
            preparation is None
            or preparation.state != IgPaymentFollowPreparation.State.PROCESSING
            or preparation.lease_token != lease_token
        ):
            return "skipped"
        event = (
            IgLifecycleEvent.objects.select_for_update()
            .select_related("client", "commercial_episode", "order")
            .filter(pk=preparation.lifecycle_event_id)
            .first()
        )
        expired = bool(
            preparation.deadline_at <= now
            or event is None
            or event.state != IgLifecycleEvent.State.PENDING
            or event.final_text
        )
        if expired:
            preparation.state = IgPaymentFollowPreparation.State.EXPIRED
            preparation.completed_at = now
            preparation.lease_token = ""
            preparation.lease_expires_at = None
            preparation.save(
                update_fields=[
                    "state",
                    "completed_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return "expired"
        if error_kind:
            preparation.state = IgPaymentFollowPreparation.State.FAILED
            preparation.last_error_kind = str(error_kind)[:32]
            preparation.completed_at = now
            preparation.lease_token = ""
            preparation.lease_expires_at = None
            preparation.save(
                update_fields=[
                    "state",
                    "last_error_kind",
                    "completed_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return "failed"

        include, candidate_text = _payment_follow_candidate(result or {})
        decision = prepare_payment_follow_snapshot(
            event.pk,
            candidate_text=candidate_text if include else "",
            model_meta=_payment_follow_model_meta(result or {}),
            now=now,
        )
        preparation.state = (
            IgPaymentFollowPreparation.State.PREPARED
            if decision is not None
            and decision.state == IgFollowCtaDecision.State.PREPARED
            else IgPaymentFollowPreparation.State.SUPPRESSED
        )
        preparation.completed_at = now
        preparation.lease_token = ""
        preparation.lease_expires_at = None
        preparation.save(
            update_fields=[
                "state",
                "completed_at",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return "prepared" if preparation.state == IgPaymentFollowPreparation.State.PREPARED else "suppressed"


def process_payment_follow_preparation(preparation_id: int, *, now=None) -> str:
    """Generate one optional payment CTA inside the event's short local budget.

    The payment lifecycle continues independently. A slow or malformed model result
    only leaves this row terminal and allows the frozen payment confirmation through.
    """
    claimed_at = now or timezone.now()
    with transaction.atomic():
        preparation = (
            IgPaymentFollowPreparation.objects.select_for_update()
            .select_related("lifecycle_event", "lifecycle_event__client")
            .filter(pk=preparation_id)
            .first()
        )
        if preparation is None:
            return "missing"
        if preparation.state == IgPaymentFollowPreparation.State.PROCESSING:
            if preparation.lease_expires_at and preparation.lease_expires_at > claimed_at:
                return "skipped"
        elif preparation.state != IgPaymentFollowPreparation.State.PENDING:
            return "skipped"
        event = preparation.lifecycle_event
        if (
            preparation.deadline_at <= claimed_at
            or event.state != IgLifecycleEvent.State.PENDING
            or event.final_text
        ):
            preparation.state = IgPaymentFollowPreparation.State.EXPIRED
            preparation.completed_at = claimed_at
            preparation.lease_token = ""
            preparation.lease_expires_at = None
            preparation.save(
                update_fields=[
                    "state",
                    "completed_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return "expired"
        lease_token = uuid.uuid4().hex
        preparation.state = IgPaymentFollowPreparation.State.PROCESSING
        preparation.attempts += 1
        preparation.lease_token = lease_token
        preparation.lease_expires_at = min(
            preparation.deadline_at,
            claimed_at + PAYMENT_PREPARATION_LEASE,
        )
        preparation.save(
            update_fields=[
                "state",
                "attempts",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        deadline_at = preparation.deadline_at
        event_id = event.pk

    remaining = (deadline_at - (now or timezone.now())).total_seconds()
    if remaining <= 0.5:
        return _finish_payment_follow_preparation(
            preparation_id,
            lease_token,
            error_kind="deadline",
            now=now,
        )
    total_timeout = min(5.0, remaining)
    connect_timeout = min(1.0, max(0.25, total_timeout * 0.2))
    read_timeout = max(0.25, total_timeout - connect_timeout)
    try:
        from management.services.call_ai_analysis import gemini_generate_json

        event = IgLifecycleEvent.objects.select_related("client").get(pk=event_id)
        result = gemini_generate_json(
            _PAYMENT_FOLLOW_COPY_SYSTEM,
            _payment_follow_copy_prompt(event),
            role="management",
            max_output_tokens=256,
            reasoning_task="follow_cta_copy",
            timeout=(connect_timeout, read_timeout),
            deadline_seconds=remaining,
        )
    except Exception:
        return _finish_payment_follow_preparation(
            preparation_id,
            lease_token,
            error_kind="gemini",
            now=now,
        )
    return _finish_payment_follow_preparation(
        preparation_id,
        lease_token,
        result=result,
        now=now,
    )


def prepare_payment_follow_snapshot(
    event_id: int,
    *,
    candidate_text: str,
    model_meta=None,
    now=None,
):
    """Validate model-authored payment copy against cached truth only."""
    now = now or timezone.now()
    event = (
        IgLifecycleEvent.objects.select_related(
            "client",
            "commercial_episode",
            "order",
        )
        .filter(pk=event_id, kind=IgLifecycleEvent.Kind.PAYMENT_VERIFIED)
        .first()
    )
    if event is None:
        return None
    existing = IgFollowCtaDecision.objects.filter(
        lifecycle_event_id=event.pk,
        opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
    ).first()
    if existing is not None:
        return existing
    base_text = str((event.payload or {}).get("message_snapshot") or "").strip()
    if not base_text:
        from management.services.ig_lifecycle import _base_message

        base_text = _base_message(event)
    opportunity = evaluate_follow_opportunity(
        client=event.client,
        opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
        episode=event.commercial_episode,
        order=event.order,
        lifecycle_event=event,
        base_text=base_text,
        now=now,
    )
    if not opportunity.allowed and set(opportunity.reason_codes) == {"follow_state"}:
        try:
            from management.services.ig_follow_state import request_follow_refresh

            request_follow_refresh(event.client, trigger="payment", now=now)
        except Exception:
            # Payment delivery must remain independent from optional follow
            # observation. The reconciler can recover future demand.
            pass
        return None
    return prepare_follow_decision(
        opportunity,
        candidate_text=candidate_text,
        model_meta=model_meta,
    )


def _candidate_language(*, source_message=None, client=None) -> str:
    """Prefer the current turn over a stale conversation-language projection."""
    try:
        from management.services.bot_sales_classifier import detect_language

        current = detect_language(_source_text(source_message))
    except Exception:
        current = ""
    if current in {"uk", "ru", "en"}:
        return current
    language = _enum_value(getattr(client, "language", "")) if client is not None else ""
    return language.split("-", 1)[0].casefold()


def _candidate_matches_language(candidate: str, expected_language: str) -> bool:
    if expected_language not in {"uk", "ru", "en"}:
        return True
    try:
        from management.services.bot_sales_classifier import detect_language

        detected = detect_language(candidate)
    except Exception:
        detected = ""
    if detected:
        return detected == expected_language
    if expected_language == "en":
        return bool(re.search(r"[A-Za-z]", candidate)) and not bool(
            re.search(r"[А-Яа-яІіЇїЄєҐґ]", candidate)
        )
    if expected_language == "uk":
        return bool(re.search(r"[ІіЇїЄєҐґ]", candidate))
    return bool(re.search(r"[ЫыЪъЭэЁё]", candidate))


def _normalized_candidate(value: str) -> str:
    return re.sub(r"\W+", " ", str(value or "").casefold()).strip()


def _candidate_is_similar(candidate: str, other: str) -> bool:
    normalized_candidate = _normalized_candidate(candidate)
    normalized_other = _normalized_candidate(other)
    return bool(
        normalized_candidate
        and normalized_other
        and (
            normalized_candidate in normalized_other
            or normalized_other in normalized_candidate
            or SequenceMatcher(None, normalized_candidate, normalized_other).ratio() >= 0.88
        )
    )


def _candidate_error(
    candidate: str,
    *,
    base_text: str,
    client=None,
    source_message=None,
) -> str:
    candidate = str(candidate or "").strip()
    static_error = follow_cta_static_error(candidate)
    if static_error:
        return static_error
    language = _candidate_language(source_message=source_message, client=client)
    if not _candidate_matches_language(candidate, language):
        return "candidate_language"
    if _candidate_is_similar(candidate, base_text):
        return "candidate_similarity"
    if client is not None:
        earlier_candidates = IgFollowCtaDecision.objects.filter(
            client_id=client.pk,
            state__in=(
                IgFollowCtaDecision.State.SENT,
                IgFollowCtaDecision.State.AMBIGUOUS,
            ),
        ).values_list("candidate_text", flat=True)
        if any(_candidate_is_similar(candidate, earlier) for earlier in earlier_candidates):
            return "candidate_similarity_history"
    try:
        from management.services.instagram_bot import _split_for_send

        if len(_split_for_send(f"{base_text.strip()} {candidate}")) != 1:
            return "candidate_chunk_limit"
    except Exception:
        return "candidate_chunk_limit"
    return ""


def _split_for_send(text: str, limit: int = 950, max_chunks: int = 4) -> list[str]:
    """Lazy public test/policy hook for the existing Instagram sender splitter."""
    from management.services.instagram_bot import _split_for_send as split_for_send

    return split_for_send(text, limit=limit, max_chunks=max_chunks)


def _safe_model_meta(model_meta) -> dict[str, str]:
    model_meta = model_meta if isinstance(model_meta, dict) else {}
    return {
        "model": str(model_meta.get("model") or model_meta.get("analysis_model") or "")[:80],
        "key_alias": str(model_meta.get("key_alias") or model_meta.get("model_key_alias") or "")[:80],
        "prompt_version": str(model_meta.get("prompt_version") or "")[:40],
    }


def prepare_follow_decision(
    opportunity: FollowOpportunity, *, candidate_text: str, model_meta=None
) -> IgFollowCtaDecision:
    existing = IgFollowCtaDecision.objects.filter(trigger_key=opportunity.trigger_key).first()
    if existing is not None:
        return existing
    from management.models import IgClient as _IgClient

    client = _IgClient.objects.filter(pk=opportunity.client_id).first()
    source_message = (
        InstagramBotMessage.objects.filter(pk=opportunity.source_message_id).first()
        if opportunity.source_message_id
        else None
    )
    candidate_error = _candidate_error(
        candidate_text,
        base_text=opportunity.base_text,
        client=client,
        source_message=source_message,
    )
    valid = bool(opportunity.allowed and not candidate_error)
    meta = _safe_model_meta(model_meta)
    reason_codes = list(opportunity.reason_codes)
    if candidate_error:
        reason_codes.extend(("invalid_candidate", candidate_error))
    state = IgFollowCtaDecision.State.PREPARED if valid else IgFollowCtaDecision.State.SUPPRESSED
    decision = IgFollowCtaDecision.objects.create(
        trigger_key=opportunity.trigger_key,
        client_id=opportunity.client_id,
        commercial_episode_id=opportunity.episode_id,
        order_id=opportunity.order_id,
        lifecycle_event_id=opportunity.lifecycle_event_id,
        source_message_id=opportunity.source_message_id,
        opportunity=opportunity.opportunity,
        policy_version=POLICY_VERSION,
        state=state,
        follow_state_revision=opportunity.follow_state_revision,
        conversation_watermark=opportunity.conversation_watermark,
        context_fingerprint=opportunity.context_fingerprint,
        base_text=opportunity.base_text,
        candidate_text=str(candidate_text or "").strip() if valid else "",
        candidate_hash=hashlib.sha256(str(candidate_text or "").strip().encode("utf-8")).hexdigest()
        if valid
        else "",
        suppression_reason=("invalid_candidate" if candidate_error else opportunity.suppression_reason)[:64],
        reason_codes=reason_codes,
        model=meta["model"],
        model_key_alias=meta["key_alias"],
        prompt_version=meta["prompt_version"],
    )
    return decision


def _current_opportunity_for_decision(decision, *, client, episode, now):
    source_message = (
        InstagramBotMessage.objects.filter(pk=decision.source_message_id).first()
        if decision.source_message_id
        else None
    )
    order = None
    if decision.order_id:
        try:
            from orders.models import Order

            order = Order.objects.filter(pk=decision.order_id).first()
        except Exception:
            order = None
    lifecycle_event = (
        IgLifecycleEvent.objects.filter(pk=decision.lifecycle_event_id).first()
        if decision.lifecycle_event_id
        else None
    )
    view = effective_follow_state(client, now=now)
    reasons, analysis = _suppression_codes(
        client=client,
        opportunity=decision.opportunity,
        episode=episode,
        source_message=source_message,
        order=order,
        lifecycle_event=lifecycle_event,
        base_text=decision.base_text,
        now=now,
        follow_view=view,
        conversation_watermark=decision.conversation_watermark,
        include_history=True,
    )
    # The decision itself is the current prepared row; its own episode slot is
    # nullable and therefore is not counted as another CTA here.
    reasons = tuple(code for code in reasons if code != "another_cta")
    return reasons, view, analysis


def authorize_follow_cta(
    decision_id, *, current_base_text: str, now=None
) -> AuthorizedFollowCta | None:
    now = now or timezone.now()
    try:
        with transaction.atomic():
            decision = (
                IgFollowCtaDecision.objects.select_for_update()
                .select_related("client", "commercial_episode")
                .filter(pk=decision_id)
                .first()
            )
            if decision is None or decision.state != IgFollowCtaDecision.State.PREPARED:
                return None
            if str(current_base_text or "").strip() != decision.base_text:
                return None
            client = IgClient.objects.select_for_update().get(pk=decision.client_id)
            episode = (
                IgCommercialEpisode.objects.filter(pk=decision.commercial_episode_id).first()
                if decision.commercial_episode_id
                else None
            )
            state = (
                IgFollowState.objects.select_for_update()
                .filter(client_id=decision.client_id)
                .first()
            )
            if state is None:
                return None
            # The client row is the global serialization boundary. Re-read
            # committed contenders only after owning it so two workers that
            # locked different decision rows cannot both reserve a CTA.
            if IgFollowCtaDecision.objects.filter(
                client_id=decision.client_id,
                state=IgFollowCtaDecision.State.RESERVED,
            ).exclude(pk=decision.pk).exists():
                return None
            if decision.commercial_episode_id:
                active_same_episode = IgFollowCtaDecision.objects.filter(
                    commercial_episode_id=decision.commercial_episode_id,
                    state__in=(
                        IgFollowCtaDecision.State.RESERVED,
                        IgFollowCtaDecision.State.SENT,
                        IgFollowCtaDecision.State.AMBIGUOUS,
                    ),
                ).exclude(pk=decision.pk)
                if active_same_episode.exists():
                    return None
                prepared = list(
                    IgFollowCtaDecision.objects.filter(
                        commercial_episode_id=decision.commercial_episode_id,
                        state=IgFollowCtaDecision.State.PREPARED,
                    ).values_list("pk", "opportunity")
                )
                winner_id = min(
                    prepared,
                    key=lambda row: (
                        _PREPARED_OPPORTUNITY_PRIORITY.get(row[1], 99),
                        row[0],
                    ),
                    default=(decision.pk, decision.opportunity),
                )[0]
                if winner_id != decision.pk:
                    return None
            reasons, view, _analysis = _current_opportunity_for_decision(
                decision, client=client, episode=episode, now=now
            )
            if reasons or view.revision != decision.follow_state_revision:
                return None
            source_message = (
                InstagramBotMessage.objects.filter(pk=decision.source_message_id).first()
                if decision.source_message_id
                else None
            )
            current_watermark = max(
                int(getattr(episode, "opened_watermark_message_id", 0) or 0),
                int(getattr(source_message, "pk", 0) or 0),
            )
            current_context = _fingerprint(
                POLICY_VERSION,
                client.pk,
                getattr(episode, "pk", None),
                decision.opportunity,
                getattr(source_message, "pk", None),
                decision.order_id,
                decision.lifecycle_event_id,
                view.revision,
                current_watermark,
                decision.base_text,
            )
            if current_context != decision.context_fingerprint:
                return None
            candidate_error = _candidate_error(
                decision.candidate_text,
                base_text=decision.base_text,
                client=client,
                source_message=source_message,
            )
            if candidate_error:
                return None
            # The state projection is the serialization row for both episode
            # and cross-episode reservations.  The unique slots remain a
            # database-backed last line against incorrect application retries.
            episode_slot = f"ig-follow-episode:{decision.commercial_episode_id}" if decision.commercial_episode_id else None
            history = _delivery_timestamps(decision.client_id, now=now)
            if history and now - max(history) < COOLDOWN:
                return None
            if len([stamp for stamp in history if now - stamp < ROLLING_YEAR]) >= 2:
                return None
            token = uuid.uuid4().hex
            decision.state = IgFollowCtaDecision.State.RESERVED
            decision.episode_slot_key = episode_slot
            decision.sent_scope_key = f"ig-follow-scope:{decision.client_id}:{token}"
            decision.lease_token = token
            decision.lease_expires_at = now + RESERVATION_LEASE
            decision.final_text = f"{decision.base_text} {decision.candidate_text}".strip()
            decision.save(
                update_fields=[
                    "state",
                    "episode_slot_key",
                    "sent_scope_key",
                    "lease_token",
                    "lease_expires_at",
                    "final_text",
                    "updated_at",
                ]
            )
            return AuthorizedFollowCta(
                decision_id=decision.pk,
                text=decision.candidate_text,
                base_text=decision.base_text,
                final_text=decision.final_text,
                lease_token=token,
            )
    except IntegrityError:
        return None


@contextmanager
def follow_provider_request_boundary(authorized: AuthorizedFollowCta, *, now=None):
    """Revalidate one reserved CTA while holding its truth rows for Meta I/O.

    ``send_text`` invokes its provider-I/O marker immediately before entering
    this boundary. If the latest policy no longer permits the optional clause,
    no provider request has happened yet, so the marker and reservation can be
    released safely and the caller may send the useful base reply instead.
    """

    now = now or timezone.now()
    decision_id = int(getattr(authorized, "decision_id", 0) or 0)
    lease_token = str(getattr(authorized, "lease_token", "") or "")
    allowed = False
    rejection_reason = "decision_missing"
    snapshot_current = False
    cancelled_same_lease = False
    trusted_base_text = ""
    with transaction.atomic():
        decision = (
            IgFollowCtaDecision.objects.select_for_update()
            .select_related("client", "commercial_episode")
            .filter(pk=decision_id)
            .first()
        )
        if decision is not None:
            if decision.state != IgFollowCtaDecision.State.RESERVED:
                rejection_reason = "decision_not_reserved"
            elif not lease_token or decision.lease_token != lease_token:
                rejection_reason = "lease_changed"
            elif decision.lease_expires_at and decision.lease_expires_at <= now:
                rejection_reason = "lease_expired"
            elif decision.base_text != str(getattr(authorized, "base_text", "") or ""):
                rejection_reason = "base_text_changed"
            elif decision.final_text != str(getattr(authorized, "final_text", "") or ""):
                rejection_reason = "final_text_changed"
            else:
                snapshot_current = True
                trusted_base_text = decision.base_text
                client = IgClient.objects.select_for_update().get(pk=decision.client_id)
                episode = (
                    IgCommercialEpisode.objects.select_for_update()
                    .filter(pk=decision.commercial_episode_id)
                    .first()
                    if decision.commercial_episode_id
                    else None
                )
                state = (
                    IgFollowState.objects.select_for_update()
                    .filter(client_id=decision.client_id)
                    .first()
                )
                if state is None:
                    rejection_reason = "follow_state_missing"
                else:
                    reasons, view, _analysis = _current_opportunity_for_decision(
                        decision,
                        client=client,
                        episode=episode,
                        now=now,
                    )
                    source_message = (
                        InstagramBotMessage.objects.filter(pk=decision.source_message_id).first()
                        if decision.source_message_id
                        else None
                    )
                    current_watermark = max(
                        int(getattr(episode, "opened_watermark_message_id", 0) or 0),
                        int(getattr(source_message, "pk", 0) or 0),
                    )
                    current_context = _fingerprint(
                        POLICY_VERSION,
                        client.pk,
                        getattr(episode, "pk", None),
                        decision.opportunity,
                        getattr(source_message, "pk", None),
                        decision.order_id,
                        decision.lifecycle_event_id,
                        view.revision,
                        current_watermark,
                        decision.base_text,
                    )
                    candidate_error = _candidate_error(
                        decision.candidate_text,
                        base_text=decision.base_text,
                        client=client,
                    )
                    if reasons:
                        rejection_reason = reasons[0]
                    elif view.revision != decision.follow_state_revision:
                        rejection_reason = "follow_revision_changed"
                    elif current_context != decision.context_fingerprint:
                        rejection_reason = "context_changed"
                    elif candidate_error:
                        rejection_reason = candidate_error
                    else:
                        allowed = True

            if not allowed and (
                decision.state == IgFollowCtaDecision.State.RESERVED
                and decision.lease_token == lease_token
            ):
                reason_codes = list(decision.reason_codes or [])
                reason_codes.append(f"provider_boundary:{rejection_reason}")
                decision.state = IgFollowCtaDecision.State.CANCELLED
                decision.suppression_reason = rejection_reason[:64]
                decision.reason_codes = list(dict.fromkeys(reason_codes))
                decision.provider_io_started_at = None
                decision.episode_slot_key = None
                decision.sent_scope_key = None
                decision.lease_token = ""
                decision.lease_expires_at = None
                decision.completed_at = now
                decision.save(
                    update_fields=[
                        "state",
                        "suppression_reason",
                        "reason_codes",
                        "provider_io_started_at",
                        "episode_slot_key",
                        "sent_scope_key",
                        "lease_token",
                        "lease_expires_at",
                        "completed_at",
                        "updated_at",
                    ]
                )
                cancelled_same_lease = True
        from management.services.instagram_bot import ProviderRequestBoundaryResult

        yield ProviderRequestBoundaryResult(
            allowed=allowed,
            replacement_text=(
                trusted_base_text
                if snapshot_current and cancelled_same_lease and not allowed
                else ""
            ),
            reason="" if allowed else rejection_reason,
        )


def finalize_follow_delivery(
    decision_id,
    *,
    outcome,
    provider_message_ids: Iterable[str] = (),
    lease_token: str | None = None,
    now=None,
) -> None:
    now = now or timezone.now()
    outcome_value = str(getattr(outcome, "value", outcome) or "").lower()
    with transaction.atomic():
        decision = IgFollowCtaDecision.objects.select_for_update().filter(pk=decision_id).first()
        if decision is None:
            return
        if decision.state in {
            IgFollowCtaDecision.State.SENT,
            IgFollowCtaDecision.State.AMBIGUOUS,
            IgFollowCtaDecision.State.CANCELLED,
            IgFollowCtaDecision.State.FAILED,
        }:
            return
        if lease_token is not None and decision.lease_token != str(lease_token or ""):
            return
        ids = [str(value)[:255] for value in (provider_message_ids or ()) if str(value).strip()]
        if outcome_value in {"provider_io_started", "sending", "started"}:
            decision.provider_io_started_at = decision.provider_io_started_at or now
            decision.save(update_fields=["provider_io_started_at", "updated_at"])
            return
        # A malformed/unsafe model candidate is a policy suppression, not a
        # reserved delivery that needs a cancellation transition. Keeping the
        # row suppressed preserves the audit reason and makes the distinction
        # visible to reconciliation and manager tooling.
        if (
            decision.state == IgFollowCtaDecision.State.SUPPRESSED
            and outcome_value in {"cancelled", "canceled", "cancelled_before_io"}
        ):
            return
        if outcome_value in {"sent", "receipt", "provider_receipt", "confirmed", "delivered"}:
            decision.state = IgFollowCtaDecision.State.SENT
            decision.provider_message_ids = ids
        elif outcome_value in {"ambiguous", "timeout_after_io", "unknown"}:
            decision.state = IgFollowCtaDecision.State.AMBIGUOUS
            decision.provider_message_ids = ids
        elif outcome_value in {"cancelled", "canceled", "cancelled_before_io"}:
            if decision.provider_io_started_at:
                # Once the non-idempotent provider boundary has started, a
                # local cancellation cannot prove that Meta rejected it.
                decision.state = IgFollowCtaDecision.State.AMBIGUOUS
                decision.provider_message_ids = ids
            else:
                decision.state = IgFollowCtaDecision.State.CANCELLED
                decision.episode_slot_key = None
                decision.sent_scope_key = None
        else:
            decision.state = IgFollowCtaDecision.State.AMBIGUOUS if decision.provider_io_started_at else IgFollowCtaDecision.State.FAILED
            if decision.state == IgFollowCtaDecision.State.FAILED:
                decision.episode_slot_key = None
                decision.sent_scope_key = None
        decision.completed_at = now
        decision.lease_token = ""
        decision.lease_expires_at = None
        decision.save(
            update_fields=[
                "state",
                "provider_message_ids",
                "episode_slot_key",
                "sent_scope_key",
                "completed_at",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )


def reconcile_expired_follow_reservations(*, now=None, limit=100) -> dict[str, int]:
    """Recover reservations whose owner died before recording an outcome.

    A reservation without a provider marker is safe to release. Once provider
    I/O may have started, the only safe terminal state is ambiguous, which also
    consumes the cooldown and prevents an automatic replay.
    """
    now = now or timezone.now()
    bounded = max(1, min(500, int(limit or 100)))
    counts = {"cancelled": 0, "ambiguous": 0}
    with transaction.atomic():
        rows = list(
            IgFollowCtaDecision.objects.select_for_update()
            .filter(
                state=IgFollowCtaDecision.State.RESERVED,
                lease_expires_at__lt=now,
            )
            .order_by("lease_expires_at", "id")[:bounded]
        )
        for decision in rows:
            decision.lease_token = ""
            decision.lease_expires_at = None
            decision.completed_at = now
            if decision.provider_io_started_at:
                decision.state = IgFollowCtaDecision.State.AMBIGUOUS
                counts["ambiguous"] += 1
            else:
                decision.state = IgFollowCtaDecision.State.CANCELLED
                decision.episode_slot_key = None
                decision.sent_scope_key = None
                counts["cancelled"] += 1
            decision.save(
                update_fields=[
                    "state",
                    "episode_slot_key",
                    "sent_scope_key",
                    "completed_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
    return counts


__all__ = [
    "AuthorizedFollowCta",
    "FollowOpportunity",
    "authorize_follow_cta",
    "evaluate_follow_opportunity",
    "follow_opportunity_prompt_note",
    "follow_provider_request_boundary",
    "finalize_follow_delivery",
    "live_follow_opportunity",
    "prepare_local_payment_follow_snapshot",
    "prepare_payment_follow_snapshot",
    "prepare_follow_decision",
    "record_follow_refusal_from_inbound",
    "reconcile_expired_follow_reservations",
]
