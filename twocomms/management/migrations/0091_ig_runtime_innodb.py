from django.db import migrations


IG_RUNTIME_TABLES = (
    "management_igclient",
    "management_igclientstageevent",
    "management_igconversationsignal",
    "management_igdeal",
    "management_igdealitem",
    "management_igfollowuptask",
    "management_igmetaeventlog",
    "management_instagrambotlog",
    "management_instagrambotmessage",
    "management_instagrambotprocessedmessage",
    "management_instagrambotrawevent",
    "management_instagrambotsettings",
)


def convert_ig_runtime_to_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    quote = schema_editor.quote_name
    with schema_editor.connection.cursor() as cursor:
        for table in IG_RUNTIME_TABLES:
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
        ("management", "0090_payment_truth_projection"),
    ]

    operations = [
        migrations.RunPython(convert_ig_runtime_to_innodb, migrations.RunPython.noop),
    ]
