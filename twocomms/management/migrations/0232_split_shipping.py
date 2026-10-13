"""Immutable split-shipping authority and transactional partial stock fulfillment."""
import re
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from ._resumable_schema import _columns, _constraints, _validate_field


def _normalize_type(value):
    value = re.sub(r"\s+", "", str(value).lower())
    value = value.replace("integer", "int").replace("numeric", "decimal").replace("auto_increment", "")
    return re.sub(r"(bigint|smallint|tinyint|int)\([0-9]+\)", r"\1", value)


def _validate_existing_field(schema_editor, table, field, column):
    """Do not mistake a same-name incompatible column for completed DDL."""
    physical = field.target_field if field.is_relation else field
    if physical.get_internal_type() == "UUIDField":
        # Native MariaDB UUID metadata exposes a driver byte buffer length
        # (144 on the supported runtime), not UUIDField's CHAR(32) width.
        # Verify actual storage below through INFORMATION_SCHEMA instead of
        # applying a character-length rule to this native binary type.
        if bool(column.null_ok) != bool(field.null):
            raise RuntimeError(f"{table}.{field.column} has incompatible UUID nullability")
        if schema_editor.connection.vendor == "sqlite" and _normalize_type(column.type_code) != "char(32)":
            raise RuntimeError(f"{table}.{field.column} has incompatible UUID storage")
    else:
        _validate_field(schema_editor, table, field, column)
    # ADD COLUMN may have committed its temporary application default before
    # Django dropped that default. It is safe to resume only when it matches
    # the intended constant; an unrelated persistent default must fail closed.
    default = getattr(column, "default", None)
    if default not in (None, "NULL") and not field.has_db_default():
        if not field.has_default() or callable(field.default) or str(default).strip("'\"") != str(field.default):
            raise RuntimeError(f"{table}.{field.column} has an incompatible database default")
    connection = schema_editor.connection
    if connection.vendor == "mysql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s", [table, field.column])
            actual = cursor.fetchone()
        expected_type = _normalize_type(physical.db_type(connection))
        actual_type = _normalize_type(actual[0]) if actual else ""
        # MariaDB's JSON alias is LONGTEXT plus JSON_VALID, verified by the
        # shared introspection validator above rather than by the alias name.
        if not (expected_type == "json" and actual_type == "longtext") and actual_type != expected_type:
            raise RuntimeError(f"{table}.{field.column} has incompatible physical type: {actual_type}, expected {expected_type}")


def _validate_unique(actual, columns, name):
    if (not actual.get("unique") or actual.get("primary_key") or actual.get("check")
        or actual.get("foreign_key") or list(actual.get("columns") or ()) != list(columns)):
        raise RuntimeError(f"{name} has an incompatible unique constraint")


def _normalized_check(value):
    return re.sub(r"[\s()\"`\[\]]", "", value).lower()


def _check_clause(schema_editor, table, name):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        if connection.vendor == "mysql":
            cursor.execute("SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s", [table, name])
            row = cursor.fetchone()
            return row[0] if row else None
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", [table])
        row = cursor.fetchone()
    sql = row[0] if row else ""
    match = re.search(r"CONSTRAINT\s+[\"`\[]?" + re.escape(name) + r"[\"`\]]?\s+CHECK\s*\(", sql, re.I)
    if not match:
        return None
    start, depth = match.end(), 1
    for index in range(start, len(sql)):
        depth += (sql[index] == "(") - (sql[index] == ")")
        if depth == 0:
            return sql[start:index]
    return None


def _validate_check(schema_editor, model, constraint, actual):
    if (not actual.get("check") or actual.get("unique") or actual.get("primary_key")
        or actual.get("foreign_key") or actual.get("index")):
        raise RuntimeError(f"{constraint.name} has an incompatible constraint type")
    # This migration owns one simple column-to-column bound. Comparing the
    # actual clause also rejects a same-name CHECK(quantity > 0), which column
    # introspection alone cannot distinguish from the required bound.
    expected = "fulfilled_quantity<=quantity"
    clause = _check_clause(schema_editor, model._meta.db_table, constraint.name)
    if clause is None or _normalized_check(clause) != expected:
        raise RuntimeError(f"{constraint.name} has an incompatible check expression")


class ResumableAddField(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.name)
        existing = _columns(schema_editor, model._meta.db_table).get(field.column)
        if existing is not None:
            _validate_existing_field(schema_editor, model._meta.db_table, field, existing)
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class ResumableAddConstraint(migrations.AddConstraint):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        existing = _constraints(schema_editor, model._meta.db_table).get(self.constraint.name)
        if existing is not None:
            _validate_check(schema_editor, model, self.constraint, existing)
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class ResumableCreateModel(migrations.CreateModel):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        table = model._meta.db_table
        if table not in schema_editor.connection.introspection.table_names():
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        columns, constraints = _columns(schema_editor, table), _constraints(schema_editor, table)
        if set(columns) != {field.column for field in model._meta.local_fields}:
            raise RuntimeError(f"{table} has incompatible columns")
        for field in model._meta.local_fields:
            _validate_existing_field(schema_editor, table, field, columns[field.column])
            shapes = [entry for entry in constraints.values() if list(entry.get("columns") or ()) == [field.column]]
            if field.primary_key and not any(entry.get("primary_key") for entry in shapes):
                raise RuntimeError(f"{table}.{field.column} is missing its primary key")
            if field.unique and not field.primary_key and not any(
                entry.get("unique") and not entry.get("primary_key") and not entry.get("check")
                and not entry.get("foreign_key") for entry in shapes
            ):
                # Django may defer indexes past CREATE TABLE. On interrupted
                # DDL the table survives but those deferred statements do not.
                name = schema_editor._create_index_name(table, [field.column], suffix="_uniq")
                if name in constraints:
                    _validate_unique(constraints[name], [field.column], name)
                schema_editor.add_constraint(model, models.UniqueConstraint(fields=[field.name], name=name))
            elif field.db_index and not field.unique and not any(entry.get("index") or entry.get("unique") for entry in shapes):
                schema_editor.add_index(model, models.Index(fields=[field.name],
                    name=schema_editor._create_index_name(table, [field.column])))
        for constraint in model._meta.constraints:
            existing = constraints.get(constraint.name)
            if existing is None:
                schema_editor.add_constraint(model, constraint)
            elif isinstance(constraint, models.UniqueConstraint):
                _validate_unique(existing, [model._meta.get_field(name).column for name in constraint.fields], constraint.name)
            else:
                raise RuntimeError(f"Unexpected split model constraint: {constraint.name}")


TABLES = {
    "offer": "management_igsplitshippingoffer",
    "receipt": "management_igsplitshippingreceipt",
    "choice": "management_igsplitshippingchoice",
    "plan": "management_igsplitorderplan",
    "parcel": "management_igsplitparcel",
    "dispatch": "management_igsplitparceldispatch",
    "tracking": "management_igsplitparceltracking",
}
# These are the rows locked/written by the split fulfillment transaction.
TRANSACTION_TABLES = (
    *TABLES.values(), "management_igclient", "management_igfulfillmentreviewrequest",
    "management_igfulfillmentreviewdecision", "management_igfollowuptask",
    "management_igcheckoutinventoryreservation", "management_igcheckoutproposal",
    "management_igorderassignment", "management_igorderattribution",
    "management_igordershipment", "productcolors_productcolorvariant",
    "warehouse_stockitem", "warehouse_stockmovement", "warehouse_writeoffrequest",
    "orders_order", "orders_orderitem",
)


def _require_innodb(connection, table):
    if table not in TRANSACTION_TABLES:
        raise RuntimeError(f"Split shipping engine conversion is outside its owned tables: {table}")
    with connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
            raise RuntimeError(f"Unexpected split-shipping table/engine: {table}: {row}")
        if row[0] == "MyISAM":
            cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if cursor.fetchone() != ("InnoDB",):
            raise RuntimeError(f"Split shipping requires InnoDB: {table}")


def ensure_transactions(apps, schema_editor):
    del apps
    connection = schema_editor.connection
    if connection.vendor == "mysql":
        # Validate the complete finite scope before the first ALTER. A later
        # interruption can then resume safely: every completed conversion is
        # already InnoDB and will be verified without another mutation.
        with connection.cursor() as cursor:
            for table in TRANSACTION_TABLES:
                cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                row = cursor.fetchone()
                if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
                    raise RuntimeError(f"Unexpected split-shipping table/engine: {table}: {row}")
        for table in TRANSACTION_TABLES:
            _require_innodb(connection, table)
    elif connection.vendor != "sqlite":
        raise RuntimeError("Split shipping requires MariaDB or SQLite")


def initialize_fulfilled_quantities(apps, schema_editor):
    reservation = apps.get_model("management", "IgCheckoutInventoryReservation")
    reservation.objects.using(schema_editor.connection.alias).filter(
        state__in=["fulfilled", "consumed"],
    ).update(fulfilled_quantity=models.F("quantity"))


def install_guards(apps, schema_editor):
    """Also callable by the isolated native MariaDB migration verifier."""
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Split shipping evidence requires MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    offer, receipt, choice, plan, parcel, dispatch, tracking = TABLES.values()
    user = connection.ops.quote_name(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)

    def extracted(path, envelope="NEW.envelope"):
        return (f"JSON_UNQUOTE(JSON_EXTRACT({envelope}, '$.{path}'))" if mysql
            else f"json_extract({envelope}, '$.{path}')")

    def exact(left, right):
        return f"BINARY {left}=BINARY {right}" if mysql else f"{left}={right}"

    def date_value(value, *, database=False):
        if mysql:
            if database:
                return value
            return (f"CASE WHEN SUBSTRING({value},20,1)='.' THEN "
                f"STR_TO_DATE(REPLACE(SUBSTRING({value},1,26),'T',' '),'%Y-%m-%d %H:%i:%s.%f') "
                f"ELSE STR_TO_DATE(REPLACE(SUBSTRING({value},1,19),'T',' '),'%Y-%m-%d %H:%i:%s') END")
        normalized = f"CAST({value} AS TEXT)" if database else f"REPLACE(REPLACE({value},'T',' '),'+00:00','')"
        return (f"SUBSTR({normalized},1,19)||'.'||SUBSTR(CASE WHEN LENGTH({normalized})>19 "
            f"THEN SUBSTR({normalized},21) ELSE '' END||'000000',1,6)")

    def match(name, kind="scalar", nullable=False):
        value, column = extracted("columns." + name), "NEW." + name
        if kind == "uuid":
            cast = f"CAST({column} AS CHAR)" if mysql else f"CAST({column} AS TEXT)"
            condition = exact(f"REPLACE({value}, '-', '')", f"REPLACE({cast}, '-', '')")
        elif kind == "text":
            condition = exact(value, column)
        elif kind == "date":
            condition = f"({date_value(value)})=({date_value(column, database=True)})"
        elif kind == "decimal":
            canonical = f"CAST({column} AS CHAR)" if mysql else f"printf('%.2f',{column})"
            condition = exact(value, canonical)
        else:
            condition = f"{value}={column}"
        if nullable:
            # Missing columns must not masquerade as a signed JSON null.
            present = (f"JSON_CONTAINS_PATH(NEW.envelope,'one','$.columns.{name}')=1" if mysql
                else f"json_type(NEW.envelope,'$.columns.{name}') IS NOT NULL")
            json_null = (f"JSON_TYPE(JSON_EXTRACT(NEW.envelope,'$.columns.{name}'))='NULL'" if mysql
                else f"json_type(NEW.envelope,'$.columns.{name}')='null'")
            condition = f"({present} AND (({column} IS NULL AND {json_null}) OR ({column} IS NOT NULL AND {condition})))"
        return f"COALESCE(({condition}),0)=1"

    signed = {
        "offer": [("id", "uuid"), ("decision_id", "uuid"), ("client_id", "scalar"),
            ("actor_id", "scalar"), ("operation_id", "uuid"), ("version", "scalar"),
            ("task_id", "scalar", True), ("expires_at", "date")],
        "receipt": [("id", "uuid"), ("offer_id", "uuid"), ("task_id", "scalar"), ("native_mid", "text")],
        "choice": [("id", "uuid"), ("offer_id", "uuid"), ("receipt_id", "uuid"),
            ("client_id", "scalar"), ("source_id", "scalar"), ("revision_id", "scalar", True), ("option", "text")],
        "plan": [("id", "uuid"), ("choice_id", "uuid"), ("order_id", "scalar"), ("client_id", "scalar")],
        "parcel": [("id", "uuid"), ("plan_id", "uuid"), ("key", "text")],
        "dispatch": [("id", "uuid"), ("parcel_id", "uuid"), ("actor_id", "scalar"),
            ("operation_id", "uuid"), ("shipment_id", "scalar"), ("tracking_number", "text"),
            ("customer_delivery_amount", "decimal"), ("cod_amount", "decimal")],
    }
    hmac_invalid = ("BINARY NEW.envelope_hmac NOT REGEXP '^[0-9a-f]{64}$'" if mysql else
        "(length(NEW.envelope_hmac)<>64 OR NEW.envelope_hmac GLOB '*[^0-9a-f]*')")
    common = f"{hmac_invalid} OR NEW.key_id=''"
    envelope_valid = f"{extracted('version')}=1 AND {exact(extracted('purpose'), chr(39)+'split-shipping.v1'+chr(39))}"
    guards = {}
    for short, fields in signed.items():
        column_valid = " AND ".join([match(*field) for field in fields] + [match("key_id", "text")])
        guards[short] = f"{common} OR COALESCE(({envelope_valid} AND {column_valid}),0)<>1"

    open_client = "c.privacy_erasure_started_at IS NULL"
    inserts = {
        "offer": f"""{guards['offer']} OR NEW.version<1 OR NOT EXISTS (
            SELECT 1 FROM management_igfulfillmentreviewdecision d
            JOIN management_igfulfillmentreviewrequest r ON r.id=d.request_id
            JOIN management_igclient c ON c.id=r.client_id JOIN {user} u ON u.id=NEW.actor_id
            WHERE d.id=NEW.decision_id AND c.id=NEW.client_id AND {open_client} AND u.is_active=1
                AND {exact(extracted('decision_digest'),'d.envelope_hmac')}
                AND {exact(extracted('request_digest'),'r.envelope_hmac')})
            OR (NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM management_igfollowuptask t
                WHERE t.id=NEW.task_id AND t.client_id=NEW.client_id
                    AND t.reason='split_shipping_offer' AND t.trigger='event'))""",
        "receipt": f"""{guards['receipt']} OR NEW.native_mid='' OR NOT EXISTS (
            SELECT 1 FROM {offer} o JOIN management_igclient c ON c.id=o.client_id
            JOIN management_igfollowuptask t ON t.id=NEW.task_id
            WHERE o.id=NEW.offer_id AND {open_client} AND t.client_id=c.id
                AND (o.task_id IS NULL OR o.task_id=t.id) AND t.status='sent'
                AND t.reason='split_shipping_offer' AND t.trigger='event' AND t.attempt_count=1
                AND {exact(extracted('offer_digest'),'o.envelope_hmac')}
                AND {exact('t.provider_message_id','NEW.native_mid')}
                AND t.sent_at IS NOT NULL
                AND ({date_value(extracted('delivered_at'))})=({date_value('t.sent_at', database=True)})
                AND t.sent_at<o.expires_at)""",
        "choice": f"""{guards['choice']} OR NEW.option NOT IN ('together','separate') OR NOT EXISTS (
            SELECT 1 FROM {offer} o JOIN {receipt} r ON r.offer_id=o.id
            JOIN management_igclient c ON c.id=o.client_id
            JOIN management_instagrambotmessage m ON m.id=NEW.source_id
            WHERE o.id=NEW.offer_id AND r.id=NEW.receipt_id AND c.id=NEW.client_id AND {open_client}
                AND {exact(extracted('offer_digest'),'o.envelope_hmac')}
                AND {exact(extracted('receipt_digest'),'r.envelope_hmac')}
                AND m.client_id=c.id AND {exact('m.sender_id','c.igsid')} AND m.role='user'
                AND m.source='webhook' AND m.mid IS NOT NULL AND m.mid<>''
                AND m.provider_created_at IS NOT NULL
                AND ({date_value('m.provider_created_at', database=True)})>=({date_value(extracted('delivered_at', 'r.envelope'))})
                AND m.provider_created_at<o.expires_at)
            OR (NEW.revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM management_igcustomerturnrevision v
                JOIN management_igturnrevisionsource s ON s.revision_id=v.id
                JOIN management_instagrambotmessage m ON m.id=s.message_id
                WHERE v.id=NEW.revision_id AND v.client_id=NEW.client_id AND m.id=NEW.source_id
                    AND s.role='user' AND {exact('s.source_namespace','m.provider_namespace')}))""",
        "plan": f"""{guards['plan']} OR NOT EXISTS (
            SELECT 1 FROM {choice} ch JOIN management_igclient c ON c.id=ch.client_id
            JOIN orders_order o ON o.id=NEW.order_id
            WHERE ch.id=NEW.choice_id AND c.id=NEW.client_id AND {open_client}
              AND (EXISTS (SELECT 1 FROM management_igorderassignment a
                    WHERE a.order_id=o.id AND a.client_id=c.id AND a.unassigned_at IS NULL)
                OR (NOT EXISTS (SELECT 1 FROM management_igorderassignment a WHERE a.order_id=o.id AND a.unassigned_at IS NULL)
                    AND EXISTS (SELECT 1 FROM management_igorderattribution a WHERE a.order_id=o.id AND a.client_id=c.id))))""",
        "parcel": f"""{guards['parcel']} OR NEW.key='' OR NOT EXISTS (
            SELECT 1 FROM {plan} p JOIN management_igclient c ON c.id=p.client_id
            WHERE p.id=NEW.plan_id AND {open_client})""",
        "dispatch": f"""{guards['dispatch']} OR NEW.tracking_number='' OR NEW.customer_delivery_amount<0
            OR NEW.cod_amount<0 OR NOT EXISTS (
            SELECT 1 FROM {parcel} p JOIN {plan} pl ON pl.id=p.plan_id
            JOIN management_igclient c ON c.id=pl.client_id JOIN {user} u ON u.id=NEW.actor_id
            JOIN management_igordershipment s ON s.id=NEW.shipment_id
            WHERE p.id=NEW.parcel_id AND {open_client} AND u.is_active=1 AND s.order_id=pl.order_id
                AND s.direction='outbound' AND s.purpose='split_initial' AND s.created_by_id=NEW.actor_id
                AND {exact('s.tracking_number','NEW.tracking_number')})""",
        "tracking": f"""NOT EXISTS (SELECT 1 FROM {parcel} p JOIN {plan} pl ON pl.id=p.plan_id
            JOIN management_igclient c ON c.id=pl.client_id WHERE p.id=NEW.parcel_id AND {open_client})""",
    }
    owner_joins = {
        "offer": ("", "c.id=OLD.client_id"),
        "receipt": (f"JOIN {offer} o ON o.client_id=c.id", "o.id=OLD.offer_id"),
        "choice": ("", "c.id=OLD.client_id"),
        "plan": ("", "c.id=OLD.client_id"),
        "parcel": (f"JOIN {plan} pl ON pl.client_id=c.id", "pl.id=OLD.plan_id"),
        "dispatch": (f"JOIN {plan} pl ON pl.client_id=c.id JOIN {parcel} p ON p.plan_id=pl.id", "p.id=OLD.parcel_id"),
        "tracking": (f"JOIN {plan} pl ON pl.client_id=c.id JOIN {parcel} p ON p.plan_id=pl.id", "p.id=OLD.parcel_id"),
    }
    with connection.cursor() as cursor:
        for short, table in TABLES.items():
            if mysql:
                _require_innodb(connection, table)
            joins, predicate = owner_joins[short]
            privacy = f"EXISTS (SELECT 1 FROM management_igclient c {joins} WHERE {predicate} AND c.privacy_erasure_started_at IS NOT NULL)"
            update = "1=1" if short != "tracking" else f"NEW.id<>OLD.id OR NEW.parcel_id<>OLD.parcel_id OR ({inserts['tracking']})"
            for event, invalid in (("insert", inserts[short]), ("update", update), ("delete", "NOT " + privacy)):
                name = f"ig_split_{short}_{event}"
                cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
                if mysql:
                    cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {table} FOR EACH ROW "
                        f"BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Split shipping immutable/owner/privacy guard'; END IF; END")
                else:
                    cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {table} WHEN {invalid} "
                        "BEGIN SELECT RAISE(ABORT,'Split shipping immutable/owner/privacy guard'); END")


def remove_guards(apps, schema_editor):
    del apps
    with schema_editor.connection.cursor() as cursor:
        for short in TABLES:
            for event in ("insert", "update", "delete"):
                cursor.execute(f"DROP TRIGGER IF EXISTS ig_split_{short}_{event}")


def _id():
    return ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False))


def _evidence():
    return [("envelope", models.JSONField(default=dict)), ("key_id", models.CharField(max_length=24)),
        ("envelope_hmac", models.CharField(max_length=64)), ("created_at", models.DateTimeField(auto_now_add=True)), _id()]


def _relation(to, related_name, *, one=False, nullable=False):
    field = models.OneToOneField if one else models.ForeignKey
    return field(to=to, on_delete=django.db.models.deletion.PROTECT, db_constraint=False,
        related_name=related_name, **({"null": True, "blank": True} if nullable else {}))


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0231_fulfillment_stock_sync"),
        ("orders", "0059_order_delivery_and_dtf_film"), ("warehouse", "0012_mariadb_native_uuid_compatibility"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        ResumableAddField(model_name="igcheckoutinventoryreservation", name="fulfilled_quantity",
            field=models.PositiveIntegerField(default=0)),
        migrations.RunPython(initialize_fulfilled_quantities, migrations.RunPython.noop),
        ResumableAddConstraint(model_name="igcheckoutinventoryreservation",
            constraint=models.CheckConstraint(condition=models.Q(fulfilled_quantity__lte=models.F("quantity")), name="ig_res_fulfilled_lte_qty")),
        migrations.AlterField(model_name="igordershipment", name="purpose", field=models.CharField(
            choices=[("initial", "Перша відправка"), ("split_initial", "Часткова перша відправка"),
                ("exchange_replacement", "Заміна відправлена"), ("return_inbound", "Повернення від клієнта"),
                ("correction", "Переоформлена ТТН")], db_index=True, max_length=32)),
        ResumableCreateModel(name="IgSplitShippingOffer", fields=_evidence() + [
            ("decision", _relation("management.igfulfillmentreviewdecision", "split_shipping_offers")),
            ("client", _relation("management.igclient", "split_shipping_offers")),
            ("actor", _relation(settings.AUTH_USER_MODEL, "split_shipping_offers")),
            ("operation_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("version", models.PositiveIntegerField()),
            ("task", _relation("management.igfollowuptask", "split_shipping_offer", one=True, nullable=True)),
            ("expires_at", models.DateTimeField()),
        ], options={"constraints": [models.UniqueConstraint(fields=["decision", "version"], name="ig_split_offer_version")]}),
        ResumableCreateModel(name="IgSplitShippingReceipt", fields=_evidence() + [
            ("offer", _relation("management.igsplitshippingoffer", "receipts")),
            ("task", _relation("management.igfollowuptask", "split_shipping_receipt", one=True)),
            ("native_mid", models.CharField(max_length=256)),
        ], options={"abstract": False}),
        ResumableCreateModel(name="IgSplitShippingChoice", fields=_evidence() + [
            ("offer", _relation("management.igsplitshippingoffer", "choice", one=True)),
            ("receipt", _relation("management.igsplitshippingreceipt", "choices")),
            ("client", _relation("management.igclient", "split_shipping_choices")),
            ("source", _relation("management.instagrambotmessage", "split_shipping_choices")),
            ("revision", _relation("management.igcustomerturnrevision", "split_shipping_choices", nullable=True)),
            ("option", models.CharField(max_length=16, choices=[("together", "Together"), ("separate", "Separate parcels")])),
        ], options={"abstract": False}),
        ResumableCreateModel(name="IgSplitOrderPlan", fields=_evidence() + [
            ("choice", _relation("management.igsplitshippingchoice", "plan", one=True)),
            ("order", _relation("orders.order", "instagram_split_plan", one=True)),
            ("client", _relation("management.igclient", "split_order_plans")),
        ], options={"abstract": False}),
        ResumableCreateModel(name="IgSplitParcel", fields=_evidence() + [
            ("plan", _relation("management.igsplitorderplan", "parcels")),
            ("key", models.CharField(max_length=24)),
        ], options={"constraints": [models.UniqueConstraint(fields=["plan", "key"], name="ig_split_parcel_key")]}),
        ResumableCreateModel(name="IgSplitParcelDispatch", fields=_evidence() + [
            ("parcel", _relation("management.igsplitparcel", "dispatch", one=True)),
            ("actor", _relation(settings.AUTH_USER_MODEL, "split_parcel_dispatches")),
            ("operation_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("shipment", _relation("management.igordershipment", "split_parcel_dispatch", one=True)),
            ("tracking_number", models.CharField(max_length=64, unique=True)),
            ("customer_delivery_amount", models.DecimalField(max_digits=10, decimal_places=2)),
            ("cod_amount", models.DecimalField(max_digits=10, decimal_places=2)),
        ], options={"abstract": False}),
        ResumableCreateModel(name="IgSplitParcelTracking", fields=[_id(),
            ("parcel", _relation("management.igsplitparcel", "tracking", one=True)),
            ("code", models.CharField(max_length=32, blank=True)),
            ("status", models.CharField(max_length=500, blank=True)),
            ("provider_event_at", models.DateTimeField(null=True, blank=True)),
            ("terminal_at", models.DateTimeField(null=True, blank=True)),
            ("checked_at", models.DateTimeField(null=True, blank=True)),
        ]),
        migrations.RunPython(ensure_transactions, migrations.RunPython.noop),
        migrations.RunPython(install_guards, remove_guards),
    ]
