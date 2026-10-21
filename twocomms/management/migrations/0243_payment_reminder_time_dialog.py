"""Append-only ownership evidence for a requested payment-reminder time."""
import uuid

import django.db.models.deletion
from django.db import migrations, models


TABLE = "management_igpaymentremindertimerequest"


def install_guards(apps, schema_editor):
    del apps
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Payment reminder time evidence requires MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    invalid_hmac = ("CAST(NEW.envelope_hmac AS BINARY) NOT REGEXP '^[0-9a-f]{64}$'" if mysql
        else "(length(NEW.envelope_hmac)<>64 OR NEW.envelope_hmac GLOB '*[^0-9a-f]*')")
    namespace_match = ("CAST(s.source_namespace AS BINARY)=CAST(m.provider_namespace AS BINARY)" if mysql
        else "s.source_namespace=m.provider_namespace")
    insert = f"""{invalid_hmac} OR NEW.locale NOT IN ('uk','ru','en')
        OR NEW.body='' OR NEW.expires_at<=NEW.created_at
        OR NEW.expires_at>NEW.window_deadline
        OR NOT EXISTS (SELECT 1 FROM management_igclient c
            JOIN management_instagrambotmessage m ON m.id=NEW.request_source_id
            JOIN management_igcustomerturnrevision r ON r.id=NEW.revision_id
            JOIN management_igturnrevisionsource s ON s.revision_id=r.id AND s.message_id=m.id
            WHERE c.id=NEW.client_id AND c.privacy_erasure_started_at IS NULL
              AND r.client_id=c.id AND m.client_id=c.id AND m.sender_id=c.igsid
              AND m.role='user' AND s.role='user' AND m.source IN ('webhook','poll')
              AND {namespace_match}
              AND m.mid IS NOT NULL AND m.mid<>'' AND m.provider_created_at IS NOT NULL
              AND NEW.expires_at>m.provider_created_at)"""
    privacy = """EXISTS (SELECT 1 FROM management_igclient c
        WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"""
    with connection.cursor() as cursor:
        for event, invalid in (("insert", insert), ("update", "1=1"), ("delete", "NOT " + privacy)):
            name = f"ig_paytime_{event}"
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            if mysql:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {TABLE} FOR EACH ROW BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Payment reminder time immutable/source guard'; END IF; END")
            else:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {TABLE} WHEN {invalid} BEGIN SELECT RAISE(ABORT,'Payment reminder time immutable/source guard'); END")


def remove_guards(apps, schema_editor):
    del apps
    with schema_editor.connection.cursor() as cursor:
        for event in ("insert", "update", "delete"):
            cursor.execute(f"DROP TRIGGER IF EXISTS ig_paytime_{event}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0242_restock_followup_receipts")]
    operations = [
        migrations.CreateModel(
            name="IgPaymentReminderTimeRequest",
            fields=[
                ("envelope", models.JSONField(default=dict)),
                ("envelope_hmac", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("expires_at", models.DateTimeField()),
                ("window_deadline", models.DateTimeField()),
                ("locale", models.CharField(max_length=2)),
                ("body", models.TextField()),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="payment_reminder_time_requests", to="management.igclient")),
                ("request_source", models.OneToOneField(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="payment_reminder_time_request", to="management.instagrambotmessage")),
                ("revision", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="payment_reminder_time_requests", to="management.igcustomerturnrevision")),
            ],
            options={"indexes": [models.Index(fields=["client", "expires_at"], name="ig_paytime_client_expiry")]},
        ),
        migrations.RunPython(install_guards, remove_guards),
    ]
