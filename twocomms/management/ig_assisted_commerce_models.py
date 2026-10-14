"""Source-owned manager-approved assisted commerce evidence.

Reuses existing _ReviewEvidence envelope/HMAC and
exact-client privacy deletion conventions. The service verifies HMAC/source
currentness, holds existing client/request/proposal row locks, and uses native
CAS guards. No provider or stock lifecycle is duplicated here.
"""
import uuid

from django.conf import settings
from django.db import models

from .ig_fulfillment_review_models import _ReviewEvidence


class IgAssistedCommerceRequest(_ReviewEvidence):
    """Immutable complete intention version, before payable execution selection."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intention_id = models.UUIDField(editable=False)
    version = models.PositiveIntegerField()
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="assisted_commerce_requests", db_constraint=False)
    task = models.ForeignKey("management.IgFollowUpTask", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_requests", db_constraint=False)
    source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_requests", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_requests", db_constraint=False)
    episode = models.ForeignKey("management.IgCommercialEpisode", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_requests", db_constraint=False)
    deal = models.ForeignKey("management.IgDeal", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_requests", db_constraint=False)
    source_namespace = models.CharField(max_length=128)
    reset_floor = models.PositiveBigIntegerField()
    client_permission_epoch = models.PositiveBigIntegerField()
    full_capture_digest = models.CharField(max_length=64)
    snapshot_digest = models.CharField(max_length=64)

    class Meta:
        app_label = "management"
        constraints = [models.UniqueConstraint(fields=["intention_id", "version"], name="ig_assist_request_version"),
            models.CheckConstraint(condition=models.Q(version__gte=1) & models.Q(reset_floor__gte=1), name="ig_assist_request_bounds")]
        indexes = [models.Index(fields=["client", "created_at"], name="ig_assist_request_client")]


class IgAssistedCommerceDecision(_ReviewEvidence):
    """Append-only exact manager edit/approval version; generic task status is irrelevant."""
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved exact intention"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(IgAssistedCommerceRequest, on_delete=models.PROTECT,
        related_name="decisions", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="assisted_commerce_decisions", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="assisted_commerce_decisions", db_constraint=False)
    version = models.PositiveIntegerField()
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices)
    request_digest = models.CharField(max_length=64)
    draft_digest = models.CharField(max_length=64)
    full_capture_digest = models.CharField(max_length=64)
    snapshot_digest = models.CharField(max_length=64)
    currency = models.CharField(max_length=8, default="UAH")
    approved_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    approved_custom_quantity = models.PositiveIntegerField(default=0)
    physical_checked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "management"
        constraints = [models.UniqueConstraint(fields=["request", "version"], name="ig_assist_decision_version"),
            models.CheckConstraint(condition=models.Q(version__gte=1) & models.Q(approved_total__gte=0) & models.Q(approved_total__lte=1000000)
                & models.Q(approved_custom_quantity__lte=50), name="ig_assist_decision_bounds")]
        indexes = [models.Index(fields=["request", "status", "version"], name="ig_assist_decision_head")]


class IgAssistedCommerceStockSync(_ReviewEvidence):
    """One exact manager recount receipt, written by the stock effect owner.

    Sealed pool/count snapshots and native movement identities stay in the
    verified envelope. Dialogue approval alone never changes inventory.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(IgAssistedCommerceDecision, on_delete=models.PROTECT,
        related_name="stock_sync", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="assisted_commerce_stock_syncs", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="assisted_commerce_stock_syncs", db_constraint=False)
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    snapshot_digest = models.CharField(max_length=64)

    class Meta:
        app_label = "management"


class IgAssistedCommerceExecution(_ReviewEvidence):
    """One physical approval -> one existing proposal series -> one canonical order.

The sealed envelope covers immutable columns and selected/deferred full-basket
proof. MUTABLE_FIELDS are terminal projections, excluded from envelope columns
by the assisted service's existing-style _columns implementation. No invoice
state, provider retry state or second payment winner lives here.

Paid RESOURCE_REVIEW retains the active intention hold. This slice has no
verified refund-resolution closure: deferred/new same-intention execution is
blocked until the existing authoritative refund path is separately verified.
Never clear the historical generation winner to release this row.
"""
    class Scope(models.TextChoices):
        FULL = "full", "Full intention"
        PARTIAL = "partial", "Customer-accepted selection"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intention_id = models.UUIDField(editable=False)
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request = models.ForeignKey(IgAssistedCommerceRequest, on_delete=models.PROTECT,
        related_name="executions", db_constraint=False)
    decision = models.OneToOneField(IgAssistedCommerceDecision, on_delete=models.PROTECT,
        related_name="execution", db_constraint=False)
    proposal = models.OneToOneField("management.IgCheckoutProposal", on_delete=models.PROTECT,
        related_name="assisted_commerce_execution", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="assisted_commerce_executions", db_constraint=False)
    episode = models.ForeignKey("management.IgCommercialEpisode", on_delete=models.PROTECT,
        related_name="assisted_commerce_executions", db_constraint=False)
    choice_source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_choices", db_constraint=False)
    purchase_source = models.ForeignKey("management.InstagramBotMessage", on_delete=models.PROTECT,
        related_name="assisted_commerce_purchases", db_constraint=False)
    revision = models.ForeignKey("management.IgCustomerTurnRevision", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_executions", db_constraint=False)
    scope = models.CharField(max_length=16, choices=Scope.choices)
    scope_digest = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    decision_digest = models.CharField(max_length=64)
    full_capture_digest = models.CharField(max_length=64)
    snapshot_digest = models.CharField(max_length=64)
    currency = models.CharField(max_length=8, default="UAH")
    quoted_total = models.DecimalField(max_digits=12, decimal_places=2)
    quoted_custom_quantity = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    active_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1)
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT,
        null=True, blank=True, related_name="assisted_commerce_execution", db_constraint=False)
    settled_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=64, blank=True, default="")

    MUTABLE_FIELDS = frozenset({"active_slot", "order", "settled_at", "released_at", "release_reason"})

    class Meta:
        app_label = "management"
        constraints = [models.UniqueConstraint(fields=["intention_id", "active_slot"], name="ig_assist_intention_active"),
            models.CheckConstraint(condition=models.Q(active_slot__isnull=True) | models.Q(active_slot=1), name="ig_assist_active_shape"),
            models.CheckConstraint(condition=models.Q(scope="full") | models.Q(scope="partial", choice_source__isnull=False),
                name="ig_assist_execution_scope"),
            models.CheckConstraint(condition=models.Q(quoted_total__gt=0) & models.Q(quoted_total__lte=1000000)
                & models.Q(quoted_custom_quantity__lte=50), name="ig_assist_execution_bounds")]
        indexes = [models.Index(fields=["client", "created_at"], name="ig_assist_execution_client")]

    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        fields = kwargs.get("update_fields")
        if not fields or not set(fields).issubset(self.MUTABLE_FIELDS):
            raise ValueError("assisted execution permits only terminal service projection")
        # Service holds this row lock and verifies immutable evidence. Native
        # guards perform single-assignment CAS on terminal columns.
        return models.Model.save(self, *args, **kwargs)


class IgAssistedCommerceImage(_ReviewEvidence):
    """Owned raster metadata; opaque private storage key, never a public URL."""
    class State(models.TextChoices):
        ACTIVE = "active", "Active owned image"
        DELETE_PENDING = "delete_pending", "Deletion pending"
        DELETING = "deleting", "Deletion in progress"
        DELETED = "deleted", "Deleted"
        DELETE_FAILED = "delete_failed", "Deletion retry required"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(IgAssistedCommerceRequest, on_delete=models.PROTECT,
        related_name="images", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.PROTECT,
        related_name="assisted_commerce_images", db_constraint=False)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="assisted_commerce_images", db_constraint=False)
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    storage_key = models.CharField(max_length=128, unique=True)
    content_digest = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=32)
    byte_size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    snapshot_digest = models.CharField(max_length=64)
    delete_after = models.DateTimeField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE, db_index=True)
    erased_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")

    MUTABLE_FIELDS = frozenset({"state", "erased_at", "failure_code"})

    class Meta:
        app_label = "management"
        constraints = [models.CheckConstraint(condition=models.Q(byte_size__gte=1) & models.Q(byte_size__lte=6291456)
            & models.Q(width__gte=1) & models.Q(height__gte=1), name="ig_assist_image_bounds")]
        indexes = [models.Index(fields=["state", "delete_after"], name="ig_assist_image_erasure")]

    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        fields = kwargs.get("update_fields")
        if not fields or not set(fields).issubset(self.MUTABLE_FIELDS):
            raise ValueError("assisted image permits only erasure service projection")
        return models.Model.save(self, *args, **kwargs)

    @property
    def url(self):
        raise ValueError("assisted owned images have no public URL")
