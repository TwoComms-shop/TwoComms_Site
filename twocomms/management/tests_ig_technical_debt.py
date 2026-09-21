from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone


class TechnicalDebtCollectorTests(SimpleTestCase):
    def test_transient_rows_are_filtered_by_the_grace_window(self):
        from management.services import ig_technical_debt

        now = timezone.now()
        with (
            patch.object(ig_technical_debt, "_query_case", return_value={"count": 0}),
            patch("management.models.IgRevisionDeliveryEffect.objects.filter") as effects,
            patch("management.models.IgDeferredEcho.objects.filter") as echoes,
            patch("management.models.IgWebhookInboxEvent.objects.filter") as inbox,
        ):
            ig_technical_debt._collect_db(now, 10)

        effect_kwargs = [call.kwargs for call in effects.call_args_list]
        self.assertIn(
            {"state": "planned", "created_at__lt": now - ig_technical_debt.TRANSIENT_DEBT_GRACE},
            effect_kwargs,
        )
        self.assertIn(
            {
                "state__in": ("waiting_receipt", "ambiguous"),
                "observed_at__lt": now - ig_technical_debt.TRANSIENT_DEBT_GRACE,
            },
            [call.kwargs for call in echoes.call_args_list],
        )
        self.assertIn(
            {
                "decision": "accepted",
                "processed_at__isnull": True,
                "received_at__lt": now - ig_technical_debt.TRANSIENT_DEBT_GRACE,
            },
            [call.kwargs for call in inbox.call_args_list],
        )

    def test_capture_claim_ids_are_unique_and_preserved(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def iterator(self, **kwargs):
                started = (timezone.now() - timedelta(minutes=10)).isoformat()
                yield type("Row", (), {
                    "pk": 42,
                    "attachment_media": [
                        {"status": "acquiring", "capture_started_at": started},
                        {"status": "acquiring", "capture_started_at": started},
                    ],
                })()

        with (
            patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", "/private/tmp/media"),
            patch("management.services.ig_technical_debt.os.path.isdir", return_value=True),
            patch("management.services.ig_technical_debt.os.walk", return_value=[]),
        ):
            cases, complete = _collect_media(timezone.now(), 10)

        self.assertTrue(complete)
        self.assertEqual(cases[0]["count"], 1)
        self.assertEqual(cases[0]["sample_ids"], [42])

    def test_unset_media_root_is_optional_coverage_not_debt(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def iterator(self, **kwargs):
                return iter(())

        with (
            patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", ""),
        ):
            cases, complete = _collect_media(timezone.now(), 10)

        self.assertTrue(complete)
        self.assertEqual([case["reason"] for case in cases], ["media_capture_claim_expired"])

    def test_fingerprint_changes_for_new_sampled_case_identity_but_not_age(self):
        from management.services.ig_technical_debt import technical_debt_snapshot

        first = {
            "reason": "canonical_delivery_unknown",
            "scope": "delivery_effect",
            "count": 1,
            "oldest_age_seconds": 10,
            "sample_ids": [7],
            "has_more": False,
        }
        older = {**first, "count": 9, "oldest_age_seconds": 900}
        new_case = {**older, "sample_ids": [8]}
        with patch(
            "management.services.ig_technical_debt._collect_db",
            side_effect=[[first], [older], [new_case]],
        ), patch(
            "management.services.ig_technical_debt._collect_media",
            return_value=([], True),
        ):
            first_snapshot = technical_debt_snapshot()
            older_snapshot = technical_debt_snapshot()
            new_case_snapshot = technical_debt_snapshot()

        self.assertEqual(first_snapshot["fingerprint"], older_snapshot["fingerprint"])
        self.assertNotEqual(first_snapshot["fingerprint"], new_case_snapshot["fingerprint"])
