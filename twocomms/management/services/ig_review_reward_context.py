"""Bounded read-only benefit snapshot; no grant, redemption or delivery authority."""
from copy import deepcopy
from datetime import timedelta

from django.utils import timezone

from management.services.ig_client_state_card import SCOPE_KEYS

VERSION = "ugc-review-benefit-context.v1"
SLOT = "benefits.ugc_review_reward"
POLICY = {
    "version": VERSION, "ugc_base_percent": 10, "review_added_percent": 5,
    "combined_percent": 15, "accepted_star_ratings": [1, 2, 3, 4, 5],
    "requires_approved_source_bound_website_review": True, "comments_without_stars_qualify": False,
    "same_existing_unused_10_percent_coupon_only": True,
    "same_source_order_required_for_delivered_order_uplift": True, "new_coupon": False,
    "standalone_review_5_percent_coupon": False, "second_ugc_grant": False,
    "original_expiry_days": 90, "extends_expiry": False,
    "unsolicited_offers_require_live_order_scoped_business_consent": True,
    "business_consent_does_not_extend_meta_messaging_window": True,
    "actions": "Only deterministic reward/review/redemption owners may issue, upgrade, or apply a coupon.",
}


def _source_order_held(reward):
    from management.ig_bot_models import IgDeal, IgPaymentProjection, IgPostSaleCase
    from management.services.ig_ugc_rewards import UGC_LIFECYCLE_REFUND_TRUTHS
    from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed

    if reward.reward_path != "delivered_order":
        return False
    order = reward.order
    if order is None or order.status == "cancelled" or not nova_poshta_order_fulfillment_confirmed(order):
        return True
    projections = IgPaymentProjection.objects.filter(deal__order_id=order.pk)
    refunded = projections.filter(truth__in=UGC_LIFECYCLE_REFUND_TRUTHS).exists() if projections.exists() else (
        IgDeal.objects.filter(order_id=order.pk, payment_truth__in=UGC_LIFECYCLE_REFUND_TRUTHS).exists())
    return refunded or IgPostSaleCase.objects.filter(order_id=order.pk, case_type="return", status="completed").exists()


def capture_review_reward_context(client_id, *, boundary, captured_at):
    """Capture one lifetime reward and a finite policy; failures reveal no capability."""
    value = {"policy": deepcopy(POLICY), "reward_present": None, "base_percent": None,
        "effective_percent": None, "available_percent": None, "state": "unknown",
        "benefit_scope": "client_lifetime", "source_order_id": None, "review_uplift_source_order_matches": None,
        "original_valid_until": None, "component_status": "unknown", "reason": "ugc_reward_context_unavailable"}
    value.update(business_consent_state="unknown", marketing_send_basis="standard_window_only",
                 native_future_marketing_grant="unverified")
    refs = [{"kind": "ugc_review_policy", "id": 1}]
    scope = {key: boundary.get(key) for key in SCOPE_KEYS}
    try:
        from management.ig_bot_models import IgClient
        from management.services.ig_review_reward import _identity_reward, effective_reward_percent
        from management.services.ig_ugc_rewards import _ugc_promo_has_consumed_usage, ugc_service_case_reason
        from orders.models import PaymentAttempt
        from storefront.models import PromoCodeGuestUsage

        if (type(client_id) is not int or client_id != boundary["client_id"]
                or timezone.is_naive(captured_at) or boundary.get("erasure_epoch")):
            raise ValueError("capture_scope_invalid")
        client = IgClient.objects.filter(pk=client_id).first()
        if client is None or client.privacy_erasure_started_at or client.hidden_at or client.is_blocked:
            raise ValueError("client_unavailable")
        if type(boundary.get("order_id")) is int and boundary["order_id"] > 0:
            from management.services.ig_marketing_consent import business_consent_projections

            consent = business_consent_projections(client, [boundary["order_id"]], now=captured_at)
            consent_state = consent.get(boundary["order_id"], {}).get("permission", {}).get("status")
            if consent_state in {"business_accepted", "unconfirmed", "blocked", "declined", "revoked", "expired"}:
                value["business_consent_state"] = consent_state
        reward = _identity_reward(client)
        if reward is None:
            value.update(reward_present=False, reason="ugc_reward_absent")
        else:
            promo = reward.promo_code
            percent = effective_reward_percent(reward)
            if (reward.discount_percent not in {5, 10} or percent not in {5, 10, 15}
                    or promo.discount_value != percent or promo.discount_type != "percentage"
                    or not promo.guest_redeemable or promo.max_uses != 1 or promo.one_time_per_user
                    or promo.group_id is not None or promo.promo_type != "regular"
                    or reward.reward_path not in {"delivered_order", "external_ugc"}
                    or (reward.reward_path == "external_ugc" and reward.assessment_id is None)
                    or (reward.reward_path == "delivered_order" and (reward.order_id is None or reward.assignment_id is None))
                    or promo.valid_until is None or promo.valid_until != reward.issued_at + timedelta(days=90)
                    or reward.issued_at > captured_at):
                raise ValueError("reward_policy_invalid")
            used = _ugc_promo_has_consumed_usage(promo)
            reserved = PromoCodeGuestUsage.objects.filter(promo_code_id=promo.pk, state="reserved").exists() or (
                PaymentAttempt.objects.filter(promo_code_id=promo.pk,
                    event_state__promo_reservation__state="reserved").exists())
            held = reward.lifecycle_state != "active" or ugc_service_case_reason(client) or _source_order_held(reward)
            if used:
                state = "used"
            elif reserved or promo.current_uses:
                state = "reserved"
            elif captured_at >= promo.valid_until:
                state = "expired"
            elif held or not promo.is_active or promo.valid_from and captured_at < promo.valid_from:
                state = "held"
            else:
                state = "active"
            order_matches = (boundary.get("order_id") == reward.order_id
                if boundary.get("order_id") is not None and reward.order_id is not None else None)
            value.update(reward_present=True, source_order_id=reward.order_id,
                review_uplift_source_order_matches=order_matches,
                base_percent=int(reward.discount_percent), effective_percent=percent,
                available_percent=percent if state == "active" else None, state=state,
                original_valid_until=promo.valid_until.isoformat(),
                component_status="validated" if percent == 15 else "absent", reason="")
            refs.append({"kind": "ugc_reward", "id": reward.pk})
    except Exception:
        # Never expose an unvalidated percentage, a code, proof body or error text.
        pass
    return {"value": value, "scope": scope, "status": "confirmed", "authority": "derived",
        "source_refs": refs, "observed_at": captured_at.isoformat(),
        "source_watermark": deepcopy(boundary.get("source_watermark") or boundary.get("watermark") or {}),
        "validity": "captured_policy_context", "mandatory": False}
