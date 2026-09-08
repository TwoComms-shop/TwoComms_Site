"""Real accounting ownership across immutable-source execution successors."""
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import GeminiRequest, IgCustomerTurnRevision, InstagramBotMessage
from management.services import gemini_accounting_runtime as runtime
from management.services.ig_turn_lineage import turn_lineage
from management.services.ig_turn_revisions import create_refresh_successor
from management.tests_gemini_accounting_shadow import SHADOW, _raw_plan, seed_shadow_profiles
from management import tests_ig_revision_live as live_fixtures


@override_settings(**SHADOW, GOOGLE_INDEXING_ENABLED=False)
class RevisionRequestIdentityTests(TransactionTestCase):
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare

    def setUp(self):
        live_fixtures.RevisionLiveTests.setUp(self)
        seed_shadow_profiles()
        self._prepare()

    def _begin(self, *, revision=True, token=None, lane="live", source=None, logical=None):
        capability = runtime.revision_request_execution(
            self.revision.pk, self.token if token is None else token,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
        ) if revision else nullcontext()
        with capability, turn_lineage(
            lane=lane, client_id=self.customer.pk,
            source_message_id=(source or self.source).pk,
            logical_turn_id=logical if logical is not None else (
                f"ig-revision:{self.revision.pk}" if revision else "legacy-turn"
            ),
        ):
            return runtime.begin_request(
                request_id=None, role="chat", reasoning_task="reply",
                candidate_plan=_raw_plan(), deadline_seconds=30,
            )

    def _attempt(self, observer):
        self.assertTrue(observer.enabled, observer.block_reason)
        return observer.attempt(key_name="GEMINI_API", model="gemini-3.7-flash")

    def test_legacy_and_revision_graphs_keep_separate_admission_and_settlement(self):
        legacy = self._begin(revision=False)
        legacy_attempt = self._attempt(legacy)
        self.assertTrue(legacy_attempt.before_provider(serialized_bytes=80))
        observer = self._begin()
        boundary = self._attempt(observer)
        self.assertTrue(boundary.before_provider(serialized_bytes=80))
        legacy_attempt.succeeded({"promptTokenCount": 5})
        boundary.succeeded({"promptTokenCount": 7})
        graphs = list(GeminiRequest.objects.filter(source_message_id=self.source.pk))
        self.assertEqual(len(graphs), 2)
        self.assertEqual({row.source_execution_key for row in graphs}, {"", f"ig-revision:{self.revision.pk}"})
        self.assertTrue(all(row.winner_attempt_id for row in graphs))
        self.assertEqual({row.winner_attempt.source_message_id for row in graphs}, {self.source.pk})

    def test_refresh_reuses_original_source_and_late_old_usage_survives(self):
        original = self._begin()
        old_attempt = self._attempt(original)
        self.assertTrue(old_attempt.before_provider(serialized_bytes=80))
        result = create_refresh_successor(
            self.revision.pk, self.token, reason="publication_changed",
        )
        self.assertTrue(result.created, result.reason)
        self.revision = result.revision
        self._prepare()
        self.assertEqual(self._begin().block_reason, "provider_wait")
        old_attempt.succeeded({"promptTokenCount": 11})
        successor = self._begin()
        next_attempt = self._attempt(successor)
        self.assertTrue(next_attempt.before_provider(serialized_bytes=80))
        next_attempt.succeeded({"promptTokenCount": 12})
        graphs = list(GeminiRequest.objects.order_by("pk"))
        self.assertEqual(len(graphs), 2)
        self.assertTrue(all(row.winner_attempt_id for row in graphs))
        self.assertEqual({row.source_message_id for row in graphs}, {self.source.pk})
        self.assertEqual(len({row.source_execution_key for row in graphs}), 2)

    def test_manual_revision_bypasses_historical_no_model_without_rewriting_it(self):
        from management.tests_ig_revision_manual_resume import ManualResumeSuccessorTests

        InstagramBotMessage.objects.filter(pk=self.source.pk).update(gemini_task_class="no_model")
        GeminiRequest.objects.create(
            request_id="legacy-no-model", lane="live", task_class="no_model",
            source_message_id=self.source.pk, client_id=self.customer.pk,
        )
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        result = ManualResumeSuccessorTests._create(self, self.customer, self.source)
        self.assertTrue(result.created, result.reason)
        self.revision = result.revision
        self._prepare()
        observer = self._begin()
        attempt = self._attempt(observer)
        self.assertTrue(attempt.before_provider(serialized_bytes=80))
        attempt.succeeded()
        self.source.refresh_from_db()
        self.assertEqual(self.source.gemini_task_class, "no_model")
        self.assertEqual(GeminiRequest.objects.get(pk=observer.graph_id).source_message_id, self.source.pk)

    def test_duplicate_revision_and_legacy_remain_denied(self):
        self.assertTrue(self._begin().enabled)
        self.assertEqual(self._begin().block_reason, "duplicate_source_lane")
        self.assertTrue(self._begin(revision=False).enabled)
        self.assertEqual(self._begin(revision=False).block_reason, "duplicate_source_lane")
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiRequest.objects.create(request_id="legacy-duplicate", source_message_id=self.source.pk, lane="live")

    def test_forged_namespace_key_wrong_claim_and_source_are_denied(self):
        other = self._message("Other", "other-source")
        for arguments in (
            {"revision": False, "logical": f"ig-revision:{self.revision.pk}"},
            {"token": "forged"}, {"lane": "recovery"}, {"source": other},
            {"logical": "ig-revision:999999"},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self._begin(**arguments).block_reason, "revision_execution_invalid")
        with self.assertRaises(ValidationError):
            GeminiRequest.objects.create(
                request_id="forged-key", lane="live", source_message_id=self.source.pk,
                client_id=self.customer.pk, source_execution_key=f"ig-revision:{self.revision.pk}",
                logical_turn_id=f"ig-revision:{self.revision.pk}",
            )
        self.assertEqual(GeminiRequest.objects.count(), 0)

    def test_claim_permission_and_deadline_rechecked_before_dispatch(self):
        observer = self._begin()
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        self.assertFalse(self._attempt(observer).before_provider(serialized_bytes=80))
        self.assertEqual(self._begin().block_reason, "revision_execution_invalid")

    def test_stale_lease_deadline_and_settings_epoch_block_admission(self):
        observer = self._begin()
        with patch.object(runtime.timezone, "now", return_value=self.revision.overall_deadline + timedelta(seconds=1)):
            self.assertFalse(self._attempt(observer).validate_ownership())
            self.assertEqual(self._begin().block_reason, "revision_execution_invalid")
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1),
        )
        self.assertFalse(self._attempt(observer).validate_ownership())
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            lease_until=timezone.now() + timedelta(seconds=30),
        )
        self.settings.reply_permission_epoch += 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        self.assertFalse(self._attempt(observer).before_provider(serialized_bytes=80))

    def test_bulk_create_cannot_forge_execution_key(self):
        with self.assertRaises(ValidationError):
            GeminiRequest.objects.bulk_create([GeminiRequest(
                request_id="bulk-forgery", lane="live", client_id=self.customer.pk,
                source_message_id=self.source.pk, source_execution_key="arbitrary",
            )])

    def test_duplicate_revision_cannot_switch_to_another_bundle_source(self):
        other = self._message("Second", "second")
        self.assertTrue(self._begin().enabled)
        self.assertEqual(self._begin(source=other).block_reason, "revision_execution_invalid")

    def test_execution_key_is_immutable_and_raw_writer_default_remains_empty(self):
        observer = self._begin()
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        graph.source_execution_key = ""
        with self.assertRaises(ValidationError):
            graph.save()
        with self.assertRaises(ValidationError):
            GeminiRequest.objects.filter(pk=graph.pk).update(source_execution_key="")
        field = GeminiRequest._meta.get_field("source_execution_key")
        self.assertEqual(field.default, "")
        self.assertEqual(field.db_default, "")
