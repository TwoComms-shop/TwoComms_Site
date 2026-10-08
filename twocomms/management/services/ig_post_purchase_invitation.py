"""Shared copy and read-only eligibility for the existing delivery outboxes."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

INVITATION_VERSION = 1
MODE_REVIEW_ONLY = "review_only"
MODE_REVIEW_AND_REWARD = "review_and_reward"
INVITATION_PAYLOAD_KEY = "post_purchase_invitation"

INVITATION_BLOCK_REASONS = frozenset({
    "post_purchase_service_case_open",
    "post_purchase_reply_debt_open",
    "post_purchase_already_rewarded",
    "post_purchase_eligibility_unknown",
})


def post_purchase_invitation_text(locale, order, *, mode=MODE_REVIEW_AND_REWARD, order_number=None):
    """Version-one copy; review-only snapshots never contain an offer clause."""
    if mode not in {MODE_REVIEW_ONLY, MODE_REVIEW_AND_REWARD}:
        raise ValueError("Unsupported post-purchase invitation mode")
    number = order_number if order_number is not None else (order.order_number or str(order.pk))
    if locale == "en":
        review = (
            f"Thank you for your order #{number}! Is everything all right with the "
            "items' quality and fit, if they have been tried on? "
            "We would appreciate a short honest review here."
        )
        offer = (
            " If you wish, share the "
            "items in an Instagram story and tag @twocomms, then send the story link "
            "or screenshot in Direct. After we verify the story and reward eligibility, "
            "we will issue a one-use 10% discount for your next order, valid for "
            "90 days from issuance."
        )
    elif locale == "ru":
        review = (
            f"Спасибо за заказ №{number}! Всё ли хорошо с вещами — качеством "
            "и посадкой, если уже примеряли? "
            "Будем благодарны за короткий честный отзыв здесь."
        )
        offer = (
            " Если захотите, "
            "покажите вещи в сторис и отметьте @twocomms, затем пришлите ссылку "
            "или скрин в Direct. После проверки сторис и права на награду выдадим "
            "одноразовую скидку 10% на следующий заказ, действующую 90 дней "
            "с момента выдачи."
        )
    else:
        review = (
            f"Дякуємо за замовлення №{number}! Чи все добре з речами — якістю "
            "та посадкою, якщо вже приміряли? "
            "Будемо вдячні за короткий чесний відгук тут."
        )
        offer = (
            " Якщо захочете, покажіть "
            "речі в сторіс і відмітьте @twocomms, потім надішліть посилання або скрін "
            "у Direct. Після перевірки сторіс і права на нагороду видамо одноразову "
            "знижку 10% на наступне замовлення, що діятиме 90 днів з моменту видачі."
        )
    return review if mode == MODE_REVIEW_ONLY else review + offer


def post_purchase_invitation_snapshot(client, order, locale):
    """Freeze plain review only after positive proof of a consumed lifetime grant.

    Creation grants no send authority. Unknown offer eligibility keeps the
    combined mode, whose live guards can recover before dispatch; exceptions
    never select a review-only bypass.
    """
    from management.services.ig_ugc_rewards import ugc_identity_already_rewarded

    mode = MODE_REVIEW_AND_REWARD
    try:
        if ugc_identity_already_rewarded(client):
            mode = MODE_REVIEW_ONLY
    except Exception:
        logger.warning("Post-purchase snapshot lifetime authority unavailable")
    number = str(order.order_number or order.pk)
    metadata = {"version": INVITATION_VERSION, "mode": mode, "order_number": number}
    return metadata, post_purchase_invitation_text(locale, order, mode=mode, order_number=number)


def post_purchase_invitation_block_reason(client, order, payload=None, text=None, locale=None, final_text=""):
    """Fail closed without creating consent, lifetime slots, or reward records.

    Called again at the provider boundary: service/debt changes block either
    mode, while lifetime consumption blocks only an invitation offering a reward.
    """
    from management.services.ig_response_debt import unresolved_reply_debts
    from management.services.ig_ugc_rewards import (
        ugc_identity_already_rewarded,
        ugc_identity_lifetime_conflicted,
        ugc_service_case_reason,
    )

    if not getattr(client, "pk", None) or not getattr(order, "pk", None):
        return "post_purchase_eligibility_unknown"
    mode = MODE_REVIEW_AND_REWARD
    if payload is not None and not isinstance(payload, dict):
        return "post_purchase_eligibility_unknown"
    if isinstance(payload, dict) and INVITATION_PAYLOAD_KEY in payload:
        metadata = payload[INVITATION_PAYLOAD_KEY]
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("version")) is not int
            or metadata.get("version") != INVITATION_VERSION
            or not isinstance(metadata.get("mode"), str)
            or metadata.get("mode") not in {MODE_REVIEW_ONLY, MODE_REVIEW_AND_REWARD}
            or not isinstance(metadata.get("order_number"), str)
            or not metadata.get("order_number", "").strip()
            or len(metadata.get("order_number", "")) > 20
            or any(ord(character) < 32 for character in metadata.get("order_number", ""))
        ):
            return "post_purchase_eligibility_unknown"
        mode = metadata["mode"]
        if mode == MODE_REVIEW_ONLY:
            if not isinstance(locale, str) or locale not in {"uk", "ru", "en"}:
                return "post_purchase_eligibility_unknown"
            expected = post_purchase_invitation_text(
                locale, order, mode=MODE_REVIEW_ONLY,
                order_number=metadata["order_number"],
            )
            if text != expected or (final_text and final_text != expected):
                return "post_purchase_eligibility_unknown"
    try:
        if ugc_service_case_reason(client, order=order):
            return "post_purchase_service_case_open"
        if unresolved_reply_debts().filter(client_id=client.pk).exists():
            return "post_purchase_reply_debt_open"
        if mode == MODE_REVIEW_ONLY:
            # The immutable copy offers no reward. Later keyring/identity
            # uncertainty cannot turn this service review into a new offer.
            return ""
        if ugc_identity_already_rewarded(client):
            return "post_purchase_already_rewarded"
        if ugc_identity_lifetime_conflicted(client):
            return "post_purchase_eligibility_unknown"
    except Exception:
        # A missing keyring or unreadable authority is not an unused grant.
        logger.warning("Post-purchase invitation eligibility unavailable")
        return "post_purchase_eligibility_unknown"
    return ""
