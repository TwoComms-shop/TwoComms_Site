"""Immutable evidence for a manager's exact fulfillment stock reconciliation."""
import uuid

from django.conf import settings
from django.db import models

from .ig_fulfillment_review_models import _ReviewEvidence


class IgFulfillmentStockSync(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(
        "management.IgFulfillmentReviewDecision", on_delete=models.PROTECT,
        related_name="stock_sync", db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient", on_delete=models.PROTECT,
        related_name="fulfillment_stock_syncs", db_constraint=False,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="fulfillment_stock_syncs", db_constraint=False,
    )
    operation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
