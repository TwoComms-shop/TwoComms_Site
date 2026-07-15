from django.apps import apps
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase
from django.test.utils import override_settings


class Fable5V2SchemaSafetyContractTests(SimpleTestCase):
    external_relations = (
        ("VariantImageAltI18n", "color_image"),
        ("ProductImageAltI18n", "product_image"),
        ("ProductOptionProfile", "product"),
        ("VariantCombinationProfile", "variant"),
        ("GarmentFlowCategory", "category"),
        ("ProductPrintLink", "product"),
        ("ProductPrintLink", "print_ref"),
        ("SizeGridProfile", "size_grid"),
        ("ProductOptionSizeGrid", "product"),
        ("ProductOptionSizeGrid", "size_grid"),
        ("VariantOptionSizeGrid", "variant"),
        ("VariantOptionSizeGrid", "size_grid"),
        ("VariantBlankLink", "variant"),
        ("VariantBlankLink", "storage_subcategory"),
        ("ProductSizeRule", "product"),
        ("CoverSource", "product"),
        ("CoverSource", "color_image"),
        ("CoverSource", "product_image"),
        ("ProductEditorState", "product"),
        ("ProductEditorState", "updated_by"),
        ("EditorDraft", "user"),
        ("EditorDraft", "product"),
    )

    def test_every_v2_relation_to_a_legacy_table_is_unconstrained(self):
        for model_name, field_name in self.external_relations:
            field = apps.get_model("fable5", model_name)._meta.get_field(field_name)
            self.assertFalse(
                field.db_constraint,
                f"{model_name}.{field_name} must remain compatible with production MyISAM",
            )

    @override_settings(MIGRATION_MODULES={})
    def test_fable5_migration_dependencies_do_not_mutate_legacy_apps(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        fable_nodes = [
            node
            for node in loader.graph.node_map
            if node[0] == "fable5"
        ]

        self.assertTrue(fable_nodes)
        for node in fable_nodes:
            migration = loader.get_migration(*node)
            for operation in migration.operations:
                self.assertNotIn(
                    getattr(operation, "model_name", ""),
                    {
                        "product",
                        "sizegrid",
                        "productcolorvariant",
                        "productcolorimage",
                        "print",
                    },
                    f"Fable migration {node} must not alter a legacy table",
                )
