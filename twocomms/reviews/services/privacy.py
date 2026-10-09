"""Review payload erasure inside the existing audited DIRECT_BOT privacy path."""
from django.db import connections, transaction
from django.db.models import Q


def purge_purchase_review_data(client_ids, *, using="default"):
    """Purge exact owners only after their committed privacy fences.

    `_delete_direct_bot_records` calls this in its second, client-locked
    transaction before deleting private UGC rewards. Normal proof/queryset
    immutability stays intact; this is the narrow audited SQL exception.
    The owning privacy path preserves consumed lifetime tombstones separately.
    """
    from management.ig_bot_models import IgClient, IgUgcReward
    from reviews.models import Review, ReviewImage, ReviewPurchaseInvitation, ReviewRewardUplift, ReviewVote
    from storefront.services.catalog_helpers import bump_public_product_order_version

    ids = sorted({int(value) for value in client_ids})
    if not ids:
        return {"reviews": 0, "invitations": 0, "components": 0}
    connection = connections[using]
    if not connection.in_atomic_block or any(value <= 0 for value in ids):
        raise ValueError("Review privacy purge requires the audited atomic erasure phase")
    fenced = list(IgClient.objects.using(using).select_for_update().filter(
        pk__in=ids, privacy_erasure_started_at__isnull=False,
    ).order_by("pk").values_list("pk", flat=True))
    if fenced != ids:
        raise ValueError("Review privacy purge requires every exact owner privacy fence")
    invitations = list(ReviewPurchaseInvitation.objects.using(using).filter(
        client_id_snapshot__in=ids,
    ).values_list("pk", flat=True))
    review_ids = list(Review.objects.using(using).filter(
        purchase_invitation_id__in=invitations,
    ).values_list("pk", flat=True))
    reward_ids = list(IgUgcReward.objects.using(using).filter(client_id__in=ids).values_list("pk", flat=True))
    components = list(ReviewRewardUplift.objects.using(using).filter(
        Q(review_id__in=review_ids) | Q(reward_id__in=reward_ids),
    ).values_list("pk", flat=True))
    # Delete the owned public photo blobs while the persisted privacy fence
    # already prevents new ownership. A storage failure aborts DB erasure and
    # keeps the durable request retryable rather than leaving orphaned images.
    for image in ReviewImage.objects.using(using).filter(review_id__in=review_ids):
        if image.image.name:
            image.image.storage.delete(image.image.name)

    def erase(model, row_ids):
        if not row_ids:
            return
        field = model._meta.pk
        prepared = [field.get_db_prep_value(value, connection, prepared=False) for value in row_ids]
        placeholders = ", ".join(["%s"] * len(prepared))
        with connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} "
                f"WHERE {connection.ops.quote_name(field.column)} IN ({placeholders})", prepared,
            )

    erase(ReviewRewardUplift, components)
    ReviewVote.objects.using(using).filter(review_id__in=review_ids).delete()
    ReviewImage.objects.using(using).filter(review_id__in=review_ids).delete()
    erase(Review, review_ids)
    erase(ReviewPurchaseInvitation, invitations)
    if review_ids:
        transaction.on_commit(bump_public_product_order_version, using=using)
    return {"reviews": len(review_ids), "invitations": len(invitations), "components": len(components)}
