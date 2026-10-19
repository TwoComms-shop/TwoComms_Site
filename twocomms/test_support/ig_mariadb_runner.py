"""Use Django's full migration graph and disposable journal-safe cleanup.

The parent settings enforce disposable credentials, loopback and no network.
The gate CLI prepares migration predecessor canaries before invoking Django.
"""

from django.db import connection, connections
from django.core.management.sql import emit_post_migrate_signal
from django.test import TestCase, TransactionTestCase
from django.test.runner import DiscoverRunner


def _truncate_disposable(instance):
    """Append-only SQL triggers require TRUNCATE, never journal DELETE."""
    _truncate_aliases(instance._databases_names(include_mirrors=False))


def _truncate_aliases(aliases):
    for alias in aliases:
        database = connections[alias]
        if database.vendor != "mysql" or not str(database.settings_dict.get("NAME") or "").startswith("test_twocomms_ig_"):
            raise RuntimeError("Instagram teardown requires its disposable MariaDB database")
        tables = database.introspection.django_table_names(only_existing=True)
        with database.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                for table in tables:
                    if table != "django_migrations":
                        cursor.execute(f"TRUNCATE TABLE {database.ops.quote_name(table)}")
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        # Match Django's normal flush contract: recreate content types and
        # capabilities after removing test rows. Migration history is retained.
        emit_post_migrate_signal(verbosity=0, interactive=False, db=alias)


class InstagramMariaDbGateRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        name = str(connection.settings_dict.get("NAME") or "")
        test_name = str(connection.settings_dict.get("TEST", {}).get("NAME") or "")
        if (connection.vendor != "mysql" or not name.startswith("test_twocomms_ig_")
                or not test_name.startswith("test_twocomms_ig_")):
            raise RuntimeError("Instagram migration gate requires disposable MariaDB")
        return super().setup_databases(**kwargs)

    def build_suite(self, *args, **kwargs):
        suite = super().build_suite(*args, **kwargs)
        # Fixtures still exercise real locks/CAS/constraints. Only the generic
        # DELETE-based fixture cleanup changes for append-only journal tables.
        classes = {}
        def adapt(items):
            for item in items:
                if hasattr(item, "__iter__"):
                    adapt(item)
                elif isinstance(item, TransactionTestCase) and not isinstance(item, TestCase):
                    original = type(item)
                    if original._fixture_teardown is not TransactionTestCase._fixture_teardown:
                        continue
                    if original not in classes:
                        classes[original] = type(original.__name__, (original,), {
                            "_fixture_teardown": _truncate_disposable,
                            "__module__": original.__module__,
                        })
                    item.__class__ = classes[original]
        adapt(suite)
        return suite
