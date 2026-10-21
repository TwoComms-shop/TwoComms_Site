"""Bind native restock poll sources to the application's verified proof witness.

Python verifies the provider HMAC and signs the restock envelope. SQL binds its
poll witness to the exact current persisted proof and sealed source; it does
not claim to verify a Django signing key. Existing webhook predicates, other
tables, immutability and privacy-delete guards remain unchanged.
"""
from django.db import migrations


def _poll_source(mysql):
    value = lambda expression, path: (f"JSON_UNQUOTE(JSON_EXTRACT({expression},'{path}'))" if mysql
        else f"json_extract({expression},'{path}')")
    equal = lambda left, right: f"BINARY {left}=BINARY {right}" if mysql else f"{left}={right}"
    signed = value("m.ingress_proof", "$.signed")
    digest = f"SHA2({signed},256)" if mysql else f"SHA256({signed})"
    revision_id = value("NEW.envelope", "$.source.revision_id")
    return f"""(m.source='poll' AND m.status NOT IN ('failed','ignored')
        AND {equal(value('m.ingress_proof', '$.version'), "'current-provider-poll-v1'")}
        AND length({signed})>0
        AND {equal(value('NEW.envelope', '$.source.poll_admission.version'), "'current-provider-poll-v1'")}
        AND {equal(value('NEW.envelope', '$.source.poll_admission.signed_digest'), digest)}
        AND {value('NEW.envelope', '$.source.message_id')}=m.id
        AND {equal(value('NEW.envelope', '$.source.mid'), 'm.mid')}
        AND {equal(value('NEW.envelope', '$.source.namespace'), 'm.provider_namespace')}
        AND m.id>COALESCE((SELECT MAX(f.reset_after_message_id)
            FROM management_igfunnelresetaudit f WHERE f.client_id=c.id),0)
        AND EXISTS (SELECT 1 FROM management_igcustomerturnrevision pr
            JOIN management_igturnrevisionsource ps ON ps.revision_id=pr.id AND ps.message_id=m.id
            WHERE pr.id={revision_id} AND pr.client_id=c.id AND ps.role='user'
              AND pr.permission_epoch=c.reply_permission_epoch
              AND pr.erasure_started_at_snapshot IS NULL
              AND {equal('ps.source_namespace', 'm.provider_namespace')}
              AND {equal(value('NEW.envelope', '$.source.digest'), 'ps.source_digest')}
              AND {equal(value('NEW.envelope', '$.source.revision_digest'), 'pr.snapshot_digest')}))"""


def source_guards(apps, schema_editor, *, poll_enabled=True):
    del apps
    connection = schema_editor.connection
    if connection.vendor not in {"mysql", "sqlite"}:
        raise RuntimeError("Restock source guards require MariaDB or SQLite")
    mysql = connection.vendor == "mysql"
    hmac_invalid = ("BINARY NEW.envelope_hmac NOT REGEXP '^[0-9a-f]{64}$'" if mysql else
        "(length(NEW.envelope_hmac)<>64 OR NEW.envelope_hmac GLOB '*[^0-9a-f]*')")
    common = f"{hmac_invalid} OR NEW.key_id=''"
    # When disabled these are the exact original 0230 source predicates.
    source = f"(m.source='webhook' OR {_poll_source(mysql)})" if poll_enabled else "m.source='webhook'"
    inv, rec = "management_igrestockinvitation", "management_igrestockreceipt"
    inserts = {
        "inv": f"""{common} OR NEW.locale NOT IN ('uk','ru','en') OR NEW.body=''
            OR NEW.button_title='' OR NEW.accept_payload='' OR NEW.revoke_payload=''
            OR NEW.accept_payload=NEW.revoke_payload OR NEW.acceptance_deadline>=NEW.expires_at
            OR NOT EXISTS (SELECT 1 FROM management_igclient c
                JOIN management_instagrambotmessage m ON m.id=NEW.request_source_id
                JOIN management_igcustomerturnrevision r ON r.id=NEW.revision_id
                JOIN management_igturnrevisionsource s ON s.revision_id=r.id AND s.message_id=m.id
                WHERE c.id=NEW.client_id AND c.privacy_erasure_started_at IS NULL
                  AND m.client_id=c.id AND m.sender_id=c.igsid AND m.role='user'
                  AND {source} AND m.mid<>'' AND m.provider_created_at IS NOT NULL
                  AND r.client_id=c.id AND s.role='user'
                  AND {('BINARY s.source_namespace=BINARY m.provider_namespace' if mysql else 's.source_namespace=m.provider_namespace')}
                  AND NEW.acceptance_deadline>m.provider_created_at)""",
        "ans": f"""{common} OR NEW.kind NOT IN ('accept','revoke') OR NOT EXISTS (
            SELECT 1 FROM {inv} i JOIN {rec} r ON r.invitation_id=i.id AND r.id=NEW.receipt_id
              JOIN management_igclient c ON c.id=i.client_id
              JOIN management_instagrambotmessage m ON m.id=NEW.source_id
            WHERE i.id=NEW.invitation_id AND c.privacy_erasure_started_at IS NULL
              AND m.client_id=c.id AND m.sender_id=c.igsid AND m.role='user' AND {source}
              AND m.mid<>'' AND m.provider_created_at=NEW.answered_at
              AND NEW.answered_at>=r.delivered_at AND NEW.answered_at<i.expires_at
              AND (NEW.kind='revoke' OR NEW.answered_at<i.acceptance_deadline)
              AND {('BINARY m.quick_reply_payload=BINARY (CASE NEW.kind WHEN \'accept\' THEN i.accept_payload ELSE i.revoke_payload END)' if mysql else "m.quick_reply_payload=CASE NEW.kind WHEN 'accept' THEN i.accept_payload ELSE i.revoke_payload END")})""",
    }
    with connection.cursor() as cursor:
        for short, table in (("inv", inv), ("ans", "management_igrestockanswer")):
            if mysql:
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                if cursor.fetchone() != ("InnoDB",):
                    raise RuntimeError("Restock sources require transactional storage")
            name = f"ig_ful_{short}_insert"
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            invalid = inserts[short]
            if mysql:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW BEGIN IF {invalid} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Fulfilment immutable/source/privacy guard'; END IF; END")
            else:
                cursor.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN {invalid} BEGIN SELECT RAISE(ABORT,'Fulfilment immutable/source/privacy guard'); END")


def restore_webhook_guards(apps, schema_editor):
    source_guards(apps, schema_editor, poll_enabled=False)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0244_response_reliability_core_default")]
    operations = [migrations.RunPython(source_guards, restore_webhook_guards)]
