"""Source-bound split shipping evidence and independently refreshed parcel tracking."""
import uuid

from django.conf import settings
from django.db import models

from .ig_fulfillment_review_models import _ReviewEvidence


class IgSplitShippingOffer(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.ForeignKey("management.IgFulfillmentReviewDecision", on_delete=models.PROTECT,
        related_name="split_shipping_offers", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="split_shipping_offers", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="split_shipping_offers", db_constraint=False)
    operation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField()
    task = models.OneToOneField("management.IgFollowUpTask", on_delete=models.PROTECT,
        null=True, blank=True, related_name="split_shipping_offer", db_constraint=False)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["decision", "version"], name="ig_split_offer_version")]


class IgSplitShippingReceipt(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offer = models.ForeignKey(IgSplitShippingOffer, on_delete=models.PROTECT,
        related_name="receipts", db_constraint=False)
    task = models.OneToOneField("management.IgFollowUpTask", on_delete=models.PROTECT,
        related_name="split_shipping_receipt", db_constraint=False)
    native_mid = models.CharField(max_length=256)


class IgSplitShippingChoice(_ReviewEvidence):
    class Option(models.TextChoices):
        TOGETHER = "together", "Together"
        SEPARATE = "separate", "Separate parcels"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offer = models.OneToOneField(IgSplitShippingOffer, on_delete=models.PROTECT,
        related_name="choice", db_constraint=False)
    receipt = models.ForeignKey(IgSplitShippingReceipt, on_delete=models.PROTECT,
        related_name="choices", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="split_shipping_choices", db_constraint=False)
    source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT,
        related_name="split_shipping_choices", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT,
        null=True, blank=True, related_name="split_shipping_choices", db_constraint=False)
    option = models.CharField(max_length=16, choices=Option.choices)


class IgSplitOrderPlan(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    choice = models.OneToOneField(IgSplitShippingChoice, on_delete=models.PROTECT,
        related_name="plan", db_constraint=False)
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT,
        related_name="instagram_split_plan", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="split_order_plans", db_constraint=False)


class IgSplitParcel(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(IgSplitOrderPlan, on_delete=models.PROTECT,
        related_name="parcels", db_constraint=False)
    key = models.CharField(max_length=24)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "key"], name="ig_split_parcel_key")]


class IgSplitParcelDispatch(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parcel = models.OneToOneField(IgSplitParcel, on_delete=models.PROTECT,
        related_name="dispatch", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="split_parcel_dispatches", db_constraint=False)
    operation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    shipment = models.OneToOneField("management.IgOrderShipment", on_delete=models.PROTECT,
        related_name="split_parcel_dispatch", db_constraint=False)
    tracking_number = models.CharField(max_length=64, unique=True)
    customer_delivery_amount = models.DecimalField(max_digits=10, decimal_places=2)
    cod_amount = models.DecimalField(max_digits=10, decimal_places=2)


class IgSplitParcelTracking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parcel = models.OneToOneField(IgSplitParcel, on_delete=models.PROTECT,
        related_name="tracking", db_constraint=False)
    code = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=500, blank=True)
    provider_event_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
