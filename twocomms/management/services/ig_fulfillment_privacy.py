"""Delete fulfilment evidence only behind the committed client privacy fence."""
from django.db import connections


CHECKOUT_PRIVACY_TOMBSTONE = "privacy_erased_financial_retention"


def retain_checkout_privacy_tombstone(client, *, using="default"):
    """Retain protected economic ownership, erase its operational identity.

    A proposal's client/deal/episode graph is protected financial history even
    before payment. It cannot be deleted or reparented to another customer.
    The original erasure owner calls this after bytes and private ledgers have
    gone, within its final atomic phase; ordinary clients still use deletion.
    """
    import uuid
    from django.db.models.deletion import CASCADE
    from django.utils import timezone
    from management.models import (IgClient, IgCheckoutProposal, IgDeal,
        IgCommercialEpisode, IgCheckoutAccessToken, IgCommerceSelectionSession,
        IgCommerceSelectionTransition, IgCommerceTurnDecision, IgCommerceManagerReview)

    connection = connections[using]
    if not connection.in_atomic_block:
        raise ValueError("Checkout retention requires the atomic privacy phase")
    owner = IgClient.objects.using(using).select_for_update().get(pk=client.pk)
    if owner.privacy_erasure_started_at is None:
        raise ValueError("Checkout retention requires the exact persisted owner fence")
    if not IgCheckoutProposal.objects.using(using).filter(client_id=owner.pk).exists():
        return False
    now = timezone.now()
    IgCheckoutAccessToken.objects.using(using).filter(proposal__client_id=owner.pk,
        revoked_at__isnull=True).update(revoked_at=now)

    # Delete the same operational CASCADE dependants that physical client
    # deletion would collect. Financial owner graphs remain in place. Clear
    # decisions before transitions so SET_NULL never mutates immutable proof.
    IgCommerceManagerReview.objects.using(using).filter(
        client_id=owner.pk, session__client_id=owner.pk).delete()
    session_table = connection.ops.quote_name(IgCommerceSelectionSession._meta.db_table)
    with connection.cursor() as cursor:
        for model in (IgCommerceTurnDecision, IgCommerceSelectionTransition):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} "
                f"WHERE session_id IN (SELECT id FROM {session_table} WHERE client_id=%s)", [owner.pk])
    # Sessions use DO_NOTHING for their client/episode references, so neither
    # physical client deletion nor the CASCADE pass removes their private cart.
    # Native receipts have already gone in the bytes-first fulfilment phase.
    IgCommerceSelectionSession.objects.using(using).filter(client_id=owner.pk).delete()
    financial = {IgCheckoutProposal, IgDeal, IgCommercialEpisode}
    for relation in IgClient._meta.related_objects:
        if getattr(relation.field.remote_field, "on_delete", None) is CASCADE and relation.related_model not in financial:
            relation.related_model._base_manager.using(using).filter(
                **{relation.field.attname: owner.pk}).delete()

    # These are mutable display/contact caches, not verified payment amounts,
    # provider receipts or the legally retained Order delivery record.
    IgDeal.objects.using(using).filter(client_id=owner.pk).update(
        np_full_name="", np_phone="", np_city="", np_office="",
        np_settlement_ref="", np_city_ref="", np_warehouse_ref="",
        delivery_source="", delivery_error="")
    retained = {"id", "created_at", "updated_at", "privacy_erasure_started_at", "reply_permission_epoch"}
    updates = {}
    for field in IgClient._meta.concrete_fields:
        if field.name in retained:
            continue
        if field.has_default():
            updates[field.attname] = field.get_default()
        elif field.null:
            updates[field.attname] = None
    updates.update(igsid=f"erased:{uuid.uuid4().hex}", hidden_at=now,
        hidden_reason=CHECKOUT_PRIVACY_TOMBSTONE, bot_paused=True,
        paused_reason="privacy_erased", is_blocked=True, updated_at=now)
    IgClient.objects.using(using).filter(pk=owner.pk,
        privacy_erasure_started_at__isnull=False).update(**updates)
    return True


def purge_fulfillment_data(client_ids, *, using="default"):
    from management.models import (IgClient, IgCheckoutCartAmendment,
        IgCheckoutCartAmendmentResolution, IgCommerceSelectionTransition)
    from management.ig_restock_subscription_models import (
        IgRestockAvailabilityEvent, IgRestockSubscription, IgRestockAnswer,
        IgRestockReceipt, IgRestockInvitation,
    )
    from management.ig_fulfillment_review_models import (
        IgFulfillmentReviewDecision, IgFulfillmentReviewRequest,
    )
    from management.ig_fulfillment_stock_sync_models import IgFulfillmentStockSync
    from management.ig_split_shipping_models import (IgSplitShippingOffer, IgSplitShippingReceipt,
        IgSplitShippingChoice, IgSplitOrderPlan, IgSplitParcel, IgSplitParcelDispatch, IgSplitParcelTracking)
    from management.ig_assisted_commerce_models import (IgAssistedCommerceImage,
        IgAssistedCommerceExecution, IgAssistedCommerceDecision, IgAssistedCommerceRequest,
        IgAssistedCommerceStockSync)
    from management.services.ig_assisted_commerce_media import delete_asset_bytes
    ids = sorted({int(value) for value in client_ids})
    if not ids:
        return
    connection = connections[using]
    if not connection.in_atomic_block or any(value <= 0 for value in ids):
        raise ValueError("Fulfilment erasure requires the atomic privacy phase")
    fenced = list(IgClient.objects.using(using).select_for_update().filter(
        pk__in=ids, privacy_erasure_started_at__isnull=False,
    ).order_by("pk").values_list("pk", flat=True))
    if fenced != ids:
        raise ValueError("Fulfilment erasure requires every exact persisted owner fence")
    # Bytes precede every assisted metadata deletion. A filesystem failure
    # aborts this atomic phase and keeps all owned pointers for an idempotent
    # retry, including pointers whose bytes were already removed this time.
    for image in IgAssistedCommerceImage.objects.using(using).select_for_update().filter(
            client_id__in=ids).order_by("client_id", "pk"):
        delete_asset_bytes(image)
    invitation_table = connection.ops.quote_name(IgRestockInvitation._meta.db_table)
    request_table = connection.ops.quote_name(IgFulfillmentReviewRequest._meta.db_table)
    answer_table = connection.ops.quote_name(IgRestockAnswer._meta.db_table)
    subscription_table = connection.ops.quote_name(IgRestockSubscription._meta.db_table)
    placeholders = ",".join(["%s"] * len(ids))
    split_offer_table = connection.ops.quote_name(IgSplitShippingOffer._meta.db_table)
    split_plan_table = connection.ops.quote_name(IgSplitOrderPlan._meta.db_table)
    split_parcel_table = connection.ops.quote_name(IgSplitParcel._meta.db_table)
    amendment_table = connection.ops.quote_name(IgCheckoutCartAmendment._meta.db_table)
    split_parcels = f"SELECT p.id FROM {split_parcel_table} p JOIN {split_plan_table} sp ON sp.id=p.plan_id WHERE sp.client_id IN ({placeholders})"
    # Normal ORM collectors must never bypass immutable evidence guards. Raw
    # deletions here still execute the database's owner/privacy triggers.
    steps = (
        # Successors protect both the holder receipt and assisted approval.
        # Its native selection transition in turn protects that receipt.
        (IgCheckoutCartAmendmentResolution, f"client_id IN ({placeholders})"),
        (IgCommerceSelectionTransition, f"checkout_amendment_id IN (SELECT id FROM {amendment_table} WHERE client_id IN ({placeholders}))"),
        (IgCheckoutCartAmendment, f"client_id IN ({placeholders})"),
        (IgAssistedCommerceImage, f"client_id IN ({placeholders})"),
        (IgAssistedCommerceExecution, f"client_id IN ({placeholders})"),
        (IgAssistedCommerceStockSync, f"client_id IN ({placeholders})"),
        (IgAssistedCommerceDecision, f"client_id IN ({placeholders})"),
        (IgAssistedCommerceRequest, f"client_id IN ({placeholders})"),
        (IgSplitParcelTracking, f"parcel_id IN ({split_parcels})"),
        (IgSplitParcelDispatch, f"parcel_id IN ({split_parcels})"),
        (IgSplitParcel, f"plan_id IN (SELECT id FROM {split_plan_table} WHERE client_id IN ({placeholders}))"),
        (IgSplitOrderPlan, f"client_id IN ({placeholders})"),
        (IgSplitShippingChoice, f"client_id IN ({placeholders})"),
        (IgSplitShippingReceipt, f"offer_id IN (SELECT id FROM {split_offer_table} WHERE client_id IN ({placeholders}))"),
        (IgSplitShippingOffer, f"client_id IN ({placeholders})"),
        (IgRestockAvailabilityEvent, f"subscription_id IN (SELECT id FROM {subscription_table} WHERE client_id IN ({placeholders}))"),
        (IgRestockSubscription, f"client_id IN ({placeholders})"),
        (IgRestockAnswer, f"invitation_id IN (SELECT id FROM {invitation_table} WHERE client_id IN ({placeholders}))"),
        (IgRestockReceipt, f"invitation_id IN (SELECT id FROM {invitation_table} WHERE client_id IN ({placeholders}))"),
        (IgRestockInvitation, f"client_id IN ({placeholders})"),
        (IgFulfillmentStockSync, f"client_id IN ({placeholders})"),
        (IgFulfillmentReviewDecision, f"request_id IN (SELECT id FROM {request_table} WHERE client_id IN ({placeholders}))"),
        (IgFulfillmentReviewRequest, f"client_id IN ({placeholders})"),
    )
    with connection.cursor() as cursor:
        for model, condition in steps:
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} WHERE {condition}", ids)
