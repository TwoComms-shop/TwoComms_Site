from datetime import timedelta
from dataclasses import replace
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart
from management.models import (
    AdminAuditLog,
    IgClient,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_human_reply import (
    HumanReplyRejected,
    _window_deadline,
    create_human_reply_command,
    dispatch_human_reply_command,
)


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class HumanReplyCommandTests(TransactionTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.provider_mid = "mid-human-1"
        self.provider_exception = None
        self.expected_legacy_body = None
        token = patch("management.services.instagram_bot.get_page_token", return_value="local-test-token")
        self.token = token.start()
        self.addCleanup(token.stop)
        physical = patch("management.services.instagram_bot._provider_http", side_effect=self._physical_http)
        self.http = physical.start()
        self.addCleanup(physical.stop)
        memory = patch("management.services.instagram_bot._enqueue_memory_source_event", return_value=True)
        self.memory = memory.start()
        self.addCleanup(memory.stop)
        self.actor = get_user_model().objects.create_superuser(
            username="human-reply-admin", email="human@example.test", password="x"
        )
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="999999", page_id="999999")
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
            provider_namespace="instagram_login:999999",
        )

    def _physical_http(self, settings, url, *, token, data):
        # These legacy regressions now exercise real send_text callbacks and a
        # genuinely committed start marker, not an opaque fabricated receipt.
        self.assertFalse(connection.in_atomic_block)
        payload = json.loads(data)
        self.assertEqual(payload["recipient"], {"id": self.customer.igsid})
        part = HumanReplyPart.objects.filter(state=HumanReplyPart.State.PROVIDER_STARTED).first()
        if part is not None:
            self.assertEqual(payload, part.payload)
            self.assertIsNotNone(part.provider_started_at)
            self.assertTrue(part.claim_token)
        else:
            legacy = HumanReplyCommand.objects.get(state=HumanReplyCommand.State.PROVIDER_STARTED)
            self.assertEqual(payload["message"]["text"], self.expected_legacy_body or legacy.text)
            self.assertIsNotNone(legacy.provider_started_at)
        if self.provider_exception is not None:
            raise self.provider_exception
        return 200, json.dumps({"message_id": self.provider_mid})

    def _legacy_unplanned_command(self, text):
        # Actual durable shape accepted before per-part planning was introduced.
        self.customer.bot_paused = True
        self.customer.manager_takeover = True
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["bot_paused", "manager_takeover", "reply_permission_epoch"])
        deadline = _window_deadline(self.inbound)
        return HumanReplyCommand.objects.create(client=self.customer, actor=self.actor, context_message=self.inbound,
            recipient_igsid=self.customer.igsid, provider_namespace="instagram_login:999999", text=text,
            permission_epoch=self.customer.reply_permission_epoch, context_revision=str(self.inbound.pk),
            window_deadline=deadline, operation_context={"client_id": self.customer.pk,
                "context_message_id": self.inbound.pk, "recipient_igsid": self.customer.igsid,
                "provider_namespace": "instagram_login:999999", "permission_epoch": self.customer.reply_permission_epoch,
                "window_deadline": deadline.isoformat()})

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

    def test_provider_receipt_creates_manager_message_once(self):
        result = create_human_reply_command(
            self.customer.pk, actor=self.actor, text="Готово", operation_id="22222222-2222-2222-2222-222222222222"
        )
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.SENT)
        self.assertEqual(self.http.call_count, 1)
        message = command.reply_message
        message.refresh_from_db()
        self.assertEqual(message.role, InstagramBotMessage.Role.MANAGER)
        self.assertEqual(message.source, "human_reply")
        self.assertEqual(message.provider_message_id, "mid-human-1")
        self.assertEqual(InstagramBotMessage.objects.filter(source="human_reply").count(), 1)

    def test_unknown_is_terminal_and_never_retried(self):
        self.provider_exception = TimeoutError("controlled provider timeout")
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
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_unknown_has_explicit_resolution_without_continuation_or_provider_retry(
        self
    ):
        self.provider_exception = TimeoutError("controlled provider timeout")
        command = dispatch_human_reply_command(
            create_human_reply_command(
                self.customer.pk, actor=self.actor, text="Готово"
            ).command.pk
        )
        task = IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")
        from django.urls import reverse

        self.client.force_login(self.actor)
        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.customer.pk])
        )
        row = next(item for item in detail.json()["followups"] if item["id"] == task.pk)
        self.assertEqual(row["continue_url"], "")
        self.assertEqual(
            row["allowed_outcomes"], ["delivered", "not_delivered", "handled"]
        )
        resolve_url = row["human_reply_resolution_url"]
        first = self.client.post(
            resolve_url,
            {"outcome": "handled", "note": "Звірено оператором"},
        )
        second = self.client.post(
            resolve_url,
            {"outcome": "handled", "note": "Повтор"},
        )
        conflict = self.client.post(resolve_url, {"outcome": "delivered"})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertFalse(first.json()["idempotent"])
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(conflict.status_code, 409)
        self.http.assert_called_once()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.COMPLETED)
        self.assertEqual(task.manager_context["resolution"]["outcome"], "handled")
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action="ig_human_reply_delivery_resolved", entity_id=str(task.pk)
            ).count(),
            1,
        )
        from management.bot_views import _with_latest_interaction

        projected = _with_latest_interaction(IgClient.objects.all()).get(pk=self.customer.pk)
        self.assertFalse(projected.has_manager_action)

    def test_unknown_resolution_requires_operator_capability(self):
        self.provider_exception = TimeoutError("controlled provider timeout")
        command = dispatch_human_reply_command(
            create_human_reply_command(
                self.customer.pk, actor=self.actor, text="Готово"
            ).command.pk
        )
        task = IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")
        from django.urls import reverse

        ordinary = get_user_model().objects.create_user(
            username="human-reply-ordinary", password="x"
        )
        self.client.force_login(ordinary)
        response = self.client.post(
            reverse(
                "management_bot_client_human_reply_delivery_resolve_api",
                args=[self.customer.pk, task.pk],
            ),
            {"outcome": "not_delivered"},
        )
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.http.assert_called_once()

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

    def test_provider_started_reentry_is_read_only_and_recursive_dispatch_is_safe(
        self
    ):
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")
        command = result.command

        self.provider_mid = "mid-reentry"

        def recursive_http(*args, **kwargs):
            nested = dispatch_human_reply_command(command.pk)
            self.assertEqual(nested.state, HumanReplyCommand.State.PROVIDER_STARTED)
            return self._physical_http(*args, **kwargs)

        self.http.side_effect = recursive_http
        returned = dispatch_human_reply_command(command.pk)
        self.assertEqual(returned.state, HumanReplyCommand.State.SENT)
        self.assertEqual(self.http.call_count, 1)

    def test_boundary_rechecks_late_inbound_before_provider_io(self):
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")

        def late_token(settings):
            InstagramBotMessage.objects.create(sender_id=self.customer.igsid, client=self.customer,
                role=InstagramBotMessage.Role.USER, text="Пізніше питання", mid="human-inbound-late",
                status=InstagramBotMessage.Status.DONE, provider_created_at=timezone.now(),
                provider_namespace="instagram_login:999999")
            return "local-test-token"

        self.token.side_effect = late_token
        command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.CANCELLED)
        self.assertEqual(command.failure_code, "newer_inbound")
        self.http.assert_not_called()
        self.assertIsNone(HumanReplyPart.objects.get(command=command).provider_started_at)

    def test_late_inbound_at_start_callback_retains_exact_cancellation_reason(self):
        from management.services.ig_human_reply_delivery import record_human_part_started
        result = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово")

        def late_start(part_id, token, **kwargs):
            InstagramBotMessage.objects.create(sender_id=self.customer.igsid, client=self.customer,
                role=InstagramBotMessage.Role.USER, text="Між permission check і start marker",
                mid="human-inbound-at-start", status=InstagramBotMessage.Status.DONE,
                provider_created_at=timezone.now(), provider_namespace="instagram_login:999999")
            return record_human_part_started(part_id, token, **kwargs)

        with patch("management.services.ig_human_reply_transport.record_human_part_started", side_effect=late_start):
            command = dispatch_human_reply_command(result.command.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.CANCELLED)
        self.assertEqual(command.failure_code, "newer_inbound")
        self.http.assert_not_called()
        self.assertIsNone(HumanReplyPart.objects.get(command=command).provider_started_at)

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

    def test_provider_receipt_rejects_degraded_delivery_plan(self):
        # Retain the old opaque-result contract for genuine unplanned commands.
        # New prepared parts cannot legally remove URLs: fallback is disabled.
        from management.services.instagram_bot import send_text as real_send_text
        self.provider_mid = "mid-degraded"
        self.expected_legacy_body = "Готово"
        legacy = self._legacy_unplanned_command("Готово https://example.test/catalog")

        def degraded_legacy_send(settings, recipient, text, **kwargs):
            receipt = real_send_text(settings, recipient, self.expected_legacy_body, **kwargs)
            return replace(receipt, kind="degraded_link_restriction", hint="url removed")

        with patch("management.services.instagram_bot.send_text", side_effect=degraded_legacy_send):
            command = dispatch_human_reply_command(legacy.pk)
        self.assertEqual(command.state, HumanReplyCommand.State.DEFINITE_FAILED)
        self.assertEqual(command.failure_code, "delivery_plan_incomplete")
        self.assertFalse(HumanReplyPart.objects.filter(command=legacy).exists())
        self.http.assert_called_once()
        self.assertEqual(command.provider_message_ids, ["mid-degraded"])
