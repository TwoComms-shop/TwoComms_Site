"""Assisted evidence guards; never authenticates JSON semantics.

Depends on the released ordinary checkout and fulfillment foundation.
HMAC is verified by the effect owner; SQL checks envelope/column parity, owner
references, immutable data, finite native totals and canonical payment attachment.
"""
from importlib import import_module
import re
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from ._resumable_schema import _columns, _constraints

_split = import_module("management.migrations.0232_split_shipping")
TABLES = {
    "request": "management_igassistedcommercerequest",
    "decision": "management_igassistedcommercedecision",
    "execution": "management_igassistedcommerceexecution",
    "image": "management_igassistedcommerceimage",
    "stock_sync": "management_igassistedcommercestocksync",
}
OWNER_MODELS = (
    ("management", "IgClient"), ("management", "IgFollowUpTask"),
    ("management", "InstagramBotMessage"), ("management", "IgCustomerTurnRevision"),
    ("management", "IgTurnRevisionSource"), ("management", "IgCommercialEpisode"),
    ("management", "IgDeal"), ("management", "IgCheckoutProposal"),
    ("management", "IgCheckoutInvoiceGeneration"), ("management", "IgOrderAssignment"),
    ("management", "IgOrderAttribution"), ("orders", "Order"), ("orders", "PaymentAttempt"),
    ("productcolors", "ProductColorVariant"),
    ("product_catalog", "ProductInventoryPolicy"), ("product_catalog", "VariantSizeRule"),
    ("product_catalog", "VariantBlankLink"), ("warehouse", "StockItem"), ("warehouse", "StockMovement"),
)
METADATA_MODELS = (("storefront", "Product"),)


class ResumableAssistedCreateModel(migrations.CreateModel):
    """Verify same-name tables after interrupted non-atomic native DDL."""
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        table = model._meta.db_table
        if table not in schema_editor.connection.introspection.table_names():
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        columns, constraints = _columns(schema_editor, table), _constraints(schema_editor, table)
        if set(columns) != {field.column for field in model._meta.local_fields}:
            raise RuntimeError(f"Assisted table has incompatible columns: {table}")
        for field in model._meta.local_fields:
            _split._validate_existing_field(schema_editor, table, field, columns[field.column])
            shapes = [value for value in constraints.values() if list(value.get("columns") or ()) == [field.column]]
            if field.primary_key and not any(value.get("primary_key") for value in shapes):
                raise RuntimeError(f"Assisted table missing primary key: {table}")
            if field.unique and not field.primary_key and not any(
                    value.get("unique") and not value.get("primary_key") and not value.get("check")
                    and not value.get("foreign_key") for value in shapes):
                schema_editor.add_constraint(model, models.UniqueConstraint(fields=[field.name],
                    name=schema_editor._create_index_name(table, [field.column], suffix="_uniq")))
            elif field.db_index and not field.unique and not any(value.get("index") or value.get("unique") for value in shapes):
                schema_editor.add_index(model, models.Index(fields=[field.name],
                    name=schema_editor._create_index_name(table, [field.column])))
        for constraint in model._meta.constraints:
            existing = constraints.get(constraint.name)
            if existing is None:
                schema_editor.add_constraint(model, constraint)
            elif isinstance(constraint, models.UniqueConstraint):
                _split._validate_unique(existing, [model._meta.get_field(name).column for name in constraint.fields], constraint.name)
            elif isinstance(constraint, models.CheckConstraint):
                actual = _split._check_clause(schema_editor, table, constraint.name)
                expected_sql = str(constraint.constraint_sql(model, schema_editor))
                expected = expected_sql[expected_sql.upper().index("CHECK") + 5:].strip()
                if not existing.get("check") or actual is None or _split._normalized_check(actual) != _split._normalized_check(expected):
                    raise RuntimeError(f"Assisted CHECK shape mismatch: {constraint.name}")
            else:
                raise RuntimeError(f"Unexpected assisted constraint: {constraint.name}")
        for index in model._meta.indexes:
            existing = constraints.get(index.name)
            expected = [model._meta.get_field(name).column for name in index.fields]
            if existing is None:
                schema_editor.add_index(model, index)
            elif not existing.get("index") or list(existing.get("columns") or ()) != expected:
                raise RuntimeError(f"Assisted index shape mismatch: {index.name}")


def ensure_transactions(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "sqlite":
        return
    if connection.vendor != "mysql":
        raise RuntimeError("Assisted commerce supports only native MariaDB and SQLite")
    user = apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table
    owners = [apps.get_model(app, model)._meta.db_table for app, model in OWNER_MODELS]
    metadata = [user, *(apps.get_model(app, model)._meta.db_table for app, model in METADATA_MODELS)]
    # Foundation owns parent engines. Fail closed instead of changing an
    # unrelated source/account table from this additive migration.
    with connection.cursor() as cursor:
        for table in owners:
            cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            if cursor.fetchone() != ("InnoDB", "BASE TABLE"):
                raise RuntimeError(f"Assisted parent requires released InnoDB foundation: {table}")
        # Production authentication and Product identity/price metadata remain
        # MyISAM. Fresh role/configuration checks belong to the effect owner;
        # neither a role edit nor Product SELECT FOR UPDATE is transactional
        # on that engine. Counts/mappings/movements above must remain InnoDB.
        for table in metadata:
            cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            row = cursor.fetchone()
            if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
                raise RuntimeError(f"Unexpected assisted metadata table: {table}")
        for table in TABLES.values():
            cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            row = cursor.fetchone()
            if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
                raise RuntimeError(f"Unexpected additive assisted engine: {table}")
            if row[0] == "MyISAM":
                cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            if cursor.fetchone() != ("InnoDB",):
                raise RuntimeError(f"Assisted child requires InnoDB: {table}")


def install_guards(apps, schema_editor, *, allow_partial=False):
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Assisted guards require MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    request, decision, execution, image, stock_sync = TABLES.values()
    user = connection.ops.quote_name(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)

    def extracted(path, envelope="NEW.envelope"):
        return (f"JSON_UNQUOTE(JSON_EXTRACT({envelope}, '$.{path}'))" if mysql
            else f"json_extract({envelope}, '$.{path}')")

    def exact(left, right):
        return f"BINARY {left}=BINARY {right}" if mysql else f"{left}={right}"

    def uuid_value(column):
        cast = f"CAST({column} AS CHAR)" if mysql else f"CAST({column} AS TEXT)"
        return f"REPLACE({cast},'-','')"

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

    def match(name, kind="scalar", nullable=False, *, envelope="NEW.envelope"):
        value, column = extracted("columns." + name, envelope), "NEW." + name
        if kind == "uuid":
            condition = exact(f"REPLACE({value},'-','')", uuid_value(column))
        elif kind == "text":
            condition = exact(value, column)
        elif kind == "date":
            condition = f"({date_value(value)})=({date_value(column, database=True)})"
        elif kind == "decimal":
            condition = exact(value, f"CAST({column} AS CHAR)" if mysql else f"printf('%.2f',{column})")
        else:
            condition = f"{value}={column}"
        if nullable:
            present = (f"JSON_CONTAINS_PATH({envelope},'one','$.columns.{name}')=1" if mysql
                else f"json_type({envelope},'$.columns.{name}') IS NOT NULL")
            json_null = (f"JSON_TYPE(JSON_EXTRACT({envelope},'$.columns.{name}'))='NULL'" if mysql
                else f"json_type({envelope},'$.columns.{name}')='null'")
            condition = f"({present} AND (({column} IS NULL AND {json_null}) OR ({column} IS NOT NULL AND {condition})))"
        return f"COALESCE(({condition}),0)=1"

    def hash_invalid(column):
        return (f"BINARY {column} NOT REGEXP '^[0-9a-f]{{64}}$'" if mysql
            else f"(length({column})<>64 OR {column} GLOB '*[^0-9a-f]*')")

    def changed(column):
        return f"NOT (NEW.{column}<=>OLD.{column})" if mysql else f"NEW.{column} IS NOT OLD.{column}"

    def revision_owner(column, client="NEW.client_id", source=None):
        member = ""
        if source:
            member = f"""AND EXISTS (SELECT 1 FROM management_igturnrevisionsource s
                JOIN management_instagrambotmessage m ON m.id=s.message_id
                WHERE s.revision_id=v.id AND s.message_id={source} AND s.role='user'
                    AND {exact('s.source_namespace','m.provider_namespace')})"""
        return f"({column} IS NULL OR EXISTS (SELECT 1 FROM management_igcustomerturnrevision v WHERE v.id={column} AND v.client_id={client} {member}))"

    def customer_source(column, client="NEW.client_id", nullable=False):
        expression = f"""EXISTS (SELECT 1 FROM management_instagrambotmessage m
            JOIN management_igclient c ON c.id=m.client_id
            WHERE m.id={column} AND m.client_id={client} AND m.role='user'
              AND {exact('m.sender_id','c.igsid')} AND m.source='webhook'
              AND m.mid IS NOT NULL AND m.mid<>'')"""
        return f"({column} IS NULL OR {expression})" if nullable else expression

    def signed_guard(kind):
        parity = " AND ".join(match(*field) for field in SIGNED[kind])
        header = (f"{extracted('version')}=1 AND {exact(extracted('purpose'), chr(39)+'assisted-commerce.v1'+chr(39))} "
                  f"AND {exact(extracted('kind'), chr(39)+kind+chr(39))}")
        return (f"{hash_invalid('NEW.envelope_hmac')} OR {hash_invalid('NEW.snapshot_digest')} OR NEW.key_id='' "
                f"OR COALESCE(({header} AND {parity}),0)<>1")

    open_client = "EXISTS (SELECT 1 FROM management_igclient c WHERE c.id=NEW.client_id AND c.hidden_at IS NULL AND c.privacy_erasure_started_at IS NULL)"
    request_owner = f"""EXISTS (SELECT 1 FROM {request} r WHERE r.id=NEW.request_id AND r.client_id=NEW.client_id)"""
    request_insert = f"""{signed_guard('request')} OR NOT ({open_client}) OR NEW.version<1 OR NEW.reset_floor<1
        OR NEW.source_namespace='' OR {hash_invalid('NEW.full_capture_digest')}
        OR (NEW.source_id IS NULL AND NEW.revision_id IS NULL)
        OR NOT {customer_source('NEW.source_id', nullable=True)}
        OR NOT {revision_owner('NEW.revision_id')}
        OR (NEW.source_id IS NOT NULL AND NEW.source_id<NEW.reset_floor)
        OR (NEW.source_id IS NOT NULL AND NEW.revision_id IS NOT NULL AND NOT {revision_owner('NEW.revision_id', source='NEW.source_id')})
        OR (NEW.source_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM management_instagrambotmessage m WHERE m.id=NEW.source_id AND {exact('m.provider_namespace','NEW.source_namespace')}))
        OR (NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM management_igfollowuptask t WHERE t.id=NEW.task_id AND t.client_id=NEW.client_id AND t.kind='manager_task'))
        OR (NEW.deal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM management_igdeal d WHERE d.id=NEW.deal_id AND d.client_id=NEW.client_id))
        OR (NEW.episode_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM management_igcommercialepisode e WHERE e.id=NEW.episode_id AND e.client_id=NEW.client_id AND (NEW.deal_id IS NULL OR e.deal_id=NEW.deal_id)))
        OR (NEW.version>1 AND NOT EXISTS (SELECT 1 FROM {request} prior_row
            WHERE prior_row.intention_id=NEW.intention_id AND prior_row.client_id=NEW.client_id
                AND prior_row.version+1=NEW.version))
        OR EXISTS (SELECT 1 FROM {request} r WHERE r.intention_id=NEW.intention_id AND r.client_id<>NEW.client_id)"""
    decision_insert = f"""{signed_guard('decision')} OR NOT ({open_client}) OR NOT ({request_owner})
        OR NEW.version<1 OR NEW.status NOT IN ('draft','approved','rejected') OR NOT ({exact('NEW.currency', chr(39)+'UAH'+chr(39))})
        OR NEW.approved_total<0 OR NEW.approved_total>1000000 OR NEW.approved_custom_quantity>50
        OR {hash_invalid('NEW.request_digest')} OR {hash_invalid('NEW.draft_digest')} OR {hash_invalid('NEW.full_capture_digest')}
        OR NOT EXISTS (SELECT 1 FROM {request} r WHERE r.id=NEW.request_id
            AND {exact('r.envelope_hmac','NEW.request_digest')} AND {exact('r.full_capture_digest','NEW.full_capture_digest')})
        OR NOT EXISTS (SELECT 1 FROM {user} u WHERE u.id=NEW.actor_id AND u.is_active=1 AND u.is_staff=1)
        OR (NEW.version>1 AND NOT EXISTS (SELECT 1 FROM {decision} prior_row
            WHERE prior_row.request_id=NEW.request_id AND prior_row.version+1=NEW.version))
        OR (NEW.status='approved' AND NEW.expires_at IS NULL)
        OR (NEW.approved_custom_quantity>0 AND NEW.status='approved' AND (NEW.physical_checked_at IS NULL OR NEW.physical_checked_at>=NEW.expires_at))
        OR (NEW.status<>'approved' AND (NEW.approved_total<>0 OR NEW.approved_custom_quantity<>0
            OR NEW.physical_checked_at IS NOT NULL OR NEW.expires_at IS NOT NULL))"""
    # The released 0233 entry point remains FULL39. Only the additive 0234
    # migration enables independently manager-priced, customer-chosen scopes.
    quote_owner = "NEW.quoted_total=d.approved_total AND NEW.quoted_custom_quantity=d.approved_custom_quantity"
    scope_owner = exact('NEW.scope', "'full'")
    if allow_partial:
        selected = "snapshot.scoped_quotes.selected."
        selected_total = extracted(selected + "total", envelope="d.envelope")
        native_total = "CAST(NEW.quoted_total AS CHAR)" if mysql else "printf('%.2f',NEW.quoted_total)"
        partial_quote_owner = " AND ".join((
            exact('NEW.scope', "'partial'"), "NEW.choice_source_id IS NOT NULL",
            exact(extracted(selected + "scope", envelope="d.envelope"), "'partial'"),
            exact(selected_total, native_total),
            f"{extracted(selected + 'custom_quantity', envelope='d.envelope')}=NEW.quoted_custom_quantity",
            exact(extracted(selected + "scope_digest", envelope="d.envelope"), "NEW.scope_digest"),
        ))
        quote_owner = f"(({exact('NEW.scope', chr(39)+'full'+chr(39))} AND {quote_owner}) OR ({partial_quote_owner}))"
        scope_owner = f"({scope_owner} OR ({exact('NEW.scope', chr(39)+'partial'+chr(39))} AND NEW.choice_source_id IS NOT NULL))"
    execution_owner = f"""EXISTS (SELECT 1 FROM {request} r JOIN {decision} d ON d.request_id=r.id
        JOIN management_igcheckoutproposal p ON p.id=NEW.proposal_id
        JOIN management_igcommercialepisode e ON e.id=NEW.episode_id
        JOIN management_igdeal deal ON deal.id=p.deal_id
        WHERE r.id=NEW.request_id AND d.id=NEW.decision_id AND r.client_id=NEW.client_id
            AND d.client_id=r.client_id AND r.intention_id=NEW.intention_id AND p.client_id=r.client_id
            AND e.client_id=r.client_id AND p.commercial_episode_id=e.id AND e.deal_id=p.deal_id
            AND deal.client_id=r.client_id AND p.assisted_checkout_v2=1
            AND d.status='approved' AND d.currency=NEW.currency AND p.currency=NEW.currency
            AND {exact('r.envelope_hmac','NEW.request_digest')} AND {exact('d.envelope_hmac','NEW.decision_digest')}
            AND {exact('r.full_capture_digest','NEW.full_capture_digest')}
            AND NEW.quoted_total=p.quoted_total AND ({quote_owner})
            AND NEW.expires_at<=d.expires_at AND NEW.expires_at<=p.expires_at)"""
    execution_initial = f"""{signed_guard('execution')}
        OR NOT ({open_client}) OR NOT ({execution_owner}) OR NOT ({exact('NEW.currency', chr(39)+'UAH'+chr(39))}) OR NEW.quoted_total<=0 OR NEW.quoted_total>1000000
        OR NEW.quoted_custom_quantity>50 OR NOT ({scope_owner})
        OR {hash_invalid('NEW.scope_digest')} OR {hash_invalid('NEW.full_capture_digest')}
        OR {hash_invalid('NEW.request_digest')} OR {hash_invalid('NEW.decision_digest')}
        OR NOT {customer_source('NEW.purchase_source_id')}
        OR NOT {customer_source('NEW.choice_source_id', nullable=True)}
        OR NOT {revision_owner('NEW.revision_id', source='NEW.purchase_source_id')}
        OR NEW.active_slot<>1 OR NEW.active_slot IS NULL OR NEW.order_id IS NOT NULL
        OR NEW.settled_at IS NOT NULL OR NEW.released_at IS NOT NULL OR NEW.release_reason<>''"""
    order_owner = f"""EXISTS (SELECT 1 FROM management_igcheckoutproposal p
        JOIN management_igcheckoutinvoicegeneration g ON g.id=p.winner_invoice_generation_id
        JOIN orders_paymentattempt a ON a.id=g.payment_attempt_id JOIN orders_order o ON o.id=a.order_id
        WHERE p.id=NEW.proposal_id AND p.client_id=NEW.client_id AND p.status='paid'
          AND g.proposal_id=p.id AND g.winner_slot=1 AND g.state='paid_winner'
          AND p.payment_attempt_id=a.id AND a.status='converted' AND o.id=NEW.order_id
          AND o.payment_status IN ('paid','prepaid','partial') AND o.source='manual'
          AND EXISTS (SELECT 1 FROM management_igorderassignment assign WHERE assign.order_id=o.id
              AND assign.client_id=NEW.client_id AND assign.unassigned_at IS NULL))"""
    # Canonical attachment runs after bind_verified_payment sets proposal.PAID,
    # generation.PAID_WINNER and the intended client's actual order assignment.
    # Paid RESOURCE_REVIEW deliberately retains its active hold: authoritative
    # refund-resolution release is unverified and excluded from this migration.
    # Never erase winner evidence or permit a generic manual release to satisfy
    # the nonpaid branch below.
    execution_terminal = f"""NEW.active_slot IS NOT NULL OR OLD.active_slot IS NULL
        OR OLD.order_id IS NOT NULL OR OLD.settled_at IS NOT NULL OR OLD.released_at IS NOT NULL
        OR NOT ((NEW.order_id IS NOT NULL AND NEW.settled_at IS NOT NULL AND NEW.released_at IS NULL
                AND NEW.release_reason='' AND ({order_owner}))
            OR (NEW.order_id IS NULL AND NEW.settled_at IS NULL AND NEW.released_at IS NOT NULL
                AND NEW.release_reason IN ('expired','revoked','superseded','cancelled')
                AND EXISTS (SELECT 1 FROM management_igcheckoutproposal p WHERE p.id=NEW.proposal_id
                    AND p.status=NEW.release_reason
                    AND p.winner_invoice_generation_id IS NULL)))"""
    extension = "CASE WHEN NEW.mime_type='image/jpeg' THEN '.jpg' WHEN NEW.mime_type='image/png' THEN '.png' ELSE '.webp' END"
    storage_parts = ["'assisted/'", uuid_value('NEW.request_id'), "'/'", uuid_value('NEW.id'), extension]
    owned_storage_key = "CONCAT(" + ",".join(storage_parts) + ")" if mysql else "(" + "||".join(storage_parts) + ")"
    image_insert = f"""{signed_guard('image')} OR NOT ({open_client}) OR NOT ({request_owner})
        OR NOT EXISTS (SELECT 1 FROM {user} u WHERE u.id=NEW.uploader_id AND u.is_active=1 AND u.is_staff=1)
        OR {hash_invalid('NEW.content_digest')} OR NEW.mime_type NOT IN ('image/jpeg','image/png','image/webp')
        OR NEW.byte_size<1 OR NEW.byte_size>6291456 OR NEW.width<1 OR NEW.height<1
        OR NEW.width>24000000 OR NEW.height>24000000 OR NEW.width*NEW.height>24000000
        OR NEW.delete_after<=NEW.created_at
        OR NOT ({exact('NEW.storage_key', owned_storage_key)})
        OR NEW.state<>'active' OR NEW.erased_at IS NOT NULL OR NEW.failure_code<>''"""
    stock_sync_insert = f"""{signed_guard('stock_sync')} OR NOT ({open_client})
        OR NOT EXISTS (SELECT 1 FROM {decision} d JOIN {request} r ON r.id=d.request_id
            WHERE d.id=NEW.decision_id AND d.client_id=NEW.client_id AND r.client_id=NEW.client_id
                AND d.status='approved'
                AND {exact(extracted('snapshot.decision_digest'), 'd.envelope_hmac')})
        OR NOT EXISTS (SELECT 1 FROM {user} u WHERE u.id=NEW.actor_id AND u.is_active=1 AND u.is_staff=1)"""
    immutable = {kind: " OR ".join(changed(name) for name in IMMUTABLE[kind]) for kind in TABLES}
    execution_update = f"""({immutable['execution']}) OR NOT ({open_client}) OR ({execution_terminal})"""
    image_transition = """NOT (
        (OLD.state='active' AND NEW.state='delete_pending')
        OR (OLD.state='delete_pending' AND NEW.state='deleting')
        OR (OLD.state='deleting' AND NEW.state IN ('deleted','delete_failed'))
        OR (OLD.state='delete_failed' AND NEW.state IN ('delete_pending','deleting')))"""
    image_update = f"""({immutable['image']}) OR ({image_transition})
        OR (NEW.state='deleted' AND NEW.erased_at IS NULL) OR (NEW.state<>'deleted' AND NEW.erased_at IS NOT NULL)
        OR (NEW.state='delete_failed' AND NEW.failure_code='')"""
    inserts = {"request": request_insert, "decision": decision_insert, "execution": execution_initial,
        "image": image_insert, "stock_sync": stock_sync_insert}
    updates = {"request": "1=1", "decision": "1=1", "execution": execution_update,
        "image": image_update, "stock_sync": "1=1"}
    with connection.cursor() as cursor:
        for kind, table in TABLES.items():
            privacy = "EXISTS (SELECT 1 FROM management_igclient c WHERE c.id=OLD.client_id AND c.privacy_erasure_started_at IS NOT NULL)"
            for event, invalid in (("insert", inserts[kind]), ("update", updates[kind]), ("delete", "NOT " + privacy)):
                name = f"ig_assist_{kind}_{event}"
                cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
                if mysql:
                    cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {table} FOR EACH ROW "
                        f"BEGIN IF COALESCE(({invalid}),1) THEN SIGNAL SQLSTATE '45000' "
                        "SET MESSAGE_TEXT='Assisted immutable/owner/privacy/lifecycle guard'; END IF; END")
                else:
                    cursor.execute(f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {table} WHEN COALESCE(({invalid}),1) "
                        "BEGIN SELECT RAISE(ABORT,'Assisted immutable/owner/privacy/lifecycle guard'); END")


def remove_guards(apps, schema_editor):
    del apps
    with schema_editor.connection.cursor() as cursor:
        for kind in TABLES:
            for event in ("insert", "update", "delete"):
                cursor.execute(f"DROP TRIGGER IF EXISTS ig_assist_{kind}_{event}")


# SIGNED, IMMUTABLE and model operations are generated below
# from the private model source by AST only; no Django/model import was executed.

SIGNED = {'request': [('id', 'uuid'), ('intention_id', 'uuid'), ('version', 'scalar'), ('operation_id', 'uuid'), ('client_id', 'scalar'), ('task_id', 'scalar', True), ('source_id', 'scalar', True), ('revision_id', 'scalar', True), ('episode_id', 'scalar', True), ('deal_id', 'scalar', True), ('source_namespace', 'text'), ('reset_floor', 'scalar'), ('client_permission_epoch', 'scalar'), ('full_capture_digest', 'text'), ('snapshot_digest', 'text'), ('key_id', 'text')], 'decision': [('id', 'uuid'), ('request_id', 'uuid'), ('client_id', 'scalar'), ('actor_id', 'scalar'), ('version', 'scalar'), ('operation_id', 'uuid'), ('status', 'text'), ('request_digest', 'text'), ('draft_digest', 'text'), ('full_capture_digest', 'text'), ('snapshot_digest', 'text'), ('currency', 'text'), ('approved_total', 'decimal'), ('approved_custom_quantity', 'scalar'), ('physical_checked_at', 'date', True), ('expires_at', 'date', True), ('key_id', 'text')], 'execution': [('id', 'uuid'), ('intention_id', 'uuid'), ('operation_id', 'uuid'), ('request_id', 'uuid'), ('decision_id', 'uuid'), ('proposal_id', 'scalar'), ('client_id', 'scalar'), ('episode_id', 'scalar'), ('choice_source_id', 'scalar', True), ('purchase_source_id', 'scalar'), ('revision_id', 'scalar', True), ('scope', 'text'), ('scope_digest', 'text'), ('request_digest', 'text'), ('decision_digest', 'text'), ('full_capture_digest', 'text'), ('snapshot_digest', 'text'), ('currency', 'text'), ('quoted_total', 'decimal'), ('quoted_custom_quantity', 'scalar'), ('expires_at', 'date'), ('key_id', 'text')], 'image': [('id', 'uuid'), ('request_id', 'uuid'), ('client_id', 'scalar'), ('uploader_id', 'scalar'), ('operation_id', 'uuid'), ('storage_key', 'text'), ('content_digest', 'text'), ('mime_type', 'text'), ('byte_size', 'scalar'), ('width', 'scalar'), ('height', 'scalar'), ('snapshot_digest', 'text'), ('delete_after', 'date'), ('key_id', 'text')], 'stock_sync': [('id', 'uuid'), ('decision_id', 'uuid'), ('client_id', 'scalar'), ('actor_id', 'scalar'), ('operation_id', 'uuid'), ('snapshot_digest', 'text'), ('key_id', 'text')]}
IMMUTABLE = {'request': ['envelope', 'key_id', 'envelope_hmac', 'created_at', 'id', 'intention_id', 'version', 'operation_id', 'client_id', 'task_id', 'source_id', 'revision_id', 'episode_id', 'deal_id', 'source_namespace', 'reset_floor', 'client_permission_epoch', 'full_capture_digest', 'snapshot_digest'], 'decision': ['envelope', 'key_id', 'envelope_hmac', 'created_at', 'id', 'request_id', 'client_id', 'actor_id', 'version', 'operation_id', 'status', 'request_digest', 'draft_digest', 'full_capture_digest', 'snapshot_digest', 'currency', 'approved_total', 'approved_custom_quantity', 'physical_checked_at', 'expires_at'], 'execution': ['envelope', 'key_id', 'envelope_hmac', 'created_at', 'id', 'intention_id', 'operation_id', 'request_id', 'decision_id', 'proposal_id', 'client_id', 'episode_id', 'choice_source_id', 'purchase_source_id', 'revision_id', 'scope', 'scope_digest', 'request_digest', 'decision_digest', 'full_capture_digest', 'snapshot_digest', 'currency', 'quoted_total', 'quoted_custom_quantity', 'expires_at'], 'image': ['envelope', 'key_id', 'envelope_hmac', 'created_at', 'id', 'request_id', 'client_id', 'uploader_id', 'operation_id', 'storage_key', 'content_digest', 'mime_type', 'byte_size', 'width', 'height', 'snapshot_digest', 'delete_after'], 'stock_sync': ['envelope', 'key_id', 'envelope_hmac', 'created_at', 'id', 'decision_id', 'client_id', 'actor_id', 'operation_id', 'snapshot_digest']}


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0232_split_shipping"), ("orders", "0059_order_delivery_and_dtf_film"),
        ("product_catalog", "0008_product_inventory_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        ResumableAssistedCreateModel(
            name='IgAssistedCommerceRequest',
            fields=[
                ('envelope', models.JSONField(default=dict)),
                ('key_id', models.CharField(max_length=24)),
                ('envelope_hmac', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('intention_id', models.UUIDField(editable=False)),
                ('version', models.PositiveIntegerField()),
                ('operation_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ('client', models.ForeignKey('management.igclient', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_requests', db_constraint=False)),
                ('task', models.ForeignKey('management.igfollowuptask', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_requests', db_constraint=False)),
                ('source', models.ForeignKey('management.instagrambotmessage', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_requests', db_constraint=False)),
                ('revision', models.ForeignKey('management.igcustomerturnrevision', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_requests', db_constraint=False)),
                ('episode', models.ForeignKey('management.igcommercialepisode', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_requests', db_constraint=False)),
                ('deal', models.ForeignKey('management.igdeal', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_requests', db_constraint=False)),
                ('source_namespace', models.CharField(max_length=128)),
                ('reset_floor', models.PositiveBigIntegerField()),
                ('client_permission_epoch', models.PositiveBigIntegerField()),
                ('full_capture_digest', models.CharField(max_length=64)),
                ('snapshot_digest', models.CharField(max_length=64)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=['intention_id', 'version'], name='ig_assist_request_version'), models.CheckConstraint(condition=models.Q(version__gte=1) & models.Q(reset_floor__gte=1), name='ig_assist_request_bounds')], 'indexes': [models.Index(fields=['client', 'created_at'], name='ig_assist_request_client')]},
        ),
        ResumableAssistedCreateModel(
            name='IgAssistedCommerceDecision',
            fields=[
                ('envelope', models.JSONField(default=dict)),
                ('key_id', models.CharField(max_length=24)),
                ('envelope_hmac', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('request', models.ForeignKey('management.igassistedcommercerequest', on_delete=django.db.models.deletion.PROTECT, related_name='decisions', db_constraint=False)),
                ('client', models.ForeignKey('management.igclient', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_decisions', db_constraint=False)),
                ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_decisions', db_constraint=False)),
                ('version', models.PositiveIntegerField()),
                ('operation_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ('status', models.CharField(max_length=16, choices=[('draft', 'Draft'), ('approved', 'Approved exact intention'), ('rejected', 'Rejected')])),
                ('request_digest', models.CharField(max_length=64)),
                ('draft_digest', models.CharField(max_length=64)),
                ('full_capture_digest', models.CharField(max_length=64)),
                ('snapshot_digest', models.CharField(max_length=64)),
                ('currency', models.CharField(max_length=8, default='UAH')),
                ('approved_total', models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                ('approved_custom_quantity', models.PositiveIntegerField(default=0)),
                ('physical_checked_at', models.DateTimeField(null=True, blank=True)),
                ('expires_at', models.DateTimeField(null=True, blank=True)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=['request', 'version'], name='ig_assist_decision_version'), models.CheckConstraint(condition=models.Q(version__gte=1) & models.Q(approved_total__gte=0) & models.Q(approved_total__lte=1000000) & models.Q(approved_custom_quantity__lte=50), name='ig_assist_decision_bounds')], 'indexes': [models.Index(fields=['request', 'status', 'version'], name='ig_assist_decision_head')]},
        ),
        ResumableAssistedCreateModel(
            name='IgAssistedCommerceExecution',
            fields=[
                ('envelope', models.JSONField(default=dict)),
                ('key_id', models.CharField(max_length=24)),
                ('envelope_hmac', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('intention_id', models.UUIDField(editable=False)),
                ('operation_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ('request', models.ForeignKey('management.igassistedcommercerequest', on_delete=django.db.models.deletion.PROTECT, related_name='executions', db_constraint=False)),
                ('decision', models.OneToOneField('management.igassistedcommercedecision', on_delete=django.db.models.deletion.PROTECT, related_name='execution', db_constraint=False)),
                ('proposal', models.OneToOneField('management.igcheckoutproposal', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_execution', db_constraint=False)),
                ('client', models.ForeignKey('management.igclient', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_executions', db_constraint=False)),
                ('episode', models.ForeignKey('management.igcommercialepisode', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_executions', db_constraint=False)),
                ('choice_source', models.ForeignKey('management.instagrambotmessage', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_choices', db_constraint=False)),
                ('purchase_source', models.ForeignKey('management.instagrambotmessage', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_purchases', db_constraint=False)),
                ('revision', models.ForeignKey('management.igcustomerturnrevision', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_executions', db_constraint=False)),
                ('scope', models.CharField(max_length=16, choices=[('full', 'Full intention'), ('partial', 'Blocked: customer-accepted selection (future continuation)')])),
                ('scope_digest', models.CharField(max_length=64)),
                ('request_digest', models.CharField(max_length=64)),
                ('decision_digest', models.CharField(max_length=64)),
                ('full_capture_digest', models.CharField(max_length=64)),
                ('snapshot_digest', models.CharField(max_length=64)),
                ('currency', models.CharField(max_length=8, default='UAH')),
                ('quoted_total', models.DecimalField(max_digits=12, decimal_places=2)),
                ('quoted_custom_quantity', models.PositiveIntegerField(default=0)),
                ('expires_at', models.DateTimeField()),
                ('active_slot', models.PositiveSmallIntegerField(null=True, blank=True, default=1)),
                ('order', models.OneToOneField('orders.order', on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name='assisted_commerce_execution', db_constraint=False)),
                ('settled_at', models.DateTimeField(null=True, blank=True)),
                ('released_at', models.DateTimeField(null=True, blank=True)),
                ('release_reason', models.CharField(max_length=64, blank=True, default='')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=['intention_id', 'active_slot'], name='ig_assist_intention_active'), models.CheckConstraint(condition=models.Q(active_slot__isnull=True) | models.Q(active_slot=1), name='ig_assist_active_shape'), models.CheckConstraint(condition=models.Q(quoted_total__gt=0) & models.Q(quoted_total__lte=1000000) & models.Q(quoted_custom_quantity__lte=50), name='ig_assist_execution_bounds')], 'indexes': [models.Index(fields=['client', 'created_at'], name='ig_assist_execution_client')]},
        ),
        ResumableAssistedCreateModel(
            name='IgAssistedCommerceImage',
            fields=[
                ('envelope', models.JSONField(default=dict)),
                ('key_id', models.CharField(max_length=24)),
                ('envelope_hmac', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('request', models.ForeignKey('management.igassistedcommercerequest', on_delete=django.db.models.deletion.PROTECT, related_name='images', db_constraint=False)),
                ('client', models.ForeignKey('management.igclient', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_images', db_constraint=False)),
                ('uploader', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_images', db_constraint=False)),
                ('operation_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ('storage_key', models.CharField(max_length=128, unique=True)),
                ('content_digest', models.CharField(max_length=64)),
                ('mime_type', models.CharField(max_length=32)),
                ('byte_size', models.PositiveIntegerField()),
                ('width', models.PositiveIntegerField()),
                ('height', models.PositiveIntegerField()),
                ('snapshot_digest', models.CharField(max_length=64)),
                ('delete_after', models.DateTimeField()),
                ('state', models.CharField(max_length=16, choices=[('active', 'Active owned image'), ('delete_pending', 'Deletion pending'), ('deleting', 'Deletion in progress'), ('deleted', 'Deleted'), ('delete_failed', 'Deletion retry required')], default='active', db_index=True)),
                ('erased_at', models.DateTimeField(null=True, blank=True)),
                ('failure_code', models.CharField(max_length=64, blank=True, default='')),
            ],
            options={'constraints': [models.CheckConstraint(condition=models.Q(byte_size__gte=1) & models.Q(byte_size__lte=6291456) & models.Q(width__gte=1) & models.Q(height__gte=1), name='ig_assist_image_bounds')], 'indexes': [models.Index(fields=['state', 'delete_after'], name='ig_assist_image_erasure')]},
        ),
        ResumableAssistedCreateModel(
            name='IgAssistedCommerceStockSync',
            fields=[
                ('envelope', models.JSONField(default=dict)),
                ('key_id', models.CharField(max_length=24)),
                ('envelope_hmac', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('decision', models.OneToOneField('management.igassistedcommercedecision', on_delete=django.db.models.deletion.PROTECT, related_name='stock_sync', db_constraint=False)),
                ('client', models.ForeignKey('management.igclient', on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_stock_syncs', db_constraint=False)),
                ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT, related_name='assisted_commerce_stock_syncs', db_constraint=False)),
                ('operation_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ('snapshot_digest', models.CharField(max_length=64)),
            ],
            options={},
        ),
        migrations.RunPython(ensure_transactions, migrations.RunPython.noop),
        migrations.RunPython(install_guards, remove_guards),
    ]
