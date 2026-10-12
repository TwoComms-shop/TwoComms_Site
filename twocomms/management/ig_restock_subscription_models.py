"""Source-bound, one-off restock agreements; evidence never grants delivery by itself."""
import uuid

from django.db import models


class _EvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("restock evidence is append-only")

    def delete(self):
        raise ValueError("restock evidence is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("restock evidence is append-only")


class _Evidence(models.Model):
    envelope = models.JSONField(default=dict)
    envelope_hmac = models.CharField(max_length=64)
    key_id = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(_EvidenceQuerySet)()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("restock evidence is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("restock evidence is append-only")


class IgRestockInvitation(_Evidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT, related_name="restock_invitations", db_constraint=False)
    request_source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT, related_name="restock_requests", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT, related_name="restock_invitations", db_constraint=False)
    target = models.JSONField(default=dict)
    target_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    acceptance_deadline = models.DateTimeField()
    locale = models.CharField(max_length=2)
    body = models.TextField()
    button_title = models.CharField(max_length=20)
    accept_payload = models.CharField(max_length=180)
    revoke_payload = models.CharField(max_length=180)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["revision", "request_source"], name="ig_restock_inv_source_once")]
        indexes = [models.Index(fields=["client", "created_at"], name="ig_restock_inv_client")]


class IgRestockReceipt(_Evidence):
    invitation = models.OneToOneField(IgRestockInvitation, on_delete=models.PROTECT, related_name="native_receipt", db_constraint=False)
    effect = models.OneToOneField("management.IgRevisionDeliveryEffect", on_delete=models.PROTECT, related_name="restock_receipt", db_constraint=False)
    provider_message_id = models.CharField(max_length=255)
    delivered_at = models.DateTimeField()


class IgRestockAnswer(_Evidence):
    class Kind(models.TextChoices):
        ACCEPT = "accept", "Accept one restock notification"
        REVOKE = "revoke", "Revoke restock notification"

    invitation = models.ForeignKey(IgRestockInvitation, on_delete=models.PROTECT, related_name="answers", db_constraint=False)
    source = models.OneToOneField("management.InstagramBotMessage", on_delete=models.PROTECT, related_name="restock_answer", db_constraint=False)
    receipt = models.ForeignKey(IgRestockReceipt, on_delete=models.PROTECT, related_name="answers", db_constraint=False)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    answered_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["invitation", "kind"], name="ig_restock_answer_kind_once")]


class IgRestockSubscription(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Awaiting confirmed replenishment"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"
        SENT = "sent", "Sent once"
        UNKNOWN = "unknown", "Provider outcome unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    accepted_answer = models.OneToOneField(IgRestockAnswer, on_delete=models.PROTECT, related_name="subscription", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT, related_name="restock_subscriptions", db_constraint=False)
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    expires_at = models.DateTimeField()
    next_check_at = models.DateTimeField(db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_reason = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["state", "next_check_at"], name="ig_restock_fair_scan")]


class IgRestockAvailabilityEvent(_Evidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.OneToOneField(IgRestockSubscription, on_delete=models.PROTECT, related_name="availability_event", db_constraint=False)
    task = models.OneToOneField("management.IgFollowUpTask", on_delete=models.PROTECT, related_name="restock_availability_event", db_constraint=False)
    event_key = models.CharField(max_length=180, unique=True)
    observed_at = models.DateTimeField()
    availability_digest = models.CharField(max_length=64)
