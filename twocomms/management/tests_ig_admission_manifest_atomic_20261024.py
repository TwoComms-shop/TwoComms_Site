"""Native zero-dispatch crash/recovery contracts; external network is denied."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from management.models import (
    BotAdCampaign, GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurnRevision,
    IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services import call_ai_analysis as gateway
from management.services import gemini_accounting_runtime as accounting
from management.services.ig_revision_live import (
    RevisionGenerationBoundary, RevisionLiveResult, _generate_proposal,
    _process_terminal_fault_acknowledgements, execute_claimed_revision,
)
from management.services.ig_revision_execution import prepare_revision
from management.services.ig_revision_media import collect_revision_media
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_revision_provider_execution import (
    MANIFEST_KEY, REFERENCE_KEY, ProviderContinuation, _frozen_candidates, inspect_revision_provider_execution,
    revision_provider_continuation,
)
from management.services.ig_revision_recovery import (
    MAX_RECOVERY_GENERATIONS, prepare_outage_recovery, record_generation_admission,
    record_local_preprovider_fault, record_policy_readiness_failure, schedule_revision_recovery,
)
from management.services.ig_turn_capture import capture_revision_context, capture_revision_routing
from management.services.ig_turn_intelligence import TurnContextError
from management.services.ig_turn_lineage import turn_lineage
from management.services.ig_turn_revisions import claim_sealed_revision, create_refresh_successor
from management.tests_gemini_accounting_shadow import SHADOW
from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_turn_capture as capture_fixtures


class WorkerKilled(BaseException):
    """Process death bypasses the worker's ordinary Exception handler."""


@override_settings(**SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class AdmissionManifestAtomicTests(TransactionTestCase):
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare

    def setUp(self):
        live_fixtures.RevisionLiveTests.setUp(self)
        self._prepare()
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        self.plan = [dict(candidate_index=index, key_name=f"GEMINI_API{index if index > 1 else ''}",
            model="gemini-3.5-flash-lite", project_identity=f"project-{index}",
            identity_status="known", skip_reason="", key_value="secret-must-never-persist",
            prompt="private-prompt-must-never-persist") for index in (1, 2)]
        for target in ("management.services.instagram_bot._provider_http", "requests.sessions.Session.request"):
            denied = patch(target, side_effect=AssertionError("external dispatch forbidden"))
            denied.start()
            self.addCleanup(denied.stop)

    def admit(self, *, revision=None, token=None, now=None, plan=None):
        revision = revision or self.revision
        token = token or self.token
        authority = RevisionGenerationBoundary(revision, token, self.settings, self.binding).baseline
        return record_generation_admission(revision.pk, token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.binding,
            authority=authority, candidate_plan=self.plan if plan is None else plan, now=now)

    def recover(self, revision, now):
        scheduled = schedule_revision_recovery(revision.pk, now=now)
        if scheduled.state != "waiting":
            return scheduled
        revision.refresh_from_db()
        due = revision.recovery_due_at
        lease = f"native-recovery-worker-{revision.pk}"
        IgClient.objects.filter(pk=self.customer.pk).update(automation_lease_token=lease,
            automation_lease_until=due + timedelta(minutes=3))
        return prepare_outage_recovery(revision.pk, now=due, automation_lease_token=lease)

    def assert_zero_dispatch(self):
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertFalse(GeminiRequestAttempt.objects.exists())
        self.assertFalse(IgRevisionDeliveryEffect.objects.exists())

    @override_settings(IG_TURN_CONTEXT_MODE="unified")
    def test_death_after_admission_before_context_uses_one_existing_bounded_child(self):
        collection = collect_revision_media(self.revision.pk, self.token)
        def die(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.revision.refresh_from_db()
            self.assertIn("generation_admission", self.revision.action_receipts)
            self.assertIn(MANIFEST_KEY, self.revision.action_receipts)
            raise WorkerKilled()
        with patch.object(gateway, "prepare_live_chat_candidate_plan", return_value=(
                ["gemini-3.5-flash-lite"], self.plan)), patch(
                "management.services.ig_turn_integration.prepare_revision_turn_context", side_effect=die):
            with self.assertRaises(WorkerKilled):
                _generate_proposal(self.revision, self.token, self.settings, self.binding, collection)
        self.assert_zero_dispatch()
        frozen = deepcopy(self.revision.action_receipts[MANIFEST_KEY])
        encoded = str(self.revision.action_receipts)
        self.assertNotIn("secret-must-never-persist", encoded)
        self.assertNotIn("private-prompt-must-never-persist", encoded)
        recovered = self.recover(self.revision, self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(recovered.state, "spawned", recovered.reason)
        child = IgCustomerTurnRevision.objects.get(pk=recovered.child_id)
        continuation = inspect_revision_provider_execution(child, now=child.created_at)
        self.assertTrue(continuation.ready, continuation.reason)
        self.assertEqual((continuation.http_remaining, continuation.scarce_remaining,
            continuation.repair_remaining), (8, 2, True))
        self.assertEqual(continuation.manifest, frozen)
        self.assertEqual(child.action_receipts[REFERENCE_KEY], {
            "root_revision_id": self.revision.pk, "manifest_digest": frozen["digest"]})
        again = prepare_outage_recovery(self.revision.pk)
        self.assertEqual(again.child_id, child.pk)
        self.assertEqual(IgCustomerTurnRevision.objects.filter(parent=self.revision).count(), 1)
        self.assert_zero_dispatch()

    def test_append_failure_rolls_back_manifest_and_admission_together(self):
        original = IgCustomerTurnRevision.save
        def fail_admission(row, *args, **kwargs):
            if "generation_admission" in (row.action_receipts or {}):
                raise RuntimeError("simulated process failure before paired commit")
            return original(row, *args, **kwargs)
        with patch.object(IgCustomerTurnRevision, "save", fail_admission):
            with self.assertRaises(RuntimeError):
                self.admit()
        self.revision.refresh_from_db()
        self.assertNotIn(MANIFEST_KEY, self.revision.action_receipts)
        self.assertNotIn("generation_admission", self.revision.action_receipts)
        self.assert_zero_dispatch()

    def test_idempotent_admission_keeps_original_digest_horizon_and_route(self):
        self.assertTrue(self.admit().ready)
        self.revision.refresh_from_db()
        previous = deepcopy(self.revision.action_receipts)
        self.assertTrue(self.admit(plan=list(reversed(self.plan))).ready)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts, previous)
        self.assert_zero_dispatch()

    def test_historical_admission_without_manifest_is_never_retrofilled(self):
        # Explicit historical fixture: the old producer committed the authority
        # receipt without calling the manifest producer at all.
        with patch("management.services.ig_revision_provider_execution.freeze_revision_provider_manifest_locked",
                return_value=ProviderContinuation(ready=True)):
            self.assertTrue(self.admit().ready)
        self.revision.refresh_from_db()
        legacy = {"generation_admission": self.revision.action_receipts["generation_admission"]}
        self.assertEqual(self.revision.action_receipts, legacy)
        self.assertEqual(self.admit().reason, "provider_manifest_missing")
        self.assertEqual(revision_provider_continuation(self.revision.pk, self.token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            candidate_plan=self.plan).reason, "provider_manifest_missing")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts, legacy)
        expired = schedule_revision_recovery(self.revision.pk,
            now=self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual((expired.state, expired.reason), ("manual", "provider_manifest_missing"))
        self.assert_zero_dispatch()

    def test_all_skipped_candidates_still_have_a_valid_immutable_manifest(self):
        plan = [dict(row, skip_reason="quota_cooldown") for row in self.plan]
        self.assertTrue(self.admit(plan=plan).ready)
        self.revision.refresh_from_db()
        self.assertIn(MANIFEST_KEY, self.revision.action_receipts)
        continuation = inspect_revision_provider_execution(self.revision)
        self.assertEqual(continuation.reason, "provider_candidates_exhausted")
        self.assert_zero_dispatch()

    def test_refresh_child_cannot_retrofill_a_historical_admission_only_root(self):
        with patch("management.services.ig_revision_provider_execution.freeze_revision_provider_manifest_locked",
                return_value=ProviderContinuation(ready=True)):
            self.assertTrue(self.admit().ready)
        refreshed = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(refreshed.created, refreshed.reason)
        prepared = prepare_revision(refreshed.revision.pk, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        child = IgCustomerTurnRevision.objects.get(pk=refreshed.revision.pk)
        result = self.admit(revision=child, token=prepared.execution_token)
        self.assertEqual(result.reason, "provider_manifest_missing")
        self.revision.refresh_from_db()
        child.refresh_from_db()
        self.assertNotIn(MANIFEST_KEY, self.revision.action_receipts)
        self.assertNotIn("generation_admission", child.action_receipts)
        self.assert_zero_dispatch()

    def test_invalid_manifest_is_not_replaced_or_repaired(self):
        with patch("management.services.ig_revision_provider_execution._digest", return_value="f" * 64):
            self.assertTrue(self.admit().ready)
        self.revision.refresh_from_db()
        invalid = deepcopy(self.revision.action_receipts)
        self.assertFalse(self.admit().ready)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts, invalid)
        self.assert_zero_dispatch()

    def test_disabled_accounting_stays_closed_after_atomic_admission(self):
        self.assertTrue(self.admit().ready)
        with accounting.revision_request_execution(self.revision.pk, self.token,
                settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch), turn_lineage(
                lane="live", client_id=self.customer.pk, source_message_id=self.source.pk,
                logical_turn_id=f"ig-revision:{self.revision.pk}"), patch.object(
                accounting, "shadow_runtime_active", return_value=False):
            observer = accounting.begin_request(request_id=None, role="chat", reasoning_task="customer_chat",
                candidate_plan=self.plan, deadline_seconds=30)
        self.assertTrue(observer.provider_blocked)
        self.assertEqual(observer.block_reason, "provider_accounting_unavailable")
        self.assert_zero_dispatch()
        self.assertEqual(inspect_revision_provider_execution(self.revision).http_remaining, 8)

    def test_existing_graph_without_admission_is_not_repaired(self):
        with accounting.revision_request_execution(self.revision.pk, self.token,
                settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch), turn_lineage(
                lane="live", client_id=self.customer.pk, source_message_id=self.source.pk,
                logical_turn_id=f"ig-revision:{self.revision.pk}"):
            observer = accounting.begin_request(request_id=None, role="chat", reasoning_task="customer_chat",
                candidate_plan=self.plan, deadline_seconds=30)
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(self.admit().reason, "generation_admission_missing_before_request")
        self.revision.refresh_from_db()
        self.assertNotIn("generation_admission", self.revision.action_receipts)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertFalse(GeminiRequestAttempt.objects.exists())
        self.assertFalse(IgRevisionDeliveryEffect.objects.exists())

    def test_horizon_stops_recovery_without_budget_extension(self):
        self.assertTrue(self.admit().ready)
        self.revision.refresh_from_db()
        manifest = deepcopy(self.revision.action_receipts[MANIFEST_KEY])
        from django.utils import timezone
        horizon = timezone.datetime.fromisoformat(manifest["horizon_at"])
        result = self.recover(self.revision, horizon + timedelta(seconds=1))
        self.assertEqual((result.state, result.reason), ("manual", "provider_horizon_exhausted"))
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[MANIFEST_KEY], manifest)
        self.assert_zero_dispatch()

    def test_newer_customer_source_stops_zero_graph_recovery(self):
        self.assertTrue(self.admit().ready)
        self._message("Новий запит", "newer-source")
        result = self.recover(self.revision, self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual((result.state, result.reason), ("cancelled", "recovery_newer_inbound"))
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assert_zero_dispatch()

    def test_invalid_plan_or_paused_permission_cannot_commit_half_admission(self):
        invalid = [dict(self.plan[0], candidate_index=True)]
        self.assertEqual(self.admit(plan=invalid).reason, "provider_route_invalid")
        IgClient.objects.filter(pk=self.customer.pk).update(bot_paused=True)
        self.assertFalse(self.admit().ready)
        self.revision.refresh_from_db()
        self.assertNotIn(MANIFEST_KEY, self.revision.action_receipts)
        self.assertNotIn("generation_admission", self.revision.action_receipts)
        self.assert_zero_dispatch()

    def test_changed_source_cannot_freeze_an_admission(self):
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Changed customer evidence")
        self.assertFalse(self.admit().ready)
        self.revision.refresh_from_db()
        self.assertNotIn(MANIFEST_KEY, self.revision.action_receipts)
        self.assertNotIn("generation_admission", self.revision.action_receipts)
        self.assert_zero_dispatch()

    def test_unknown_delivery_cannot_use_zero_graph_recovery(self):
        self.assertTrue(self.admit().ready)
        effect = IgRevisionDeliveryEffect.objects.create(revision=self.revision, source_message=self.source,
            effect_key="native-unknown", group="reply", kind="text", order_index=0, part_index=0,
            part_count=1, plan_digest="a" * 64, payload_digest="b" * 64,
            recipient_igsid=self.customer.igsid, provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            client_permission_epoch=self.customer.reply_permission_epoch,
            revision_snapshot_digest=self.revision.snapshot_digest, publication_id=self.publication.pk,
            publication_version=self.publication.version, publication_hash=self.publication.snapshot_hash,
            authority_context_digest="c" * 64, state="unknown")
        result = self.recover(self.revision, self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(result.state, "manual")
        self.assertEqual(result.reason, "recovery_delivery_reconciliation")
        effect.refresh_from_db()
        self.assertEqual(effect.state, "unknown")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_policy_budget_fault_accepts_admission_with_manifest_and_zero_graphs(self):
        self.assertTrue(self.admit().ready)
        result = record_policy_readiness_failure(self.revision.pk, self.token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, code="required_context_budget_exceeded", park=False)
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        self.assertIn(MANIFEST_KEY, self.revision.action_receipts)
        self.assertEqual(self.revision.action_receipts["policy_readiness_failure"]["code"],
            "required_context_budget_exceeded")
        self.assert_zero_dispatch()

    def test_zero_dispatch_recovery_keeps_existing_eight_generation_cap(self):
        revision, token = self.revision, self.token
        self.assertTrue(self.admit().ready)
        self.revision.refresh_from_db()
        frozen = self.revision.action_receipts[MANIFEST_KEY]
        for round_number in range(MAX_RECOVERY_GENERATIONS):
            result = self.recover(revision, revision.overall_deadline + timedelta(seconds=1))
            self.assertEqual(result.state, "spawned", (round_number, result.reason))
            revision = IgCustomerTurnRevision.objects.get(pk=result.child_id)
            at = revision.quiet_started_at
            claimed = claim_sealed_revision(revision.pk, now=at)
            self.assertTrue(claimed.token, claimed.reason)
            revision, token = claimed.revision, claimed.token
            self.assertTrue(self.admit(revision=revision, token=token, now=at).ready)
            self.assertEqual(inspect_revision_provider_execution(revision, now=at).manifest, frozen)
        result = self.recover(revision, revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual((result.state, result.reason), ("manual", "recovery_generations_exhausted"))
        self.assertEqual(IgCustomerTurnRevision.objects.filter(origin="outage_recovery").count(), 8)
        self.assert_zero_dispatch()

    def test_native_local_fault_records_candidate_without_synchronous_send(self):
        self.assertTrue(self.admit().ready)
        with patch("management.services.ig_revision_live._execute_claimed_revision",
                side_effect=ValueError("private-local-error")):
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual((result.state, result.reasons), ("blocked", ("local_preprovider_fault",)))
        self.revision.refresh_from_db()
        self.assertIn("terminal_fault_ack_candidate", self.revision.action_receipts)
        self.assertEqual(self.revision.recovery_code, "local_preprovider_fault")
        self.assert_zero_dispatch()

    def test_optional_ack_candidate_failure_preserves_original_fault(self):
        self.assertTrue(self.admit().ready)
        with patch("management.services.ig_revision_live._execute_claimed_revision",
                side_effect=ValueError("private-original")), patch(
                "management.services.ig_terminal_fault_ack.record_terminal_fault_ack_candidate",
                side_effect=RuntimeError("private-optional")), self.assertLogs(
                    "management.services.ig_revision_live", level="WARNING") as logged:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(result.reasons, ("local_preprovider_fault",))
        self.assertNotIn("private", str(logged.output))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_code, "local_preprovider_fault")
        self.assert_zero_dispatch()

    def test_ack_worker_uses_one_existing_client_lease_and_releases_after_failure(self):
        from django.utils import timezone
        from management.services.ig_terminal_fault_ack import record_terminal_fault_ack_candidate

        self.assertTrue(self.admit().ready)
        fault = record_local_preprovider_fault(self.revision.pk, self.token,
            stage="revision_execution", exception_type="ValueError")
        self.assertTrue(fault.ready, fault.reason)
        candidate = record_terminal_fault_ack_candidate(self.revision.pk, settings_id=self.settings.pk)
        self.assertTrue(candidate.ready, candidate.reason)
        lease = "existing-customer-worker-slot"
        IgClient.objects.filter(pk=self.customer.pk).update(automation_lease_token=lease,
            automation_lease_until=timezone.now() + timedelta(minutes=2))
        with patch("management.services.instagram_bot.maintenance_status", return_value={"active": False}), patch(
                "management.services.instagram_bot.acquire_client_automation_lease",
                return_value=(self.customer, lease)) as acquire, patch(
                "management.services.instagram_bot.release_client_automation_lease") as release, patch(
                "management.services.ig_terminal_fault_ack.execute_terminal_fault_ack",
                side_effect=RuntimeError("no raw error logged")) as execute:
            self.assertEqual(_process_terminal_fault_acknowledgements(self.settings), 0)
        acquire.assert_called_once_with(self.customer.pk)
        execute.assert_called_once()
        release.assert_called_once_with(self.customer.pk, lease)
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.claim_token.startswith("debt:terminal-ack:"))
        self.assertIn("terminal_fault_ack_attempt", self.revision.action_receipts)
        self.assert_zero_dispatch()


class CanonicalRoutingEnvelopeTests(TestCase):
    message = capture_fixtures.RevisionTurnCaptureTests.message
    seal = capture_fixtures.RevisionTurnCaptureTests.seal

    def setUp(self):
        capture_fixtures.RevisionTurnCaptureTests.setUp(self)

    def test_full_context_reuses_same_sources_referral_time_and_canonical_route(self):
        for text, fact in (("Яка ціна?", ""), ("Хочу свій принт", "custom_print_brief"),
                           ("Порадьте розмір за мірками", "personalized_fit_required"),
                           ("Порівняйте футболки", "comparison_required")):
            with self.subTest(text=text):
                revision, collection, boundary = self.seal(self.message(text))
                route = capture_revision_routing(revision, generation_boundary=boundary,
                    collection=collection, settings_row=self.settings_row, publication=self.publication, now=self.now)
                with patch("management.services.ig_turn_capture._capture_sources", side_effect=AssertionError(
                        "must reuse admitted source capture")), patch(
                        "management.services.ig_turn_capture._referral", side_effect=AssertionError(
                            "must reuse admitted referral capture")):
                    result = capture_revision_context(revision, generation_boundary=boundary,
                        collection=collection, settings_row=self.settings_row, publication=self.publication,
                        now=self.now, routing_capture=route)
                self.assertIs(result.facts, route.facts)
                self.assertIs(result.decision, route.decision)
                self.assertEqual(result.decision.model_chain, route.decision.model_chain)
                if fact:
                    self.assertTrue(getattr(route.facts, fact))
                    self.assertEqual(route.decision.task_class, "complex_live")
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_media_and_ambiguous_referral_use_the_same_complex_route(self):
        bodies, sources = {}, []
        for index, mime in enumerate(("image/jpeg", "audio/mpeg")):
            part_id, body = "mp1_" + str(index) * 32, (mime + "-owned-body").encode()
            bodies[part_id] = mime, body
            part = dict(source_part_id=part_id, original_index=0, identity_origin="ingress",
                type="image" if index == 0 else "audio", status="owned", mime=mime, bytes=len(body),
                content_hash=hashlib.sha256(body).hexdigest(), private_storage=True,
                storage_name="ig-private/" + str(index), url="https://signed.invalid/media")
            sources.append(self.message("Підпис " + str(index), parts=[part],
                private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE))
        BotAdCampaign.objects.create(ad_id="early-routing-ad", is_active=True, theme="first")
        BotAdCampaign.objects.create(ad_id="early-routing-ad", is_active=True, theme="second")
        with patch("management.services.instagram_bot._owned_media_bytes",
                side_effect=lambda item, **kw: bodies[item["source_part_id"]]):
            revision, collection, boundary = self.seal(*sources, referral={"ad_id": "early-routing-ad"})
        route = capture_revision_routing(revision, generation_boundary=boundary, collection=collection,
            settings_row=self.settings_row, publication=self.publication, now=self.now)
        result = capture_revision_context(revision, generation_boundary=boundary, collection=collection,
            settings_row=self.settings_row, publication=self.publication, now=self.now, routing_capture=route)
        self.assertTrue(route.facts.has_image)
        self.assertTrue(route.facts.has_audio)
        self.assertTrue(route.facts.ambiguous_ad_referral)
        self.assertEqual(result.decision, route.decision)
        self.assertEqual(route.decision.task_class, "complex_live")
        self.assertEqual(route.decision.deadline_ms, 45000)

    def test_changed_clock_or_plan_cannot_rebind_the_early_route(self):
        revision, collection, boundary = self.seal(self.message("Яка ціна?"))
        route = capture_revision_routing(revision, generation_boundary=boundary, collection=collection,
            settings_row=self.settings_row, publication=self.publication, now=self.now)
        with self.assertRaises(TurnContextError) as error:
            capture_revision_context(revision, generation_boundary=boundary, collection=collection,
                settings_row=self.settings_row, publication=self.publication,
                now=self.now + timedelta(seconds=1), routing_capture=route)
        self.assertEqual(error.exception.reason, "routing_capture_binding_changed")


class CanonicalCandidateFactoryTests(SimpleTestCase):
    def test_custom_key_permissions_alias_dedup_and_frozen_secret_projection(self):
        raw = [dict(key_name="GEMINI_API", key_value="configured-private", model="gemini-3.5-flash-lite",
            project_identity="project-one", identity_status="known", skip_reason="")]
        for allowed, alias, expected in ((True, "", "(manual)"), (False, "", "GEMINI_API"),
                                         (True, "GEMINI_API", "GEMINI_API")):
            with self.subTest(allowed=allowed, alias=alias), patch.object(
                    gateway.gemini_keys, "manual_key_allowed", return_value=allowed), patch.object(
                    gateway.gemini_keys, "configured_alias_for_secret", return_value=alias), patch.object(
                    gateway.gemini_keys, "live_chat_candidate_plan", return_value=raw), patch.object(
                    gateway.gemini_keys, "model_circuit_open", return_value=False), patch.object(
                    gateway.gemini_scoreboard, "order_candidates", side_effect=lambda rows, **kw: rows):
                models, candidates = gateway.prepare_live_chat_candidate_plan(
                    model_chain_override=["forbidden-model", "gemini-3.5-flash-lite"], manual_key="custom-private")
            self.assertEqual(models, ["gemini-3.5-flash-lite"])
            self.assertEqual(candidates[0]["key_name"], expected)
            frozen = _frozen_candidates(candidates)
            self.assertTrue(frozen)
            self.assertNotIn("private", str(frozen))
            self.assertNotIn("key_value", str(frozen))
