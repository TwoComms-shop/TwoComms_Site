from django.db import migrations


ANALYSIS_TABLES = (
    "management_igconversationanalysissnapshot",
    "management_igconversationanalysisjob",
    "management_geminikeystate",
)


def convert_analysis_tables_to_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    quote = schema_editor.quote_name
    with schema_editor.connection.cursor() as cursor:
        for table in ANALYSIS_TABLES:
            cursor.execute(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                [table],
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(
                    f"required analysis table is missing: {table}"
                )
            if str(row[0]).lower() != "innodb":
                schema_editor.execute(f"ALTER TABLE {quote(table)} ENGINE=InnoDB")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("management", "0095_ig_conversation_analysis_jobs"),
    ]

    operations = [
        migrations.RunPython(convert_analysis_tables_to_innodb, migrations.RunPython.noop),
    ]
