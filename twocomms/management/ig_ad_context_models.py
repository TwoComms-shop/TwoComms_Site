"""Bounded asynchronous observations of public advertising metadata."""
from django.db import models


class IgAdMetadataRecord(models.Model):
    namespace = models.CharField(max_length=128)
    ad_id = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="pending", db_index=True)
    reason = models.CharField(max_length=64, blank=True, default="")
    api_version = models.CharField(max_length=16, blank=True, default="")
    snapshot = models.JSONField(default=dict, blank=True)
    snapshot_digest = models.CharField(max_length=64, blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    claim_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "management"
        constraints = [models.UniqueConstraint(fields=["namespace", "ad_id"], name="ig_ad_metadata_identity")]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="ig_ad_metadata_due")]
