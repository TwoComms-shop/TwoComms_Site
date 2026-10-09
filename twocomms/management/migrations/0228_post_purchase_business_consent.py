"""One purpose's receipt-bound invitations; no historical opt-in backfill."""
import uuid

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone

INVITATION_TABLE = "management_igmarketingconsentinvitation"
ANSWER_TABLE = "management_igmarketingconsentanswer"
IMMUTABLE = (
    "client_id", "order_id", "assignment_id", "assignment_version", "reset_audit_id",
    "purpose", "recipient_igsid", "provider_namespace", "settings_id_snapshot",
    "locale", "language_proof", "source_message_id", "source_digest", "payment_digest",
    "message_snapshot", "accept_payload", "decline_payload", "revoke_payload",
    "expires_at", "issued_at", "provider_basis", "native_grant", "signing_key_id",
    "snapshot_hmac", "created_at",
)


def install_consent_guards(apps, schema_editor):
    del apps
    vendor = schema_editor.connection.vendor
    if vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Consent physical guards require MariaDB or SQLite")
    names = ("ig_consent_inv_insert", "ig_consent_inv_transition", "ig_consent_inv_delete",
             "ig_consent_ans_insert", "ig_consent_ans_update", "ig_consent_ans_delete")
    mysql = vendor == "mysql"
    eq = lambda left, right: f"{left} <=> {right}" if mysql else f"{left} IS {right}"
    changed = " OR ".join(f"NOT ({eq('OLD.' + field, 'NEW.' + field)})" for field in IMMUTABLE)
    hex_shape = lambda field: f"{field} NOT REGEXP '^[0-9a-f]{{64}}$'" if mysql else f"(length({field})<>64 OR {field} GLOB '*[^0-9a-f]*')"
    expiry = "NEW.expires_at <> DATE_ADD(NEW.issued_at, INTERVAL 90 DAY)" if mysql else "datetime(NEW.expires_at) <> datetime(NEW.issued_at, '+90 days')"
    insert_invalid = f"""NEW.state<>'pending' OR NEW.attempts<>0 OR NEW.provider_message_id<>''
        OR NEW.receipt_hmac<>'' OR NEW.sent_at IS NOT NULL OR NEW.lease_token<>'' OR NEW.lease_until IS NOT NULL
        OR NEW.purpose<>'post_purchase_marketing' OR NEW.provider_basis<>'standard_window'
        OR NEW.native_grant<>'unverified' OR NEW.locale NOT IN ('uk','ru','en')
        OR {expiry} OR {hex_shape('NEW.snapshot_hmac')} OR {hex_shape('NEW.source_digest')}
        OR {hex_shape('NEW.payment_digest')} OR length(NEW.signing_key_id)<>24
        OR NOT EXISTS (SELECT 1 FROM management_igclient c JOIN orders_order o ON o.id=NEW.order_id
            JOIN management_igorderassignment a ON a.id=NEW.assignment_id
            JOIN management_instagrambotmessage m ON m.id=NEW.source_message_id
            WHERE c.id=NEW.client_id AND c.privacy_erasure_started_at IS NULL AND c.hidden_at IS NULL
              AND c.igsid=NEW.recipient_igsid AND o.payment_status='paid' AND o.status<>'cancelled'
              AND a.order_id=o.id AND a.client_id=c.id AND a.version=NEW.assignment_version AND a.unassigned_at IS NULL
              AND m.client_id=c.id AND m.sender_id=NEW.recipient_igsid AND m.role='user'
              AND m.source IN ('webhook','poll') AND m.provider_namespace=NEW.provider_namespace AND m.mid IS NOT NULL AND m.mid<>''
              AND NEW.reset_audit_id=COALESCE((SELECT MAX(r.id) FROM management_igfunnelresetaudit r WHERE r.client_id=c.id),0))"""
    transition_invalid = f"""({changed}) OR NEW.attempts<OLD.attempts OR NEW.attempts>OLD.attempts+1
        OR (OLD.state='pending' AND NEW.state NOT IN ('pending','processing','failed'))
        OR (OLD.state='processing' AND NEW.state NOT IN ('processing','sent','unknown','failed'))
        OR (OLD.state IN ('sent','unknown','failed') AND (NEW.state<>OLD.state
            OR NOT ({eq('NEW.provider_message_id','OLD.provider_message_id')})
            OR NOT ({eq('NEW.receipt_hmac','OLD.receipt_hmac')}) OR NOT ({eq('NEW.sent_at','OLD.sent_at')})))
        OR (NEW.state='processing' AND (NEW.lease_token='' OR NEW.lease_until IS NULL OR NEW.provider_started_at IS NULL))
        OR (NEW.state='sent' AND (NEW.provider_message_id='' OR NEW.sent_at IS NULL OR {hex_shape('NEW.receipt_hmac')}))"""
    answer_invalid = f"""NEW.choice NOT IN ('accept','decline','revoke') OR NEW.source_mid=''
        OR {hex_shape('NEW.source_digest')} OR {hex_shape('NEW.answer_hmac')} OR length(NEW.signing_key_id)<>24
        OR NOT EXISTS (SELECT 1 FROM {INVITATION_TABLE} i
            JOIN management_igclient c ON c.id=i.client_id JOIN management_instagrambotmessage m ON m.id=NEW.source_message_id
            WHERE i.id=NEW.invitation_id AND c.privacy_erasure_started_at IS NULL
              AND i.state='sent' AND i.provider_message_id<>'' AND i.sent_at IS NOT NULL AND i.receipt_hmac<>''
              AND m.client_id=i.client_id AND m.sender_id=i.recipient_igsid AND m.role='user'
              AND m.source IN ('webhook','poll') AND m.mid=NEW.source_mid
              AND m.provider_namespace=i.provider_namespace AND NEW.provider_namespace=i.provider_namespace
              AND NEW.signing_key_id=i.signing_key_id AND NEW.answered_at=COALESCE(m.provider_created_at,m.created_at)
              AND NEW.answered_at>=i.issued_at AND NEW.answered_at<i.expires_at
              AND m.quick_reply_payload=CASE NEW.choice WHEN 'accept' THEN i.accept_payload WHEN 'decline' THEN i.decline_payload ELSE i.revoke_payload END)"""
    invite_delete_invalid = "NOT EXISTS (SELECT 1 FROM management_igclient c WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"
    answer_delete_invalid = f"NOT EXISTS (SELECT 1 FROM {INVITATION_TABLE} i JOIN management_igclient c ON c.id=i.client_id WHERE i.id=OLD.invitation_id AND c.privacy_erasure_started_at IS NOT NULL)"
    guards = (
        (names[0], INVITATION_TABLE, "INSERT", insert_invalid),
        (names[1], INVITATION_TABLE, "UPDATE", transition_invalid),
        (names[2], INVITATION_TABLE, "DELETE", invite_delete_invalid),
        (names[3], ANSWER_TABLE, "INSERT", answer_invalid),
        (names[4], ANSWER_TABLE, "UPDATE", "1=1"),
        (names[5], ANSWER_TABLE, "DELETE", answer_delete_invalid),
    )
    with schema_editor.connection.cursor() as cursor:
        for name, table, event, invalid in guards:
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            if mysql:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Consent source or immutable guard'; END IF; END")
            else:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {invalid} BEGIN SELECT RAISE(ABORT, 'Consent source or immutable guard'); END")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0227_ugc_review_delivery_generations")]
    operations = [
        migrations.CreateModel(name="IgMarketingConsentInvitation", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("assignment_version", models.PositiveIntegerField()),
            ("reset_audit_id", models.PositiveBigIntegerField(default=0)),
            ("purpose", models.CharField(max_length=32, default="post_purchase_marketing")),
            ("recipient_igsid", models.CharField(max_length=64)),
            ("provider_namespace", models.CharField(max_length=128)),
            ("settings_id_snapshot", models.PositiveIntegerField()),
            ("locale", models.CharField(max_length=2)),
            ("language_proof", models.JSONField(default=dict)),
            ("source_digest", models.CharField(max_length=64)),
            ("payment_digest", models.CharField(max_length=64)),
            ("message_snapshot", models.TextField()),
            ("accept_payload", models.CharField(max_length=1000)),
            ("decline_payload", models.CharField(max_length=1000)),
            ("revoke_payload", models.CharField(max_length=1000)),
            ("expires_at", models.DateTimeField()),
            ("issued_at", models.DateTimeField()),
            ("provider_basis", models.CharField(max_length=32, default="standard_window")),
            ("native_grant", models.CharField(max_length=16, default="unverified")),
            ("signing_key_id", models.CharField(max_length=24)),
            ("snapshot_hmac", models.CharField(max_length=64)),
            ("state", models.CharField(max_length=16, default="pending", db_index=True)),
            ("lease_token", models.CharField(max_length=64, blank=True, default="")),
            ("lease_until", models.DateTimeField(null=True, blank=True)),
            ("attempts", models.PositiveSmallIntegerField(default=0)),
            ("provider_started_at", models.DateTimeField(null=True, blank=True)),
            ("provider_message_id", models.CharField(max_length=255, blank=True, default="")),
            ("receipt_hmac", models.CharField(max_length=64, blank=True, default="")),
            ("sent_at", models.DateTimeField(null=True, blank=True)),
            ("last_error", models.CharField(max_length=80, blank=True, default="")),
            ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("client", models.ForeignKey(to="management.igclient", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="marketing_consent_invitations")),
            ("order", models.ForeignKey(to="orders.order", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="marketing_consent_invitations")),
            ("assignment", models.ForeignKey(to="management.igorderassignment", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="marketing_consent_invitations")),
            ("source_message", models.ForeignKey(to="management.instagrambotmessage", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="marketing_consent_invitations")),
        ], options={"constraints": [
            models.UniqueConstraint(fields=["client", "order", "assignment", "assignment_version", "reset_audit_id", "purpose"], name="ig_consent_scope_once"),
            models.CheckConstraint(condition=models.Q(purpose="post_purchase_marketing", provider_basis="standard_window", native_grant="unverified", locale__in=["uk", "ru", "en"]), name="ig_consent_policy_shape"),
            models.CheckConstraint(condition=models.Q(state__in=["pending", "processing", "sent", "unknown", "failed"]), name="ig_consent_state_shape"),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F("issued_at")), name="ig_consent_expiry_order"),
            models.CheckConstraint(condition=~models.Q(state="sent") | (~models.Q(provider_message_id="") & models.Q(sent_at__isnull=False)), name="ig_consent_sent_receipt"),
        ], "indexes": [models.Index(fields=["state", "issued_at"], name="ig_consent_due")]}),
        migrations.CreateModel(name="IgMarketingConsentAnswer", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("provider_namespace", models.CharField(max_length=128)),
            ("source_mid", models.CharField(max_length=255)),
            ("choice", models.CharField(max_length=16)),
            ("source_digest", models.CharField(max_length=64)),
            ("answered_at", models.DateTimeField()),
            ("recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
            ("signing_key_id", models.CharField(max_length=24)),
            ("answer_hmac", models.CharField(max_length=64)),
            ("invitation", models.ForeignKey(to="management.igmarketingconsentinvitation", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="answers")),
            ("source_message", models.ForeignKey(to="management.instagrambotmessage", on_delete=django.db.models.deletion.DO_NOTHING, db_constraint=False, related_name="marketing_consent_answers")),
        ], options={"constraints": [
            models.UniqueConstraint(fields=["provider_namespace", "source_mid"], name="ig_consent_answer_source"),
            models.CheckConstraint(condition=models.Q(choice__in=["accept", "decline", "revoke"]), name="ig_consent_answer_choice"),
        ], "indexes": [models.Index(fields=["invitation", "-answered_at", "-id"], name="ig_consent_answer_latest")]}),
        migrations.RunPython(install_consent_guards, reverse_code=None),
    ]
