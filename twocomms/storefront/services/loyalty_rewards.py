"""Exchange the existing points balance for an individually owned reward."""

import hashlib
import secrets

from django.db import transaction


class LoyaltyRewardError(ValueError):
    pass


@transaction.atomic
def redeem_points_reward(*, user, operation_id):
    """Issue one 10% right per 100 points; retrying an operation is read-only.

    UserPromoCode is the reward/operation ledger; PointsHistory remains the
    only points ledger. No expiry is invented for this programme.
    """
    from accounts.models import UserPoints
    from storefront.models import PromoCode, UserPromoCode

    operation_id = str(operation_id or "").strip()
    if not user or not user.is_authenticated or not operation_id or len(operation_id) > 200:
        raise LoyaltyRewardError("invalid_operation")
    operation_key = "points10:" + hashlib.sha256(operation_id.encode()).hexdigest()
    # The existing balance is the transactional serialization row. A missing
    # balance cannot spend or create a reward; auth_user need not be InnoDB.
    balance = UserPoints.objects.select_for_update().filter(user=user).first()
    existing = UserPromoCode.objects.select_related("promo_code").filter(
        user=user, survey_key=operation_key, source="points_reward",
    ).first()
    if existing is not None:
        return existing
    if balance is None or balance.points < 100:
        raise LoyaltyRewardError("insufficient_points")
    promo = PromoCode.objects.create(
        code="PTS" + secrets.token_hex(8).upper(),
        discount_type="percentage", discount_value=10,
        promo_type="regular", max_uses=1, one_time_per_user=True,
        description="Обмін 100 балів на персональну знижку 10%",
    )
    grant = UserPromoCode.objects.create(
        user=user, promo_code=promo, survey_key=operation_key,
        source="points_reward",
    )
    if not balance.spend_points(100, f"Обмін 100 балів на промокод {promo.code}"):
        raise LoyaltyRewardError("insufficient_points")
    return grant
