"""Run Instagram gates against their real migration dependency graph only.

Unrelated application leaf migrations do not prove Instagram correctness and
may require legacy production engines. This runner never marks them applied.
The parent settings enforce disposable credentials, loopback and no network.
"""

from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.core.management.sql import emit_post_migrate_signal
from django.test import TestCase, TransactionTestCase
from django.test.runner import DiscoverRunner


def _truncate_disposable(instance):
    """Append-only SQL triggers require TRUNCATE, never journal DELETE."""
    _truncate_aliases(instance._databases_names(include_mirrors=False))


def _truncate_aliases(aliases):
    for alias in aliases:
        database = connections[alias]
        if not database.settings_dict["NAME"].startswith("test_twocomms_ig_"):
            raise RuntimeError("Instagram teardown requires its disposable database")
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
        name = connection.settings_dict["NAME"]
        if not name.startswith("test_twocomms_ig_") or connection.vendor != "mysql":
            raise RuntimeError("Instagram migration gate requires disposable MariaDB")
        with connection._nodb_cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {connection.ops.quote_name(name)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        executor = MigrationExecutor(connection)
        # The production predecessor uses MariaDB's unique HASH endpoint
        # index. Django's fresh-database migration instead creates BTREE at
        # this length; 0097 deliberately asserts the legacy HASH contract.
        # Recreate that empty predecessor fixture without faking a migration.
        prerequisite = ("storefront", "0096_alter_catalogcolorseooverride_body_html_and_more")
        if ("storefront", "0097_mariadb_generated_uniqueness") not in executor.loader.applied_migrations:
            executor.migrate([prerequisite])
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM storefront_webpushdevicesubscription")
                if cursor.fetchone()[0]:
                    raise RuntimeError("HASH predecessor fixture requires an empty disposable table")
                cursor.execute(
                    "ALTER TABLE storefront_webpushdevicesubscription "
                    "DROP INDEX endpoint, ADD UNIQUE INDEX endpoint (endpoint) USING HASH"
                )
            executor = MigrationExecutor(connection)
        # Human actor erasure traverses the installed User relations. Apply
        # their actual schema, without unrelated later Finance features. Its
        # legacy predecessor creates FundingSource as MyISAM while our fresh
        # Transaction fixture is InnoDB; prepare only this empty parent before
        # the real 0020 FK operation (no migration is marked as faked).
        if ("finance", "0020_finance_v2_classification_audit") not in executor.loader.applied_migrations:
            executor.migrate([("finance", "0019_finance_v2_ledger")])
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM finance_fundingsource")
                if cursor.fetchone()[0]:
                    raise RuntimeError("Finance FK predecessor requires an empty disposable table")
                cursor.execute("ALTER TABLE finance_fundingsource ENGINE=InnoDB")
            executor = MigrationExecutor(connection)
        targets = []
        # Actor deletion follows auth's admin.LogEntry relation as well; its
        # real schema is required to verify SET_NULL/CASCADE receipt privacy.
        for app in ("management", "admin", "sessions", "sites", "accounts", "product_catalog", "productcolors"):
            targets += executor.loader.graph.leaf_nodes(app)
        targets += [("storefront", "0098_sqlite_generated_fit_identity")]
        targets += [
            ("reviews", "0002_mariadb_vote_uniqueness"),
            ("social_django", "0017_usersocialauth_user_social_auth_uid_required"),
            ("finance", "0020_finance_v2_classification_audit"),
        ]
        executor.migrate(targets)
        _truncate_aliases(("default",))
        return [(connection, name, False)]

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
                    if original not in classes:
                        classes[original] = type(original.__name__, (original,), {
                            "_fixture_teardown": _truncate_disposable,
                            # TRUNCATE resets all existing sequences before
                            # and after cases. Global model introspection would
                            # include tables outside this migration scope.
                            "_reset_sequences": staticmethod(lambda db_name: None),
                            "__module__": original.__module__,
                        })
                    item.__class__ = classes[original]
        adapt(suite)
        return suite
