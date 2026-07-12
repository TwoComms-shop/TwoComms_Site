from django.db import migrations


REPAIRS = {
    "last-breath": {
        "title": "Футболка «Череп З Трояндою»",
        "seo_old": "Футболка «last breath». — купити футболку TwoComms",
    },
    "last-breath-hd": {
        "title": "Худі «Череп З Трояндою»",
        "seo_old": "Худі «last breath». — купити худі TwoComms",
    },
    "last-breath-ls": {
        "title": "Лонгслів «Череп З Трояндою»",
        "seo_old": "лонгслів «last breath». — купити лонгслів TwoComms",
    },
    "death-grabs-ass": {
        "title": "Футболка «Серце Та Грощі»",
        "seo_old": "Футболка «death grabs ass» — купити футболку TwoComms",
    },
    "death-grabs-ass-hd": {
        "title": "Худі «Серце Та Грощі»",
        "seo_old": "Худі «death grabs ass». — купити худі TwoComms",
    },
    "death-grabs-ass-ls": {
        "title": "Лонгслів «Серце Та Грощі»",
        "seo_old": "лонгслів «death grabs ass». — купити лонгслів TwoComms",
    },
    "lord-of-the-lending": {
        "title": "Футболка «Це Моя Посадка»",
        "seo_old": "Футболка «Lord Of The Lending» — купити футболку TwoComms",
    },
    "lord-of-the-lending-hd": {
        "title": "Худі «Це Моя Посадка»",
        "seo_old": "Худі «Lord Of The Lending» — купити худі TwoComms",
    },
    "lord-of-the-lending-ls": {
        "title": "Лонгслів «Це Моя Посадка»",
        "seo_old": "Лонгслів «Lord Of The Lending» — купити лонгслів TwoComms",
    },
    "hoodie-silent-winter": {
        "title": "Худі «Дівчина Снайпер»",
        "seo_old": "Худі «Silent Winter» — купити худі TwoComms",
    },
    "death-gbs-ass-ts": {
        "title_old": "Футболка «Череп с дупою»",
        "title": "Футболка «Череп із дупою»",
        "seo_old": "Футболка «І На Той Світ З Собою Візьму» — TwoComms",
    },
    "death-gbs-ass-hd": {
        "title_old": "Худі «Череп с дупою»",
        "title": "Худі «Череп із дупою»",
        "seo_old": "Худі «І На Той Світ З Собою Візьму» — купити худі TwoComms",
    },
    "death-gbs-ass-ls": {
        "title_old": "Лонгслів «Череп с дупою»",
        "title": "Лонгслів «Череп із дупою»",
        "seo_old": "Лонгслів «І На Той Світ З Собою Візьму» — TwoComms",
    },
}


def _seo_new(payload):
    return f"{payload['title']} — купити в TwoComms"


def align_titles(apps, schema_editor):
    Product = apps.get_model("storefront", "Product")
    for slug, payload in REPAIRS.items():
        if payload.get("title_old"):
            for field in ("title", "title_uk"):
                Product.objects.filter(
                    slug=slug,
                    **{field: payload["title_old"]},
                ).update(**{field: payload["title"]})
        for field in ("seo_title", "seo_title_uk"):
            Product.objects.filter(
                slug=slug,
                **{field: payload["seo_old"]},
            ).update(**{field: _seo_new(payload)})


def reverse_alignment(apps, schema_editor):
    Product = apps.get_model("storefront", "Product")
    for slug, payload in REPAIRS.items():
        for field in ("seo_title", "seo_title_uk"):
            Product.objects.filter(
                slug=slug,
                **{field: _seo_new(payload)},
            ).update(**{field: payload["seo_old"]})
        if payload.get("title_old"):
            for field in ("title", "title_uk"):
                Product.objects.filter(
                    slug=slug,
                    **{field: payload["title"]},
                ).update(**{field: payload["title_old"]})


class Migration(migrations.Migration):
    dependencies = [
        ("storefront", "0081_repair_truncated_category_seo_titles"),
    ]

    operations = [
        migrations.RunPython(align_titles, reverse_alignment),
    ]
