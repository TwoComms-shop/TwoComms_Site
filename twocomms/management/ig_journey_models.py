"""Append-only conversational decisions, independent of commercial episodes."""
from django.core.exceptions import ValidationError
from django.db import models


class _RouteDecisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Conversation route decisions are immutable.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Conversation route decisions are immutable.")

    def delete(self):
        raise ValidationError("Conversation route decisions are append-only.")

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Conversation route decisions are immutable.")
        return super().bulk_create(objs, **kwargs)


class IgConversationRouteDecision(models.Model):
    """An accepted interpretation and its actual delta; never business authority."""

    client = models.ForeignKey("management.IgClient", on_delete=models.DO_NOTHING,
        db_constraint=False, related_name="conversation_route_decisions")
    revision = models.ForeignKey("management.IgCustomerTurnRevision", null=True,
        on_delete=models.DO_NOTHING, db_constraint=False, related_name="route_decisions")
    analysis_result = models.ForeignKey("management.IgConversationAnalysisResult", null=True,
        on_delete=models.DO_NOTHING, db_constraint=False, related_name="route_decisions")
    previous = models.ForeignKey("self", null=True, on_delete=models.DO_NOTHING,
        db_constraint=False, related_name="successors")
    decision_key = models.CharField(max_length=64, unique=True)
    sequence = models.PositiveBigIntegerField()
    reset_floor = models.PositiveBigIntegerField()
    watermark_message_id = models.PositiveBigIntegerField()
    input_digest = models.CharField(max_length=64)
    interpretation_digest = models.CharField(max_length=64)
    decision_digest = models.CharField(max_length=64)
    source_binding = models.JSONField()
    interpretation = models.JSONField()
    active_intents = models.JSONField(default=list)
    focus_key = models.CharField(max_length=64, blank=True, default="")
    transitions = models.JSONField(default=list)
    reason_code = models.CharField(max_length=32, default="customer_intent")
    schema_version = models.CharField(max_length=32, default="customer-route.v1")
    producer_version = models.CharField(max_length=32, default="conversation-route.v1")
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    objects = _RouteDecisionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(
                models.Q(revision__isnull=False, analysis_result__isnull=True)
                | models.Q(revision__isnull=True, analysis_result__isnull=False)
            ), name="ig_route_exact_source"),
            models.UniqueConstraint(fields=["client", "reset_floor", "sequence"],
                name="ig_route_scope_sequence"),
            models.UniqueConstraint(fields=["client", "reset_floor", "input_digest",
                "interpretation_digest"], name="ig_route_input_interpretation"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1)
                & models.Q(reset_floor__gte=1) & models.Q(watermark_message_id__gte=1),
                name="ig_route_positive_scope"),
        ]
        indexes = [models.Index(fields=["client", "reset_floor", "-sequence"],
            name="ig_route_current_scope")]

    def save(self, *args, **kwargs):
        if not self._state.adding or kwargs.get("force_update") or (
            self.pk is not None and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise ValidationError("Conversation route decisions are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Conversation route decisions are append-only.")


class _JourneyTraceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Journey trace snapshots are immutable.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Journey trace snapshots are immutable.")

    def delete(self):
        raise ValidationError("Journey trace snapshots are append-only.")

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Journey trace snapshots are immutable.")
        return super().bulk_create(objs, **kwargs)


class IgJourneyTraceSnapshot(models.Model):
    """Source-bound transcript interpretation; never business authority."""

    snapshot_key = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey("management.IgClient", on_delete=models.DO_NOTHING,
        db_constraint=False, related_name="journey_trace_snapshots")
    commercial_episode = models.ForeignKey("management.IgCommercialEpisode", null=True, blank=True,
        on_delete=models.DO_NOTHING, db_constraint=False, related_name="journey_trace_snapshots")
    watermark_message_id = models.PositiveBigIntegerField()
    source_digest = models.CharField(max_length=64)
    trace = models.JSONField()
    trace_digest = models.CharField(max_length=64)
    schema_version = models.CharField(max_length=32, default="journey-trace.v1")
    prompt_version = models.CharField(max_length=32)
    producer_version = models.CharField(max_length=32, default="journey-trace-store.v1")
    analysis_model = models.CharField(max_length=80)
    analyzed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = _JourneyTraceQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["client", "commercial_episode", "-id"], name="ig_trace_client_episode")]
        constraints = [models.CheckConstraint(condition=models.Q(watermark_message_id__gte=1),
                                               name="ig_trace_positive_watermark")]

    def save(self, *args, **kwargs):
        if not self._state.adding or kwargs.get("force_update") or (
            self.pk is not None and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise ValidationError("Journey trace snapshots are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Journey trace snapshots are append-only.")
