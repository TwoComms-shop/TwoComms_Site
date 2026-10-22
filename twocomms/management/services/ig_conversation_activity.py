"""Read-only conversation ordering, independent of CRM/window rollups."""

import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Case, Count, DateTimeField, Exists, F, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce, JSONObject


HISTORICAL_SOURCES = ("manual_refresh", "poll_history", "backfill", "history", "import")
ORDER_REVISION_FIELDS = {
    "id", "status", "payment_status", "pay_type", "total_sum", "discount_amount",
    "tracking_number", "shipment_status", "shipment_status_updated", "tracking_status_code",
    "delivery_method", "full_name", "phone", "email", "city", "np_office",
    "updated", "updated_at",
}


def with_conversation_activity(queryset):
    """Use event time, never the local time at which history was imported.

    USER rows are observed messages regardless of processing outcome. Outgoing
    rows count only after successful delivery/observation. Legacy clients with
    no transcript retain their old timestamp; a timestamp-less historical row
    cannot inherit a possibly polluted ingest-time client rollup.
    """
    from management.models import InstagramBotMessage

    transcript = InstagramBotMessage.objects.filter(client_id=OuterRef("pk"))
    latest = transcript.filter(
        Q(role=InstagramBotMessage.Role.USER)
        | Q(role__in=[InstagramBotMessage.Role.MODEL, InstagramBotMessage.Role.MANAGER],
            status=InstagramBotMessage.Status.DONE)
    ).exclude(
        source__in=HISTORICAL_SOURCES, provider_created_at__isnull=True,
    ).exclude(
        # The older poll importer also used source="poll" for backfilled
        # provider rows. Signed live poll admission has ingress_proof; local
        # sent replies have no provider mid. Neither needs this exclusion.
        source="poll", provider_created_at__isnull=True,
        mid__isnull=False, ingress_proof={},
    ).exclude(
        role__in=[InstagramBotMessage.Role.MODEL, InstagramBotMessage.Role.MANAGER],
        send_state__in=["sending", "unknown", "failed", "cancelled", "duplicate"],
    ).annotate(
        _activity_event_at=Coalesce(
            "provider_created_at",
            Case(
                When(role=InstagramBotMessage.Role.USER, then=F("created_at")),
                default=Coalesce("send_completed_at", "created_at"),
                output_field=DateTimeField(),
            ),
        ),
    ).order_by("-_activity_event_at", "-id")
    return queryset.annotate(
        activity_at=Coalesce(
            Subquery(latest.values("_activity_event_at")[:1]),
            Case(
                When(~Exists(transcript), then=F("last_message_at")),
                default=Value(None), output_field=DateTimeField(),
            ),
        ),
        activity_message_id=Subquery(latest.values("id")[:1]),
        activity_sort_client_id=F("pk"),
    )


def activity_payload(client):
    """Serialize an annotated row, or fetch just the activity for a detail row."""
    if not hasattr(client, "activity_at"):
        row = with_conversation_activity(type(client).objects.filter(pk=client.pk)).values(
            "activity_at", "activity_message_id", "activity_sort_client_id",
        ).get()
    else:
        row = {name: getattr(client, name) for name in (
            "activity_at", "activity_message_id", "activity_sort_client_id",
        )}
    return {
        "activity_at": row["activity_at"].isoformat() if row["activity_at"] else "",
        "activity_message_id": row["activity_message_id"] or 0,
        "activity_sort_client_id": row["activity_sort_client_id"],
    }


def detail_revision(client, *, activity=None):
    """Opaque, persisted-state change token for inexpensive detail polling.

    One query reads a fixed set of current/latest semantic records plus their
    latest update timestamps. Scalar values detect bulk updates that omit
    ``updated_at``; aggregate timestamps also detect edits to older records.
    This is deliberately a bounded invalidation hint, not a complete history
    checksum. Clients retain a slower full refresh for edits to arbitrary old
    records that omit all mutation timestamps. Clock-driven labels never enter
    this token. No underlying profile/payment values leave the server.
    """
    from management import ig_bot_models
    from management.models import InstagramBotMessage

    source_names = (
        "IgConversationAnalysisSnapshot", "IgConversationAnalysisJob",
        "IgCommercialEpisode", "IgFollowState", "IgDeal",
        "IgPaymentProjection", "IgPaymentConfirmationReview",
        "IgOrderAssignment", "IgOrderAttribution", "IgCommerceSelectionSession",
        "IgPostSaleCase", "IgFollowUpTask", "HumanReplyCommand",
        "IgCustomerTurnRevision",
    )
    annotations = {}
    for index, name in enumerate(source_names):
        model = getattr(ig_bot_models, name)
        related = model.objects.filter(client_id=OuterRef("pk"))
        # Large immutable snapshots, prompt bodies and media payloads are not
        # copied through every poll. Their persisted revision/update markers
        # and scalar semantic state invalidate the visible card instead.
        fields = {
            field.attname: F(field.attname)
            for field in model._meta.concrete_fields
            if not isinstance(field, (models.TextField, models.JSONField, models.BinaryField))
        }
        # The latest linked physical order may change independently of its
        # attribution/assignment record (including QuerySet.update()).
        order_field = next((field for field in model._meta.concrete_fields
                            if field.name in {"order", "intended_order"}), None)
        if order_field is not None and order_field.is_relation:
            for field in order_field.related_model._meta.concrete_fields:
                if field.name in ORDER_REVISION_FIELDS:
                    fields[f"order_{field.attname}"] = F(f"{order_field.name}__{field.attname}")
        update_field = next((field.name for field in model._meta.concrete_fields
                             if field.name in {"updated_at", "updated"}), None)
        latest_order = [f"-{update_field}", "-id"] if update_field else ["-id"]
        annotations[f"source_{index}"] = Subquery(
            related.order_by(*latest_order).annotate(_marker=JSONObject(**fields)).values("_marker")[:1],
        )
        aggregate_fields = {"count": Count("pk")}
        if update_field:
            aggregate_fields["changed"] = Max(update_field)
        # Actionable tasks/reviews may be older than the newest closed row.
        # Persisted state counts catch their bulk transitions without relying
        # on save signals or a caller remembering to update a timestamp.
        for field in model._meta.concrete_fields:
            if field.name in {"status", "state", "truth", "recovery_state"} and field.choices:
                for choice_index, (value, _label) in enumerate(field.flatchoices):
                    aggregate_fields[f"{field.name}_{choice_index}"] = Count("pk", filter=Q(**{field.name: value}))
        annotations[f"changed_{index}"] = Subquery(
            related.order_by().values("client_id").annotate(_changed=JSONObject(**aggregate_fields)).values("_changed")[:1],
        )
    episode_model = ig_bot_models.IgCommercialEpisode
    episode_fields = {
        field.attname: F(f"current_commercial_episode__{field.attname}")
        for field in episode_model._meta.concrete_fields
        if not isinstance(field, (models.TextField, models.JSONField, models.BinaryField))
    }
    order_model = episode_model._meta.get_field("intended_order").related_model
    for field in order_model._meta.concrete_fields:
        if field.name in ORDER_REVISION_FIELDS:
            episode_fields[f"order_{field.attname}"] = F(
                f"current_commercial_episode__intended_order__{field.attname}",
            )
    annotations["current_episode"] = JSONObject(**episode_fields)
    annotations["latest_message"] = Subquery(
        InstagramBotMessage.objects.filter(client_id=OuterRef("pk")).order_by("-id").annotate(
            _marker=JSONObject(id=F("id"), status=F("status"), send_state=F("send_state"),
                               processed_at=F("processed_at"), private_media_state=F("private_media_state")),
        ).values("_marker")[:1],
    )
    markers = type(client).objects.filter(pk=client.pk).annotate(**annotations).values(*annotations).get()
    persisted_client = {field.attname: getattr(client, field.attname)
                        for field in client._meta.concrete_fields}
    raw = json.dumps({"v": 1, "client": persisted_client, "sources": markers,
                      "activity": activity or activity_payload(client)},
                     cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
