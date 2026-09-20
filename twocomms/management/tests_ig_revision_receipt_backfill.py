from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_reply_projection as projection_fixtures
from management.services.ig_revision_receipt_backfill import inspect_historical_receipts


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class HistoricalReceiptBackfillTests(TransactionTestCase):
    reset_sequences = True
    setUp = projection_fixtures.RevisionReplyProjectionTests.setUp
    _message = projection_fixtures.RevisionReplyProjectionTests._message
    _prepare = projection_fixtures.RevisionReplyProjectionTests._prepare
    _admit = projection_fixtures.RevisionReplyProjectionTests._admit
    _parts = projection_fixtures.RevisionReplyProjectionTests._parts

    def test_dry_run_is_read_only_and_reports_confirmed_receipt(self):
        self._parts()
        result = inspect_historical_receipts(revision_ids=(self.revision.pk,))
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["provider_calls"], 0)
        self.revision.refresh_from_db()
        self.assertNotIn("sent_reply_projection", self.revision.action_receipts)

    def test_apply_projects_once_without_provider_send_and_replay_is_empty(self):
        self._parts()
        with patch("management.services.instagram_bot._provider_http") as send:
            result = inspect_historical_receipts(revision_ids=(self.revision.pk,), apply=True)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["rows"][0]["count_outcome"], "counted")
        send.assert_not_called()
        replay = inspect_historical_receipts(revision_ids=(self.revision.pk,), apply=True)
        self.assertEqual(replay["scanned"], 0)

    def test_apply_keeps_legacy_funnel_without_admission(self):
        self._parts(admitted=False)
        result = inspect_historical_receipts(revision_ids=(self.revision.pk,), apply=True)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["rows"][0]["count_outcome"], "legacy_count_requires_review")
