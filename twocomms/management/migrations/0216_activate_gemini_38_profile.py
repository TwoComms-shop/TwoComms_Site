import datetime

from django.db import migrations


PROFILE_VERSION = "production-observed-2026-08-31.v2"
MODEL = "gemini-3.8-flash"
EFFECTIVE_FROM = datetime.datetime(
    2026, 9, 22, 0, 0, tzinfo=datetime.timezone.utc
)


def activate_gemini_38_profile(apps, schema_editor):
    del schema_editor
    profile_model = apps.get_model("management", "GeminiQuotaProfile")
    updated = profile_model.objects.filter(
        profile_version=PROFILE_VERSION,
        model=MODEL,
    ).update(effective_from=EFFECTIVE_FROM)
    if updated != 1:
        raise RuntimeError(
            "Expected exactly one Gemini 3.8 profile to activate; "
            f"updated={updated}"
        )


class Migration(migrations.Migration):
    dependencies = [("management", "0215_gemini_38_quota_profile")]

    operations = [
        migrations.RunPython(activate_gemini_38_profile, migrations.RunPython.noop),
    ]
