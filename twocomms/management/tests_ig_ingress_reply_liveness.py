"""Provider-free source admission, actual worker/outbox and presence regressions."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from types import MethodType
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgCustomerTurnRevision, InstagramBotMessage, IgWebhookInboxEvent, IgPollCursor
from management.services import ig_presence as presence
from management.services.ig_ingress_source_proof import (
    bind_polled_ingress_proof, build_polled_ingress_proof,
    is_current_customer_source, is_promotable_poll_placeholder, presence_source_current,
)
from management.services.ig_revision_live import process_pending_revisions
from management.services.ig_revision_outbox import revision_has_newer_source
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2020-01-01T00:00:00+00:00")
class IngressReplyLivenessTests(TransactionTestCase):
    def setUp(self):
        from management.tests_ig_revision_live import RevisionLiveTests

        for name in ("_message", "_prepare", "_generate", "_execute"):
            setattr(self, name, MethodType(getattr(RevisionLiveTests, name), self))
        RevisionLiveTests.setUp(self)
        no_network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden in ingress regression"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def provider_message(self, **changes):
        return {"id": self.source.mid, "message": self.source.text,
            "from": {"id": self.customer.igsid}, "to": {"data": [{"id": "owner-1"}]},
            "created_time": timezone.now().isoformat(), **changes}

    def signed_poll(self, **changes):
        from management.services import instagram_bot as bot

        event = self.provider_message(**changes)
        proof = build_polled_ingress_proof(self.settings, event, conversation_id="conversation-1")
        self.assertTrue(proof)
        self.source.source = "poll"
        self.source.provider_created_at = timezone.datetime.fromisoformat(event["created_time"])
        self.source.text = event["message"]
        self.source.attachment_media = bot._normalize_message_media(bot._historicalize_provider_media(
            bot._provider_attachment_metadata(event)), message_scope=self.source.mid, identity_origin="legacy_positional")
        self.source.save(update_fields=["source", "provider_created_at", "text", "attachment_media"])
        with transaction.atomic():
            bind_polled_ingress_proof(self.source, proof)
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        return event, proof

    def test_signed_poll_reaches_actual_worker_and_outbox_sent(self):
        import traceback

        self.signed_poll()
        self.settings.ai_enabled = False
        self.settings.trigger_text = self.source.text
        self.settings.reply_text = "Вітаємо! Чим можемо допомогти?"
        self.settings.save()
        errors = []
        def record_worker_error(level, *_args, **_kwargs):
            if level == "error":
                errors.append(traceback.format_exc())

        with patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
             patch("management.services.instagram_bot._provider_http", return_value=(200, '{"message_id":"poll-sent"}')) as http, \
             patch("management.services.instagram_bot._register_outgoing_message"), \
             patch("management.services.instagram_bot.log", side_effect=record_worker_error):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1, errors)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        effect = self.revision.delivery_effects.get()
        self.assertEqual((effect.state, effect.source_message_id), ("sent", self.source.pk))
        self.assertEqual(self.source.source, "poll")
        self.assertEqual(self.source.status, "done")

    def test_unsigned_poll_is_not_accepted_by_actual_commerce_execution(self):
        self.source.source = "poll"
        self.source.save(update_fields=["source"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("commerce_source_changed",))
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_signed_poll_reaches_actual_generation_projection_and_outbox(self):
        self.signed_poll()
        self._prepare()
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")

    def test_signed_poll_unavailable_media_keeps_honest_caption_answer_executable(self):
        from management.services.ig_revision_execution import prepare_revision
        from management.services.ig_revision_live import capture_revision_source

        self.signed_poll(attachments=[{"type": "image", "payload": {"url": "https://media.invalid/photo.jpg"}}])
        prepared = prepare_revision(self.revision.pk, capture_revision_source)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        self.assertFalse(self.source.media_capture_eligible)
        self.assertEqual(self.revision.bundle_snapshot["sources"][0]["media_parts"][0]["capture_outcome"], "unavailable")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)

    def test_signed_poll_content_namespace_identity_and_timestamp_edits_are_rejected(self):
        self.signed_poll()
        self.assertTrue(is_current_customer_source(self.source, namespace="instagram_login:owner-1"))
        for field, value in (("text", "replaced text"), ("provider_namespace", "instagram_login:other"),
            ("sender_id", "17849999999999"), ("mid", "other-mid"),
            ("provider_created_at", timezone.now() - timedelta(days=1))):
            before = getattr(self.source, field)
            setattr(self.source, field, value)
            self.assertFalse(is_current_customer_source(self.source), field)
            setattr(self.source, field, before)

    def test_signed_poll_question_survives_following_webhook_burst(self):
        from management.services.ig_customer_turns import ensure_turn_for_inbound
        from management.services.ig_revision_source_coverage import validate_source_transfer

        self.signed_poll()
        fresh = self._message("А ще підкажіть розміри", "following-webhook")
        attached = ensure_turn_for_inbound(fresh)
        successor = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.assertTrue(validate_source_transfer(successor))
        self.assertEqual(list(successor.sources.order_by("ordinal").values_list("message_id", flat=True)),
            [self.source.pk, fresh.pk])
        self.source.refresh_from_db()
        self.assertEqual(self.source.source, "poll")
        self.assertTrue(is_current_customer_source(self.source))

    def test_poll_proof_is_rechecked_at_real_outbox_after_generation(self):
        self.signed_poll()
        self._prepare()
        self.after_validate = lambda: InstagramBotMessage.objects.filter(pk=self.source.pk).update(ingress_proof={})
        result, generate, http = self._execute()
        self.assertIn("revision_sources_changed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_untrusted_or_historical_provider_shapes_cannot_mint_current_proof(self):
        variants = ({"from": {"id": "owner-1"}}, {"to": {"data": [{"id": "other"}]}},
            {"created_time": (timezone.now() - timedelta(hours=24)).isoformat()},
            {"created_time": (timezone.now() + timedelta(hours=1)).isoformat()},
            {"created_time": ""}, {"id": ""})
        for changes in variants:
            self.assertEqual(build_polled_ingress_proof(self.settings, self.provider_message(**changes),
                conversation_id="conversation-1"), {}, changes)

    def test_historical_higher_pk_does_not_cancel_actual_outbox_send(self):
        self._prepare()
        historical = self._message("Old conversation context", "historical-late-insert")
        historical.source = "poll_history"
        historical.status = "done"
        historical.provider_created_at = timezone.now() - timedelta(days=3)
        historical.save()
        self.assertFalse(revision_has_newer_source(self.revision))
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")

    def test_fresh_webhook_still_fences_outbox_before_provider_io(self):
        self._prepare()
        self._message("New current request", "fresh-request")
        self.assertTrue(revision_has_newer_source(self.revision))
        result, generate, http = self._execute()
        self.assertIn("pending_inbound", result.reasons)
        generate.assert_not_called()
        http.assert_not_called()

    def test_presence_fences_unmaterialized_ingress_and_retains_source_ownership(self):
        watermark = self.source.pk
        args = ("instagram_login:owner-1", self.customer.pk, self.customer.igsid, watermark)
        self.assertTrue(presence_source_current(*args))
        event = IgWebhookInboxEvent.objects.create(namespace=args[0], owner_id="owner-1",
            customer_igsid=self.customer.igsid, event_key="fresh-pending", decision="accepted",
            payload={"entry": [{"id": "owner-1", "messaging": [{"sender": {"id": self.customer.igsid},
                "recipient": {"id": "owner-1"}, "message": {"mid": "pending-new", "text": "question"}}]}]},
            payload_digest="a" * 64)
        self.assertFalse(presence_source_current(*args))
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
        self.assertTrue(presence_source_current(*args))
        self.source.source = "manual_refresh"
        self.source.save(update_fields=["source"])
        self.assertFalse(presence_source_current(*args))

    def test_poll_placeholder_promotion_requires_current_exact_untouched_identity(self):
        self.source.source, self.source.status, self.source.text = "poll_history", "done", "(медіа)"
        self.source.provider_created_at = timezone.now()
        self.source.save()
        at = self.source.provider_created_at
        self.assertTrue(is_promotable_poll_placeholder(self.source, self.settings, received_at=at))
        self.assertFalse(is_promotable_poll_placeholder(self.source, self.settings, received_at=at + timedelta(seconds=2)))
        self.source.send_state = "unknown"
        self.assertFalse(is_promotable_poll_placeholder(self.source, self.settings, received_at=at))

    def test_preparation_failure_does_not_mark_seen_or_invent_customer_response(self):
        calls = []
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        controller = presence.PresenceController()
        with patch.object(presence, "PRESENCE_DIRECTORY", Path(directory.name)), \
             patch.object(presence.threading.Thread, "start"):
            handle = controller.start(key="test:preparation", source_watermark=self.source.pk,
                transport=lambda action: calls.append(action) or presence.SenderActionResult(True, 200, "accepted", action),
                guard=lambda cleanup: True, owner_check=lambda: True, completion_guard=lambda: True)
            # Stop at the preparation failure boundary before any transport.
            handle.stop()
            handle._run()
        self.assertNotIn("mark_seen", calls)
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role="model").exists())

    def test_actual_enqueue_binds_proof_before_turn_and_duplicate_does_not_replay(self):
        from management.services import instagram_bot as bot
        from management.services.ig_customer_turns import ensure_turn_for_inbound

        event = self.provider_message(id="poll-enqueue", message="Вітаю, цікавить співпраця")
        proof = build_polled_ingress_proof(self.settings, event, conversation_id="conversation-1")
        observed = []
        def verified_turn(row, **kwargs):
            self.assertTrue(row.ingress_proof)
            self.assertTrue(is_current_customer_source(row))
            observed.append(row.pk)
            return ensure_turn_for_inbound(row, **kwargs)

        args = dict(sender_id=self.customer.igsid, text=event["message"], mid=event["id"],
            source="poll", received_at=timezone.datetime.fromisoformat(event["created_time"]),
            poll_ingress_proof=proof, persistence_only=True)
        with patch("management.services.ig_customer_turns.ensure_turn_for_inbound", side_effect=verified_turn):
            self.assertTrue(bot.enqueue_inbound(self.settings, **args))
            self.assertFalse(bot.enqueue_inbound(self.settings, **args))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertEqual(observed, [row.pk])
        self.assertEqual(row.source, "poll")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.turn_membership.message_id, row.pk)

    def test_actual_enqueue_rejects_unsigned_or_tampered_poll_before_identity_consumption(self):
        from management.services import instagram_bot as bot

        event = self.provider_message(id="untrusted-poll", message="New question")
        proof = build_polled_ingress_proof(self.settings, event, conversation_id="conversation-1")
        args = dict(sender_id=self.customer.igsid, text=event["message"], mid=event["id"],
            source="poll", received_at=timezone.datetime.fromisoformat(event["created_time"]),
            persistence_only=True)
        self.assertFalse(bot.enqueue_inbound(self.settings, **args))
        self.assertFalse(bot.enqueue_inbound(self.settings, **{**args, "text": "STOP", "poll_ingress_proof": proof}))
        self.assertFalse(InstagramBotMessage.objects.filter(mid=event["id"]).exists())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.opted_out_at)

    def test_actual_enqueue_poll_media_never_waits_for_unauthorized_capture(self):
        from management.services import instagram_bot as bot
        from management.services.ig_revision_execution import prepare_revision
        from management.services.ig_revision_live import capture_revision_source

        event = self.provider_message(id="poll-no-capture", message="Що можете порадити?",
            attachments=[{"type": "image", "payload": {"url": "https://media.invalid/current.jpg"}}])
        proof = build_polled_ingress_proof(self.settings, event, conversation_id="conversation-1")
        self.assertTrue(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            mid=event["id"], text=event["message"], source="poll", poll_ingress_proof=proof,
            attachments=bot._extract_media_urls(event), attachment_metadata=bot._provider_attachment_metadata(event),
            received_at=timezone.datetime.fromisoformat(event["created_time"]), persistence_only=True))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertFalse(row.media_capture_eligible)
        revision = row.revision_sources.get(revision__active_slot=1).revision
        prepared = prepare_revision(revision.pk, capture_revision_source,
            now=timezone.now() + timedelta(seconds=2))
        self.assertTrue(prepared.ready, prepared.reason)
        revision.refresh_from_db()
        media = next(item for item in revision.bundle_snapshot["sources"] if item["message_id"] == row.pk)["media_parts"]
        self.assertEqual(media[0]["capture_outcome"], "unavailable")

    def test_actual_poll_partial_then_complete_admits_once_and_advances_only_complete_cursor(self):
        from management.services import instagram_bot as bot

        self.settings.receive_via_poll = True
        self.settings.save(update_fields=["receive_via_poll"])
        cursor = IgPollCursor.objects.create(conversation_id="conversation-1", participant_igsid=self.customer.igsid)
        event = self.provider_message(id="partial-poll", message="Вітаю, цікавить співпраця")
        partial = {"messages": [event], "requests": 1, "complete": False,
            "budget_exhausted": False, "reason": "http_503"}
        complete = {**partial, "complete": True, "reason": "complete"}
        with patch.object(bot, "get_page_token", return_value="test-token"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conversation-1"]), \
             patch.object(bot, "_fetch_polled_conversation", side_effect=[partial, complete]), \
             patch.object(bot, "_schedule_inbound_analysis"), \
             patch("management.services.bot_sales_classifier.classify_message", return_value={}), \
             patch("management.services.bot_followups.schedule_after_inbound"):
            first = bot.poll_ingest(self.settings)
            self.assertEqual(first["enqueued"], 1)
            cursor.refresh_from_db()
            self.assertFalse(cursor.last_message_id)
            # A later worker pass happens after the provider retry delay.
            IgPollCursor.objects.filter(pk=cursor.pk).update(next_attempt_at=None)
            second = bot.poll_ingest(self.settings)
        self.assertEqual(second["enqueued"], 0)
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertTrue(is_current_customer_source(row))
        cursor.refresh_from_db()
        self.assertEqual(cursor.last_message_id, event["id"])

    def test_actual_richer_webhook_promotes_current_empty_poll_placeholder_once(self):
        from management.services import instagram_bot as bot

        event = self.provider_message(id="placeholder-webhook", message="")
        self.assertTrue(bot._persist_polled_message(self.settings, event, observed_only=True))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertEqual((row.source, row.status), ("poll_history", "done"))
        self.assertFalse(row.revision_sources.exists())
        args = dict(sender_id=self.customer.igsid, mid=event["id"], text="Пропоную співпрацю для вашого бренду",
            source="webhook", received_at=timezone.datetime.fromisoformat(event["created_time"]), persistence_only=True)
        self.assertTrue(bot.enqueue_inbound(self.settings, **args))
        self.assertFalse(bot.enqueue_inbound(self.settings, **args))
        row.refresh_from_db()
        self.assertEqual((row.source, row.status), ("webhook", "pending"))
        self.assertTrue(row.revision_sources.exists())

    def test_actual_caption_promotes_current_typed_url_less_poll_placeholder(self):
        from management.services import instagram_bot as bot

        event = self.provider_message(id="typed-placeholder", message="",
            attachments=[{"type": "audio", "payload": {"id": "audio-part"}}])
        self.assertTrue(bot._persist_polled_message(self.settings, event, observed_only=True))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertEqual(row.text, "(голосове повідомлення)")
        self.assertTrue(is_promotable_poll_placeholder(row, self.settings,
            received_at=timezone.datetime.fromisoformat(event["created_time"])))
        self.assertTrue(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            mid=event["id"], text="Пропоную співпрацю для вашого бренду", source="webhook",
            received_at=timezone.datetime.fromisoformat(event["created_time"]), persistence_only=True))
        row.refresh_from_db()
        self.assertEqual((row.source, row.status), ("webhook", "pending"))

    def test_typed_placeholder_wrong_time_and_manual_real_text_mismatch_stay_observed(self):
        from management.services import instagram_bot as bot

        event = self.provider_message(id="typed-negative", message="",
            attachments=[{"type": "audio", "payload": {"id": "audio-part"}}])
        self.assertTrue(bot._persist_polled_message(self.settings, event, observed_only=True))
        at = timezone.datetime.fromisoformat(event["created_time"])
        self.assertFalse(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            mid=event["id"], text="Different content", source="webhook",
            received_at=at + timedelta(seconds=2), persistence_only=True))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertEqual((row.source, row.status), ("poll_history", "done"))
        row.source, row.text = "manual_refresh", "Previously observed real customer text"
        row.save(update_fields=["source", "text"])
        self.assertFalse(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            mid=event["id"], text="Different content", source="webhook", received_at=at, persistence_only=True))
        row.refresh_from_db()
        self.assertEqual((row.source, row.text), ("manual_refresh", "Previously observed real customer text"))

    def test_actual_old_poll_history_is_not_promoted_by_delayed_webhook(self):
        from management.services import instagram_bot as bot

        event = self.provider_message(id="old-placeholder", message="",
            created_time=(timezone.now() - timedelta(days=2)).isoformat())
        self.assertTrue(bot._persist_polled_message(self.settings, event, observed_only=True))
        self.assertFalse(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            mid=event["id"], text="Older request", source="webhook",
            received_at=timezone.datetime.fromisoformat(event["created_time"]), persistence_only=True))
        row = InstagramBotMessage.objects.get(mid=event["id"])
        self.assertEqual((row.source, row.status), ("poll_history", "done"))
        self.assertFalse(row.revision_sources.exists())
