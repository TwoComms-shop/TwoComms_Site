from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import (
    HumanReplyRejected,
    create_human_reply_command,
    dispatch_human_reply_command,
)


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class HumanReplyCommandTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="human-reply-admin", email="human@example.test", password="x"
        )
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ig_user_id = "999999"
        self.settings.page_id = "999999"
        self.settings.save(update_fields=["is_enabled", "ig_user_id", "page_id"])
        self.customer = IgClient.get_or_create_for_sender("123456789")
        self.at = timezone.now() - timedelta(minutes=5)
        self.inbound = InstagramBotMessage.objects.create(
            sender_id=self.customer.igsid,
            client=self.customer,
            role=InstagramBotMessage.Role.USER,
            text="Підкажіть ціну",
            mid="human-inbound-1",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=self.at,
            processed_at=self.at,
        )

    def test_takeover_and_duplicate_operation_are_idempotent(self):
        first = create_human_reply_command(
            self.customer.pk,
            actor=self.actor,
            text="Вітаю! Підкажу ціну.",
            operation_id="11111111-1111-1111-1111-111111111111",
        )
        duplicate = create_human_reply_command(
            self.customer.pk,
            actor=self.actor,
            text="Вітаю! Підкажу ціну.",
            operation_id="11111111-1111-1111-1111-111111111111",
        )
        self.customer.refresh_from_db()
        self.assertEqual(first.command.pk, duplicate.command.pk)
        self.assertTrue(duplicate.idempotent)
        self.assertTrue(self.customer.bot_paused)
        self.assertTrue(self.customer.manager_takeover)
        self.assertEqual(HumanReplyCommand.objects.count(), 1)

    @patch("management.services.ig_human_reply.InstagramBotSettings.load")
    @patch("management.services.instagram_bot.send_text")
    def test_provider_receipt_creates_manager_message_once(self, send_text, load):
        load.return_value = self.settings
        send_text.return_value = SimpleNamespace(
            ok=True, kind="", hint="", provider_message_ids=("mid-human-1",)
        )
        result = create_human_reply_command(
            self.customer.pk, actor=self.actor, text="Готово", operation_id="22222222-2222-2222-2222-222222222222"
        )
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.SENT)
        self.assertEqual(send_text.call_count, 1)
        message = command.reply_message
        message.refresh_from_db()
        self.assertEqual(message.role, InstagramBotMessage.Role.MANAGER)
        self.assertEqual(message.source, "human_reply")
        self.assertEqual(message.provider_message_id, "mid-human-1")
        self.assertEqual(InstagramBotMessage.objects.filter(source="human_reply").count(), 1)

    @patch("management.services.ig_human_reply.InstagramBotSettings.load")
    @patch("management.services.instagram_bot.send_text")
    def test_unknown_is_terminal_and_never_retried(self, send_text, load):
        load.return_value = self.settings
        send_text.return_value = SimpleNamespace(
            ok=False, kind="unknown", hint="timeout", provider_message_ids=()
        )
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.UNKNOWN)
        dispatch_human_reply_command(command.pk)
        self.assertEqual(send_text.call_count, 1)

    def test_new_inbound_invalidates_context_before_send(self):
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")
        InstagramBotMessage.objects.create(
            sender_id=self.customer.igsid,
            client=self.customer,
            role=InstagramBotMessage.Role.USER,
            text="Ще питання",
            mid="human-inbound-2",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=timezone.now(),
        )
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.CANCELLED)
        self.assertEqual(command.failure_code, "newer_inbound")
