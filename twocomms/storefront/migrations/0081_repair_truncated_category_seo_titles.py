from django.db import migrations


TITLE_REPAIRS = {
    "tshirts": (
        "Футболки TwoComms — стрітвеар та мілітарі-принти від",
        "Футболки TwoComms — стрітвеар і мілітарі-принти",
    ),
    "hoodie": (
        "Худі TwoComms — теплі толстовки зі стрітвеар-принтами та",
        "Худі TwoComms — теплі моделі зі стрітвеар-принтами",
    ),
    "long-sleeve": (
        "Лонгсліви TwoComms — лаконічний стрітвеар з рукавами на",
        "Лонгсліви TwoComms — стрітвеар на кожен день",
    ),
}


def repair_titles(apps, schema_editor):
    Category = apps.get_model("storefront", "Category")
    for slug, (broken, repaired) in TITLE_REPAIRS.items():
        # Exact-value guards preserve any copy changed by an editor after the
        # audit. Both columns were independently damaged on production.
        Category.objects.filter(slug=slug, seo_title=broken).update(
            seo_title=repaired
        )
        Category.objects.filter(slug=slug, seo_title_uk=broken).update(
            seo_title_uk=repaired
        )


def reverse_repairs(apps, schema_editor):
    Category = apps.get_model("storefront", "Category")
    for slug, (broken, repaired) in TITLE_REPAIRS.items():
        Category.objects.filter(slug=slug, seo_title=repaired).update(
            seo_title=broken
        )
        Category.objects.filter(slug=slug, seo_title_uk=repaired).update(
            seo_title_uk=broken
        )


class Migration(migrations.Migration):
    dependencies = [
        ("storefront", "0080_useraction_site_session_index"),
    ]

    operations = [
        migrations.RunPython(repair_titles, reverse_repairs),
    ]
