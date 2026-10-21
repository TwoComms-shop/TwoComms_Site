"""Canonical source ownership shared by explicit customer agreements."""
from management.services.ig_turn_revisions import _digest, _source_payload


def owned_source(revision, source, settings_obj, *, require_head=True):
    from management.models import IgCustomerTurnRevision, InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_ingress_source_proof import is_current_customer_source
    from management.services.instagram_bot import ingress_provider_namespace
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision.pk).first()
    if not revision or not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        return None, "revision_unsealed"
    if require_head and (revision.active_slot != 1 or revision.state not in {"sealed", "claimed", "processed"} or revision.origin != "inbound"):
        return None, "revision_not_current"
    row = InstagramBotMessage.objects.select_related("client").filter(pk=getattr(source, "message_id", None) or source.pk).first()
    # A current authenticated poll is the same customer ingress as a webhook.
    # Its label alone cannot establish ownership: validate the signed provider
    # identity/content proof before checking the sealed revision below.
    if (not row or not is_current_customer_source(row, namespace=ingress_provider_namespace(settings_obj))
        or row.status == "failed" or row.client_id != revision.client_id):
        return None, "source_not_owned"
    client = revision.client
    floor = conversation_route_reset_floor(client.pk)
    frozen = revision.sources.filter(message_id=row.pk).first()
    snapshot = next((item for item in revision.bundle_snapshot.get("sources", ()) if item.get("message_id") == row.pk), None)
    if (not frozen or not snapshot or row.pk < floor or row.sender_id != client.igsid
        or not row.mid or not row.provider_created_at or row.provider_created_at.tzinfo is None
        or row.provider_namespace != ingress_provider_namespace(settings_obj)
        or row.provider_namespace != frozen.source_namespace
        or snapshot.get("source_digest") != frozen.source_digest
        or _source_payload(row, previous=frozen, ordinal=frozen.ordinal)["source_digest"] != frozen.source_digest
        or revision.permission_epoch != client.reply_permission_epoch
        or revision.erasure_started_at_snapshot != client.privacy_erasure_started_at):
        return None, "source_changed"
    proof = {"message_id": row.pk, "digest": frozen.source_digest, "revision_id": revision.pk,
        "revision_digest": revision.snapshot_digest, "namespace": row.provider_namespace,
        "mid": row.mid, "provider_created_at": row.provider_created_at.isoformat(), "reset_floor": floor}
    return (revision, row, proof), ""


def latest_customer_source(client, settings_obj, *, now, after_id=0, pending_only=False):
    """A real post-reset source, never the CRM last-message cache."""
    from management.models import InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.instagram_bot import ingress_provider_namespace
    rows = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
        role="user", source__in=["webhook", "poll"], provider_namespace=ingress_provider_namespace(settings_obj),
        pk__gte=conversation_route_reset_floor(client.pk), pk__gt=after_id).exclude(status="failed")
    if pending_only:
        return rows.filter(status__in=["pending", "processing"]).order_by("-pk").first()
    rows = rows.filter(provider_created_at__lte=now).exclude(mid="").exclude(status="ignored")
    return rows.order_by("-provider_created_at", "-pk").first()
