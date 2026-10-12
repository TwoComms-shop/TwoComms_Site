"""Make exact stock reconciliation transactional and preserve its evidence."""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


TABLE = "management_igfulfillmentstocksync"
VARIANT_TABLE = "productcolors_productcolorvariant"
DECISION_TABLE = "management_igfulfillmentreviewdecision"
REQUEST_TABLE = "management_igfulfillmentreviewrequest"
TRIGGER_PREFIX = "ig_ful_sync"


def _require_innodb(connection, table):
    """Change only the engine; never recreate or rewrite the catalogue schema."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table],
        )
        row = cursor.fetchone()
        if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
            raise RuntimeError(f"Unexpected stock reconciliation table/engine: {table}: {row}")
        if row[0] == "MyISAM":
            cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
        cursor.execute(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table],
        )
        if cursor.fetchone() != ("InnoDB",):
            raise RuntimeError(f"Stock reconciliation requires InnoDB: {table}")


def ensure_variant_transactions(apps, schema_editor):
    del apps
    connection = schema_editor.connection
    if connection.vendor == "mysql":
        _require_innodb(connection, VARIANT_TABLE)
    elif connection.vendor != "sqlite":
        raise RuntimeError("Stock reconciliation requires MariaDB or SQLite")


def install_guards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Stock reconciliation evidence requires MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    if mysql:
        _require_innodb(connection, TABLE)
    user_table = connection.ops.quote_name(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)

    def extracted(path):
        return (f"JSON_UNQUOTE(JSON_EXTRACT(NEW.envelope, '$.{path}'))" if mysql
                else f"json_extract(NEW.envelope, '$.{path}')")

    def exact(left, right):
        return f"BINARY {left}=BINARY {right}" if mysql else f"{left}={right}"

    def uuid_matches(path, column):
        # MariaDB may expose UUID as native UUID or CHAR(32); SQLite uses
        # CHAR(32). The canonical signed JSON always uses hyphenated UUIDs.
        value = extracted(path)
        cast = f"CAST({column} AS CHAR)" if mysql else f"CAST({column} AS TEXT)"
        return exact(f"REPLACE({value}, '-', '')", f"REPLACE({cast}, '-', '')")

    hmac_invalid = ("BINARY NEW.envelope_hmac NOT REGEXP '^[0-9a-f]{64}$'" if mysql else
                    "(length(NEW.envelope_hmac)<>64 OR NEW.envelope_hmac GLOB '*[^0-9a-f]*')")
    columns = " AND ".join((
        uuid_matches("columns.id", "NEW.id"),
        uuid_matches("columns.decision_id", "NEW.decision_id"),
        uuid_matches("columns.operation_id", "NEW.operation_id"),
        f"{extracted('columns.client_id')}=NEW.client_id",
        f"{extracted('columns.actor_id')}=NEW.actor_id",
        exact(extracted("columns.key_id"), "NEW.key_id"),
        exact(extracted("decision_digest"), "d.envelope_hmac"),
    ))
    insert = f"""{hmac_invalid} OR NEW.key_id='' OR NOT EXISTS (
        SELECT 1 FROM {DECISION_TABLE} d
          JOIN {REQUEST_TABLE} r ON r.id=d.request_id
          JOIN management_igclient c ON c.id=r.client_id
          JOIN {user_table} u ON u.id=NEW.actor_id
        WHERE d.id=NEW.decision_id AND c.id=NEW.client_id
          AND c.privacy_erasure_started_at IS NULL AND u.is_active=1
          AND {columns})"""
    privacy = """EXISTS (SELECT 1 FROM management_igclient c
        WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"""
    with connection.cursor() as cursor:
        for event, invalid in (("insert", insert), ("update", "1=1"), ("delete", "NOT " + privacy)):
            name = f"{TRIGGER_PREFIX}_{event}"
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            if mysql:
                cursor.execute(
                    f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {TABLE} FOR EACH ROW "
                    f"BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT='Fulfilment stock immutable/owner/privacy guard'; END IF; END"
                )
            else:
                cursor.execute(
                    f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {TABLE} WHEN {invalid} "
                    "BEGIN SELECT RAISE(ABORT,'Fulfilment stock immutable/owner/privacy guard'); END"
                )


def remove_guards(apps, schema_editor):
    del apps
    with schema_editor.connection.cursor() as cursor:
        for event in ("insert", "update", "delete"):
            cursor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_PREFIX}_{event}")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("management", "0230_restock_and_fulfillment_review"),
        ("productcolors", "0007_normalize_slugs"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # InnoDB is retained on reversal: returning stock to MyISAM would
        # silently remove transaction and row-lock guarantees.
        migrations.RunPython(ensure_variant_transactions, migrations.RunPython.noop),
        migrations.CreateModel(
            name="IgFulfillmentStockSync",
            fields=[
                ("envelope", models.JSONField(default=dict)),
                ("key_id", models.CharField(max_length=24)),
                ("envelope_hmac", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("operation_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("actor", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="fulfillment_stock_syncs", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="fulfillment_stock_syncs", to="management.igclient")),
                ("decision", models.OneToOneField(db_constraint=False, on_delete=django.db.models.deletion.PROTECT,
                    related_name="stock_sync", to="management.igfulfillmentreviewdecision")),
            ],
            options={"abstract": False},
        ),
        migrations.RunPython(install_guards, remove_guards),
    ]
