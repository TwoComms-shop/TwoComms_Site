"""Live identity latency and native manager-media projection regressions."""
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgDeferredEcho, IgPermissionTransitionJob, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot
from management.services.ig_revision_echo import _projection_matches
from management.services.ig_webhook_inbox import accept_webhook, drain_webhook_inbox, has_pending_ingress
from management import tests_ig_revision_echo as echo_fixtures
from management.management.commands import run_instagram_bot as runner


@override_settings(IG_REVISION_EXECUTION_ENABLED=True,
                   IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
                   SITE_BASE_URL="https://twocomms.test")
class NativeManagerMediaTests(TransactionTestCase):
    setUp = echo_fixtures.RevisionEchoTests.setUp

    def test_native_audio_and_video_materialize_once_without_provider_io(self):
        for kind, label in (("audio", "(голосове повідомлення)"), ("video", "(відео)")):
            with self.subTest(kind=kind):
                mid = f"native-manager-{kind}"
                payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [{
                    "sender": {"id": "owner-1"}, "recipient": {"id": self.client_row.igsid},
                    "timestamp": int(timezone.now().timestamp() * 1000),
                    "message": {"mid": mid, "is_echo": True, "attachments": [
                        {"type": kind, "payload": {"url": f"https://example.test/{kind}"}},
                    ]},
                }]}]}
                raw = json.dumps(payload).encode()
                with patch.object(bot, "_provider_http") as http, patch.object(bot, "download_image") as download:
                    self.assertEqual(accept_webhook(raw, self.settings).accepted, 1)
                    self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 1)
                    self.assertEqual(accept_webhook(raw, self.settings).duplicates, 1)
                    self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 0)
                http.assert_not_called()
                download.assert_not_called()
                message = InstagramBotMessage.objects.get(mid=mid)
                self.assertEqual(message.text, label)
                self.assertEqual(message.attachment_media[0]["media_type"], kind)
                self.assertEqual(IgDeferredEcho.objects.get(provider_message_id=mid).state, "manager_applied")
                self.assertEqual(IgPermissionTransitionJob.objects.filter(source_message=message).count(), 1)
                self.assertFalse(has_pending_ingress(self.settings, self.client_row.igsid))

    def test_historical_audio_keeps_original_empty_text_and_no_takeover(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo

        self.assertTrue(observe_and_project_echo(
            self.settings, namespace=self.namespace, recipient=self.client_row.igsid,
            mid="historical-audio", historical=True,
            attachments=[{"url": "https://example.test/audio", "type": "audio"}],
        ))
        message = InstagramBotMessage.objects.get(mid="historical-audio")
        self.assertEqual(message.text, "")
        self.assertEqual(message.source, "poll_history")
        self.assertFalse(message.media_capture_eligible)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_projection_matching_requires_event_specific_typed_label(self):
        event = SimpleNamespace(payload={"text": "", "attachments": [
            {"type": "audio", "url": "https://example.test/audio"},
        ]})
        message = SimpleNamespace(text="(голосове повідомлення)", attachments='["https://example.test/audio"]')
        self.assertTrue(_projection_matches(event, message))
        message.text = "(відео)"
        self.assertFalse(_projection_matches(event, message))
        message.text = "(голосове повідомлення)"
        self.assertFalse(_projection_matches(event, message, historical=True))


class PromptProfileFactsTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.settings_row = InstagramBotSettings.load()
        self.settings_row.page_id = "synthetic-owner"
        self.settings_row.ig_user_id = "synthetic-owner"
        self.settings_row.save(update_fields=["page_id", "ig_user_id"])

    def test_provider_identity_is_saved_before_avatar_download_even_if_it_fails(self):
        client = IgClient.get_or_create_for_sender("fresh-identity")

        def slow_avatar(*args, **kwargs):
            self.assertTrue(transaction.get_autocommit())
            saved = IgClient.objects.get(pk=client.pk)
            self.assertEqual(saved.display_name, "Provider Name")
            self.assertEqual(saved.username, "provider_name")
            self.assertEqual(saved.profile_pic_url, "https://example.test/avatar")
            raise RuntimeError("cdn unavailable")

        body = json.dumps({"name": "Provider Name", "username": "provider_name",
                           "profile_pic": "https://example.test/avatar"})
        with patch.object(bot, "get_page_token", return_value="synthetic-token"), patch.object(bot, "_provider_http", return_value=(200, body)) as http, patch.object(bot, "_localize_avatar", side_effect=slow_avatar):
            self.assertTrue(bot.ensure_profile(self.settings_row, client))
        http.assert_called_once()
        client.refresh_from_db()
        self.assertEqual(client.username, "provider_name")
        self.assertFalse(client.avatar_local)

    def test_urgent_batch_prioritizes_new_active_identity_and_respects_backoff(self):
        old = IgClient.get_or_create_for_sender("old-unresolved")
        past = timezone.now() - timedelta(days=10)
        IgClient.objects.filter(pk=old.pk).update(created_at=past, last_message_at=past)
        fresh = IgClient.get_or_create_for_sender("fresh-active")
        IgClient.objects.filter(pk=fresh.pk).update(last_message_at=timezone.now())
        hidden = IgClient.get_or_create_for_sender("hidden-active")
        IgClient.objects.filter(pk=hidden.pk).update(hidden_at=timezone.now())
        deferred = IgClient.get_or_create_for_sender("deferred-active")
        IgClient.objects.filter(pk=deferred.pk).update(profile_sync_next_at=timezone.now() + timedelta(hours=1))
        with patch.object(bot, "get_page_token", return_value="fake"), patch.object(bot, "ensure_profile", return_value=True) as ensure:
            result = bot.refresh_profiles_batch(self.settings_row, limit=1, urgent_only=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(ensure.call_args.args[1].pk, fresh.pk)


class BackgroundProfileCadenceTests(SimpleTestCase):
    def test_urgent_profile_tick_ignores_active_reply_gate_and_shares_owner_rate_cap(self):
        row = SimpleNamespace()
        with patch.object(bot, "_provider_owner_id", return_value="owner"), patch.object(runner.cache, "add", side_effect=[True, False]), patch.object(bot, "refresh_profiles_batch", return_value={"checked": 1, "state": "ok"}) as refresh, patch.object(runner, "_active_customer_input", side_effect=AssertionError("must not gate profiles")):
            runner._profile_refresh_tick(row)
            runner._profile_refresh_tick(row)
        refresh.assert_called_once_with(row, limit=1, urgent_only=True)

    def test_maintenance_profile_runs_only_when_urgent_queue_is_empty(self):
        row = SimpleNamespace()
        with patch.object(bot, "_provider_owner_id", return_value="owner"), patch.object(runner.cache, "add", return_value=True), patch.object(bot, "refresh_profiles_batch", side_effect=[{"checked": 0, "state": "ok"}, {"checked": 1, "state": "ok"}]) as refresh:
            runner._profile_refresh_tick(row)
        self.assertEqual(refresh.call_args_list[1].kwargs, {"limit": 1})

    def test_existing_worker_wakes_for_profiles_without_accelerating_discovery(self):
        self._check_worker_discovery_cadence(failing=False)

    def test_discovery_failure_keeps_backoff_and_does_not_starve_profiles(self):
        self._check_worker_discovery_cadence(failing=True)

    def _check_worker_discovery_cadence(self, *, failing):
        clock = {"now": 0.0, "waits": []}
        stop = Mock()
        stop.is_set.side_effect = lambda: len(clock["waits"]) >= 10
        def wait(seconds):
            clock["waits"].append(seconds)
            clock["now"] += seconds
        stop.wait.side_effect = wait
        row = SimpleNamespace(receive_via_poll=True, conversation_discovery_cursor="", refresh_from_db=Mock())
        discoveries = []
        def discover(*args):
            discoveries.append(clock["now"])
            if failing:
                raise RuntimeError("synthetic discovery failure")
        with patch.object(runner, "worker_iteration", return_value=nullcontext()), patch.object(runner, "close_old_connections"), patch.object(runner, "require_database_ready"), patch.object(runner.InstagramBotSettings, "load", return_value=row), patch.object(runner, "maintenance_status", return_value={"active": False}), patch.object(runner, "_profile_refresh_tick") as profile, patch.object(bot, "_provider_account_id", return_value="owner"), patch.object(bot, "get_page_token", return_value="fake"), patch.object(bot, "refresh_conv_ids", side_effect=discover), patch.object(runner.time, "monotonic", side_effect=lambda: clock["now"]), patch.object(runner, "_database_disconnected", return_value=False), patch.object(bot, "log"):
            runner._conv_refresher(stop)
        self.assertEqual(discoveries, [0.0, 120.0])
        self.assertEqual(profile.call_count, 10)
        self.assertLessEqual(max(clock["waits"]), bot.PROFILE_URGENT_REFRESH_INTERVAL)
