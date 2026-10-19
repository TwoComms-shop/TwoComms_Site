"""Deterministic Assisted Checkout V2 payment-policy evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from django.core.signing import salted_hmac


PROPOSAL_TTL_SECONDS = 12 * 60 * 60
GENERATION_TTL_SECONDS = 25 * 60
PROVIDER_VALIDITY_SECONDS = 1500
PREPAY_200_AMOUNT = "200.00"

PREPAY_200_QUICK_REPLY = "twc:v1:checkout_payment:prepay_200_cod"
_DIRECT_PREPAY_QUESTION_RE = re.compile(
    r"(?:\b(?:200|двісті|двести)\s*(?:грн|uah|₴)?\b.{0,80}"
    r"(?:передоплат|предоплат|післяплат|налож|cod|cash\s+on\s+delivery)"
    r"|(?:передоплат|предоплат|післяплат|налож|cod|cash\s+on\s+delivery)"
    r".{0,80}\b(?:200|двісті|двести)\s*(?:грн|uah|₴)?\b)",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_MARKER_RE = re.compile(
    r"(?:\?|\b(?:можна|можно|чи|можливо|возможно|can\s+i|could\s+i|"
    r"do\s+you|is\s+it\s+possible)\b)",
    re.IGNORECASE,
)
_COD_REQUEST_RE = re.compile(r'(?:післяплат|налож|накладен\w*\s+плат|оплат\w*\s+(?:на|у)\s+пошт|cash\s+on\s+delivery|\bcod\b)', re.I)
_UNSAFE_COMMERCIAL_RE = re.compile(
    r'(?:[?？«»“”"„]|\b(?:не|ні|нет|not|no|раніше|раньше|колись|ранее|previously|'
    r'раніш\w*|минул\w*|прошл\w*|бул[аои]|был\w*|змін\w*|измен\w*|інш\w*|друг\w*|'
    r'можна|можно|чи|якщо|если|якби|если\s+бы|could|would|was|were|'
    r'торік|вчора|вчера|казав|казала|кажуть|говорят|писали|питали|обговорювали|'
    r'конкурент\w*|скільки|сколько|яка|який|какая|какой|чому|почему|what|how)\b)', re.I)
_ACCEPTANCE_RE = re.compile(
    r'^\s*(?:так|да|ок(?:ей)?|okay|ok|yes|добре|хорошо|підходить|подходит|'
    r'домовились|договорились|погоджуюсь|згод\w*|соглас\w*|оформлюйте|оформляйте|беру)'
    r'(?:[\s,.!;:—-]+(?:так|да|мені|мне|це|это|все|всё|підходить|подходит|добре|хорошо|'
    r'домовились|договорились|погоджуюсь|згод\w*|соглас\w*|оформлюйте|оформляйте|беру|дякую|спасибо))*[\s.!]*$', re.I)
_CONSULTATION_NEED_RE = re.compile(
    r'(?:розмір|размер|посадк|сісти|підійд|подойд|подар\w*|якіст|качеств|довір|довер|боюс|страш|сумнів|сомнен)', re.I)
_CONSULTATION_HELP_RE = re.compile(
    r'(?:замір|замер|сітц|сітк|сетк|таблиц|мірк|мерк|ширин|довжин|длин|фото|відео|видео|огляд|осмотр|примір|пример)', re.I)


def affirmative_commercial_statement(text):
    """Questions, denied amounts and historical mentions grant no money authority."""
    return bool(str(text or '').strip() and not _UNSAFE_COMMERCIAL_RE.search(str(text))
        and not re.search(r'\b(?:не|ні|нет|not)\s*\d', str(text), re.I))


def accepts_current_commercial_offer(text, *, amount=None):
    if not affirmative_commercial_statement(text):
        return False
    body = str(text)
    if amount is not None:
        quoted = re.findall(r'(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)\s*(?:грн|uah|₴)', body, re.I)
        if any(Decimal(value.replace(',', '.')) != amount for value in quoted):
            return False
        body = re.sub(r'(?:за\s+)?(?<!\d)\d{2,6}(?:[.,]\d{1,2})?\s*(?:грн|uah|₴)', '', body, flags=re.I)
    return bool(_ACCEPTANCE_RE.fullmatch(body.rstrip(' ,.;:!—-')))


def _accepted_current_prepay_offer(client, latest, floor):
    """A short yes accepts only the immediately preceding, consulted 200 offer.

    No user scope change may intervene. Episode/reset floor and a bounded offer
    lifetime prevent a phrase from an earlier purchase becoming current consent.
    """
    from management.models import InstagramBotMessage
    if not accepts_current_commercial_offer(latest.text):
        return None
    prior = list(InstagramBotMessage.objects.filter(client_id=client.pk,
        pk__gte=floor, pk__lt=latest.pk).order_by('-pk')[:12])
    if not prior:
        return None
    offer = prior[0]
    text = str(offer.text or '')
    if (offer.role not in {'model', 'manager'} or offer.status != 'done'
            or (latest.created_at - offer.created_at).total_seconds() > PROPOSAL_TTL_SECONDS
            or re.search(r'(?:[?？«»“”"]|\b(?:раніше|раньше|колись|ранее|previously|был\w*|бул[аои])\b|\bне\s*(?:200|передоплат|предоплат|післяплат|налож))', text, re.I)
            or not _DIRECT_PREPAY_QUESTION_RE.search(text)
            or not _COD_REQUEST_RE.search(text)
            or not re.search(r'(?:решт|залиш|остат|remainder|balance)', text, re.I)):
        return None
    # An isolated old bot phrase is not an eligible consultation. The latest
    # customer need and a substantive answer must precede this exact offer.
    need = next((row for row in prior[1:] if row.role == 'user'), None)
    if (need is None or not _CONSULTATION_NEED_RE.search(need.text or '')
            or not any(row.role in {'model', 'manager'} and row.status == 'done'
                and _CONSULTATION_HELP_RE.search(row.text or '')
                for row in prior if row.pk > need.pk)):
        return None
    if re.search(r'(?:кастом|custom|за\s+кордон|за\s+границ|international|самовив|самовыв)',
            ' '.join(row.text or '' for row in prior[:prior.index(need) + 1]), re.I):
        return None
    return offer


@dataclass(frozen=True, slots=True)
class PaymentPolicyDecision:
    policy: str
    evidence_message_id: int | None = None
    evidence_kind: str = ""
    evidence_revision: int = 0
    evidence_digest: str = ""
    custom_print_full_only: bool = False


def _evidence_digest(*, client_id: int, message_id: int, kind: str) -> str:
    return salted_hmac(
        "management.assisted-checkout-v2.payment-policy",
        f"{int(client_id)}:{int(message_id)}:{kind}",
        algorithm="sha256",
    ).hexdigest()[:64]


def resolve_payment_policy(
    *,
    client,
    evidence_message_ids=(),
    custom_print_full_only=False,
) -> PaymentPolicyDecision:
    """Only the current customer's latest direct turn may unlock 200+COD."""
    from management.models import IgCheckoutProposal, InstagramBotMessage
    from management.services.ig_funnel_reset import current_message_floor

    if custom_print_full_only:
        return PaymentPolicyDecision(
            policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY,
            custom_print_full_only=True,
        )
    evidence_ids = {
        int(value)
        for value in evidence_message_ids or ()
        if str(value).isdigit() and int(value) > 0
    }
    if not evidence_ids:
        return PaymentPolicyDecision(
            policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY
        )
    floor = current_message_floor(client)
    latest = (
        InstagramBotMessage.objects.filter(
            client_id=client.pk,
            role=InstagramBotMessage.Role.USER,
            pk__gte=floor,
        )
        .order_by("-pk")
        .only("pk", "text", "quick_reply_payload")
        .first()
    )
    if latest is None or latest.pk not in evidence_ids:
        return PaymentPolicyDecision(
            policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY
        )
    if latest.quick_reply_payload == PREPAY_200_QUICK_REPLY:
        kind = "quick_reply"
    elif (
        (_DIRECT_PREPAY_QUESTION_RE.search(str(latest.text or "")) or _COD_REQUEST_RE.search(str(latest.text or "")))
        and _QUESTION_MARKER_RE.search(str(latest.text or ""))
        and not re.search(r'(?:\b(?:не|ні|нет|раніше|раньше|колись|был\w*|бул[аои])\b|[«»“”"])', str(latest.text or ''), re.I)
    ):
        kind = "direct_question"
    else:
        offer = _accepted_current_prepay_offer(client, latest, floor)
        if offer is None:
            return PaymentPolicyDecision(policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY)
        # Persist source identity/content in the digest with the existing schema.
        kind = 'offer_acceptance'
    return PaymentPolicyDecision(
        policy=IgCheckoutProposal.PaymentPolicy.FULL_OR_200_COD,
        evidence_message_id=latest.pk,
        evidence_kind=kind,
        evidence_revision=latest.pk,
        evidence_digest=_evidence_digest(
            client_id=client.pk,
            message_id=latest.pk,
            kind=(f'{kind}:{offer.pk}:{offer.text}:{latest.text}' if kind == 'offer_acceptance' else kind),
        ),
    )


def payment_choice_for_post(proposal, raw_choice: object):
    """Validate browser choice against immutable server-side policy."""
    from management.models import IgCheckoutInvoiceGeneration, IgCheckoutProposal

    choice = str(raw_choice or "online_full").strip().casefold()
    if choice in {"", "full", "online_full"}:
        return IgCheckoutInvoiceGeneration.PaymentChoice.FULL
    if choice != IgCheckoutInvoiceGeneration.PaymentChoice.PREPAY_200_COD:
        raise ValueError("forged_payment_choice")
    if (
        not proposal.assisted_checkout_v2
        or proposal.custom_print_full_only
        or proposal.payment_policy
        != IgCheckoutProposal.PaymentPolicy.FULL_OR_200_COD
        or not proposal.payment_policy_evidence_message_id
        or proposal.payment_policy_evidence_kind
        not in {"direct_question", "quick_reply", "offer_acceptance"}
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(proposal.payment_policy_evidence_digest or ""),
        )
    ):
        raise ValueError("forged_payment_choice")
    if proposal.payment_policy_evidence_kind == 'offer_acceptance':
        from management.models import InstagramBotMessage
        from management.services.ig_funnel_reset import current_message_floor
        accepted = InstagramBotMessage.objects.filter(pk=proposal.payment_policy_evidence_message_id,
            client_id=proposal.client_id, role='user').first()
        offer = (_accepted_current_prepay_offer(proposal.client, accepted,
            current_message_floor(proposal.client)) if accepted is not None else None)
        expected = (_evidence_digest(client_id=proposal.client_id, message_id=accepted.pk,
            kind=f'offer_acceptance:{offer.pk}:{offer.text}:{accepted.text}') if offer is not None else None)
        if expected != proposal.payment_policy_evidence_digest:
            raise ValueError('forged_payment_choice')
    return IgCheckoutInvoiceGeneration.PaymentChoice.PREPAY_200_COD
