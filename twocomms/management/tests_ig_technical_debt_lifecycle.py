import json
from io import StringIO
from unittest.mock import patch

from django.core import management
from django.test import TestCase
from django.utils import timezone


class TechnicalDebtLifecycleTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from management.ig_bot_models import IgTechnicalDebtCase

        self.actor = get_user_model().objects.create_user(
            username="ig-debt-operator", is_staff=True,
        )
        self.case = IgTechnicalDebtCase.objects.create(
            case_key="canonical_delivery_unknown:delivery_effect",
            reason="canonical_delivery_unknown",
            scope="delivery_effect",
            sample_ids=[12, 13],
            has_more=True,
            observation_fingerprint="fingerprint-123",
            last_count=2,
        )

    def test_list_preserves_sample_metadata_and_is_read_only(self):
        from management.services.ig_technical_debt import list_ig_technical_debt_cases

        result = list_ig_technical_debt_cases(limit=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        listed = result["cases"][0]
        self.assertEqual(listed["sample_ids"], [12, 13])
        self.assertTrue(listed["has_more"])
        self.assertEqual(listed["fingerprint"], "fingerprint-123")
        self.assertEqual(self.case.__class__.objects.count(), 1)

    def test_acknowledge_is_idempotent_and_audited_once(self):
        from management.models import AdminAuditLog
        from management.services.ig_technical_debt import acknowledge_ig_technical_debt_case

        first = acknowledge_ig_technical_debt_case(
            self.case.pk, actor=self.actor, action="triage", now=timezone.now(),
        )
        second = acknowledge_ig_technical_debt_case(
            self.case.pk, actor=self.actor, action="triage", now=timezone.now(),
        )

        self.assertTrue(first["ok"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(AdminAuditLog.objects.filter(
            action="ig_technical_debt_case_transition",
            entity_id=str(self.case.pk),
        ).count(), 1)

    def test_resolve_and_dismiss_require_action_and_evidence(self):
        from management.services.ig_technical_debt import (
            dismiss_ig_technical_debt_case,
            resolve_ig_technical_debt_case,
        )

        missing_action = resolve_ig_technical_debt_case(
            self.case.pk, actor=self.actor, evidence={"check": "done"},
        )
        missing_evidence = dismiss_ig_technical_debt_case(
            self.case.pk, actor=self.actor, action="false_positive",
        )
        resolved = resolve_ig_technical_debt_case(
            self.case.pk, actor=self.actor, action="verified_receipt",
            evidence={"source": "operator", "reference": "INC-42"},
        )

        self.assertEqual(missing_action["error"], "action_required")
        self.assertEqual(missing_evidence["error"], "evidence_required")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["case"]["status"], "resolved")

    def test_terminal_case_is_not_reopened_by_reconciler(self):
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        self.case.status = IgTechnicalDebtCase.Status.RESOLVED
        self.case.save(update_fields=["status", "updated_at"])
        snapshot = {"cases": [{
            "reason": self.case.reason, "scope": self.case.scope, "count": 8,
            "oldest_age_seconds": 10, "sample_ids": [99], "has_more": False,
        }], "coverage_complete": True, "errors": [], "sample_limit": 5}
        with patch("management.services.ig_technical_debt.technical_debt_snapshot", return_value=snapshot):
            result = reconcile_ig_technical_debt_once(limit=5, dry_run=False)

        self.assertEqual(result["writes"], 1)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, IgTechnicalDebtCase.Status.RESOLVED)
        self.assertEqual(self.case.sample_ids, [99])

    def test_reconciler_persists_disposition_and_is_idempotent(self):
        from management.services.ig_technical_debt import (
            list_ig_technical_debt_cases,
            reconcile_ig_technical_debt_once,
        )

        now = timezone.now()
        snapshot = {"cases": [{
            "reason": self.case.reason, "scope": self.case.scope, "count": 2,
            "oldest_age_seconds": 10, "sample_ids": [12], "has_more": False,
        }], "coverage_complete": True, "errors": [], "sample_limit": 5}
        with patch(
            "management.services.ig_technical_debt.technical_debt_snapshot",
            return_value=snapshot,
        ):
            first = reconcile_ig_technical_debt_once(limit=5, dry_run=False, now=now)
            second = reconcile_ig_technical_debt_once(limit=5, dry_run=False, now=now)

        self.assertEqual(first["writes"], 1)
        self.assertEqual(second["writes"], 0)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, "open")
        self.assertEqual(self.case.disposition, "manual_review_required")
        listed = list_ig_technical_debt_cases(limit=5)["cases"][0]
        self.assertEqual(listed["disposition"], "manual_review_required")

    def test_incomplete_coverage_fails_closed_without_writes(self):
        from management.services.ig_technical_debt import reconcile_ig_technical_debt_once

        with patch("management.services.ig_technical_debt.technical_debt_snapshot", return_value={
            "cases": [{"reason": "unknown", "scope": "db", "count": 1, "sample_ids": [77]}],
            "coverage_complete": False, "coverage_reasons": ["private_media_scan_capped"],
            "errors": [], "sample_limit": 5,
        }):
            result = reconcile_ig_technical_debt_once(limit=5, dry_run=False)

        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["coverage_reasons"], ["private_media_scan_capped"])
        self.assertEqual(self.case.__class__.objects.count(), 1)
        self.case.refresh_from_db()
        self.assertEqual(self.case.last_count, 2)

    def test_lifecycle_does_not_call_snapshot_or_provider_paths(self):
        from management.services.ig_technical_debt import claim_ig_technical_debt_case

        with patch("management.services.ig_technical_debt.technical_debt_snapshot") as snapshot:
            result = claim_ig_technical_debt_case(
                self.case.pk, actor=self.actor, action="take_ownership",
            )

        self.assertTrue(result["ok"])
        snapshot.assert_not_called()


class TechnicalDebtCommandOutputTests(TestCase):
    def test_command_output_is_stable_json_by_default_and_with_flag(self):
        payload = {"schema_version": "test", "proposed_cases": [], "writes": 0}
        with patch(
            "management.management.commands.reconcile_ig_technical_debt.reconcile_ig_technical_debt_once",
            return_value=payload,
        ):
            for extra in ([], ["--json"]):
                stdout = StringIO()
                management.call_command(
                    "reconcile_ig_technical_debt", *extra, stdout=stdout,
                )
                self.assertEqual(json.loads(stdout.getvalue()), payload)
