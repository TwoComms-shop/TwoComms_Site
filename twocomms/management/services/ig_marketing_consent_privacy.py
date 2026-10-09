"""Erase exact consent owners only within the audited privacy fence."""
from django.db import connections


def purge_marketing_consent_data(client_ids, *, using="default"):
    from management.ig_bot_models import IgClient
    from management.ig_consent_models import IgMarketingConsentAnswer, IgMarketingConsentInvitation

    ids = sorted({int(value) for value in client_ids})
    if not ids:
        return
    connection = connections[using]
    if not connection.in_atomic_block or any(value <= 0 for value in ids):
        raise ValueError("Consent erasure requires the atomic privacy phase")
    fenced = list(IgClient.objects.using(using).select_for_update().filter(
        pk__in=ids, privacy_erasure_started_at__isnull=False,
    ).order_by("pk").values_list("pk", flat=True))
    if fenced != ids:
        raise ValueError("Consent erasure requires every exact persisted owner fence")
    invitations = list(IgMarketingConsentInvitation.objects.using(using).select_for_update().filter(
        client_id__in=ids,
    ).order_by("pk").values_list("pk", flat=True))
    if not invitations:
        return
    prepared = [IgMarketingConsentInvitation._meta.pk.get_db_prep_value(value, connection)
                for value in invitations]
    placeholders = ", ".join(["%s"] * len(prepared))
    with connection.cursor() as cursor:
        for model, column in ((IgMarketingConsentAnswer, "invitation_id"),
                              (IgMarketingConsentInvitation, "id")):
            cursor.execute(
                f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} "
                f"WHERE {connection.ops.quote_name(column)} IN ({placeholders})", prepared,
            )
