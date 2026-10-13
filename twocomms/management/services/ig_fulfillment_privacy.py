"""Delete fulfilment evidence only behind the committed client privacy fence."""
from django.db import connections


def purge_fulfillment_data(client_ids, *, using="default"):
    from management.models import IgClient
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
    split_parcels = f"SELECT p.id FROM {split_parcel_table} p JOIN {split_plan_table} sp ON sp.id=p.plan_id WHERE sp.client_id IN ({placeholders})"
    # Normal ORM collectors must never bypass immutable evidence guards. Raw
    # deletions here still execute the database's owner/privacy triggers.
    steps = (
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
