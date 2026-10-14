"""Fresh manager approval joins immutable original and successor checkouts."""
import uuid
from django.conf import settings
from django.db import models
from .ig_checkout_cart_amendment_models import _ImmutableAmendments


class IgCheckoutCartAmendmentResolution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amendment = models.OneToOneField("management.IgCheckoutCartAmendment", on_delete=models.PROTECT,
        related_name="resolution", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="checkout_amendment_resolutions", db_constraint=False)
    origin_proposal = models.ForeignKey("management.IgCheckoutProposal", on_delete=models.PROTECT,
        related_name="amendment_resolutions", db_constraint=False)
    successor_proposal = models.OneToOneField("management.IgCheckoutProposal", on_delete=models.PROTECT,
        related_name="amendment_origin_resolution", db_constraint=False)
    request = models.ForeignKey("management.IgAssistedCommerceRequest", on_delete=models.PROTECT,
        related_name="amendment_resolutions", db_constraint=False)
    decision = models.ForeignKey("management.IgAssistedCommerceDecision", on_delete=models.PROTECT,
        related_name="amendment_resolutions", db_constraint=False)
    execution = models.OneToOneField("management.IgAssistedCommerceExecution", on_delete=models.PROTECT,
        related_name="amendment_resolution", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="ig_checkout_amendment_resolutions", db_constraint=False)
    root_token = models.ForeignKey("management.IgCheckoutAccessToken", on_delete=models.PROTECT,
        related_name="amendment_root_resolutions", db_constraint=False)
    child_token = models.ForeignKey("management.IgCheckoutAccessToken", on_delete=models.PROTECT,
        related_name="amendment_child_resolutions", db_constraint=False)
    body = models.JSONField(default=dict)
    body_hmac = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = _ImmutableAmendments.as_manager()

    class Meta:
        app_label = "management"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("checkout amendment resolutions are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("checkout amendment resolutions are append-only")
