"""Native fulfillment receipts supplement, never rewrite, revision evidence."""
from contextlib import contextmanager

import django.db.models.deletion
from django.db import migrations, models, transaction


@contextmanager
def preserve_sqlite_receipt_triggers(schema_editor):
    """Keep exact local and referencing guards across SQLite table remakes."""
    connection = schema_editor.connection
    if connection.vendor != "sqlite":
        yield
        return
    table = "management_igrestockreceipt"
    # Django remakes this table for each of the three schema operations below.
    # Its own triggers would disappear on DROP; referencing answer triggers
    # would instead survive and make SQLite reject the subsequent RENAME.
    # Keep each remake and its guard restoration in one SQLite transaction,
    # including when this non-atomic migration is being reversed.
    with transaction.atomic(using=connection.alias):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND (tbl_name=%s OR instr(lower(sql), %s)>0) ORDER BY name",
                [table, table],
            )
            triggers = cursor.fetchall()
            for name, _sql in triggers:
                cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
        yield
        with connection.cursor() as cursor:
            for _name, sql in triggers:
                cursor.execute(sql)


class PreserveReceiptTriggersMixin:
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        with preserve_sqlite_receipt_triggers(schema_editor):
            return super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        with preserve_sqlite_receipt_triggers(schema_editor):
            return super().database_backwards(app_label, schema_editor, from_state, to_state)


class PreserveReceiptAlterField(PreserveReceiptTriggersMixin, migrations.AlterField):
    pass


class PreserveReceiptAddField(PreserveReceiptTriggersMixin, migrations.AddField):
    pass


class PreserveReceiptAddConstraint(PreserveReceiptTriggersMixin, migrations.AddConstraint):
    pass


def receipt_guard(apps, schema_editor, *, task_origin=True):
    del apps
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Restock receipt guards require MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    table = "management_igrestockreceipt"
    equal = lambda left, right: f"BINARY {left}=BINARY {right}" if mysql else f"{left}={right}"
    value = lambda expression, path: (f"JSON_UNQUOTE(JSON_EXTRACT({expression},'{path}'))" if mysql
        else f"json_extract({expression},'{path}')")
    hmac_invalid = ("BINARY NEW.envelope_hmac NOT REGEXP '^[0-9a-f]{64}$'" if mysql else
        "(length(NEW.envelope_hmac)<>64 OR NEW.envelope_hmac GLOB '*[^0-9a-f]*')")
    revision = f"""EXISTS (
        SELECT 1 FROM management_igrestockinvitation i
        JOIN management_igclient c ON c.id=i.client_id
        JOIN management_igrevisiondeliveryeffect e ON e.id=NEW.effect_id
        WHERE i.id=NEW.invitation_id AND c.privacy_erasure_started_at IS NULL
          AND e.revision_id=i.revision_id AND e.state='sent'
          AND {equal('e.provider_message_id', 'NEW.provider_message_id')}
          AND NEW.delivered_at<i.acceptance_deadline)"""
    if task_origin:
        pointer = '$."restock_invitation.v1"'
        native = f"""EXISTS (
            SELECT 1 FROM management_igrestockinvitation i
            JOIN management_igclient c ON c.id=i.client_id
            JOIN management_igfollowuptask t ON t.id=NEW.followup_task_id
            WHERE i.id=NEW.invitation_id AND c.privacy_erasure_started_at IS NULL
              AND t.client_id=i.client_id AND t.kind='fulfillment'
              AND t.reason='fulfillment_manager_update' AND t.trigger='event'
              AND t.status='sent' AND t.attempt_count=1 AND t.sent_at IS NOT NULL
              AND t.sent_at=NEW.delivered_at AND NEW.delivered_at>=i.created_at
              AND NEW.delivered_at<i.acceptance_deadline
              AND {equal('t.provider_message_id', 'NEW.provider_message_id')}
              AND {equal('t.message_text', 'i.body')}
              AND REPLACE({value('t.manager_context', pointer+'.invitation_id')},'-','')=REPLACE(CAST(i.id AS CHAR),'-','')
              AND {equal(value('t.manager_context', pointer+'.invitation_hmac'), 'i.envelope_hmac')}
              AND {equal(value('NEW.envelope', '$.task_event_key'), 't.event_key')}
              AND {equal(value('NEW.envelope', '$.task_payload_digest'), value('t.manager_context', pointer+'.payload_digest'))})"""
        origin = f"""((NEW.effect_id IS NOT NULL AND NEW.followup_task_id IS NULL AND {revision})
            OR (NEW.effect_id IS NULL AND NEW.followup_task_id IS NOT NULL AND {native}))"""
    else:
        origin = revision
    invalid = f"{hmac_invalid} OR NEW.key_id='' OR NEW.provider_message_id='' OR NOT {origin}"
    with connection.cursor() as cursor:
        if mysql:
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            if cursor.fetchone() != ("InnoDB",):
                raise RuntimeError("Restock receipts require transactional storage")
        # Only this insert producer changes. Update and privacy-delete guards
        # from0230 remain installed and protect every existing receipt.
        cursor.execute("DROP TRIGGER IF EXISTS ig_ful_rec_insert")
        if mysql:
            cursor.execute(f"CREATE TRIGGER ig_ful_rec_insert BEFORE INSERT ON {table} FOR EACH ROW BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Restock native receipt source guard'; END IF; END")
        else:
            cursor.execute(f"CREATE TRIGGER ig_ful_rec_insert BEFORE INSERT ON {table} WHEN {invalid} BEGIN SELECT RAISE(ABORT,'Restock native receipt source guard'); END")


def restore_revision_guard(apps, schema_editor):
    # A downgrade must not discard native evidence in order to restore NOT NULL.
    Receipt = apps.get_model("management", "IgRestockReceipt")
    if Receipt.objects.filter(followup_task__isnull=False).exists():
        raise RuntimeError("Cannot remove native restock receipts; retain migration0242")
    receipt_guard(apps, schema_editor, task_origin=False)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0241_message_ingress_proof")]
    operations = [
        PreserveReceiptAlterField(model_name="igrestockreceipt", name="effect", field=models.OneToOneField(
            blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.PROTECT,
            related_name="restock_receipt", to="management.igrevisiondeliveryeffect")),
        PreserveReceiptAddField(model_name="igrestockreceipt", name="followup_task", field=models.OneToOneField(
            blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.PROTECT,
            related_name="restock_invitation_receipt", to="management.igfollowuptask")),
        PreserveReceiptAddConstraint(model_name="igrestockreceipt", constraint=models.CheckConstraint(
            condition=(models.Q(effect__isnull=False, followup_task__isnull=True)
                | models.Q(effect__isnull=True, followup_task__isnull=False)),
            name="ig_restock_receipt_one_origin")),
        migrations.RunPython(receipt_guard, restore_revision_guard),
    ]
