"""Durable clock for order fields that affect Instagram CRM analysis."""

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from management.ig_bot_models import (
    IgConversationAnalysisSnapshot,
    IgDeal,
    IgPaymentProjection,
    IgPostSaleCase,
    IgUgcReward,
    IgUgcRewardLifecycleJob,
)
from orders.models import Order


logger = logging.getLogger(__name__)


ORDER_TRUTH_FIELDS = frozenset({
    "status",
    "payment_status",
    "tracking_number",
    "shipment_status",
    "shipment_status_updated",
    "tracking_status_code",
    "tracking_provider_event_at",
    "tracking_terminal_at",
})
DEAL_ORDER_TRUTH_FIELDS = frozenset({"order", "order_id", "shipped_notified_at"})


def _db_alias_for(instance=None, using=None):
    return (
        using
        or getattr(getattr(instance, "_state", None), "db", None)
        or "default"
    )


def _reconcile_ugc_reward_event(job_id, *, using=None):
    db_alias = using or "default"
    try:
        from management.services.ig_ugc_rewards import (
            process_linked_ugc_reward_lifecycle_job,
        )

        result = process_linked_ugc_reward_lifecycle_job(
            job_id,
            using=db_alias,
        )
        if result.get("state") == "failed":
            logger.error(
                "Deferred linked UGC lifecycle job=%s after %s",
                job_id,
                result.get("last_error_kind") or "unknown_error",
            )
        return result
    except Exception:
        logger.exception(
            "Could not process linked UGC lifecycle job=%s",
            job_id,
        )


def _schedule_ugc_reward_event(
    *,
    order_id=None,
    client_id=None,
    source="",
    using=None,
):
    if order_id is None and client_id is None:
        return None
    db_alias = _db_alias_for(using=using)
    with transaction.atomic(using=db_alias):
        # Signals may already hold an Order or payment lock. They only persist
        # work here; the worker owns Client -> Order -> Promo -> Reward locks.
        # Concurrent empty-queue inserts can converge through that worker and
        # must never invert its locks to deduplicate an internal job.
        rewards = IgUgcReward.objects.using(db_alias).filter(
            reward_path="delivered_order",
        )
        if order_id is not None:
            rewards = rewards.filter(order_id=order_id)
        if client_id is not None:
            rewards = rewards.filter(client_id=client_id)
        reward_ids = list(
            rewards.order_by("pk").values_list("pk", flat=True)
        )
        if not reward_ids:
            return None
        existing = (
            IgUgcRewardLifecycleJob.objects.using(db_alias)
            .select_for_update()
            .filter(order_id=order_id, client_id=client_id)
            .exclude(source__startswith="review_uplift:")
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing.pk
        job = IgUgcRewardLifecycleJob.objects.using(db_alias).create(
            order_id=order_id,
            client_id=client_id,
            source=str(source or "")[:32],
        )
        transaction.on_commit(
            lambda job_id=job.pk, alias=db_alias: _reconcile_ugc_reward_event(
                job_id,
                using=alias,
            ),
            using=db_alias,
        )
        return job.pk


def order_truth_changed(previous, current, *, update_fields=None) -> bool:
    fields = ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = fields.intersection(update_fields)
    return bool(fields) and any(
        previous.get(field) != getattr(current, field, None)
        for field in fields
    )


@receiver(pre_save, sender=Order, dispatch_uid="ig_capture_order_truth_change")
def capture_order_truth_change(sender, instance, update_fields=None, **kwargs):
    instance._ig_order_truth_changed = False
    if not instance.pk:
        return
    fields = ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = fields.intersection(update_fields)
    if not fields:
        return
    db_alias = _db_alias_for(instance, kwargs.get("using"))
    previous = (
        sender.objects.using(db_alias)
        .filter(pk=instance.pk)
        .values(*fields)
        .first()
    )
    if previous is not None:
        instance._ig_order_truth_changed = order_truth_changed(
            previous,
            instance,
            update_fields=fields,
        )


@receiver(post_save, sender=Order, dispatch_uid="ig_publish_order_truth_change")
def publish_order_truth_change(sender, instance, **kwargs):
    if not getattr(instance, "_ig_order_truth_changed", False):
        return
    from management.models import IgDeal

    db_alias = _db_alias_for(instance, kwargs.get("using"))
    IgDeal.objects.using(db_alias).filter(order_id=instance.pk).update(
        order_truth_updated_at=timezone.now()
    )
    _schedule_ugc_reward_event(
        order_id=instance.pk,
        source="order_truth",
        using=_db_alias_for(instance, kwargs.get("using")),
    )

    # Do not add an after-commit callback for ordinary storefront orders.
    # Apart from avoiding needless work, this keeps the payment callback
    # boundary single and makes the Instagram truth path explicitly owned by
    # an existing deal/attribution/episode link.
    from management.ig_bot_models import (
        IgCommercialEpisode,
        IgOrderAttribution,
        IgOrderLinkEvent,
    )

    linked = (
        IgDeal.objects.using(db_alias).filter(order_id=instance.pk).exists()
        or IgOrderAttribution.objects.using(db_alias).filter(order_id=instance.pk).exists()
        or IgCommercialEpisode.objects.using(db_alias).filter(intended_order_id=instance.pk).exists()
        or IgOrderLinkEvent.objects.using(db_alias).filter(order_id=instance.pk).exists()
    )
    if not linked:
        return

    order_id = instance.pk
    transaction.on_commit(
        lambda alias=db_alias: _publish_instagram_order_truth(
            order_id,
            using=alias,
        ),
        using=db_alias,
    )


@receiver(
    post_save,
    sender=IgPaymentProjection,
    dispatch_uid="ig_reconcile_ugc_reward_payment_truth",
)
def publish_ugc_reward_payment_truth(sender, instance, using=None, **kwargs):
    db_alias = _db_alias_for(instance, using)
    order_id = (
        sender.objects.using(db_alias)
        .filter(pk=instance.pk)
        .values_list("deal__order_id", flat=True)
        .first()
    )
    _schedule_ugc_reward_event(
        order_id=order_id,
        source="payment_projection",
        using=db_alias,
    )


@receiver(
    post_save,
    sender=IgPostSaleCase,
    dispatch_uid="ig_reconcile_ugc_reward_post_sale",
)
def publish_ugc_reward_post_sale(sender, instance, using=None, **kwargs):
    _schedule_ugc_reward_event(
        client_id=instance.client_id,
        source="post_sale_case",
        using=_db_alias_for(instance, using),
    )


@receiver(
    post_save,
    sender=IgConversationAnalysisSnapshot,
    dispatch_uid="ig_reconcile_ugc_reward_analysis",
)
def publish_ugc_reward_analysis(sender, instance, using=None, **kwargs):
    if instance.interaction_type == sender.InteractionType.MANAGER_OBSERVATION:
        return
    _schedule_ugc_reward_event(
        client_id=instance.client_id,
        source="analysis_snapshot",
        using=_db_alias_for(instance, using),
    )


def _publish_instagram_order_truth(order_id, *, using=None):
    from management.models import IgClient
    from management.services.bot_conversation_analysis import schedule_client_truth_analysis
    from management.services.ig_commercial_episodes import sync_episode_fulfillment

    db_alias = using or "default"
    if db_alias != "default":
        # The legacy episode/analysis services still hard-code their database
        # managers. Do not let a non-default signal read a matching primary
        # key from default until those services accept an alias end to end.
        logger.warning(
            "Deferred Instagram order truth order=%s on unsupported database=%s",
            order_id,
            db_alias,
        )
        return
    sync_episode_fulfillment(order_id, source="order_signal")
    client_ids = set(
        IgDeal.objects.using(db_alias)
        .filter(order_id=order_id)
        .values_list("client_id", flat=True)
    )
    from management.ig_bot_models import IgCommercialEpisode, IgOrderAttribution

    client_ids.update(
        IgOrderAttribution.objects.using(db_alias)
        .filter(order_id=order_id)
        .values_list("client_id", flat=True)
    )
    client_ids.update(
        IgCommercialEpisode.objects.using(db_alias)
        .filter(intended_order_id=order_id)
        .values_list("client_id", flat=True)
    )
    for client in IgClient.objects.using(db_alias).filter(pk__in=client_ids).order_by("pk"):
        schedule_client_truth_analysis(client, trigger="order_truth")


@receiver(pre_delete, sender=Order, dispatch_uid="ig_publish_order_truth_unlink")
def publish_order_truth_unlink(sender, instance, **kwargs):
    from management.models import IgDeal
    from management.ig_bot_models import (
        IgCommercialEpisode,
        IgOrderAttribution,
        IgOrderLinkEvent,
    )

    db_alias = _db_alias_for(instance, kwargs.get("using"))
    if (
        IgDeal.objects.using(db_alias).filter(order_id=instance.pk).exists()
        or IgOrderAttribution.objects.using(db_alias).filter(order_id=instance.pk).exists()
        or IgCommercialEpisode.objects.using(db_alias).filter(intended_order_id=instance.pk).exists()
        or IgOrderLinkEvent.objects.using(db_alias).filter(order_id=instance.pk).exists()
    ):
        raise ValueError(
            "Instagram-замовлення не можна видалити, доки воно пов'язане з клієнтом або комерційним епізодом"
        )

    IgDeal.objects.using(db_alias).filter(order_id=instance.pk).update(
        order_truth_updated_at=timezone.now()
    )


@receiver(pre_save, sender=IgDeal, dispatch_uid="ig_capture_deal_order_truth_change")
def capture_deal_order_truth_change(sender, instance, update_fields=None, **kwargs):
    """Remember deal-side link/shipment edits, including narrow update_fields saves."""
    instance._ig_deal_order_truth_changed = False
    fields = DEAL_ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = {
            "order_id" if field == "order" else field
            for field in update_fields
        }.intersection(fields)
    if not fields or not instance.pk:
        instance._ig_deal_order_truth_changed = bool(
            fields and (instance.order_id or instance.shipped_notified_at)
        )
        return
    db_alias = _db_alias_for(instance, kwargs.get("using"))
    previous = (
        sender.objects.using(db_alias)
        .filter(pk=instance.pk)
        .values("order_id", "shipped_notified_at")
        .first()
    )
    if previous is None:
        return
    instance._ig_deal_order_truth_changed = any(
        previous.get(field) != getattr(instance, field, None)
        for field in fields
    )


@receiver(post_save, sender=IgDeal, dispatch_uid="ig_publish_deal_order_truth_change")
def publish_deal_order_truth_change(sender, instance, **kwargs):
    if not getattr(instance, "_ig_deal_order_truth_changed", False):
        return
    changed_at = timezone.now()
    db_alias = _db_alias_for(instance, kwargs.get("using"))
    sender.objects.using(db_alias).filter(pk=instance.pk).update(
        order_truth_updated_at=changed_at
    )
    instance.order_truth_updated_at = changed_at
