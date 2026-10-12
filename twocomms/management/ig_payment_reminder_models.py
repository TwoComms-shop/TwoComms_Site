"""Append-only, source-bound agreements for one requested payment reminder."""
import uuid

from django.db import models


class _ImmutableReminderQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("payment reminder evidence is append-only")

    def delete(self):
        raise ValueError("payment reminder evidence is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("payment reminder evidence is append-only")


class _ImmutableReminder(models.Model):
    envelope = models.JSONField(default=dict)
    envelope_hmac = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = models.Manager.from_queryset(_ImmutableReminderQuerySet)()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("payment reminder evidence is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("payment reminder evidence is append-only")


class IgPaymentReminderInvitation(_ImmutableReminder):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    request_source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT, related_name="payment_reminder_requests", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    deal = models.ForeignKey("management.IgDeal", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    proposal = models.ForeignKey("management.IgCheckoutProposal", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    commercial_episode = models.ForeignKey("management.IgCommercialEpisode", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    invoice_generation = models.ForeignKey("management.IgCheckoutInvoiceGeneration", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    payment_attempt = models.ForeignKey("orders.PaymentAttempt", on_delete=models.PROTECT, related_name="payment_reminder_invitations", db_constraint=False)
    due_at = models.DateTimeField(db_index=True)
    expires_at = models.DateTimeField()
    window_deadline = models.DateTimeField()
    locale = models.CharField(max_length=2)
    body = models.TextField()
    button_title = models.CharField(max_length=20)
    accept_payload = models.CharField(max_length=180)
    revoke_payload = models.CharField(max_length=180)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["revision", "request_source"], name="ig_payrem_inv_source_once")]
        indexes = [models.Index(fields=["client", "due_at"], name="ig_payrem_client_due")]


class IgPaymentReminderReceipt(_ImmutableReminder):
    invitation = models.OneToOneField(IgPaymentReminderInvitation, on_delete=models.PROTECT, related_name="native_receipt", db_constraint=False)
    effect = models.OneToOneField("management.IgRevisionDeliveryEffect", on_delete=models.PROTECT, related_name="payment_reminder_receipt", db_constraint=False)
    provider_message_id = models.CharField(max_length=255)
    delivered_at = models.DateTimeField()


class IgPaymentReminderAnswer(_ImmutableReminder):
    class Kind(models.TextChoices):
        ACCEPT = "accept", "Accept requested reminder"
        REVOKE = "revoke", "Revoke requested reminder"

    invitation = models.ForeignKey(IgPaymentReminderInvitation, on_delete=models.PROTECT, related_name="answers", db_constraint=False)
    source = models.OneToOneField("management.InstagramBotMessage", on_delete=models.PROTECT, related_name="payment_reminder_answer", db_constraint=False)
    receipt = models.ForeignKey(IgPaymentReminderReceipt, on_delete=models.PROTECT, related_name="answers", db_constraint=False)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    task = models.OneToOneField("management.IgFollowUpTask", null=True, blank=True, on_delete=models.PROTECT, related_name="payment_reminder_answer", db_constraint=False)
    answered_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["invitation", "kind"], name="ig_payrem_answer_kind_once")]
