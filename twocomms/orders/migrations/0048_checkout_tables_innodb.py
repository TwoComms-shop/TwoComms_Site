from django.db import migrations


CHECKOUT_TRANSACTION_TABLES = (
    "orders_order",
    "orders_orderitem",
    "orders_checkoutcapture",
    "storefront_utmsession",
    "storefront_useraction",
    "storefront_sitesession",
    "storefront_customprintlead",
    "storefront_promocode",
    "storefront_promocodeusage",
    "django_session",
)


def convert_checkout_tables_to_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return

    quote_name = schema_editor.connection.ops.quote_name
    with schema_editor.connection.cursor() as cursor:
        for table_name in CHECKOUT_TRANSACTION_TABLES:
            cursor.execute(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                [table_name],
            )
            row = cursor.fetchone()
            if row and str(row[0]).lower() != "innodb":
                schema_editor.execute(
                    f"ALTER TABLE {quote_name(table_name)} ENGINE=InnoDB"
                )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("orders", "0047_checkoutcapture"),
        ("storefront", "0080_useraction_site_session_index"),
    ]

    operations = [
        migrations.RunPython(
            convert_checkout_tables_to_innodb,
            migrations.RunPython.noop,
        ),
    ]
