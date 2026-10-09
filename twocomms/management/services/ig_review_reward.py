"""One evidenced +5 component on the existing, unused UGC 10% capability."""
from __future__ import annotations

import hashlib
import hmac
import json

from django.db import transaction
from django.utils import timezone

PROOF_SCHEMA = "ugc-star-review-uplift.v1"
REVIEW_JOB_PREFIX = "review_uplift:"
REVIEW_JOB_RETRY_REASONS = frozenset({
    "proof_invalid", "identity_unverified", "promo_reserved",
    "delivery_reconciliation_required", "service_case_open",
})


class ReviewRewardConflict(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _component(reward):
    from reviews.models import ReviewRewardUplift

    return ReviewRewardUplift.objects.using(reward._state.db or "default").filter(reward_id=reward.pk).first()


def validate_review_uplift(component, reward):
    from management.services.ig_ugc_rewards import _identity_hmac_keyring
    from reviews.services.purchase_invites import validate_bound_review

    if reward.discount_percent != 10 or component.added_percent != 5:
        raise ReviewRewardConflict("uplift_base_invalid")
    material = component.proof_snapshot
    if not isinstance(material, dict) or material.get("schema") != PROOF_SCHEMA:
        raise ReviewRewardConflict("uplift_proof_invalid")
    promo = reward.promo_code
    expected = {
        "reward_id": reward.pk, "review_id": component.review_id,
        "promo_code_id": reward.promo_code_id, "base_percent": 10, "added_percent": 5,
        "issued_at": reward.issued_at.isoformat(),
        "valid_until": promo.valid_until.isoformat() if promo.valid_until else None,
        "lifetime_slot_key": reward.lifetime_slot_key,
        "created_at": component.created_at.isoformat(),
    }
    if any(material.get(key) != value for key, value in expected.items()):
        raise ReviewRewardConflict("uplift_identity_changed")
    encoded = _canonical(material).encode()
    if not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), component.proof_digest):
        raise ReviewRewardConflict("uplift_digest_invalid")
    key = dict(_identity_hmac_keyring()).get(component.signing_key_id)
    if key is None or not hmac.compare_digest(
        hmac.new(key, b"ugc-star-review-uplift:v1:" + encoded, hashlib.sha256).hexdigest(),
        component.proof_signature,
    ):
        raise ReviewRewardConflict("uplift_signature_invalid")
    source = validate_bound_review(component.review_id, client=reward.client_id)
    # Initial moderation audit stays signed, but a legitimate reapproval may
    # update its actor/time. Current approval is independently revalidated.
    initial_source = material.get("source")
    audit_fields = {"moderated_at", "moderated_by_id"}
    if (not isinstance(initial_source, dict)
            or {key: value for key, value in source.items() if key not in audit_fields}
            != {key: value for key, value in initial_source.items() if key not in audit_fields}):
        raise ReviewRewardConflict("uplift_source_changed")
    if reward.order_id is not None and source["order_id"] != reward.order_id:
        raise ReviewRewardConflict("uplift_order_changed")
    return source


def effective_reward_percent(reward):
    """Historical base stays 5/10; a signed, currently valid component adds 5."""
    component = _component(reward)
    if component is None:
        return int(reward.discount_percent)
    validate_review_uplift(component, reward)
    return 15


def _identity_reward(client):
    from management.ig_bot_models import IgUgcReward, IgUgcRewardLifetime
    from management.services.ig_ugc_rewards import (
        _identity_digest_candidates, ugc_identity_already_rewarded,
        ugc_identity_lifetime_conflicted,
    )

    if ugc_identity_lifetime_conflicted(client):
        raise ReviewRewardConflict("identity_unverified")
    slots = list(IgUgcRewardLifetime.objects.filter(
        identity_digest__in=_identity_digest_candidates(client),
    )[:2])
    if not slots or not slots[0].reward_id:
        if ugc_identity_already_rewarded(client):
            raise ReviewRewardConflict("identity_unverified")
        return None
    slot = slots[0]
    reward = IgUgcReward.objects.select_related("promo_code").get(pk=slot.reward_id)
    if (slot.consumed_at is None or slot.client_id != client.pk
            or reward.client_id != client.pk or reward.lifetime_slot_key != slot.identity_digest):
        raise ReviewRewardConflict("identity_unverified")
    return reward


def _promo_upgrade_reason(promo, *, now):
    from management.services.ig_ugc_rewards import _ugc_promo_has_consumed_usage
    from orders.models import PaymentAttempt
    from storefront.models import PromoCodeGuestUsage

    if _ugc_promo_has_consumed_usage(promo):
        return "promo_consumed"
    if (promo.current_uses != 0 or PromoCodeGuestUsage.objects.filter(
        promo_code_id=promo.pk, state__in=("reserved", "consumed"),
    ).exists() or PaymentAttempt.objects.filter(
        promo_code_id=promo.pk,
        event_state__promo_reservation__state__in=("reserved", "consumed"),
    ).exists()):
        return "promo_reserved"
    if promo.valid_until is None or now >= promo.valid_until:
        return "promo_expired"
    if not promo.is_active or (promo.valid_from and now < promo.valid_from):
        return "promo_inactive"
    if (promo.max_uses != 1 or promo.one_time_per_user or promo.group_id is not None
            or not promo.guest_redeemable or promo.promo_type != "regular"
            or promo.discount_type != "percentage" or promo.discount_value != 10):
        return "promo_policy_invalid"
    return ""


def _delivery_upgrade_reason(reward):
    from management.ig_bot_models import IgUgcRewardDelivery

    for row in IgUgcRewardDelivery.objects.filter(reward_id=reward.pk):
        if row.state in {"processing", "ambiguous"}:
            return "delivery_reconciliation_required"
        if row.provider_message_ids and row.state != "sent":
            return "delivery_reconciliation_required"
        if row.state != "sent" and (row.attempts or row.lease_token or row.lease_expires_at):
            return "delivery_reconciliation_required"
    return ""


def review_uplift_eligibility(client, order):
    """Read-only offer classification; never creates an invite, slot, or coupon."""
    result = {"eligible": False, "kind": "none", "reward_id": None, "reason": "eligibility_unknown"}
    try:
        from management.models import IgClient
        from management.services.ig_ugc_rewards import ugc_service_case_reason, ugc_source_order_disqualified
        from reviews.services.purchase_invites import eligible_purchase_review_items

        client = IgClient.objects.get(pk=getattr(client, "pk", client))
        if (client.privacy_erasure_started_at is not None or client.hidden_at or client.is_blocked
                or ugc_service_case_reason(client) or ugc_source_order_disqualified(order)):
            return {**result, "reason": "purchase_not_eligible"}
        if not eligible_purchase_review_items(client=client, order=order):
            return {**result, "reason": "catalog_purchase_required"}
        reward = _identity_reward(client)
        if reward is None:
            return {**result, "eligible": True, "kind": "potential_base_10", "reason": ""}
        result["reward_id"] = reward.pk
        if reward.discount_percent != 10:
            return {**result, "reason": "base_not_10"}
        if _component(reward) is not None:
            return {**result, "reason": "already_uplifted"}
        if reward.lifecycle_state != "active":
            return {**result, "reason": "reward_not_active"}
        if reward.order_id is not None and reward.order_id != getattr(order, "pk", order):
            return {**result, "reason": "reward_order_mismatch"}
        reason = _promo_upgrade_reason(reward.promo_code, now=timezone.now()) or _delivery_upgrade_reason(reward)
        return {**result, "eligible": not bool(reason), "kind": "none" if reason else "unused_base_10", "reason": reason}
    except Exception:
        return result


def _result(reason, *, reward=None, applied=False, created=False, delivery=None):
    return {"applied": applied, "created": created, "reason": reason,
            "reward_id": reward.pk if reward else None,
            "delivery_id": delivery.pk if delivery else None,
            "effective_percent": 15 if applied else None}


@transaction.atomic
def apply_review_uplift(review_id, *, now=None):
    """Upgrade the same unused code under client→order→promo→reward→review locks."""
    from management.ig_bot_models import IgClient, IgUgcReward, IgUgcRewardDelivery
    from management.services.ig_ugc_rewards import (
        _identity_hmac_keyring, queue_external_ugc_reward_delivery,
        ugc_service_case_reason, ugc_source_order_disqualified,
    )
    from orders.models import Order
    from reviews.models import Review, ReviewRewardUplift
    from reviews.services.purchase_invites import validate_bound_review
    from storefront.models import PromoCode

    now = now or timezone.now()
    try:
        initial_source = validate_bound_review(review_id)
    except Exception:
        # Moderation reversal retains the component but holds an unused 15%
        # capability through the same lifecycle owner; no new coupon is minted.
        from management.services.ig_ugc_rewards import _reconcile_locked_ugc_reward_lifecycle

        existing_component = ReviewRewardUplift.objects.filter(review_id=review_id).first()
        if existing_component is not None:
            _reconcile_locked_ugc_reward_lifecycle(existing_component.reward_id, now=now)
        return _result("proof_invalid")
    client = IgClient.objects.select_for_update().get(pk=initial_source["client_id"])
    if client.privacy_erasure_started_at is not None or client.hidden_at or client.is_blocked:
        return _result("privacy_or_identity_unavailable")
    try:
        identity_reward = _identity_reward(client)
    except Exception:
        return _result("identity_unverified")
    if identity_reward is None:
        return _result("no_ugc_reward")
    order = Order.objects.select_for_update().get(pk=initial_source["order_id"])
    promo = PromoCode.objects.select_for_update().get(pk=identity_reward.promo_code_id)
    reward = IgUgcReward.objects.select_for_update().get(pk=identity_reward.pk)
    reward.promo_code = promo
    review = Review.objects.select_for_update().get(pk=review_id)
    try:
        source = validate_bound_review(review, client=client, order=order)
    except Exception:
        return _result("proof_invalid", reward=reward)
    if reward.order_id is not None and reward.order_id != order.pk:
        return _result("reward_order_mismatch", reward=reward)
    existing = _component(reward)
    if existing is not None:
        try:
            validate_review_uplift(existing, reward)
        except Exception:
            from management.services.ig_ugc_rewards import _reconcile_locked_ugc_reward_lifecycle

            _reconcile_locked_ugc_reward_lifecycle(reward.pk, now=now)
            return _result("proof_invalid", reward=reward)
        if existing.review_id != review.pk or promo.discount_value != 15:
            return _result("already_uplifted", reward=reward)
        from management.services.ig_ugc_rewards import _reconcile_locked_ugc_reward_lifecycle

        _reconcile_locked_ugc_reward_lifecycle(reward.pk, now=now)
        return _result("already_applied", reward=reward, applied=True,
                       delivery=queue_external_ugc_reward_delivery(reward))
    if ReviewRewardUplift.objects.filter(review_id=review.pk).exists():
        return _result("review_already_used", reward=reward)
    if reward.discount_percent != 10:
        return _result("base_not_10", reward=reward)
    if reward.lifecycle_state != "active" or ugc_service_case_reason(client) or ugc_source_order_disqualified(order):
        return _result("service_case_open", reward=reward)
    reason = _promo_upgrade_reason(promo, now=now) or _delivery_upgrade_reason(reward)
    if reason:
        return _result(reason, reward=reward)
    rows = list(IgUgcRewardDelivery.objects.select_for_update().filter(reward_id=reward.pk))
    material = {
        "schema": PROOF_SCHEMA, "reward_id": reward.pk, "review_id": review.pk,
        "promo_code_id": promo.pk, "base_percent": 10, "added_percent": 5,
        "issued_at": reward.issued_at.isoformat(), "valid_until": promo.valid_until.isoformat(),
        "lifetime_slot_key": reward.lifetime_slot_key, "created_at": now.isoformat(), "source": source,
    }
    encoded = _canonical(material).encode()
    key_id, key = _identity_hmac_keyring()[0]
    ReviewRewardUplift.objects.create(
        reward=reward, review=review, added_percent=5, proof_snapshot=material,
        proof_digest=hashlib.sha256(encoded).hexdigest(), signing_key_id=key_id,
        proof_signature=hmac.new(key, b"ugc-star-review-uplift:v1:" + encoded, hashlib.sha256).hexdigest(),
        created_at=now,
    )
    promo.discount_value = 15
    promo.save(update_fields=["discount_value", "updated_at"])
    for row in rows:
        if row.state != "sent" and not row.completed_at:
            row.state = "failed"
            row.completed_at = now
            row.last_error = "superseded_by_reward_generation"
            row.save(update_fields=["state", "completed_at", "last_error", "updated_at"])
    reward.__dict__.pop("_ugc_delivery_cache", None)
    delivery = queue_external_ugc_reward_delivery(reward)
    return _result("", reward=reward, applied=True, created=True, delivery=delivery)


def apply_pending_review_uplift(reward):
    """Approved review may precede UGC; retry once before first outbox snapshot."""
    if reward.discount_percent != 10:
        return None
    from reviews.services.purchase_invites import eligible_bound_reviews

    for review_id in eligible_bound_reviews(reward.client, reward.order).values_list("pk", flat=True)[:20]:
        result = apply_review_uplift(review_id)
        if result["applied"]:
            return result
    return None


def queue_review_uplift_job(review_id, *, using=None):
    """Persist the exact moderation event inside its caller's transaction.

    No client/promo lock is acquired while moderation owns Review. Each event
    gets its own durable job; serialized idempotent application handles races.
    """
    from management.ig_bot_models import IgUgcRewardLifecycleJob
    from reviews.models import Review

    db_alias = using or "default"
    review_id = int(getattr(review_id, "pk", review_id))
    if not 0 < review_id < 2 ** 63:
        raise ReviewRewardConflict("review_job_identity_invalid")
    source = Review.objects.using(db_alias).filter(
        pk=review_id, purchase_invitation__isnull=False,
    ).values("purchase_proof_signature", "purchase_invitation__client_id_snapshot",
             "purchase_invitation__order_id").get()
    if not source["purchase_proof_signature"]:
        raise ReviewRewardConflict("review_job_identity_invalid")
    return IgUgcRewardLifecycleJob.objects.using(db_alias).create(
        source=f"{REVIEW_JOB_PREFIX}{review_id:x}",
        client_id=source["purchase_invitation__client_id_snapshot"],
        order_id=source["purchase_invitation__order_id"],
    )


def process_review_uplift_job(job_id, *, now=None, using=None):
    """Retry one exact committed moderation event through the existing queue."""
    from management.ig_bot_models import IgClient, IgUgcReward, IgUgcRewardLifecycleJob
    from management.services.ig_ugc_rewards import _ugc_lifecycle_job_retry_at
    from orders.models import Order
    from reviews.models import Review, ReviewRewardUplift

    db_alias = using or "default"
    now = now or timezone.now()
    target = IgUgcRewardLifecycleJob.objects.using(db_alias).filter(pk=job_id).values(
        "client_id", "order_id", "source",
    ).first()
    if target is None:
        return {"state": "missing", "selected": 0}
    counts = {"selected": 0, "active": 0, "held": 0, "revoked": 0}
    with transaction.atomic(using=db_alias):
        # The client is the common fence with grant, checkout and privacy.
        IgClient.objects.using(db_alias).select_for_update().filter(pk=target["client_id"]).first()
        Order.objects.using(db_alias).select_for_update().filter(pk=target["order_id"]).first()
        if not IgUgcRewardLifecycleJob.objects.using(db_alias).filter(pk=job_id).exists():
            return {"state": "missing", "selected": 0}
        reason = ""
        try:
            with transaction.atomic(using=db_alias):
                source = target["source"]
                suffix = source[len(REVIEW_JOB_PREFIX):] if source.startswith(REVIEW_JOB_PREFIX) else ""
                review_id = int(suffix, 16)
                if not 0 < review_id < 2 ** 63 or source != f"{REVIEW_JOB_PREFIX}{review_id:x}":
                    raise ReviewRewardConflict("review_job_identity_invalid")
                review = Review.objects.using(db_alias).filter(pk=review_id).values(
                    "purchase_invitation__client_id_snapshot", "purchase_invitation__order_id",
                    "purchase_proof_signature", "status",
                ).first()
                # Privacy erasure may intentionally remove this source. The
                # event then has no monetary action and must never rebind.
                if review is not None:
                    if (not review["purchase_proof_signature"]
                            or review["purchase_invitation__client_id_snapshot"] != target["client_id"]
                            or review["purchase_invitation__order_id"] != target["order_id"]):
                        raise ReviewRewardConflict("review_job_identity_invalid")
                    if db_alias != "default":
                        raise ReviewRewardConflict("review_job_database_unsupported")
                    outcome = apply_review_uplift(review_id, now=now)
                    counts["selected"] = int(outcome.get("reward_id") is not None)
                    reward_id = outcome.get("reward_id") or ReviewRewardUplift.objects.using(db_alias).filter(
                        review_id=review_id,
                    ).values_list("reward_id", flat=True).first()
                    if reward_id:
                        state = IgUgcReward.objects.using(db_alias).filter(pk=reward_id).values_list(
                            "lifecycle_state", flat=True,
                        ).first()
                        counts["selected"] = 1
                        if state in counts:
                            counts[state] += 1
                    if (review["status"] == "approved"
                            and outcome.get("reason") in REVIEW_JOB_RETRY_REASONS):
                        reason = outcome["reason"]
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ReviewRewardConflict) else exc.__class__.__name__
        job = IgUgcRewardLifecycleJob.objects.using(db_alias).select_for_update().filter(pk=job_id).first()
        if job is None:
            return {"state": "missing", "selected": 0}
        if (job.client_id, job.order_id, job.source) != (
            target["client_id"], target["order_id"], target["source"],
        ):
            reason = "target_changed"
        if reason:
            job.attempts = min(65535, int(job.attempts or 0) + 1)
            job.due_at = _ugc_lifecycle_job_retry_at(now, job.attempts)
            job.last_error_kind = str(reason)[:64]
            job.save(using=db_alias, update_fields=["attempts", "due_at", "last_error_kind", "updated_at"])
            return {"state": "failed", **counts, "last_error_kind": job.last_error_kind}
        job.delete(using=db_alias)
        return {"state": "done", **counts}
