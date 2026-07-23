from django.db import migrations


OUTBOX_TABLES = (
    "management_igbotnotification",
    "management_igbotnotificationaudit",
)


def convert_outbox_to_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    quote = schema_editor.quote_name
    with schema_editor.connection.cursor() as cursor:
        for table in OUTBOX_TABLES:
            cursor.execute(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                [table],
            )
            row = cursor.fetchone()
            if row and str(row[0]).lower() != "innodb":
                schema_editor.execute(f"ALTER TABLE {quote(table)} ENGINE=InnoDB")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("management", "0093_notification_review_and_innodb"),
    ]

    operations = [
        migrations.RunPython(convert_outbox_to_innodb, migrations.RunPython.noop),
    ]
