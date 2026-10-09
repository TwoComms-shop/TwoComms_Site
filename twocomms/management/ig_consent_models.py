"""One purpose's immutable invitation and source-confirmed business answers."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

__all__ = ["IgMarketingConsentInvitation", "IgMarketingConsentAnswer"]

INVITATION_IMMUTABLE = (
    "client_id", "order_id", "assignment_id", "assignment_version", "reset_audit_id",
    "purpose", "recipient_igsid", "provider_namespace", "settings_id_snapshot",
    "locale", "language_proof", "source_message_id", "source_digest", "payment_digest",
    "message_snapshot", "accept_payload", "decline_payload", "revoke_payload",
    "expires_at", "issued_at", "provider_basis", "native_grant", "signing_key_id",
    "snapshot_hmac", "created_at",
)


class _InvitationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Consent transitions require the locked owner")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Consent transitions require the locked owner")

    def bulk_create(self, objs, *args, **kwargs):
        raise ValueError("Consent invitations require the locked owner")

    def delete(self):
        raise ValueError("Consent deletion requires privacy erasure")

    def _raw_delete(self, using):
        raise ValueError("Consent deletion requires privacy erasure")


class IgMarketingConsentInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.DO_NOTHING,
                              db_constraint=False, related_name="marketing_consent_invitations")
    order = models.ForeignKey("orders.Order", on_delete=models.DO_NOTHING,
                             db_constraint=False, related_name="marketing_consent_invitations")
    assignment = models.ForeignKey("management.IgOrderAssignment", on_delete=models.DO_NOTHING,
                                  db_constraint=False, related_name="marketing_consent_invitations")
    assignment_version = models.PositiveIntegerField()
    reset_audit_id = models.PositiveBigIntegerField(default=0)
    purpose = models.CharField(max_length=32, default="post_purchase_marketing")
    recipient_igsid = models.CharField(max_length=64)
    provider_namespace = models.CharField(max_length=128)
    settings_id_snapshot = models.PositiveIntegerField()
    locale = models.CharField(max_length=2)
    language_proof = models.JSONField(default=dict)
    source_message = models.ForeignKey("management.InstagramBotMessage", on_delete=models.DO_NOTHING,
                                      db_constraint=False, related_name="marketing_consent_invitations")
    source_digest = models.CharField(max_length=64)
    payment_digest = models.CharField(max_length=64)
    message_snapshot = models.TextField()
    accept_payload = models.CharField(max_length=1000)
    decline_payload = models.CharField(max_length=1000)
    revoke_payload = models.CharField(max_length=1000)
    expires_at = models.DateTimeField()
    issued_at = models.DateTimeField()
    provider_basis = models.CharField(max_length=32, default="standard_window")
    native_grant = models.CharField(max_length=16, default="unverified")
    signing_key_id = models.CharField(max_length=24)
    snapshot_hmac = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="pending", db_index=True)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    receipt_hmac = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    objects = _InvitationQuerySet.as_manager()

    class Meta:
        app_label = "management"
        constraints = [
            models.UniqueConstraint(fields=["client", "order", "assignment", "assignment_version", "reset_audit_id", "purpose"], name="ig_consent_scope_once"),
            models.CheckConstraint(condition=models.Q(purpose="post_purchase_marketing", provider_basis="standard_window", native_grant="unverified", locale__in=["uk", "ru", "en"]), name="ig_consent_policy_shape"),
            models.CheckConstraint(condition=models.Q(state__in=["pending", "processing", "sent", "unknown", "failed"]), name="ig_consent_state_shape"),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F("issued_at")), name="ig_consent_expiry_order"),
            models.CheckConstraint(condition=~models.Q(state="sent") | (~models.Q(provider_message_id="") & models.Q(sent_at__isnull=False)), name="ig_consent_sent_receipt"),
        ]
        indexes = [models.Index(fields=["state", "issued_at"], name="ig_consent_due")]

    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).values(*INVITATION_IMMUTABLE, "state", "provider_message_id", "receipt_hmac", "sent_at").first() if not self._state.adding else None
        if old:
            if any(getattr(self, field) != old[field] for field in INVITATION_IMMUTABLE):
                raise ValueError("Consent invitation snapshot is immutable")
            if old["state"] in {"sent", "unknown", "failed"} and (
                self.state != old["state"] or self.provider_message_id != old["provider_message_id"] or self.receipt_hmac != old["receipt_hmac"] or self.sent_at != old["sent_at"]
            ):
                raise ValueError("Consent terminal receipt is immutable")
        elif self.state != "pending" or self.attempts or self.provider_message_id:
            raise ValidationError("Consent invitation starts pending without a receipt")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Consent deletion requires privacy erasure")


class _AnswerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Consent answer is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Consent answer is append-only")

    def bulk_create(self, objs, *args, **kwargs):
        raise ValueError("Consent answers require the source-bound owner")

    def delete(self):
        raise ValueError("Consent deletion requires privacy erasure")

    def _raw_delete(self, using):
        raise ValueError("Consent deletion requires privacy erasure")


class IgMarketingConsentAnswer(models.Model):
    invitation = models.ForeignKey(IgMarketingConsentInvitation, on_delete=models.DO_NOTHING,
                                  db_constraint=False, related_name="answers")
    source_message = models.ForeignKey("management.InstagramBotMessage", on_delete=models.DO_NOTHING,
                                      db_constraint=False, related_name="marketing_consent_answers")
    provider_namespace = models.CharField(max_length=128)
    source_mid = models.CharField(max_length=255)
    choice = models.CharField(max_length=16)
    source_digest = models.CharField(max_length=64)
    answered_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    signing_key_id = models.CharField(max_length=24)
    answer_hmac = models.CharField(max_length=64)
    objects = _AnswerQuerySet.as_manager()

    class Meta:
        app_label = "management"
        constraints = [
            models.UniqueConstraint(fields=["provider_namespace", "source_mid"], name="ig_consent_answer_source"),
            models.CheckConstraint(condition=models.Q(choice__in=["accept", "decline", "revoke"]), name="ig_consent_answer_choice"),
        ]
        indexes = [models.Index(fields=["invitation", "-answered_at", "-id"], name="ig_consent_answer_latest")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Consent answer is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Consent deletion requires privacy erasure")
