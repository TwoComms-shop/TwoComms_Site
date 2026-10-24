"""Never describe an ingress-fenced, unadmitted turn as a provider failure."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_live as fixtures
from management.models import GeminiRequest, IgFollowUpTask
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_revision_live import RevisionGenerationBoundary
from management.services.ig_revision_recovery import record_generation_admission, schedule_revision_recovery


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class PreGenerationExpiryTests(TransactionTestCase):
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare

    def setUp(self):
        fixtures.RevisionLiveTests.setUp(self)
        self._prepare()
        self.expired_at = self.revision.overall_deadline + timedelta(seconds=1)

    def _classify_without_io(self):
        with (
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("classification cannot send")) as http,
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("classification cannot generate")) as generate,
        ):
            result = schedule_revision_recovery(self.revision.pk, now=self.expired_at)
        http.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(result.state, "manual")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.assertFalse(self.revision.delivery_effects.exists())
        return result

    def test_expired_claim_without_generation_admission_manifest_or_graph_is_not_started(self):
        self.assertFalse(GeminiRequest.objects.exists())
        result = self._classify_without_io()
        self.assertEqual(result.reason, "generation_not_started")
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")
        self.assertEqual(task.event_payload["reason"], "generation_not_started")
        again = schedule_revision_recovery(self.revision.pk, now=self.expired_at)
        self.assertEqual(again.reason, "generation_not_started")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertFalse(GeminiRequest.objects.exists())

    def test_admitted_generation_keeps_missing_manifest_as_a_binding_fault(self):
        from management.services.ig_revision_provider_execution import ProviderContinuation

        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        authority = RevisionGenerationBoundary(self.revision, self.token, self.settings, publication).baseline
        # Historical releases committed admission without a manifest producer.
        # Preserve that exact legacy shape; current admission is atomic.
        with patch("management.services.ig_revision_provider_execution.freeze_revision_provider_manifest_locked",
                return_value=ProviderContinuation(ready=True)):
            admission = record_generation_admission(
                self.revision.pk, self.token, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=publication, authority=authority,
            )
        self.assertTrue(admission.ready, admission.reason)
        self.assertEqual(self._classify_without_io().reason, "provider_manifest_missing")

    def test_graph_without_admission_keeps_missing_manifest_as_contradictory_context(self):
        from management.services.gemini_accounting_runtime import revision_request_execution
        with revision_request_execution(self.revision.pk, self.token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch):
            GeminiRequest.objects.create(
                request_id="contradictory-pre-generation-graph", client_id=self.customer.pk,
                source_message_id=self.source.pk, lane="live", logical_turn_id=f"ig-revision:{self.revision.pk}",
                source_execution_key=f"ig-revision:{self.revision.pk}", terminal_resolution="failed",
            )
        self.assertEqual(self._classify_without_io().reason, "provider_manifest_missing")

    def test_present_but_invalid_manifest_is_never_downgraded_to_not_started(self):
        self.revision.action_receipts = {**self.revision.action_receipts, "provider_execution_manifest": {}}
        self.revision.save(update_fields=["action_receipts"])
        self.assertEqual(self._classify_without_io().reason, "provider_manifest_missing")
