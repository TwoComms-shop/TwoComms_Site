"""Private manager documents and immutable human delivery parts.

Imported explicitly by the human adapter; integration/registration is owned by
its release. Client CASCADE uses the existing privacy/180-day client purge.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from django.conf import settings
from django.db import models


_HASH = re.compile(r"[0-9a-f]{64}\Z")


def human_payload_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class _HumanReplyPartQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).intersection(HumanReplyPart.IMMUTABLE_FIELDS | {"command", "client"}) or "provider_message_id" in kwargs:
            raise ValueError("human part payload and receipt identity are immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if set(fields).intersection(HumanReplyPart.IMMUTABLE_FIELDS | {"command", "client"}) or "provider_message_id" in fields:
            raise ValueError("human part payload and receipt identity are immutable")
        return super().bulk_update(objs, fields, batch_size=batch_size)


class HumanReplyPart(models.Model):
    """One physical manager request; a provider receipt belongs to this part."""
    class State(models.TextChoices):
        PLANNED = "planned", "Planned"
        CLAIMED = "claimed", "Claimed"
        PROVIDER_STARTED = "provider_started", "Provider started"
        SENT = "sent", "Sent"
        DEFINITE_FAILED = "definite_failed", "Definite failure"
        UNKNOWN = "unknown", "Unknown"
        CANCELLED = "cancelled", "Cancelled"

    command = models.ForeignKey("management.HumanReplyCommand", on_delete=models.CASCADE,
        related_name="delivery_parts", db_constraint=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.CASCADE,
        related_name="human_reply_parts", db_constraint=False)
    operation_id_snapshot = models.UUIDField()
    actor_id_snapshot = models.PositiveBigIntegerField()
    context_message_id_snapshot = models.PositiveBigIntegerField()
    permission_epoch = models.PositiveBigIntegerField()
    recipient_igsid = models.CharField(max_length=64)
    provider_namespace = models.CharField(max_length=128)
    ordinal = models.PositiveSmallIntegerField()
    part_count = models.PositiveSmallIntegerField()
    plan_version = models.CharField(max_length=40, default="human-delivery-plan.v1")
    plan_digest = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PLANNED, db_index=True)
    claim_token = models.CharField(max_length=64, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    response_digest = models.CharField(max_length=64, blank=True, default="")
    terminal_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_FIELDS = frozenset({"command_id", "client_id", "operation_id_snapshot", "actor_id_snapshot",
        "context_message_id_snapshot", "permission_epoch", "recipient_igsid", "provider_namespace",
        "ordinal", "part_count", "plan_version", "plan_digest", "payload", "payload_digest"})
    objects = models.Manager.from_queryset(_HumanReplyPartQuerySet)()

    class Meta:
        app_label = "management"
        ordering = ["command_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["command", "ordinal"], name="ig_human_part_ordinal"),
            models.UniqueConstraint(fields=["provider_namespace", "provider_message_id"], name="ig_human_part_receipt"),
            models.CheckConstraint(condition=models.Q(part_count__gte=1), name="ig_human_part_count"),
            models.CheckConstraint(condition=models.Q(ordinal__lt=models.F("part_count")), name="ig_human_part_bounds"),
        ]
        indexes = [models.Index(fields=["state", "lease_until", "id"], name="ig_human_part_due")]

    @property
    def text(self):
        return str((self.payload.get("message") or {}).get("text") or "") if isinstance(self.payload, dict) and isinstance(self.payload.get("message"), dict) else ""

    def save(self, *args, **kwargs):
        if (not isinstance(self.payload, dict) or not _HASH.fullmatch(self.plan_digest or "") or not _HASH.fullmatch(self.payload_digest or "")
            or self.payload_digest != human_payload_digest(self.payload)
            or not self.text or not self.provider_namespace or not self.recipient_igsid
            or self.payload.get("recipient") != {"id": self.recipient_igsid}
            or self.part_count < 1 or self.ordinal < 0 or self.ordinal >= self.part_count or self.state not in self.State.values
            or (self.state == self.State.SENT and (not self.provider_message_id or not self.provider_started_at))
            or not self.actor_id_snapshot or not self.context_message_id_snapshot):
            raise ValueError("human part binding invalid")
        if self.provider_message_id is not None and (len(self.provider_message_id) > 255 or not self.provider_message_id.strip()
            or self.provider_message_id != self.provider_message_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in self.provider_message_id)):
            raise ValueError("human provider receipt invalid")
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS, "provider_message_id").first()
            if old and any(getattr(self, key) != old[key] for key in self.IMMUTABLE_FIELDS):
                raise ValueError("human part payload is immutable")
            if old and old["provider_message_id"] is not None and self.provider_message_id != old["provider_message_id"]:
                raise ValueError("human provider receipt is single assignment")
        return super().save(*args, **kwargs)


class _PrivateDocumentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).intersection(HumanReplyPrivateDocument.IMMUTABLE_FIELDS | {"client", "text", "text_hash", "version", "state"}) or any(
            key in kwargs and kwargs[key] is not None for key in ("actor", "actor_id", "context_message", "context_message_id", "consumed_command", "consumed_command_id")):
            raise ValueError("private document requires versioned service CAS")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if set(fields).intersection(HumanReplyPrivateDocument.IMMUTABLE_FIELDS | {"client", "text", "text_hash", "version", "state", "actor", "context_message", "consumed_command", "actor_id", "context_message_id", "consumed_command_id"}):
            raise ValueError("private document requires versioned service CAS")
        return super().bulk_update(objs, fields, batch_size=batch_size)


class HumanReplyPrivateDocument(models.Model):
    """Internal text, never a transcript/source or provider instruction."""
    class Kind(models.TextChoices):
        REPLY_DRAFT = "reply_draft", "Reply draft"
        INTERNAL_NOTE = "internal_note", "Internal note"

    class State(models.TextChoices):
        OPEN = "open", "Open"
        CONSUMED = "consumed", "Consumed"
        ARCHIVED = "archived", "Archived"

    document_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    client = models.ForeignKey("management.IgClient", on_delete=models.CASCADE,
        related_name="human_private_documents", db_constraint=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="ig_human_private_documents", db_constraint=False)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    recipient_igsid = models.CharField(max_length=64)
    provider_namespace = models.CharField(max_length=128)
    context_message = models.ForeignKey("management.InstagramBotMessage", on_delete=models.SET_NULL,
        null=True, related_name="human_private_documents", db_constraint=False)
    context_digest = models.CharField(max_length=64)
    reset_floor = models.PositiveBigIntegerField(default=1)
    source_permission_epoch = models.PositiveBigIntegerField(default=0)
    text = models.TextField()
    text_hash = models.CharField(max_length=64)
    version = models.PositiveBigIntegerField(default=1)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    consumed_command = models.OneToOneField("management.HumanReplyCommand", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="private_document", db_constraint=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_FIELDS = frozenset({"document_id", "client_id", "kind", "recipient_igsid", "provider_namespace",
        "context_digest", "reset_floor", "source_permission_epoch"})
    objects = models.Manager.from_queryset(_PrivateDocumentQuerySet)()

    class Meta:
        app_label = "management"
        ordering = ["-updated_at", "-id"]
        constraints = [models.CheckConstraint(condition=models.Q(version__gte=1), name="ig_human_doc_version")]
        indexes = [models.Index(fields=["client", "actor", "kind", "state"], name="ig_human_doc_scope")]

    def save(self, *args, **kwargs):
        if (self.text_hash != hashlib.sha256(self.text.encode()).hexdigest()
            or not _HASH.fullmatch(self.context_digest or "") or not self.version
            or not self.provider_namespace or not self.recipient_igsid or self.reset_floor < 1
            or self.kind not in self.Kind.values or self.state not in self.State.values):
            raise ValueError("private document binding invalid")
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS,
                "actor_id", "context_message_id", "consumed_command_id", "version", "text", "state").first()
            if old and any(getattr(self, key) != old[key] for key in self.IMMUTABLE_FIELDS):
                raise ValueError("private document scope is immutable")
            if old and self.actor_id not in {old["actor_id"], None}:
                raise ValueError("private document owner is immutable")
            if old and self.context_message_id not in {old["context_message_id"], None}:
                raise ValueError("private document context is immutable")
            if old and old["consumed_command_id"] is not None and self.consumed_command_id != old["consumed_command_id"]:
                raise ValueError("private document consumption is single assignment")
            if old and (self.text != old["text"] or self.state != old["state"] or self.consumed_command_id != old["consumed_command_id"]) and self.version != old["version"] + 1:
                raise ValueError("private document version must advance exactly once")
            if old and old["state"] != self.State.OPEN and self.text != old["text"]:
                raise ValueError("closed private document text is immutable")
        return super().save(*args, **kwargs)
