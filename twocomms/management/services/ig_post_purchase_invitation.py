"""Shared copy and read-only eligibility for the existing delivery outboxes."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

INVITATION_BLOCK_REASONS = frozenset({
    "post_purchase_service_case_open",
    "post_purchase_reply_debt_open",
    "post_purchase_already_rewarded",
    "post_purchase_eligibility_unknown",
})


def post_purchase_invitation_text(locale, order):
    number = order.order_number or str(order.pk)
    if locale == "en":
        return (
            f"Thank you for your order #{number}! Is everything all right with the "
            "items' quality and fit, if they have been tried on? "
            "We would appreciate a short honest review here. If you wish, share the "
            "items in an Instagram story and tag @twocomms, then send the story link "
            "or screenshot in Direct. After we verify the story and reward eligibility, "
            "we will issue a one-use 10% discount for your next order, valid for "
            "90 days from issuance."
        )
    if locale == "ru":
        return (
            f"Спасибо за заказ №{number}! Всё ли хорошо с вещами — качеством "
            "и посадкой, если уже примеряли? "
            "Будем благодарны за короткий честный отзыв здесь. Если захотите, "
            "покажите вещи в сторис и отметьте @twocomms, затем пришлите ссылку "
            "или скрин в Direct. После проверки сторис и права на награду выдадим "
            "одноразовую скидку 10% на следующий заказ, действующую 90 дней "
            "с момента выдачи."
        )
    return (
        f"Дякуємо за замовлення №{number}! Чи все добре з речами — якістю "
        "та посадкою, якщо вже приміряли? "
        "Будемо вдячні за короткий чесний відгук тут. Якщо захочете, покажіть "
        "речі в сторіс і відмітьте @twocomms, потім надішліть посилання або скрін "
        "у Direct. Після перевірки сторіс і права на нагороду видамо одноразову "
        "знижку 10% на наступне замовлення, що діятиме 90 днів з моменту видачі."
    )


def post_purchase_invitation_block_reason(client, order):
    """Fail closed without creating consent, lifetime slots, or reward records.

    Called again at the provider boundary: a service case or consumed lifetime
    entitlement can appear after an invitation was materialized.
    """
    from management.services.ig_response_debt import unresolved_reply_debts
    from management.services.ig_ugc_rewards import (
        ugc_identity_already_rewarded,
        ugc_identity_lifetime_conflicted,
        ugc_service_case_reason,
    )

    if not getattr(client, "pk", None) or not getattr(order, "pk", None):
        return "post_purchase_eligibility_unknown"
    try:
        if ugc_service_case_reason(client, order=order):
            return "post_purchase_service_case_open"
        if unresolved_reply_debts().filter(client_id=client.pk).exists():
            return "post_purchase_reply_debt_open"
        if ugc_identity_already_rewarded(client):
            return "post_purchase_already_rewarded"
        if ugc_identity_lifetime_conflicted(client):
            return "post_purchase_eligibility_unknown"
    except Exception:
        # A missing keyring or unreadable authority is not an unused grant.
        logger.warning("Post-purchase invitation eligibility unavailable")
        return "post_purchase_eligibility_unknown"
    return ""
