"""Shared copy and read-only eligibility for the existing delivery outboxes."""
from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)

INVITATION_VERSION = 1
INVITATION_VERSION_REVIEW_LINKS = 2
MODE_REVIEW_ONLY = "review_only"
MODE_REVIEW_AND_REWARD = "review_and_reward"
MODE_REVIEW_AND_UPLIFT = "review_and_uplift"
INVITATION_PAYLOAD_KEY = "post_purchase_invitation"
MAX_REVIEW_LINKS = 3

INVITATION_BLOCK_REASONS = frozenset({
    "post_purchase_service_case_open",
    "post_purchase_reply_debt_open",
    "post_purchase_already_rewarded",
    "post_purchase_eligibility_unknown",
    "post_purchase_marketing_consent_required",
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


def _positive_id(value):
    return type(value) is int and 0 < value < 2 ** 63


def _plain(value, limit):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and all(ord(character) >= 32 for character in value))


def _v2_metadata_valid(metadata):
    """Finite immutable copy inputs; source authority is checked separately."""
    keys = {"version", "mode", "order_number", "client_id", "order_id",
            "assignment_id", "assignment_version", "eligibility_kind", "reward_id", "reviews", "business_consent"}
    if not isinstance(metadata, dict) or set(metadata) != keys:
        return False
    if not isinstance(metadata["business_consent"], dict) or not metadata["business_consent"]:
        return False
    if (type(metadata["version"]) is not int or metadata["version"] != INVITATION_VERSION_REVIEW_LINKS
            or not _plain(metadata["order_number"], 20)
            or not all(_positive_id(metadata[key]) for key in
                       ("client_id", "order_id", "assignment_id", "assignment_version"))):
        return False
    if metadata["mode"] == MODE_REVIEW_AND_REWARD:
        if metadata["eligibility_kind"] != "potential_base_10" or metadata["reward_id"] is not None:
            return False
    elif metadata["mode"] == MODE_REVIEW_AND_UPLIFT:
        if metadata["eligibility_kind"] != "unused_base_10" or not _positive_id(metadata["reward_id"]):
            return False
    else:
        return False
    reviews = metadata["reviews"]
    if not isinstance(reviews, list) or not 1 <= len(reviews) <= MAX_REVIEW_LINKS:
        return False
    seen_ids, seen_products = set(), set()
    for source in reviews:
        if not isinstance(source, dict) or set(source) != {
                "invitation_id", "order_item_id", "product_id", "reset_audit_id", "url", "product_label"}:
            return False
        try:
            invitation_id = str(UUID(source["invitation_id"]))
        except (TypeError, ValueError, AttributeError):
            return False
        if (invitation_id != source["invitation_id"] or invitation_id in seen_ids
                or not _positive_id(source["order_item_id"]) or not _positive_id(source["product_id"])
                or source["product_id"] in seen_products
                or type(source["reset_audit_id"]) is not int or not 0 <= source["reset_audit_id"] < 2 ** 63
                or not _plain(source["url"], 600) or not _plain(source["product_label"], 120)):
            return False
        seen_ids.add(invitation_id)
        seen_products.add(source["product_id"])
    return True


def _v2_copy_fits(text):
    from management.services.ig_delivery_plan import split_url_safe

    chunks, remainder = split_url_safe(text, limit=950, max_chunks=4)
    return bool(chunks) and not remainder.strip()


def post_purchase_invitation_v2_text(locale, order, *, metadata):
    """Version-two exact copy, including only frozen purchased-product links."""
    if not isinstance(locale, str) or locale not in {"uk", "ru", "en"} or not _v2_metadata_valid(metadata):
        raise ValueError("Invalid version-two post-purchase invitation")
    number = metadata["order_number"]
    if locale == "en":
        review = (f"Thank you for your order #{number}! How are the items' quality and fit, "
                  "if they have been tried on? Leave an honest review of your purchased item "
                  "on its product page. Any rating from 1 to 5 is welcome; reviews are published after moderation.")
        links = "\n".join(f"Review of {source['product_label']}: {source['url']}" for source in metadata["reviews"])
        story = (" If you wish, share the items in an Instagram story and tag @twocomms, "
                 "then send the story link or screenshot in Direct. After we verify the story "
                 "and reward eligibility, we will issue a one-use 10% discount for your next "
                 "order, valid for 90 days from issuance.")
        uplift = (" After your review is published, an active unused 10% story reward code can gain "
                  "an additional 5%, reaching 15% once. Any rating from 1 to 5 qualifies; a comment without a rating does not. "
                  "The original expiry, 90 days from issuance, stays the same. A used code cannot be increased.")
    elif locale == "ru":
        review = (f"Спасибо за заказ №{number}! Всё ли хорошо с вещами — качеством и посадкой, "
                  "если уже примеряли? Оставьте честный отзыв о купленном товаре на его странице. "
                  "Принимаем любую оценку от 1 до 5; отзыв публикуется после модерации.")
        links = "\n".join(f"Отзыв о «{source['product_label']}»: {source['url']}" for source in metadata["reviews"])
        story = (" Если захотите, покажите вещи в сторис и отметьте @twocomms, затем пришлите ссылку "
                 "или скрин в Direct. После проверки сторис и права на награду выдадим одноразовую "
                 "скидку 10% на следующий заказ, действующую 90 дней с момента выдачи.")
        uplift = (" После публикации отзыва к действующему неиспользованному промокоду за сторис 10% можно один раз "
                  "добавить 5% — всего 15%. Подойдёт любая оценка от 1 до 5; комментарий без оценки бонуса не даёт. "
                  "Исходный срок — 90 дней с первой выдачи — не продлевается. Использованный код увеличить нельзя.")
    else:
        review = (f"Дякуємо за замовлення №{number}! Чи все добре з речами — якістю та посадкою, "
                  "якщо вже приміряли? Залиште чесний відгук про придбаний товар на його сторінці. "
                  "Приймаємо будь-яку оцінку від 1 до 5; відгук публікується після модерації.")
        links = "\n".join(f"Відгук про «{source['product_label']}»: {source['url']}" for source in metadata["reviews"])
        story = (" Якщо захочете, покажіть речі в сторіс і відмітьте @twocomms, потім надішліть посилання "
                 "або скрін у Direct. Після перевірки сторіс і права на нагороду видамо одноразову "
                 "знижку 10% на наступне замовлення, що діятиме 90 днів з моменту видачі.")
        uplift = (" Після публікації відгуку до чинного невикористаного промокоду за сторіс 10% можна один раз "
                  "додати 5% — загалом 15%. Підійде будь-яка оцінка від 1 до 5; коментар без оцінки бонусу не дає. "
                  "Початковий строк — 90 днів від першої видачі — не подовжується. Використаний код підвищити не можна.")
    return review + "\n" + links + (story if metadata["mode"] == MODE_REVIEW_AND_REWARD else "") + uplift


def _v2_snapshot(client, order, locale, assignment):
    """Producer-only minting: never called by the dispatch or private GET paths."""
    from django.utils.translation import override
    from management.services.ig_marketing_consent import post_purchase_consent_source
    from management.services.ig_review_reward import review_uplift_eligibility
    from management.services.ig_ugc_rewards import _active_opt_out
    from reviews.services.purchase_invites import (
        eligible_purchase_review_items, ensure_purchase_review_invitation,
        invitation_url, validate_invitation,
    )

    if (not isinstance(locale, str) or locale not in {"uk", "ru", "en"}
            or not _positive_id(getattr(assignment, "pk", None))
            or not _positive_id(getattr(assignment, "version", None))
            or getattr(assignment, "client_id", None) != client.pk
            or getattr(assignment, "order_id", None) != order.pk
            or getattr(assignment, "unassigned_at", None) is not None):
        return None
    if _active_opt_out(client):
        return None
    consent = post_purchase_consent_source(client, order.pk)
    if not consent:
        return None
    offer = review_uplift_eligibility(client, order)
    if not isinstance(offer, dict) or offer.get("eligible") is not True:
        return None
    kind = offer.get("kind")
    mode = {"potential_base_10": MODE_REVIEW_AND_REWARD,
            "unused_base_10": MODE_REVIEW_AND_UPLIFT}.get(kind)
    if mode is None:
        return None
    sources, products = [], set()
    with override(locale):
        for item in eligible_purchase_review_items(client=client, order=order):
            if item.product_id in products:
                continue
            label = str(item.product.title or "").strip()[:120]
            if not _plain(label, 120):
                continue
            invitation = ensure_purchase_review_invitation(client=client, order=order, order_item=item)
            _, _, _, current_assignment, _ = validate_invitation(invitation)
            if current_assignment.pk != assignment.pk or current_assignment.version != assignment.version:
                return None
            sources.append({"invitation_id": str(invitation.pk), "order_item_id": item.pk,
                "product_id": item.product_id, "reset_audit_id": invitation.reset_audit_id,
                "url": invitation_url(invitation), "product_label": label})
            products.add(item.product_id)
            if len(sources) == MAX_REVIEW_LINKS:
                break
    metadata = {"version": INVITATION_VERSION_REVIEW_LINKS, "mode": mode,
        "order_number": str(order.order_number or order.pk), "client_id": client.pk,
        "order_id": order.pk, "assignment_id": assignment.pk,
        "assignment_version": assignment.version, "eligibility_kind": kind,
        "reward_id": offer.get("reward_id"), "reviews": sources, "business_consent": consent}
    while sources:
        if not _v2_metadata_valid(metadata):
            return None
        message = post_purchase_invitation_v2_text(locale, order, metadata=metadata)
        if _v2_copy_fits(message):
            return metadata, message
        sources.pop()
    return None


def post_purchase_invitation_snapshot(client, order, locale, *, assignment=None):
    """Freeze v2 purchased-product links only with proven reward eligibility.

    An unsolicited incentive needs explicit purpose consent. Without it, the
    customer receives only the honest service review request. Snapshots grant
    neither a coupon nor permission to send outside the standard window.
    """
    from management.services.ig_ugc_rewards import ugc_identity_already_rewarded
    from management.services.ig_marketing_consent import post_purchase_consent_source

    if assignment is not None:
        try:
            snapshot = _v2_snapshot(client, order, locale, assignment)
            if snapshot is not None:
                return snapshot
        except Exception:
            logger.warning("Purchased-product invitation snapshot authority unavailable")
    mode = MODE_REVIEW_ONLY
    try:
        if post_purchase_consent_source(client, order.pk) and not ugc_identity_already_rewarded(client):
            mode = MODE_REVIEW_AND_REWARD
    except Exception:
        logger.warning("Post-purchase snapshot lifetime authority unavailable")
    number = str(order.order_number or order.pk)
    metadata = {"version": INVITATION_VERSION, "mode": mode, "order_number": number}
    return metadata, post_purchase_invitation_text(locale, order, mode=mode, order_number=number)


def _v2_live(client, order, locale, metadata, assignment):
    """Read-only recheck of the exact source and reward capability at dispatch."""
    from django.utils.translation import override
    from management.services.ig_marketing_consent import post_purchase_consent_source
    from management.services.ig_review_reward import review_uplift_eligibility
    from management.services.ig_ugc_rewards import _active_opt_out
    from reviews.models import ReviewPurchaseInvitation
    from reviews.services.purchase_invites import invitation_url, validate_invitation

    if (metadata["client_id"] != client.pk or metadata["order_id"] != order.pk
            or getattr(assignment, "pk", None) != metadata["assignment_id"]
            or getattr(assignment, "version", None) != metadata["assignment_version"]
            or getattr(assignment, "client_id", None) != client.pk
            or getattr(assignment, "order_id", None) != order.pk
            or getattr(assignment, "unassigned_at", None) is not None
            or _active_opt_out(client)):
        return False
    if post_purchase_consent_source(client, order.pk) != metadata["business_consent"]:
        return False
    offer = review_uplift_eligibility(client, order)
    if (not isinstance(offer, dict) or offer.get("eligible") is not True
            or offer.get("kind") != metadata["eligibility_kind"]
            or offer.get("reward_id") != metadata["reward_id"]):
        return False
    with override(locale):
        for source in metadata["reviews"]:
            invitation = ReviewPurchaseInvitation.objects.filter(pk=source["invitation_id"]).first()
            if invitation is None:
                return False
            owner, purchase, item, active_assignment, reset_id = validate_invitation(invitation)
            if (owner.pk != client.pk or purchase.pk != order.pk
                    or item.pk != source["order_item_id"] or item.product_id != source["product_id"]
                    or invitation.product_id != source["product_id"]
                    or active_assignment.pk != metadata["assignment_id"]
                    or active_assignment.version != metadata["assignment_version"]
                    or reset_id != source["reset_audit_id"]
                    or invitation.reset_audit_id != source["reset_audit_id"]
                    or invitation_url(invitation) != source["url"]):
                return False
    return True


def post_purchase_invitation_block_reason(client, order, payload=None, text=None, locale=None, final_text="", *, assignment=None):
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
    linked_metadata = None
    if payload is not None and not isinstance(payload, dict):
        return "post_purchase_eligibility_unknown"
    if isinstance(payload, dict) and INVITATION_PAYLOAD_KEY in payload:
        metadata = payload[INVITATION_PAYLOAD_KEY]
        if isinstance(metadata, dict) and type(metadata.get("version")) is int and metadata["version"] == INVITATION_VERSION_REVIEW_LINKS:
            if not _v2_metadata_valid(metadata) or not isinstance(locale, str) or locale not in {"uk", "ru", "en"}:
                return "post_purchase_eligibility_unknown"
            expected = post_purchase_invitation_v2_text(locale, order, metadata=metadata)
            if text != expected or (final_text and final_text != expected) or not _v2_copy_fits(expected):
                return "post_purchase_eligibility_unknown"
            linked_metadata = metadata
            mode = metadata["mode"]
        else:
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
        if linked_metadata is not None:
            return "" if _v2_live(client, order, locale, linked_metadata, assignment) else "post_purchase_eligibility_unknown"
        if mode == MODE_REVIEW_ONLY:
            # The immutable copy offers no reward. Later keyring/identity
            # uncertainty cannot turn this service review into a new offer.
            return ""
        if ugc_identity_already_rewarded(client):
            return "post_purchase_already_rewarded"
        if ugc_identity_lifetime_conflicted(client):
            return "post_purchase_eligibility_unknown"
        from management.services.ig_marketing_consent import has_post_purchase_consent

        if not has_post_purchase_consent(client, order.pk):
            return "post_purchase_marketing_consent_required"
    except Exception:
        # A missing keyring or unreadable authority is not an unused grant.
        logger.warning("Post-purchase invitation eligibility unavailable")
        return "post_purchase_eligibility_unknown"
    return ""
