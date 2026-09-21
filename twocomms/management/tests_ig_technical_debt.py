from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone


class TechnicalDebtCollectorTests(TestCase):
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

    def test_stale_legacy_inbound_pending_projects_without_mutation(self):
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.models import InstagramBotMessage
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        now = timezone.now()
        message = InstagramBotMessage.objects.create(
            sender_id="legacy-pending-fixture",
            role="user",
            status="pending",
            text="запит без обробки",
        )
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=now - timedelta(minutes=10),
        )

        with patch(
            "management.services.ig_technical_debt._collect_media",
            return_value=([], True),
        ):
            result = reconcile_ig_technical_debt_once(now=now, dry_run=True)

        proposal = next(
            case for case in result["proposed_cases"]
            if case["identity"] == "inbound_pending_unreconciled:legacy_message"
        )
        self.assertEqual(proposal["source_ids"], [message.pk])
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["coverage_reasons"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(IgTechnicalDebtCase.objects.count(), 0)

    def test_capture_claim_ids_are_unique_and_preserved(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def count(self):
                return 1

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

    def test_media_scan_cap_is_independent_of_report_limit(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def count(self):
                return 2

            def iterator(self, **kwargs):
                return iter(())

        with (
            patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", ""),
            override_settings(IG_TECHNICAL_DEBT_MEDIA_SCAN_CAP=2),
        ):
            cases, complete = _collect_media(timezone.now(), 1)

        self.assertTrue(complete)
        self.assertEqual(cases[0]["count"], 0)

    def test_media_scan_reports_incomplete_metadata_when_cap_is_exceeded(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def count(self):
                return 3

            def iterator(self, **kwargs):
                return iter(())

        with (
            patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", ""),
            override_settings(IG_TECHNICAL_DEBT_MEDIA_SCAN_CAP=2),
        ):
            _cases, complete = _collect_media(timezone.now(), 1)

        self.assertFalse(complete)

    def test_unset_media_root_is_optional_coverage_not_debt(self):
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def count(self):
                return 0

            def iterator(self, **kwargs):
                return iter(())

        with (
            patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", ""),
        ):
            cases, complete = _collect_media(timezone.now(), 10)

        self.assertTrue(complete)
        self.assertEqual([case["reason"] for case in cases], ["media_capture_claim_expired"])

    def test_empty_complete_snapshot_has_no_coverage_reason(self):
        from management.services.ig_technical_debt import technical_debt_snapshot

        with (
            patch("management.services.ig_technical_debt._collect_db", return_value=[]),
            patch("management.services.ig_technical_debt._collect_media", return_value=([], True)),
        ):
            snapshot = technical_debt_snapshot()

        self.assertEqual(snapshot["case_count"], 0)
        self.assertTrue(snapshot["coverage_complete"])
        self.assertEqual(snapshot["coverage_reasons"], [])
        self.assertEqual(snapshot["errors"], [])

    def test_empty_incomplete_snapshot_exposes_non_pii_media_reason(self):
        from management.services.ig_technical_debt import technical_debt_snapshot

        with (
            patch("management.services.ig_technical_debt._collect_db", return_value=[]),
            patch("management.services.ig_technical_debt._collect_media", return_value=([], False)),
            patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", "/private/tmp/missing-media"),
            patch("management.services.ig_technical_debt.os.path.isdir", return_value=False),
        ):
            snapshot = technical_debt_snapshot()

        self.assertEqual(snapshot["case_count"], 0)
        self.assertFalse(snapshot["coverage_complete"])
        self.assertEqual(snapshot["coverage_reasons"], ["private_media_root_unavailable"])
        self.assertEqual(snapshot["errors"], [])

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

    def test_reconciler_defaults_to_dry_run_and_returns_operator_proposal(self):
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        now = timezone.now()
        snapshot = {
            "observed_at": now.isoformat(),
            "cases": [{
                "reason": "canonical_delivery_unknown",
                "scope": "delivery_effect",
                "count": 2,
                "oldest_age_seconds": 90,
                "sample_ids": [12, 12],
                "sampled": False,
                "has_more": False,
            }],
            "coverage_complete": True,
            "coverage_reasons": [],
            "errors": [],
            "sample_limit": 100,
        }
        with patch(
            "management.services.ig_technical_debt.technical_debt_snapshot",
            return_value=snapshot,
        ) as collect:
            result = reconcile_ig_technical_debt_once(now=now)

        collect.assert_called_once_with(now=now, limit=100)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "proposal_only")
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["coverage_reasons"], [])
        self.assertTrue(result["persistence"]["supported"])
        proposal = result["proposed_cases"][0]
        self.assertEqual(proposal["identity"], "canonical_delivery_unknown:delivery_effect")
        self.assertEqual(proposal["source_ids"], [12])
        self.assertEqual(proposal["sample_ids"], [12])
        self.assertEqual(proposal["oldest_age_seconds"], 90)
        self.assertEqual(proposal["disposition"], "manual_review_required")
        self.assertEqual(
            proposal["first_observed_at"],
            (now - timedelta(seconds=90)).isoformat(),
        )
        self.assertEqual(proposal["last_observed_at"], now.isoformat())

    def test_reconciler_identity_is_stable_when_age_and_count_change(self):
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        now = timezone.now()
        first = {
            "reason": "legacy_send_unknown", "scope": "legacy_message",
            "count": 1, "oldest_age_seconds": 10, "sample_ids": [7],
            "sampled": False, "has_more": False,
        }
        older = {**first, "count": 9, "oldest_age_seconds": 900}
        with patch(
            "management.services.ig_technical_debt.technical_debt_snapshot",
            side_effect=[
                {"cases": [first], "coverage_complete": True, "errors": [], "sample_limit": 100},
                {"cases": [older], "coverage_complete": True, "errors": [], "sample_limit": 100},
            ],
        ):
            first_result = reconcile_ig_technical_debt_once(now=now)
            older_result = reconcile_ig_technical_debt_once(now=now)

        first_case = first_result["proposed_cases"][0]
        older_case = older_result["proposed_cases"][0]
        self.assertEqual(first_case["identity"], older_case["identity"])
        self.assertEqual(first_case["case_fingerprint"], older_case["case_fingerprint"])
        self.assertNotEqual(first_case["oldest_age_seconds"], older_case["oldest_age_seconds"])

    def test_reconciler_apply_empty_is_safe_and_persistent(self):
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        with patch(
            "management.services.ig_technical_debt.technical_debt_snapshot",
            return_value={"cases": [], "coverage_complete": True, "errors": [], "sample_limit": 5},
        ):
            result = reconcile_ig_technical_debt_once(limit=5, dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["provider_calls"], 0)
        self.assertTrue(result["persistence"]["supported"])


class TechnicalDebtCaseApplyTests(TestCase):
    def test_apply_is_idempotent_and_preserves_resolved_status(self):
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        now = timezone.now()
        snapshot = {"cases": [{
            "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
            "count": 2, "oldest_age_seconds": 90, "sample_ids": [12],
            "sampled": False, "has_more": False,
        }], "coverage_complete": True, "errors": [], "sample_limit": 5}
        with patch("management.services.ig_technical_debt.technical_debt_snapshot", return_value=snapshot):
            first = reconcile_ig_technical_debt_once(now=now, limit=5, dry_run=False)
            case = IgTechnicalDebtCase.objects.get(case_key="canonical_delivery_unknown:delivery_effect")
            case.status = IgTechnicalDebtCase.Status.RESOLVED
            case.save(update_fields=["status", "updated_at"])
            second = reconcile_ig_technical_debt_once(now=now, limit=5, dry_run=False)
        self.assertEqual(first["writes"], 1)
        self.assertEqual(second["writes"], 0)
        self.assertEqual(IgTechnicalDebtCase.objects.count(), 1)
        self.assertEqual(
            IgTechnicalDebtCase.objects.get(pk=case.pk).status,
            IgTechnicalDebtCase.Status.RESOLVED,
        )

    def test_incomplete_coverage_never_persists_cases(self):
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        with patch("management.services.ig_technical_debt.technical_debt_snapshot", return_value={
            "cases": [{"reason": "unknown", "scope": "db", "count": 1, "sample_ids": []}],
            "coverage_complete": False, "errors": ["DatabaseError"], "sample_limit": 5,
        }):
            result = reconcile_ig_technical_debt_once(limit=5, dry_run=False)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(IgTechnicalDebtCase.objects.count(), 0)
