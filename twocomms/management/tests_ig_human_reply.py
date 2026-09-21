from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.models import (
    IgClient,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)
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
        task = IgFollowUpTask.objects.get(
            event_key=f"human-reply-unknown:{command.pk}"
        )
        self.assertEqual(task.kind, IgFollowUpTask.Kind.MANAGER_TASK)
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.reason, "human_reply:delivery_unknown")
        self.assertEqual(
            task.manager_approval_status,
            IgFollowUpTask.ManagerApprovalStatus.PENDING,
        )
        self.assertEqual(task.event_payload["command_id"], command.pk)
        self.assertEqual(
            task.event_payload["operation_id"], str(command.operation_id)
        )
        self.assertEqual(task.event_payload["source"], "human_reply_command")
        self.assertEqual(task.event_payload["source_message_id"], self.inbound.pk)
        self.assertEqual(task.event_payload["part_count"], 1)
        self.assertEqual(task.event_payload["parts"][0]["part_index"], 0)
        self.assertEqual(
            task.event_payload["parts"][0]["provider_message_id"], ""
        )
        self.assertEqual(len(task.event_payload["parts"][0]["payload_digest"]), 64)
        self.assertFalse(task.manager_context["automatic_http_retry"])
        from management.bot_views import _with_latest_interaction

        projected = _with_latest_interaction(
            IgClient.objects.all()
        ).get(pk=self.customer.pk)
        self.assertTrue(projected.has_manager_action)

        from django.urls import reverse

        self.client.force_login(self.actor)
        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.customer.pk])
        )
        self.assertEqual(detail.status_code, 200)
        detail_task = next(
            item
            for item in detail.json()["followups"]
            if item["event_key"] == f"human-reply-unknown:{command.pk}"
        )
        self.assertEqual(detail_task["reason"], "human_reply:delivery_unknown")
        self.assertEqual(detail_task["status_label"], "Потребує перевірки")
        self.assertEqual(detail_task["reason_label"], "Звірка ручної доставки")
        dispatch_human_reply_command(command.pk)
        self.assertEqual(send_text.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

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

    @patch("management.services.ig_human_reply.InstagramBotSettings.load")
    @patch("management.services.instagram_bot.send_text")
    def test_provider_started_reentry_is_read_only_and_recursive_dispatch_is_safe(
        self, send_text, load
    ):
        load.return_value = self.settings
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")
        command = result.command

        def recursive_send(*args, **kwargs):
            nested = dispatch_human_reply_command(command.pk)
            self.assertEqual(nested.state, HumanReplyCommand.State.PROVIDER_STARTED)
            return SimpleNamespace(
                ok=True, kind="", hint="", provider_message_ids=("mid-reentry",)
            )

        send_text.side_effect = recursive_send
        returned = dispatch_human_reply_command(command.pk)
        self.assertEqual(returned.state, HumanReplyCommand.State.SENT)
        self.assertEqual(send_text.call_count, 1)

    @patch("management.services.ig_human_reply.InstagramBotSettings.load")
    @patch("management.services.instagram_bot.send_text")
    def test_boundary_rechecks_late_inbound_before_provider_io(self, send_text, load):
        load.return_value = self.settings
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")

        def late_boundary_send(*args, **kwargs):
            InstagramBotMessage.objects.create(
                sender_id=self.customer.igsid,
                client=self.customer,
                role=InstagramBotMessage.Role.USER,
                text="Пізніше питання",
                mid="human-inbound-late",
                status=InstagramBotMessage.Status.DONE,
                provider_created_at=timezone.now(),
            )
            with kwargs["permission_boundary_factory"]() as allowed:
                self.assertFalse(allowed)
            return SimpleNamespace(
                ok=False, kind="cancelled", hint="newer_inbound", provider_message_ids=()
            )

        send_text.side_effect = late_boundary_send
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.CANCELLED)
        self.assertEqual(command.failure_code, "newer_inbound")

    def test_unknown_command_blocks_competing_operation(self):
        first = create_human_reply_command(self.customer.pk, actor=self.actor, text="Перше")
        HumanReplyCommand.objects.filter(pk=first.command.pk).update(
            state=HumanReplyCommand.State.UNKNOWN
        )
        with self.assertRaisesRegex(HumanReplyRejected, "competing_command"):
            create_human_reply_command(self.customer.pk, actor=self.actor, text="Друге")

    def test_unknown_command_blocks_same_actor_after_context_changes(self):
        first = create_human_reply_command(self.customer.pk, actor=self.actor, text="Перше")
        HumanReplyCommand.objects.filter(pk=first.command.pk).update(
            state=HumanReplyCommand.State.UNKNOWN
        )
        InstagramBotMessage.objects.create(
            sender_id=self.customer.igsid,
            client=self.customer,
            role=InstagramBotMessage.Role.USER,
            text="Нове питання",
            mid="human-inbound-after-unknown",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=timezone.now(),
        )

        with self.assertRaisesRegex(HumanReplyRejected, "competing_command"):
            create_human_reply_command(self.customer.pk, actor=self.actor, text="Друге")

        self.assertEqual(HumanReplyCommand.objects.count(), 1)

    def test_active_command_is_owned_by_one_actor_across_context_changes(self):
        other_actor = get_user_model().objects.create_superuser(
            username="human-reply-other-admin",
            email="human-other@example.test",
            password="x",
        )
        create_human_reply_command(self.customer.pk, actor=self.actor, text="Перше")
        InstagramBotMessage.objects.create(
            sender_id=self.customer.igsid,
            client=self.customer,
            role=InstagramBotMessage.Role.USER,
            text="Нове питання",
            mid="human-inbound-ownership-race",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=timezone.now(),
        )
        with self.assertRaisesRegex(HumanReplyRejected, "command_owned_by_other_actor"):
            create_human_reply_command(self.customer.pk, actor=other_actor, text="Друге")

    def test_utf8_byte_budget_requires_complete_delivery_plan(self):
        with self.assertRaisesRegex(HumanReplyRejected, "delivery_plan_incomplete"):
            create_human_reply_command(self.customer.pk, actor=self.actor, text="я" * 2000)

    @patch("management.services.ig_human_reply.InstagramBotSettings.load")
    @patch("management.services.instagram_bot.send_text")
    def test_provider_receipt_rejects_degraded_delivery_plan(self, send_text, load):
        load.return_value = self.settings
        send_text.return_value = SimpleNamespace(
            ok=True,
            kind="degraded_link_restriction",
            hint="url removed",
            provider_message_ids=("mid-degraded",),
            planned_chunk_count=1,
            delivered_chunk_count=1,
        )
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.DEFINITE_FAILED)
        self.assertEqual(command.failure_code, "delivery_plan_incomplete")
