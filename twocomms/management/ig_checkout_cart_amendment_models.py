"""Native checkout-link facts; the bearer holder is not an Instagram identity."""
import uuid

from django.db import models


class _ImmutableAmendments(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("checkout amendments are append-only")

    def delete(self):
        raise ValueError("checkout amendments are append-only")


class IgCheckoutCartAmendment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation_id = models.UUIDField(unique=True, editable=False)
    proposal = models.ForeignKey("management.IgCheckoutProposal", on_delete=models.PROTECT,
        related_name="cart_amendments", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="checkout_cart_amendments", db_constraint=False)
    session = models.ForeignKey("management.IgCommerceSelectionSession", on_delete=models.PROTECT,
        related_name="checkout_cart_amendments", db_constraint=False)
    token = models.ForeignKey("management.IgCheckoutAccessToken", on_delete=models.PROTECT,
        related_name="cart_amendments", db_constraint=False)
    from_revision = models.PositiveIntegerField()
    to_revision = models.PositiveIntegerField()
    input_digest = models.CharField(max_length=64)
    source_body = models.JSONField(default=dict)
    source_hmac = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    result_hmac = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = _ImmutableAmendments.as_manager()

    class Meta:
        app_label = "management"
        constraints = [models.UniqueConstraint(fields=["proposal", "to_revision"],
            name="ig_checkout_amend_revision")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("checkout amendments are append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("checkout amendments are append-only")
