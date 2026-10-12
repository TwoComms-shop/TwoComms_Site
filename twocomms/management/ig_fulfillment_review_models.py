"""Immutable evidence attached to the existing manager business-case task."""
import uuid

from django.conf import settings
from django.db import models


class _ReviewEvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("fulfillment review evidence is append-only")

    def delete(self):
        raise ValueError("fulfillment review evidence is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("fulfillment review evidence is append-only")


class _ReviewEvidence(models.Model):
    envelope = models.JSONField(default=dict)
    key_id = models.CharField(max_length=24)
    envelope_hmac = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(_ReviewEvidenceQuerySet)()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("fulfillment review evidence is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("fulfillment review evidence is append-only")


class IgFulfillmentReviewRequest(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.OneToOneField("management.IgFollowUpTask", on_delete=models.PROTECT,
        related_name="fulfillment_review_request", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="fulfillment_review_requests", db_constraint=False)
    source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT,
        null=True, blank=True, related_name="fulfillment_review_requests", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT,
        null=True, blank=True, related_name="fulfillment_review_requests", db_constraint=False)

    class Meta:
        indexes = [models.Index(fields=["client", "created_at"], name="ig_fulreview_client")]


class IgFulfillmentReviewDecision(_ReviewEvidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(IgFulfillmentReviewRequest, on_delete=models.PROTECT,
        related_name="decisions", db_constraint=False)
    version = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="fulfillment_review_decisions", db_constraint=False)
    operation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "version"], name="ig_fulreview_decision_ver")]
