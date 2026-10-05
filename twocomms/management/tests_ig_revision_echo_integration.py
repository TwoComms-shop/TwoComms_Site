import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_echo as fixtures
from management.models import IgDeferredEcho, IgPermissionTransitionJob, IgWebhookInboxEvent, InstagramBotMessage
from management.services import instagram_bot as bot
from management.services.ig_revision_echo_integration import reconcile_pending_revision_echoes
from management.services.ig_revision_outbox import finish_effect
from management.services.ig_webhook_inbox import accept_webhook, drain_webhook_inbox, has_pending_ingress


@override_settings(
    IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    SITE_BASE_URL="https://twocomms.test",
)
class RevisionEchoIntegrationTests(TransactionTestCase):
    _payload = fixtures.RevisionEchoTests._payload
    _plan = fixtures.RevisionEchoTests._plan
    _start = fixtures.RevisionEchoTests._start
    setUp = fixtures.RevisionEchoTests.setUp

    def _poll(self, mid, *, text="", image=False):
        result = {
            "id": mid, "from": {"id": "owner-1"},
            "to": {"data": [{"id": self.client_row.igsid}]},
            "message": text, "created_time": timezone.now().isoformat(),
        }
        if image:
            result["attachments"] = {"data": [{"type": "image", "image_data": {"url": "https://example.test/manager-photo.jpg"}}]}
        return result

    def _accept_echo(self, mid, attachments):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [{
            "sender": {"id": "owner-1"}, "recipient": {"id": self.client_row.igsid},
            "timestamp": int(timezone.now().timestamp() * 1000),
            "message": {"mid": mid, "is_echo": True, "attachments": attachments},
        }]}]}
        accept_webhook(json.dumps(payload).encode(), self.settings)
        return payload

    def test_native_ig_story_echo_materializes_once_and_retains_story_identity(self):
        attachment = {"type": "ig_story", "payload": {
            "story_media_id": "native-story-42", "story_media_url": "https://example.test/story.jpg",
        }}
        payload = self._accept_echo("native-story-echo", [attachment])
        with patch.object(bot, "_provider_http") as network, patch(
            "management.services.ig_permission_transitions.attempt_permission_transition"
        ) as immediate:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 1)
            self.assertEqual(accept_webhook(json.dumps(payload).encode(), self.settings).duplicates, 1)
            self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 0)
        network.assert_not_called()
        immediate.assert_not_called()
        inbox = IgWebhookInboxEvent.objects.get()
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(inbox.attempts, 1)
        message = InstagramBotMessage.objects.get(mid="native-story-echo")
        self.assertEqual(message.text, "(сторіс менеджера)")
        self.assertFalse(message.reply_to_provider_message_id)
        self.assertEqual(json.loads(message.attachments), ["https://example.test/story.jpg"])
        self.assertEqual(message.attachment_media[0]["provider_object_key"], "story:native-story-42")
        self.assertEqual(message.attachment_media[0]["provider_media_id"], "native-story-42")
        self.assertFalse(message.attachment_media[0].get("provider_native_mention"))
        self.assertEqual(IgDeferredEcho.objects.get().state, "manager_applied")
        self.assertEqual(IgPermissionTransitionJob.objects.count(), 1)
        self.assertFalse(has_pending_ingress(self.settings, self.client_row.igsid))

    def test_native_story_without_url_keeps_reference_without_capture(self):
        self._accept_echo("story-without-url", [{"type": "ig_story", "payload": {"story_media_id": "expired-story"}}])
        with patch.object(bot, "_provider_http") as network:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        network.assert_not_called()
        message = InstagramBotMessage.objects.get(mid="story-without-url")
        self.assertFalse(message.attachments)
        media = message.attachment_media[0]
        self.assertEqual(media["provider_media_id"], "expired-story")
        self.assertFalse(media["capture_eligible"])
        self.assertEqual(media["status"], bot.MEDIA_STATUS_METADATA_ONLY)
        self.assertEqual(IgDeferredEcho.objects.get().state, "manager_applied")

    def test_historical_story_reference_never_starts_capture_or_current_takeover(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo

        self.assertTrue(observe_and_project_echo(
            self.settings, namespace=self.namespace, recipient=self.client_row.igsid,
            mid="historical-story-reference", historical=True,
            attachments=[{"url": "", "type": "story", "provider_id": "past-story", "context_only": True}],
        ))
        message = InstagramBotMessage.objects.get(mid="historical-story-reference")
        self.assertEqual(message.source, "poll_history")
        self.assertFalse(message.media_capture_eligible)
        self.assertFalse(message.attachment_media[0]["capture_eligible"])
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_story_reference_with_early_own_receipt_never_invents_manager(self):
        claim = self._start()
        self._accept_echo("own-story-mid", [{"type": "ig_story", "payload": {"story_media_id": "own-story"}}])
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="own-story-mid")
        reconcile_pending_revision_echoes(self.settings)
        self.assertEqual(IgDeferredEcho.objects.get().state, "own")
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_deterministic_echo_rejection_stops_retry_preserves_fence_and_remains_visible(self):
        from management.services.ig_technical_debt import technical_debt_snapshot

        payload = self._accept_echo("unsupported-echo", [{"type": "future_attachment", "payload": {"new_field": "private-value"}}])
        self._accept_echo("next-good-echo", [{"type": "image", "payload": {"url": "https://example.test/photo.jpg"}}])
        with patch.object(bot, "_provider_http") as network:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 1)
            self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 0)
        network.assert_not_called()
        blocked = IgWebhookInboxEvent.objects.get(decision="blocked")
        self.assertEqual((blocked.reason, blocked.last_error, blocked.attempts), ("echo_empty", "echo_empty", 1))
        self.assertIsNone(blocked.next_attempt_at)
        self.assertIsNone(blocked.processed_at)
        self.assertEqual(blocked.payload, payload)
        self.assertTrue(has_pending_ingress(self.settings, self.client_row.igsid))
        self.assertFalse(InstagramBotMessage.objects.filter(mid="unsupported-echo").exists())
        self.assertIsNotNone(IgWebhookInboxEvent.objects.get(decision="accepted").processed_at)
        cases = technical_debt_snapshot()["cases"]
        case = next(item for item in cases if item["reason"] == "webhook_ingress_blocked")
        self.assertEqual(case["sample_ids"], [blocked.pk])

    def test_retryable_echo_capacity_failure_keeps_backoff_and_safe_reason(self):
        from management.services.ig_revision_echo import EchoAttribution

        payload = self._accept_echo("capacity-echo", [{"type": "image", "payload": {"url": "https://example.test/photo.jpg"}}])
        with patch("management.services.ig_revision_echo.observe_revision_echo", return_value=EchoAttribution(reason="echo_queue_limit", retryable=True)):
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual((row.decision, row.last_error, row.attempts), ("accepted", "echo_queue_limit", 1))
        self.assertIsNotNone(row.next_attempt_at)
        self.assertIsNone(row.processed_at)
        self.assertEqual(row.payload, payload)
        self.assertFalse(IgDeferredEcho.objects.exists())

    @override_settings(IG_WEBHOOK_MAX_MATERIALIZATION_ATTEMPTS=3)
    def test_repeated_unclassified_failure_has_bounded_budget_without_fake_success(self):
        payload = self._accept_echo("repeated-failure", [{"type": "image", "payload": {"url": "https://example.test/photo.jpg"}}])
        IgWebhookInboxEvent.objects.update(attempts=2)
        with patch.object(bot, "handle_webhook_payload", side_effect=RuntimeError("private details")):
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual((row.decision, row.reason, row.last_error, row.attempts), ("blocked", "materialization_retry_exhausted", "RuntimeError", 3))
        self.assertIsNone(row.processed_at)
        self.assertIsNone(row.next_attempt_at)
        self.assertEqual(row.payload, payload)
        self.assertTrue(has_pending_ingress(self.settings, self.client_row.igsid))

    def test_valid_story_can_recover_historic_high_attempt_count(self):
        self._accept_echo("historic-story", [{"type": "ig_story", "payload": {"story_media_id": "historic-story"}}])
        IgWebhookInboxEvent.objects.update(attempts=278)
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual(row.decision, "accepted")
        self.assertEqual(row.attempts, 279)
        self.assertIsNotNone(row.processed_at)
        self.assertFalse(row.last_error)

    def test_inbound_native_story_uses_same_normalization_without_ugc_proof(self):
        message = {"mid": "inbound-story", "attachments": [{"type": "ig_story", "payload": {
            "story_media_id": "story-42", "story_media_url": "https://example.test/story.jpg",
            "target": {"username": "twocomms"},
        }}]}
        self.assertEqual(bot._extract_media_urls(message), ["https://example.test/story.jpg"])
        media = bot._provider_attachment_metadata(message)[0]
        self.assertEqual(media["media_type"], "story")
        self.assertEqual(media["provider_attachment_type"], "ig_story")
        self.assertEqual(media["provider_object_key"], "story:story-42")
        self.assertFalse(media["provider_native_mention"])
        self.assertFalse(media["target_username"])

    def test_early_echo_is_durable_inbox_materialization_without_manager(self):
        claim = self._start()
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [{
            "sender": {"id": "owner-1"}, "recipient": {"id": self.client_row.igsid},
            "message": {"mid": "early-inbox", "is_echo": True, "text": "Canonical bot reply"},
        }]}]}
        accepted = accept_webhook(json.dumps(payload).encode(), self.settings)
        self.assertEqual(accepted.accepted, 1)
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        inbox = IgWebhookInboxEvent.objects.get()
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(inbox.decision, "accepted")
        self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="early-inbox")
        reconcile_pending_revision_echoes(self.settings)
        self.assertEqual(IgDeferredEcho.objects.get().state, "own")
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", provider_message_id="early-inbox").count(), 1)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_real_manager_photo_is_staged_even_during_automation(self):
        with patch.object(bot, "client_automation_busy", return_value=True), patch(
            "management.services.ig_permission_transitions.attempt_permission_transition"
        ) as immediate, patch.object(bot, "_provider_http") as network:
            bot._handle_echo(self.client_row.igsid, "", mid="real-manager-photo", provider_namespace=self.namespace,
                attachments=[{"url": "https://example.test/manager-photo.jpg", "type": "image"}], persistence_only=True)
        immediate.assert_not_called()
        network.assert_not_called()
        self.assertEqual(InstagramBotMessage.objects.get(mid="real-manager-photo").role, "manager")
        self.assertEqual(IgPermissionTransitionJob.objects.filter(kind="manager_takeover").count(), 1)
        self.assertEqual(IgDeferredEcho.objects.get().state, "manager_applied")

    def test_story_only_manager_echo_keeps_context_in_revision_projection(self):
        with patch.object(bot, "client_automation_busy", return_value=True):
            bot._handle_echo(
                self.client_row.igsid, "", mid="real-manager-story",
                provider_namespace=self.namespace, persistence_only=True,
                attachments=[{
                    "url": "https://example.test/story.jpg", "type": "story",
                    "title": "Відповідь на сторіс", "provider_id": "story-id",
                }],
                reply_to_provider_message_id="story-id",
            )
        message = InstagramBotMessage.objects.get(mid="real-manager-story")
        self.assertEqual(message.text, "(відповідь менеджера на сторіс)")
        self.assertEqual(message.reply_to_provider_message_id, "story-id")

    def test_historical_manager_photo_never_creates_current_takeover(self):
        row = self._poll("historical-manager", image=True)
        self.assertTrue(bot._handle_polled_page_side(self.settings, row, historical=True))
        history = InstagramBotMessage.objects.get(mid="historical-manager")
        self.assertEqual((history.role, history.source), ("manager", "poll_history"))
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.manager_takeover)

    def test_unknown_historical_echo_is_not_manager_context(self):
        claim = self._start()
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, transport_outcome="timeout")
        row = self._poll("historical-unknown", text="Unattributed provider message")
        self.assertTrue(bot._handle_polled_page_side(self.settings, row, historical=True))
        self.assertEqual(IgDeferredEcho.objects.get().state, "ambiguous")
        self.assertFalse(InstagramBotMessage.objects.filter(mid="historical-unknown").exists())
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        from management.services.ig_reply_boundary import capture_reply_permission

        permission = capture_reply_permission(self.settings.pk, self.client_row.pk)
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "echo_attribution_pending")

    def test_early_own_echo_does_not_cancel_second_physical_part(self):
        from management.services.ig_revision_delivery import drain_group
        from management.services.ig_revision_transport import build_provider_part_callback

        self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("First part")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("Second part")},
        ])
        callback = build_provider_part_callback(
            self.settings, expected_namespace=self.namespace,
            expected_recipient=self.client_row.igsid, access_token="local-fixture-token",
        )
        calls = []

        def provider(*args, **kwargs):
            calls.append(kwargs["data"])
            mid = f"physical-{len(calls)}"
            if len(calls) == 1:
                bot._handle_echo(self.client_row.igsid, "First part", mid=mid,
                    provider_namespace=self.namespace, persistence_only=True)
                self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
            return 200, {"message_id": mid}

        with patch.object(bot, "_provider_http", side_effect=provider):
            result = drain_group(self.revision.pk, self.revision_token, "substantive_text", callback)
        self.assertEqual(len(calls), 2, result.reason)
        self.assertEqual(len(result.sent_parts), 2)
        self.assertEqual(IgDeferredEcho.objects.get().state, "own")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def _drain_catalog_with_pending_echo(self, *, extra_inbound=False, echo_mid="photo-1"):
        from management.services.ig_revision_delivery import drain_group
        from management.services.ig_revision_transport import build_provider_part_callback

        images = [{"group": "catalog_media", "kind": "image", "payload": {
            "recipient": {"id": self.client_row.igsid},
            "message": {"attachment": {"type": "image", "payload": {"url": f"https://example.test/photo-{index}.jpg"}}},
        }} for index in (1, 2)]
        self._plan(images + [{"group": "substantive_text", "kind": "text", "payload": self._payload("Complete answer")}])
        callback = build_provider_part_callback(
            self.settings, expected_namespace=self.namespace,
            expected_recipient=self.client_row.igsid, access_token="local-fixture-token",
        )
        calls = []

        def provider(*args, **kwargs):
            calls.append(kwargs["data"])
            if len(calls) == 1:
                self._accept_echo(echo_mid, [{"type": "image", "payload": {"url": "https://example.test/photo-1.jpg"}}])
                # The early echo is not evidence until this request commits its
                # exact provider MID. No attribution is guessed while in flight.
                self.assertTrue(has_pending_ingress(self.settings, self.client_row.igsid))
                if extra_inbound:
                    event = {"sender": {"id": self.client_row.igsid}, "recipient": {"id": "owner-1"},
                        "message": {"mid": "new-user-mid", "text": "Changed request"}}
                    accept_webhook(json.dumps({"object": "instagram", "entry": [{"id": "owner-1", "messaging": [event]}]}).encode(), self.settings)
            return 200, {"message_id": f"photo-{len(calls)}"}

        with patch.object(bot, "_provider_http", side_effect=provider):
            images_result = drain_group(self.revision.pk, self.revision_token, "catalog_media", callback)
            text_result = drain_group(self.revision.pk, self.revision_token, "substantive_text", callback)
        return calls, images_result, text_result

    def test_queued_own_photo_echo_allows_remaining_photos_and_substantive_answer(self):
        calls, images, text = self._drain_catalog_with_pending_echo()
        self.assertEqual(len(calls), 3, (images.reason, text.reason))
        self.assertEqual(len(images.sent_parts), 2)
        self.assertEqual(len(text.sent_parts), 1)
        self.assertFalse(has_pending_ingress(self.settings, self.client_row.igsid))
        # Exemption is read-only; ingress is still normally materialized later.
        self.assertIsNone(IgWebhookInboxEvent.objects.get().processed_at)
        self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 1)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_queued_own_echo_cannot_hide_a_new_user_request(self):
        calls, images, text = self._drain_catalog_with_pending_echo(extra_inbound=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(images.reason, "pending_inbound")
        self.assertFalse(text.sent_parts)
        self.assertTrue(has_pending_ingress(self.settings, self.client_row.igsid))

    def test_unmatched_owner_echo_keeps_manager_fence_despite_a_sent_photo(self):
        calls, images, text = self._drain_catalog_with_pending_echo(echo_mid="unmatched-manager-photo")
        self.assertEqual(len(calls), 1)
        self.assertEqual(images.reason, "pending_inbound")
        self.assertFalse(text.sent_parts)
        self.assertTrue(has_pending_ingress(self.settings, self.client_row.igsid))

    def test_own_receipt_from_other_namespace_does_not_release_pending_echo(self):
        claim = self._start()
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="scoped-echo")
        self._accept_echo("scoped-echo", [{"type": "image", "payload": {"url": "https://example.test/photo.jpg"}}])
        row = IgWebhookInboxEvent.objects.get()
        payload = row.payload
        payload["entry"][0]["id"] = "other-owner"
        payload["entry"][0]["messaging"][0]["sender"]["id"] = "other-owner"
        IgWebhookInboxEvent.objects.update(namespace="instagram_login:other-owner", owner_id="other-owner", payload=payload)
        from management.services.ig_webhook_inbox import pending_ingress_blocks

        self.assertTrue(pending_ingress_blocks("instagram_login:other-owner", self.client_row.igsid))

    def test_own_receipt_for_other_recipient_does_not_release_pending_echo(self):
        claim = self._start()
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="recipient-scoped-echo")
        self._accept_echo("recipient-scoped-echo", [{"type": "image", "payload": {"url": "https://example.test/photo.jpg"}}])
        row = IgWebhookInboxEvent.objects.get()
        payload = row.payload
        payload["entry"][0]["messaging"][0]["recipient"]["id"] = "other-recipient"
        IgWebhookInboxEvent.objects.update(customer_igsid="other-recipient", payload=payload)
        self.assertTrue(has_pending_ingress(self.settings, "other-recipient"))
