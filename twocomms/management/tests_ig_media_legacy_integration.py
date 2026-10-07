"""Current native-UGC captions traverse the real legacy response worker."""
import hashlib
from unittest.mock import patch

from django.test import TestCase, override_settings

from management import tests_ig_live_reply_priority as legacy_fixture
from management.models import IgBotNotification, InstagramBotMessage
from management.services import instagram_bot


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class LegacyNativeMediaCaptionTests(TestCase):
    setUp = legacy_fixture.QuietDegradationTests.setUp
    _pending = legacy_fixture.QuietDegradationTests._pending

    @patch("management.services.bot_followups.schedule_after_bot_reply")
    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.instagram_bot._collect_media_parts", return_value=[])
    @patch("management.services.instagram_bot.send_sender_action")
    @patch("management.services.instagram_bot._wait_for_typing_window", return_value="allowed")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.instagram_bot.send_text", return_value=(True, "", "legacy-caption-reply"))
    def test_unavailable_native_mention_keeps_current_caption_generation_and_answer(
        self, send_text, generate, _typing_wait, _sender_action, _parts, _capture, schedule_followup,
    ):
        generated = "Які елементи дизайну ви хотіли б уточнити?"
        generate.return_value = generated
        source = self._pending("Що ви думаєте про цю ідею дизайну?", "native-caption-unavailable",
            media=[{
                "media_type": "story_mention", "provenance": "live_webhook",
                "provider_native_mention": True, "target_username": "twocomms",
                "status": "unavailable", "provider_object_key": "story_mention:caption-1",
                "provider_media_id": "caption-media-1", "provider_event_id": "caption-event-1",
            }])

        instagram_bot.process_pending(self.settings, max_items=1)

        generate.assert_called_once()
        self.assertIsNone(generate.call_args.kwargs["images"])
        send_text.assert_called_once()
        self.assertEqual(send_text.call_args.args[2], generated)
        schedule_followup.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(source.status, source.Status.DONE)

    def _native_manager_source(self, caption, *, owned=False):
        part = {
            "source_part_id": "mp1_" + "b" * 32, "original_index": 0, "identity_origin": "ingress",
            "media_type": "story_mention", "type": "image", "provenance": "live_webhook",
            "provider_native_mention": True, "target_username": "twocomms",
            "status": "owned" if owned else "unavailable", "provider_object_key": "story_mention:manager-caption",
            "provider_media_id": "manager-caption-media", "provider_event_id": "manager-caption-event",
        }
        if owned:
            part.update(storage_name="ugc/manager-caption.jpg", mime="image/jpeg", bytes=9,
                content_hash=hashlib.sha256(b"ugc-image").hexdigest(), private_storage=True)
        return self._pending(caption, "native-manager-caption", media=[part])

    @patch("management.services.instagram_bot._deliver_manager_notification", return_value=False)
    @patch("management.services.bot_followups.schedule_after_bot_reply")
    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.instagram_bot._collect_media_parts", return_value=[])
    @patch("management.services.instagram_bot.send_sender_action")
    @patch("management.services.instagram_bot._wait_for_typing_window", return_value="allowed")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.instagram_bot.send_text", return_value=(True, "", "legacy-manager-caption-reply"))
    def test_unavailable_native_media_retains_explicit_return_manager_request_and_queues_one_notification(
        self, send_text, generate, _typing_wait, _sender_action, _parts, _capture, schedule_followup, _telegram,
    ):
        reply = "Підкажіть, будь ласка, що сталося з товаром?"
        generate.return_value = {"reply_text": reply, "controls": [{"kind": "manager", "value": True}]}
        source = self._native_manager_source("Мені потрібен менеджер щодо повернення замовлення.")

        instagram_bot.process_pending(self.settings, max_items=1)

        generate.assert_called_once()
        send_text.assert_called_once()
        self.assertEqual(send_text.call_args.args[2], reply)
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, self.client.Stage.LEAD_TO_MANAGER)
        notification = IgBotNotification.objects.get(client=self.client, event_type="escalation")
        self.assertEqual(notification.status, IgBotNotification.Status.PENDING)
        self.assertFalse(InstagramBotMessage.objects.filter(client=self.client, role="manager").exists())
        schedule_followup.assert_not_called()

        # Completed-source replay does not regenerate or create a second queue
        # entry. A queued operator notification is not a manager reply receipt.
        instagram_bot.process_pending(self.settings, max_items=1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.client, event_type="escalation").count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.status, source.Status.DONE)

    @patch("management.services.instagram_bot._deliver_manager_notification", return_value=False)
    @patch("management.services.bot_followups.schedule_after_bot_reply")
    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.instagram_bot._collect_media_parts")
    @patch("management.services.instagram_bot.send_sender_action")
    @patch("management.services.instagram_bot._wait_for_typing_window", return_value="allowed")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.instagram_bot.send_text", return_value=(True, "", "legacy-unjustified-manager-reply"))
    def test_bare_native_photo_cannot_escalate_from_unjustified_parsed_model_manager_control(
        self, send_text, generate, _typing_wait, _sender_action, parts, _capture, schedule_followup, telegram,
    ):
        source = self._native_manager_source("(зображення)", owned=True)
        initial_stage = self.client.stage
        parts.return_value = [{
            "source_part_id": "mp1_" + "b" * 32, "source_message_scope": str(source.pk),
            "original_index": 0, "identity_origin": "ingress", "provenance": "live_webhook",
            "status": "owned", "capture_state": "owned", "mime": "image/jpeg", "bytes": 9,
            "content_hash": hashlib.sha256(b"ugc-image").hexdigest(), "data": b"ugc-image",
        }]
        generate.return_value = {"reply_text": "Дякуємо, що поділилися!",
            "controls": [{"kind": "manager", "value": True}]}

        instagram_bot.process_pending(self.settings, max_items=1)

        generate.assert_called_once()
        send_text.assert_called_once()
        self.assertFalse(IgBotNotification.objects.filter(client=self.client, event_type="escalation").exists())
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, initial_stage)
        telegram.assert_not_called()
        schedule_followup.assert_not_called()
