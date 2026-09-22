import datetime

from django.db import migrations


# Runtime ranking/admission reads the calibrated active version.  Add 3.8 to
# that immutable profile set so the new model participates in the same V2
# accounting path as 3.7/3.6/3.5 rather than remaining invisible to it.
PROFILE_VERSION = "production-observed-2026-08-31.v2"
MODEL = "gemini-3.8-flash"


def seed_gemini_38_profile(apps, schema_editor):
    del schema_editor
    profile_model = apps.get_model("management", "GeminiQuotaProfile")
    observed_at = datetime.datetime(
        2026, 9, 23, 0, 0, tzinfo=datetime.timezone.utc
    )
    expected = {
        "rpm_limit": 5,
        "input_tpm_limit": 250_000,
        "rpd_limit": 20,
        "permit_limit": 1,
        "estimator_version": "shadow-calibration-required",
        "source": "admin",
        "source_reference": (
            "owner_google_ai_studio_quota_2026-09-23;generation_verified_api4_2026-09-23"
        ),
        "observed_at": observed_at,
        "effective_from": observed_at,
        "effective_until": None,
    }
    row, created = profile_model.objects.get_or_create(
        profile_version=PROFILE_VERSION,
        model=MODEL,
        defaults=expected,
    )
    if not created:
        fields = tuple(expected)
        drift = [field for field in fields if getattr(row, field) != expected[field]]
        if drift:
            raise RuntimeError(
                "Gemini 3.8 quota profile drift: " + ", ".join(sorted(drift))
            )


class Migration(migrations.Migration):
    dependencies = [("management", "0214_ig_technical_debt_case")]

    operations = [
        migrations.RunPython(seed_gemini_38_profile, migrations.RunPython.noop),
    ]
