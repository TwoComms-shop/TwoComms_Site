"""Canonical delivery projections recover without replaying provider sends."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management.models import IgClient, IgCustomerTurnRevision, IgRevisionDeliveryEffect, InstagramBotMessage, InstagramBotSettings
from management.services.ig_revision_execution import finalization_due_ids, finalize_sent_revision_effects
from management.services.ig_revision_outbox import _digest
from management.services.ig_revision_reply_projection import ADMISSION_KEY, RECEIPT_KEY, ReplyProjectionError, admission_binding, project_sent_reply


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionReplyProjectionTests(TransactionTestCase):
    reset_sequences = True
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = live_fixtures.RevisionLiveTests._generate
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record

    def _parts(self, states=("sent",), *, group="substantive_text", origin="", at=None, actor="bot", purpose="normal_reply", admitted=True, plan_digest_factory=None):
        self._prepare()
        if origin:
            self.revision.action_receipts = {"input_decision": {"origin": origin}}
            self.revision.save(update_fields=["action_receipts"])
        result = []
        for index, state in enumerate(states):
            content = {"text": f"Confirmed reply part {index}"} if group != "catalog_media" else {
                "attachment": {"type": "image", "payload": {"url": "https://example.com/image.jpg"}},
            }
            payload = {"recipient": {"id": self.customer.igsid}, "message": content}
            result.append(IgRevisionDeliveryEffect.objects.create(
                revision=self.revision, source_message=self.source,
                effect_key=f"projection:{self.revision.pk}:{index}",
                actor=actor, purpose=purpose, group=group,
                kind="image" if group == "catalog_media" else "text",
                order_index=index, part_index=index, part_count=len(states),
                plan_digest=plan_digest_factory(payload) if plan_digest_factory else "a" * 64,
                payload=payload, payload_digest=_digest(payload),
                recipient_igsid=self.customer.igsid, provider_namespace=self.source.provider_namespace,
                settings_id_snapshot=self.settings.pk, settings_permission_epoch=0, client_permission_epoch=0,
                revision_snapshot_digest=self.revision.snapshot_digest,
                publication_id=self.publication.pk, publication_version=self.publication.version,
                publication_hash=self.publication.snapshot_hash, authority_context_digest="b" * 64,
                state=state, provider_message_id=f"projection-mid-{self.revision.pk}-{index}" if state == "sent" else "",
                terminal_at=(at or timezone.now()) + timedelta(seconds=index),
            ))
        if admitted:
            self._admit(self.revision, result[0])
        return result

    def _admit(self, revision, effect):
        revision.action_receipts = {**(revision.action_receipts or {}), ADMISSION_KEY: {
            **admission_binding(revision, plan_digest=effect.plan_digest, settings_id=effect.settings_id_snapshot),
            "admitted_at": timezone.now().isoformat(),
        }}
        revision.save(update_fields=["action_receipts"])

    def _assert_count(self, count):
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.replies_count, count)

    def test_multipart_completion_replay_and_exact_echo_count_once(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo

        parts = self._parts(("sent", "sent", "sent"))
        receipt = project_sent_reply(self.revision.pk)
        self.assertEqual(receipt["count_delta"], 1)
        self.assertEqual(len(receipt["reply_message_ids"]), 3)
        self.assertEqual(project_sent_reply(self.revision.pk), receipt)
        observe_and_project_echo(self.settings, namespace=parts[0].provider_namespace,
                                 recipient=self.customer.igsid, mid=parts[0].provider_message_id)
        self._assert_count(1)
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_reply").count(), 3)

    def test_live_finalizer_counts_generated_reply_and_replay_does_no_io(self):
        self._prepare()
        result, _, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        admission = self.revision.action_receipts[ADMISSION_KEY]
        expected = admission_binding(self.revision, plan_digest=effect.plan_digest, settings_id=self.settings.pk)
        self.assertEqual({key: admission[key] for key in expected}, expected)
        self._assert_count(1)
        with patch("management.services.instagram_bot._provider_http") as send:
            result = finalize_sent_revision_effects(self.revision.pk)
        self.assertTrue(result.completed, result.reason)
        send.assert_not_called()
        self._assert_count(1)

    def test_receipt_time_repairs_existing_transcript_without_changing_identity(self):
        sent_at = timezone.now() - timedelta(days=3)
        effect = self._parts(at=sent_at)[0]
        message = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, role="model",
            synthetic_event_key=f"ig-revision-effect:{effect.pk}", source="revision_reply",
            provider_namespace=effect.provider_namespace, provider_message_id=effect.provider_message_id,
            text="Existing canonical transcript", processed_at=timezone.now(),
        )
        receipt = project_sent_reply(self.revision.pk)
        self.assertEqual(receipt["reply_message_ids"], [message.pk])
        message.refresh_from_db()
        self.assertEqual(message.text, "Existing canonical transcript")
        self.assertEqual(message.provider_created_at, sent_at)
        self.assertEqual(message.processed_at, sent_at)
        self.assertEqual(message.send_completed_at, sent_at)
        self.customer.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(self.customer.last_bot_reply_at, sent_at)
        self.assertEqual(self.settings.last_reply_at, sent_at)

    def test_older_receipt_never_moves_existing_timestamps_backwards(self):
        latest = timezone.now()
        IgClient.objects.filter(pk=self.customer.pk).update(last_bot_reply_at=latest)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(last_reply_at=latest, replies_count=7)
        self._parts(at=latest - timedelta(days=2))
        project_sent_reply(self.revision.pk)
        self.customer.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(self.customer.last_bot_reply_at, latest)
        self.assertEqual(self.settings.last_reply_at, latest)
        self.assertEqual(self.settings.replies_count, 8)

    def test_metrics_and_receipt_rollback_together_after_transcript_then_retry(self):
        self._parts()
        original = IgCustomerTurnRevision.save
        def fail_receipt(instance, *args, **kwargs):
            if RECEIPT_KEY in (instance.action_receipts or {}):
                raise RuntimeError("projection write unavailable")
            return original(instance, *args, **kwargs)
        with patch.object(IgCustomerTurnRevision, "save", fail_receipt):
            with self.assertRaises(RuntimeError):
                project_sent_reply(self.revision.pk)
        self._assert_count(0)
        self.assertFalse(InstagramBotMessage.objects.filter(source="revision_reply").exists())
        self.revision.refresh_from_db()
        self.assertNotIn(RECEIPT_KEY, self.revision.action_receipts)
        project_sent_reply(self.revision.pk)
        self._assert_count(1)

    def test_partial_unknown_only_projects_confirmed_history_until_reconciled(self):
        first, second = self._parts(("sent", "unknown"))
        self.assertEqual(project_sent_reply(self.revision.pk), {})
        self._assert_count(0)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.last_bot_reply_at)
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_reply").count(), 1)
        second.state = "sent"
        second.provider_message_id = "late-exact-receipt"
        second.save(update_fields=["state", "provider_message_id"])
        project_sent_reply(self.revision.pk)
        self._assert_count(1)

    def test_confirmed_failure_is_not_a_complete_reply(self):
        self._parts(("sent", "definite_failed"))
        self.assertEqual(project_sent_reply(self.revision.pk), {})
        self._assert_count(0)

    def test_media_only_is_visible_but_not_a_primary_reply(self):
        self._parts(group="catalog_media")
        receipt = project_sent_reply(self.revision.pk)
        self.assertEqual(receipt["count_outcome"], "not_primary_reply")
        self._assert_count(0)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.last_bot_reply_at)

    def test_holding_and_followup_excluded_by_origin_or_effect_purpose(self):
        self._parts(origin="holding")
        self.assertEqual(project_sent_reply(self.revision.pk)["count_delta"], 0)
        self._assert_count(0)

    def test_followup_effect_does_not_increment_primary_counter(self):
        self._parts(purpose="followup")
        self.assertEqual(project_sent_reply(self.revision.pk)["count_delta"], 0)
        self._assert_count(0)

    def test_no_reply_does_not_create_receipt_or_counter(self):
        self._prepare()
        self.assertEqual(project_sent_reply(self.revision.pk), {})
        self._assert_count(0)

    def test_missing_terminal_timestamp_cannot_invent_sent_time(self):
        effect = self._parts()[0]
        IgRevisionDeliveryEffect.objects.filter(pk=effect.pk).update(terminal_at=None)
        with self.assertRaises(ReplyProjectionError):
            project_sent_reply(self.revision.pk)
        self._assert_count(0)

    def test_processed_missing_projection_is_repairable_without_send_or_generation(self):
        self._parts()
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(state="processed", claim_token="", lease_until=None)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(status="done")
        self.assertIn(self.revision.pk, finalization_due_ids())
        with patch("management.services.instagram_bot._provider_http") as send:
            result = finalize_sent_revision_effects(self.revision.pk)
        self.assertTrue(result.completed, result.reason)
        send.assert_not_called()
        self._assert_count(1)
        self.assertNotIn(self.revision.pk, finalization_due_ids())

    def test_lineage_partial_then_successor_and_late_parent_completion_count_once(self):
        first, late = self._parts(("sent", "unknown"))
        project_sent_reply(self.revision.pk)
        self._assert_count(0)
        parent = self.revision
        fields = {field.attname: getattr(parent, field.attname)
                  for field in parent._meta.concrete_fields if not field.primary_key}
        fields.update(parent_id=parent.pk, revision=parent.revision + 1,
                      origin="auto_refresh", active_slot=None, claim_token="", lease_until=None,
                      action_receipts={})
        child = IgCustomerTurnRevision.objects.create(**fields)
        fields = {field.attname: getattr(first, field.attname)
                  for field in first._meta.concrete_fields if not field.primary_key}
        fields.update(revision_id=child.pk, effect_key=f"successor:{child.pk}", part_count=1,
                      provider_message_id="successor-complete", terminal_at=timezone.now())
        child_effect = IgRevisionDeliveryEffect.objects.create(**fields)
        self._admit(child, child_effect)
        self.assertEqual(project_sent_reply(child.pk)["count_delta"], 1)
        late.state = "sent"
        late.provider_message_id = "parent-late-confirmed"
        late.save(update_fields=["state", "provider_message_id"])
        receipt = project_sent_reply(parent.pk)
        self.assertEqual(receipt["count_outcome"], "logical_reply_already_counted")
        self.assertEqual(receipt["count_delta"], 0)
        self._assert_count(1)

    def test_disabled_permission_stale_source_and_expired_deadline_do_not_erase_receipt(self):
        self._parts()
        IgClient.objects.filter(pk=self.customer.pk).update(bot_paused=True, manager_takeover=True, reply_permission_epoch=2)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False, reply_permission_epoch=2)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Later edited source")
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None, overall_deadline=timezone.now() - timedelta(days=1))
        receipt = project_sent_reply(self.revision.pk)
        self.assertEqual(receipt["count_delta"], 1)
        self.assertEqual(receipt["funnel_projection"]["outcome"], "unsupported")

    def test_old_processed_without_admission_is_not_discovered_for_counter_repair(self):
        self._parts(admitted=False)
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(state="processed", claim_token="", lease_until=None)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(status="done")
        self.assertNotIn(self.revision.pk, finalization_due_ids())
        self._assert_count(0)
        self.revision.refresh_from_db()
        self.assertNotIn(ADMISSION_KEY, self.revision.action_receipts)

    def test_old_exact_echo_repairs_history_and_time_without_guessing_counter(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo

        sent_at = timezone.now() - timedelta(days=5)
        effect = self._parts(at=sent_at, admitted=False)[0]
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(state="processed", claim_token="", lease_until=None)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(status="done")
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(replies_count=9)
        for _ in range(2):
            observe_and_project_echo(self.settings, namespace=effect.provider_namespace,
                                     recipient=self.customer.igsid, mid=effect.provider_message_id)
        self._assert_count(9)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_bot_reply_at, sent_at)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[RECEIPT_KEY]
        self.assertFalse(receipt["count_admitted"])
        self.assertEqual(receipt["count_outcome"], "legacy_count_requires_review")
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_reply").count(), 1)
        self.assertNotIn(self.revision.pk, finalization_due_ids())

    def test_old_claimed_sent_finalizes_without_adding_admission_or_count(self):
        self._parts(admitted=False)
        result = finalize_sent_revision_effects(self.revision.pk, execution_token=self.token)
        self.assertTrue(result.completed, result.reason)
        self._assert_count(0)
        self.revision.refresh_from_db()
        self.assertNotIn(ADMISSION_KEY, self.revision.action_receipts)
        self.assertEqual(self.revision.action_receipts[RECEIPT_KEY]["count_outcome"], "legacy_count_requires_review")

    def test_existing_old_plan_replay_cannot_gain_projection_admission(self):
        from management.services.ig_revision_outbox import PublicationBinding, _normalize_specs, plan_revision_effects

        def old_digest(payload):
            return _digest({
                "revision_id": self.revision.pk, "revision_snapshot_digest": self.revision.snapshot_digest,
                "actor": "bot", "purpose": "normal_reply",
                "effects": _normalize_specs([{"group": "substantive_text", "kind": "text", "payload": payload}], self.customer.igsid),
                "publication": {"id": self.publication.pk, "version": self.publication.version, "hash": self.publication.snapshot_hash},
                "authority_context_digest": "b" * 64, "fact_bindings": [], "offer_bindings": [],
            })
        effect = self._parts(admitted=False, plan_digest_factory=old_digest)[0]
        result = plan_revision_effects(
            self.revision.pk, self.token, source_message_id=self.source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            authority_context_digest="b" * 64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": effect.payload}],
        )
        self.assertEqual([row.pk for row in result.effects], [effect.pk], result.reasons)
        self.assertFalse(result.created)
        self.revision.refresh_from_db()
        self.assertNotIn(ADMISSION_KEY, self.revision.action_receipts)
        self.assertEqual(project_sent_reply(self.revision.pk)["count_outcome"], "legacy_count_requires_review")
        self._assert_count(0)

    def test_invalid_admission_binding_is_not_mistaken_for_legacy_eligibility(self):
        self._parts(admitted=False)
        self.revision.action_receipts = {ADMISSION_KEY: {"version": "invalid"}}
        self.revision.save(update_fields=["action_receipts"])
        with self.assertRaisesRegex(ReplyProjectionError, "projection_admission_binding_changed"):
            project_sent_reply(self.revision.pk)
        self._assert_count(0)

    def test_source_preference_fallback_counts_primary_reply_without_model_success(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        result, generate, _ = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[RECEIPT_KEY]
        self.assertEqual(receipt["origin"], "source_preference_fallback")
        self.assertEqual(receipt["count_delta"], 1)
        self.assertEqual(self.revision.generation_proposal_digest, "")
        self._assert_count(1)

    def test_static_reply_retains_origin_and_counts_once(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = self.source.text
        self.settings.reply_text = "Можу допомогти з вибором. Який принт?"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        self._prepare()
        result, generate, _ = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[RECEIPT_KEY]["origin"], "static_reply")
        self._assert_count(1)

    def test_erasure_suppresses_private_projection(self):
        self._parts()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertEqual(project_sent_reply(self.revision.pk), {})
        self._assert_count(0)
        self.assertFalse(InstagramBotMessage.objects.filter(source="revision_reply").exists())
