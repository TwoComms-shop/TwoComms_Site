"""Owned original-link routing to a freshly approved immutable successor."""
from importlib import import_module
import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

CreateModel = import_module("management.migrations.0233_assisted_commerce").ResumableAssistedCreateModel


def resolution_guards(apps, schema_editor):
    connection = schema_editor.connection
    mysql = connection.vendor == "mysql"
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Checkout amendment resolutions require MariaDB or SQLite")
    table = apps.get_model("management", "IgCheckoutCartAmendmentResolution")._meta.db_table
    if mysql:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
    def value(path):
        return f"JSON_UNQUOTE(JSON_EXTRACT(NEW.body,'$.{path}'))" if mysql else f"json_extract(NEW.body,'$.{path}')"
    def identity(column):
        return f"REPLACE(CAST({column} AS {'CHAR' if mysql else 'TEXT'}),'-','')"
    invalid = " OR ".join([
        f"{value('schema')}<>'ig-checkout-cart-amendment-resolution.v1'", "LENGTH(NEW.body_hmac)<>64",
        f"{identity(value('amendment_id'))}<>{identity('NEW.amendment_id')}",
        f"{identity(value('request_id'))}<>{identity('NEW.request_id')}",
        f"{identity(value('decision_id'))}<>{identity('NEW.decision_id')}",
        f"{identity(value('execution_id'))}<>{identity('NEW.execution_id')}",
        *[f"{value(field)}<>NEW.{field}" for field in ("client_id", "origin_proposal_id", "successor_proposal_id", "actor_id", "root_token_id", "child_token_id")],
        "NOT EXISTS(SELECT 1 FROM management_igclient c WHERE c.id=NEW.client_id AND c.privacy_erasure_started_at IS NULL AND c.hidden_at IS NULL)",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutcartamendment a WHERE a.id=NEW.amendment_id AND a.client_id=NEW.client_id AND a.proposal_id=NEW.origin_proposal_id AND a.token_id=NEW.root_token_id)",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutproposal p WHERE p.id=NEW.origin_proposal_id AND p.client_id=NEW.client_id AND p.payment_attempt_id IS NULL AND p.winner_invoice_generation_id IS NULL AND (p.status='superseded' OR (p.status='cancelled' AND EXISTS(SELECT 1 FROM management_igcheckoutcartamendment a WHERE a.id=NEW.amendment_id AND a.proposal_id=p.id AND " + ("JSON_UNQUOTE(JSON_EXTRACT(a.source_body,'$.cancelled_scope'))='true'" if mysql else "json_extract(a.source_body,'$.cancelled_scope')=1") + "))))",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutproposal p WHERE p.id=NEW.successor_proposal_id AND p.client_id=NEW.client_id AND p.status IN ('ready','viewed') AND p.payment_attempt_id IS NULL)",
        "NOT EXISTS(SELECT 1 FROM management_igassistedcommerceexecution e WHERE e.id=NEW.execution_id AND e.proposal_id=NEW.successor_proposal_id AND e.client_id=NEW.client_id AND e.request_id=NEW.request_id AND e.decision_id=NEW.decision_id)",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutaccesstoken t WHERE t.id=NEW.root_token_id AND t.proposal_id=NEW.origin_proposal_id AND t.kind IN ('bot','manager'))",
        "NOT EXISTS(SELECT 1 FROM management_igcheckoutaccesstoken t WHERE t.id=NEW.child_token_id AND t.proposal_id=NEW.successor_proposal_id AND t.kind IN ('bot','manager'))",
    ])
    privacy = "EXISTS(SELECT 1 FROM management_igclient c WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"
    with connection.cursor() as cursor:
        for event, condition in (("INSERT", invalid), ("UPDATE", "1=1"), ("DELETE", f"NOT {privacy}")):
            name = f"ig_cart_amend_resolution_{event.lower()}"
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            if mysql:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW BEGIN "
                    f"IF COALESCE(({condition}),1) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Checkout resolution guard'; END IF; END")
            else:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN COALESCE(({condition}),1) "
                    "BEGIN SELECT RAISE(ABORT,'Checkout resolution guard'); END")


def remove_resolution_guards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for event in ("insert", "update", "delete"):
            cursor.execute(f"DROP TRIGGER IF EXISTS ig_cart_amend_resolution_{event}")


def fk(target, related, *, one=False):
    field = models.OneToOneField if one else models.ForeignKey
    return field(to=target, on_delete=django.db.models.deletion.PROTECT, db_constraint=False, related_name=related)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0235_checkout_cart_amendments"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [CreateModel(name="IgCheckoutCartAmendmentResolution", fields=[
        ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
        ("body", models.JSONField(default=dict)), ("body_hmac", models.CharField(max_length=64)),
        ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
        ("amendment", fk("management.igcheckoutcartamendment", "resolution", one=True)),
        ("client", fk("management.igclient", "checkout_amendment_resolutions")),
        ("origin_proposal", fk("management.igcheckoutproposal", "amendment_resolutions")),
        ("successor_proposal", fk("management.igcheckoutproposal", "amendment_origin_resolution", one=True)),
        ("request", fk("management.igassistedcommercerequest", "amendment_resolutions")),
        ("decision", fk("management.igassistedcommercedecision", "amendment_resolutions")),
        ("execution", fk("management.igassistedcommerceexecution", "amendment_resolution", one=True)),
        ("actor", fk(settings.AUTH_USER_MODEL, "ig_checkout_amendment_resolutions")),
        ("root_token", fk("management.igcheckoutaccesstoken", "amendment_root_resolutions")),
        ("child_token", fk("management.igcheckoutaccesstoken", "amendment_child_resolutions")),
    ]), migrations.RunPython(resolution_guards, remove_resolution_guards)]
