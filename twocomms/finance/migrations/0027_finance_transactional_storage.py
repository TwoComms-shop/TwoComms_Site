"""Financial settlements require transactional tables on MySQL/MariaDB."""
from django.db import migrations


def enable_transactional_storage(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'mysql':
        return
    names = {m._meta.db_table for m in apps.get_app_config('finance').get_models(include_auto_created=True)
             if m._meta.managed and not m._meta.proxy}
    with connection.cursor() as cursor:
        cursor.execute('SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()')
        engines = dict(cursor.fetchall())
        for name in sorted(names):
            if name in engines and (engines[name] or '').lower() != 'innodb':
                cursor.execute(f'ALTER TABLE {schema_editor.quote_name(name)} ENGINE=InnoDB')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('finance', '0026_fundingsource_opening_amount')]
    # Keep transactional storage on rollback as well; reverting code must not
    # reintroduce partially committed financial operations.
    operations = [migrations.RunPython(enable_transactional_storage, migrations.RunPython.noop)]
