"""An exact request for a reminder time; this is never business consent."""
import uuid

from django.db import models

from management.ig_payment_reminder_models import _ImmutableReminder


class IgPaymentReminderTimeRequest(_ImmutableReminder):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="payment_reminder_time_requests", db_constraint=False)
    request_source = models.OneToOneField("management.InstagramBotMessage", on_delete=models.PROTECT,
        related_name="payment_reminder_time_request", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT,
        related_name="payment_reminder_time_requests", db_constraint=False)
    expires_at = models.DateTimeField()
    window_deadline = models.DateTimeField()
    locale = models.CharField(max_length=2)
    body = models.TextField()

    class Meta:
        indexes = [models.Index(fields=["client", "expires_at"], name="ig_paytime_client_expiry")]
