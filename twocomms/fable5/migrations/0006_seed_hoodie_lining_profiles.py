from django.db import migrations


def seed_hoodie_lining_profiles(apps, schema_editor):
    GarmentFlow = apps.get_model("fable5", "GarmentFlow")
    ProductOptionProfile = apps.get_model("fable5", "ProductOptionProfile")
    Product = apps.get_model("storefront", "Product")

    flow = GarmentFlow.objects.filter(code="hoodie").first()
    if flow is None:
        return
    category_ids = list(flow.categories.values_list("id", flat=True))
    for product_id in Product.objects.filter(
        category_id__in=category_ids
    ).values_list("id", flat=True):
        ProductOptionProfile.objects.update_or_create(
            product_id=product_id,
            option_key="lining=fleece",
            defaults={
                "option_values": {"lining": "fleece"},
                "is_active": True,
            },
        )
        ProductOptionProfile.objects.update_or_create(
            product_id=product_id,
            option_key="lining=no_fleece",
            defaults={
                "option_values": {"lining": "no_fleece"},
                "is_active": False,
                "price_delta_reason": "Тимчасово недоступно",
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("fable5", "0005_variant_resources"),
    ]

    operations = [
        migrations.RunPython(seed_hoodie_lining_profiles, migrations.RunPython.noop),
    ]
