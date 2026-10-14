"""Append-only native checkout-link amendments; no synthetic webhook source."""
from importlib import import_module
import uuid

from django.db import migrations, models
import django.db.models.deletion
from ._resumable_schema import _columns, _constraints

CreateModel = import_module("management.migrations.0233_assisted_commerce").ResumableAssistedCreateModel
_split = import_module("management.migrations.0232_split_shipping")


class ResumableNativeAddField(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.name)
        existing = _columns(schema_editor, model._meta.db_table).get(field.column)
        if existing is None:
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        _split._validate_existing_field(schema_editor, model._meta.db_table, field, existing)
        shapes = _constraints(schema_editor, model._meta.db_table)
        if field.unique and not any(value.get("unique") and value.get("columns") == [field.column]
                for value in shapes.values()):
            schema_editor.add_constraint(model, models.UniqueConstraint(fields=[field.name],
                name="ig_native_transition_source_uniq"))


def native_guards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Native checkout witnesses require MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    receipt = apps.get_model("management", "IgCheckoutCartAmendment")._meta.db_table
    transition = apps.get_model("management", "IgCommerceSelectionTransition")._meta.db_table
    decision = apps.get_model("management", "IgCommerceTurnDecision")._meta.db_table
    if mysql:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {connection.ops.quote_name(receipt)} ENGINE=InnoDB")

    def extract(path):
        return (f"JSON_UNQUOTE(JSON_EXTRACT(NEW.source_body,'$.{path}'))" if mysql
            else f"json_extract(NEW.source_body,'$.{path}')")

    def identity(value):
        return f"REPLACE(CAST({value} AS {'CHAR' if mysql else 'TEXT'}),'-','')"

    invalid = " OR ".join([
        f"{extract('schema')}<>'ig-checkout-cart-amendment.v1'",
        f"{extract('actor')}<>'checkout_link_holder'",
        f"{identity(extract('amendment_id'))}<>{identity('NEW.id')}",
        f"{identity(extract('operation_id'))}<>{identity('NEW.operation_id')}",
        f"{extract('client_id')}<>NEW.client_id", f"{extract('proposal_id')}<>NEW.proposal_id",
        f"{extract('session_id')}<>NEW.session_id", f"{extract('token_id')}<>NEW.token_id",
        f"{extract('proposal_from_revision')}<>NEW.from_revision",
        f"{extract('proposal_to_revision')}<>NEW.to_revision",
        "NEW.to_revision<>NEW.from_revision+1", "LENGTH(NEW.source_hmac)<>64", "LENGTH(NEW.result_hmac)<>64",
        f"{extract('input_digest')}<>NEW.input_digest",
        "NOT EXISTS(SELECT 1 FROM management_igclient c WHERE c.id=NEW.client_id AND c.privacy_erasure_started_at IS NULL AND c.hidden_at IS NULL)",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutproposal p WHERE p.id=NEW.proposal_id AND p.client_id=NEW.client_id AND p.revision=NEW.from_revision AND p.status IN ('ready','viewed'))",
        "NOT EXISTS(SELECT 1 FROM management_igcommerceselectionsession s WHERE s.id=NEW.session_id AND s.client_id=NEW.client_id AND s.state='open' AND s.open_slot=1)",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutaccesstoken t WHERE t.id=NEW.token_id AND t.proposal_id=NEW.proposal_id AND t.kind IN ('bot','manager') AND t.revoked_at IS NULL)",
    ])
    privacy = "EXISTS(SELECT 1 FROM management_igclient c WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"
    transition_privacy = ("EXISTS(SELECT 1 FROM management_igcommerceselectionsession s "
        "JOIN management_igclient c ON c.id=s.client_id WHERE s.id=OLD.session_id "
        "AND c.privacy_erasure_started_at IS NOT NULL)")
    source_invalid = ("(NEW.source_message_id IS NULL AND NEW.checkout_amendment_id IS NULL) OR "
        "(NEW.source_message_id IS NOT NULL AND NEW.checkout_amendment_id IS NOT NULL) OR "
        f"(NEW.checkout_amendment_id IS NOT NULL AND (NEW.action<>'checkout_cart_amended' OR NOT EXISTS(SELECT 1 FROM {receipt} a "
        "WHERE a.id=NEW.checkout_amendment_id AND a.session_id=NEW.session_id)))")
    specs = [("ig_cart_amend_insert", receipt, "INSERT", invalid),
        ("ig_cart_amend_update", receipt, "UPDATE", "1=1"),
        ("ig_cart_amend_delete", receipt, "DELETE", f"NOT {privacy}"),
        ("ig_cart_amend_source", transition, "INSERT", source_invalid),
        ("ig_com_tr_no_del", transition, "DELETE", f"NOT {transition_privacy}"),
        ("ig_com_dec_no_del", decision, "DELETE", f"NOT {transition_privacy}")]
    with connection.cursor() as cursor:
        for name, table, event, condition in specs:
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            if mysql:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW BEGIN "
                    f"IF COALESCE(({condition}),1) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Native checkout witness guard'; END IF; END")
            else:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN COALESCE(({condition}),1) "
                    "BEGIN SELECT RAISE(ABORT,'Native checkout witness guard'); END")


def remove_native_guards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for name in ("ig_cart_amend_insert", "ig_cart_amend_update", "ig_cart_amend_delete", "ig_cart_amend_source"):
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name, model in (("ig_com_tr_no_del", "IgCommerceSelectionTransition"),
                ("ig_com_dec_no_del", "IgCommerceTurnDecision")):
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            table = apps.get_model("management", model)._meta.db_table
            if schema_editor.connection.vendor == "mysql":
                cursor.execute(f"CREATE TRIGGER {name} BEFORE DELETE ON {table} FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Commerce source records are append-only'")
            elif schema_editor.connection.vendor == "sqlite":
                cursor.execute(f"CREATE TRIGGER {name} BEFORE DELETE ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'Commerce source records are append-only'); END")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0234_assisted_partial_commerce")]
    operations = [
        CreateModel(name="IgCheckoutCartAmendment", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("operation_id", models.UUIDField(unique=True, editable=False)),
            ("from_revision", models.PositiveIntegerField()),
            ("to_revision", models.PositiveIntegerField()),
            ("input_digest", models.CharField(max_length=64)),
            ("source_body", models.JSONField(default=dict)),
            ("source_hmac", models.CharField(max_length=64)),
            ("result", models.JSONField(default=dict)),
            ("result_hmac", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ("proposal", models.ForeignKey(to="management.igcheckoutproposal", on_delete=django.db.models.deletion.PROTECT,
                db_constraint=False, related_name="cart_amendments")),
            ("client", models.ForeignKey(to="management.igclient", on_delete=django.db.models.deletion.PROTECT,
                db_constraint=False, related_name="checkout_cart_amendments")),
            ("session", models.ForeignKey(to="management.igcommerceselectionsession", on_delete=django.db.models.deletion.PROTECT,
                db_constraint=False, related_name="checkout_cart_amendments")),
            ("token", models.ForeignKey(to="management.igcheckoutaccesstoken", on_delete=django.db.models.deletion.PROTECT,
                db_constraint=False, related_name="cart_amendments")),
        ], options={"constraints": [models.UniqueConstraint(fields=("proposal", "to_revision"),
            name="ig_checkout_amend_revision")]}),
        migrations.AlterField(model_name="igcheckoutrevision", name="source",
            field=models.CharField(max_length=24, choices=[("bot_create", "Створено ботом"),
                ("bot_update", "Оновлено ботом"), ("system_supersede", "Системна заміна"),
                ("checkout_holder", "Змінено на сторінці оформлення")])),
        migrations.AlterField(model_name="igcheckoutaccesstoken", name="kind",
            field=models.CharField(max_length=16, default="bot", choices=[("bot", "Посилання бота"),
                ("share", "Посилання для оплати іншою людиною"), ("replacement", "Посилання заміни"),
                ("manager", "Посилання менеджера")])),
        migrations.AlterField(model_name="igcommerceselectiontransition", name="source_message",
            field=models.ForeignKey(to="management.instagrambotmessage", on_delete=django.db.models.deletion.DO_NOTHING,
                db_constraint=False, related_name="commerce_selection_transitions", null=True, blank=True)),
        ResumableNativeAddField(model_name="igcommerceselectiontransition", name="checkout_amendment",
            field=models.OneToOneField(to="management.igcheckoutcartamendment", on_delete=django.db.models.deletion.PROTECT,
                db_constraint=False, related_name="selection_transition", null=True, blank=True)),
        migrations.RunPython(native_guards, remove_native_guards),
    ]
