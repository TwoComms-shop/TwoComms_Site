from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase


class RestockMigrationCompatibilityTests(SimpleTestCase):
    legacy_relations = ("product", "color_variant", "user")

    def test_legacy_myisam_relations_do_not_create_database_constraints(self):
        runtime_model = apps.get_model("storefront", "RestockSubscription")
        migration = import_module(
            "storefront.migrations.0084_restocksubscription"
        )
        create_model = next(
            operation
            for operation in migration.Migration.operations
            if operation.name == "RestockSubscription"
        )
        migration_fields = dict(create_model.fields)

        for field_name in self.legacy_relations:
            self.assertFalse(
                runtime_model._meta.get_field(field_name).db_constraint,
                f"runtime RestockSubscription.{field_name} must support production MyISAM",
            )
            self.assertFalse(
                migration_fields[field_name].db_constraint,
                f"migration RestockSubscription.{field_name} must support production MyISAM",
            )
