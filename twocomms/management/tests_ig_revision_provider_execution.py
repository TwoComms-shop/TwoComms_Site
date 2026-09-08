from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import GeminiRequest, GeminiRequestAttempt, IgCustomerTurnRevision, IgProviderIncident, InstagramBotMessage
from management.services import gemini_accounting_runtime as runtime
from management.services.ig_revision_provider_execution import (
    MANIFEST_KEY, REFERENCE_KEY, REPAIR_KEY, inspect_revision_provider_execution, revision_provider_continuation,
)
from management.services.ig_revision_recovery import prepare_outage_recovery, record_generation_admission, schedule_revision_recovery
from management.services.ig_revision_live import RevisionGenerationBoundary
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_lineage import turn_lineage
from management.services.ig_turn_revisions import create_refresh_successor
from management.tests_gemini_accounting_shadow import SHADOW
from management import tests_ig_revision_live as fixtures


@override_settings(**SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionProviderExecutionTests(TransactionTestCase):
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare

    def setUp(self):
        fixtures.RevisionLiveTests.setUp(self)
        self._prepare()
        self.scarcity = patch("management.services.ig_provider_dispatch_budget.ProviderDispatchBudget._is_scarce_model", return_value=False)
        self.scarcity_mock = self.scarcity.start()
        self.addCleanup(self.scarcity.stop)
        self.raw_plan = [dict(candidate_index=i + 1, key_name="GEMINI_API", model=f"gemini-test-{i}", project_identity="project-test", identity_status="known", skip_reason="", key_value="secret-never-persist", prompt="private-never-persist") for i in range(10)]

    def _begin(self, plan=None):
        with runtime.revision_request_execution(self.revision.pk, self.token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch), turn_lineage(lane="live", client_id=self.customer.pk, source_message_id=self.source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}"):
            return runtime.begin_request(request_id=None, role="chat", reasoning_task="reply", candidate_plan=self.raw_plan if plan is None else plan, deadline_seconds=30)

    def _attempt(self, observer, index, *, code=404, failure="not_found"):
        row = self.raw_plan[index]
        boundary = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=row["candidate_index"])
        self.assertTrue(boundary.before_provider(serialized_bytes=100), getattr(boundary, "provider_block_reason", ""))
        boundary.manual_result(succeeded=False, http_code=code, failure_kind=failure)
        return boundary

    def _refresh(self):
        result = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(result.created, result.reason)
        self.revision = result.revision
        self._prepare()

    def _admit(self):
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        authority = RevisionGenerationBoundary(self.revision, self.token, self.settings, publication).baseline
        result = record_generation_admission(self.revision.pk, self.token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=publication, authority=authority)
        self.assertTrue(result.ready, result.reason)

    def test_global_http_count_and_route_survive_refresh(self):
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        for index in range(4):
            self._attempt(observer, index)
        root_id = self.revision.pk
        self._refresh()
        self.assertEqual(self.revision.action_receipts[REFERENCE_KEY]["root_revision_id"], root_id)
        observer = self._begin(list(reversed(self.raw_plan)))
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(observer.provider_continuation.http_remaining, 4)
        self.assertEqual([row["candidate_index"] for row in observer.provider_continuation.candidate_plan], list(range(1, 11)))
        for index in range(4, 8):
            self._attempt(observer, index)
        row = self.raw_plan[8]
        rejected = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=row["candidate_index"])
        self.assertFalse(rejected.before_provider(serialized_bytes=100))
        self.assertEqual(rejected.provider_block_reason, "provider_dispatch_budget")
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 8)

    def test_frozen_scarce_classification_and_limit_survive_configuration_change(self):
        self.scarcity_mock.return_value = True
        observer = self._begin()
        self._attempt(observer, 0)
        self.scarcity_mock.return_value = False
        self._refresh()
        observer = self._begin()
        self.assertEqual(observer.provider_continuation.scarce_remaining, 1)
        self._attempt(observer, 1)
        row = self.raw_plan[2]
        rejected = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=row["candidate_index"])
        self.assertFalse(rejected.before_provider(serialized_bytes=100))
        self.assertEqual(rejected.provider_block_reason, "scarce_model_budget")

    def test_secret_free_manifest_and_no_expanded_or_rotated_route(self):
        observer = self._begin()
        self.revision.refresh_from_db()
        manifest = self.revision.action_receipts[MANIFEST_KEY]
        self.assertNotIn("secret-never-persist", str(manifest))
        self.assertNotIn("private-never-persist", str(manifest))
        self._refresh()
        changed = [dict(row, project_identity="different-project") for row in self.raw_plan]
        changed.append(dict(self.raw_plan[0], model="new-model", candidate_index=99))
        observer = self._begin(changed)
        self.assertTrue(observer.provider_blocked)
        self.assertEqual(observer.block_reason, "provider_route_unavailable")

    def test_single_durable_repair_survives_pre_http_failure_and_successor(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="invalid_response")
        row = self.raw_plan[0]
        self.assertTrue(observer.reserve_provider_repair(key_name=row["key_name"], model=row["model"], candidate_index=1))
        self.assertFalse(observer.reserve_provider_repair(key_name=row["key_name"], model=row["model"], candidate_index=1))
        marker = GeminiRequest.objects.get(pk=observer.graph_id).candidate_outcomes[REPAIR_KEY]
        self.assertEqual(marker["candidate_index"], 1)
        # Simulate a crash after reservation, before constructing or sending repair.
        self._refresh()
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertFalse(observer.provider_continuation.repair_remaining)
        self.assertEqual(observer.provider_continuation.candidate_plan[0]["skip_reason"], "provider_model_schema_rejected")
        self.assertFalse(observer.reserve_provider_repair(key_name=row["key_name"], model=row["model"], candidate_index=1))

    def test_exact_reserved_repair_can_dispatch_once_and_counts_as_http(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="invalid_response")
        row = self.raw_plan[0]
        self.assertTrue(observer.reserve_provider_repair(key_name=row["key_name"], model=row["model"], candidate_index=1))
        self._attempt(observer, 0, code=200, failure="invalid_response")
        rejected = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=1)
        self.assertFalse(rejected.before_provider(serialized_bytes=100))
        self.assertEqual(rejected.provider_block_reason, "provider_model_schema_rejected")
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 2)

    def test_schema_retires_model_while_credential_and_model_errors_are_scoped(self):
        self.raw_plan[1] = dict(self.raw_plan[1], key_name="GEMINI_API2", model=self.raw_plan[0]["model"], project_identity="another-project")
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="invalid_response")
        state = inspect_revision_provider_execution(self.revision)
        self.assertEqual(state.candidate_plan[1]["skip_reason"], "provider_model_schema_rejected")
        self.assertFalse(state.candidate_plan[2]["skip_reason"])

    def test_401_retires_alias_but_403_and_404_leave_other_candidates(self):
        self.raw_plan[1] = dict(self.raw_plan[1], key_name="GEMINI_API2")
        self.raw_plan[2] = dict(self.raw_plan[2], key_name="GEMINI_API3")
        observer = self._begin()
        self._attempt(observer, 0, code=401, failure="unauthorized")
        self._attempt(observer, 1, code=403, failure="forbidden")
        state = inspect_revision_provider_execution(self.revision)
        self.assertTrue(state.ready)
        self.assertFalse(state.candidate_plan[2]["skip_reason"])

    def test_recovery_successor_keeps_count_after_typed_candidate_failure(self):
        self._admit()
        observer = self._begin()
        self._attempt(observer, 0)
        observer.resolve_failure("not_found")
        self.revision.refresh_from_db()
        now = self.revision.overall_deadline + timedelta(seconds=1)
        scheduled = schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(scheduled.state, "waiting", scheduled.reason)
        self.revision.refresh_from_db()
        due = self.revision.recovery_due_at
        recovered = prepare_outage_recovery(self.revision.pk, now=due)
        self.assertEqual(recovered.state, "spawned", recovered.reason)
        child = IgCustomerTurnRevision.objects.get(pk=recovered.child_id)
        state = inspect_revision_provider_execution(child, now=due)
        self.assertEqual(state.http_remaining, 7)
        self.assertEqual(state.root_revision_id, self.revision.pk)

    def test_horizon_stops_incident_wait_without_child_spin(self):
        self._admit()
        observer = self._begin()
        self._attempt(observer, 0)
        observer.resolve_failure("not_found")
        self.revision.refresh_from_db()
        horizon = timezone.datetime.fromisoformat(self.revision.action_receipts[MANIFEST_KEY]["horizon_at"])
        scheduled = schedule_revision_recovery(self.revision.pk, now=self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(scheduled.state, "waiting", scheduled.reason)
        IgProviderIncident.objects.create(role="chat", failure_class="quota", fingerprint="test-horizon", active_fingerprint="test-horizon", state="open", opened_at=timezone.now(), last_failure_at=timezone.now())
        result = prepare_outage_recovery(self.revision.pk, now=horizon - timedelta(seconds=20))
        self.assertEqual(result.state, "manual")
        self.assertEqual(result.reason, "provider_horizon_exhausted")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def test_quota_retry_after_beyond_horizon_does_not_create_successors(self):
        self.raw_plan = self.raw_plan[:1]
        observer = self._begin()
        attempt = self._attempt(observer, 0, code=429, failure="quota_429")
        GeminiRequestAttempt.objects.filter(pk=attempt.attempt_id).update(provider_retry_after_seconds=7200)
        observer.resolve_failure("quota_429")
        self.revision.refresh_from_db()
        state = inspect_revision_provider_execution(self.revision)
        self.assertFalse(state.ready)
        self.assertEqual(state.reason, "provider_candidates_exhausted")
        self.assertEqual(state.http_remaining, 7)
        result = schedule_revision_recovery(self.revision.pk, now=self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(result.state, "manual")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def test_horizon_is_clipped_to_original_permission_window(self):
        window = timezone.now() + timedelta(minutes=5)
        with patch("management.services.ig_revision_outbox._normal_reply_window_deadline", return_value=window):
            observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(timezone.datetime.fromisoformat(observer.provider_continuation.manifest["horizon_at"]), window)

    def test_epoch_digest_and_new_inbound_stop_admission(self):
        observer = self._begin()
        self._message("new inbound", "new-inbound-budget")
        row = self.raw_plan[0]
        boundary = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=1)
        self.assertFalse(boundary.before_provider(serialized_bytes=100))
        self.assertEqual(boundary.provider_block_reason, "recovery_newer_inbound")
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        result = revision_provider_continuation(self.revision.pk, self.token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, candidate_plan=self.raw_plan)
        self.assertEqual(result.reason, "revision_execution_invalid")

    def test_manifest_mutation_is_rejected(self):
        self._begin()
        self.revision.refresh_from_db()
        self.revision.action_receipts[MANIFEST_KEY]["max_http"] = 999
        with self.assertRaisesMessage(ValueError, "revision action receipt entries are immutable"):
            self.revision.save(update_fields=["action_receipts"])
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[MANIFEST_KEY]["max_http"], 8)

    def test_settings_epoch_and_disabled_accounting_fail_closed(self):
        self._begin()
        self.settings.reply_permission_epoch += 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        result = revision_provider_continuation(self.revision.pk, self.token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, candidate_plan=self.raw_plan)
        self.assertEqual(result.reason, "provider_permission_changed")
        with patch.object(runtime, "shadow_runtime_active", return_value=False):
            self.assertEqual(self._begin().block_reason, "provider_accounting_unavailable")
