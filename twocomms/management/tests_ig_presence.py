"""Deterministic advisory interleavings; no live provider/tester traffic."""
from datetime import timedelta
from dataclasses import replace
from django.db import DatabaseError
from django.contrib.auth import get_user_model
import importlib
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
import requests

from management.services import ig_presence as presence


def accepted(action):
    return presence.SenderActionResult(True, 200, "accepted", action)


class PresenceLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.directory_patch = patch.object(presence, "PRESENCE_DIRECTORY", Path(self.directory.name))
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.interval_patch = patch.object(presence, "MIN_ACTION_INTERVAL", 0.001)
        self.interval_patch.start()
        self.addCleanup(self.interval_patch.stop)
        self.handles = []
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for handle in self.handles:
            handle.stop()
            self.assertTrue(handle.finished.wait(2), "test worker did not terminate")

    def start(self, transport, *, controller=None, guard=lambda cleanup: True, **kwargs):
        handle = (controller or presence.PresenceController()).start(
            key="instagram:account:client", transport=transport,
            guard=guard, source_watermark=10, **kwargs,
        )
        if handle:
            self.handles.append(handle)
        return handle

    def test_stop_and_substantive_send_do_not_wait_for_late_typing_on(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(action):
            calls.append(action)
            if action == "typing_on":
                entered.set()
                release.wait(2)
            return accepted(action)

        handle = self.start(transport)
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            handle.stop()
            substantive_send = Mock(return_value="sent")
            self.assertEqual(substantive_send(), "sent")
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertFalse(handle.finished.is_set())
        finally:
            release.set()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen", "typing_on", "typing_off"])

    def test_stop_during_seen_never_starts_typing(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(action):
            calls.append(action)
            entered.set()
            release.wait(2)
            return accepted(action)

        handle = self.start(transport)
        self.assertTrue(entered.wait(1))
        handle.stop()
        release.set()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen"])

    def manual(self, *, controller=None, guard=lambda cleanup: True, transport=accepted, **kwargs):
        controller = controller or presence.PresenceController()
        handle = presence.PresenceSession(
            controller, key="instagram:account:client", transport=transport,
            guard=guard, source_watermark=1, **kwargs,
        )
        controller._sessions[handle.key] = handle
        return handle

    def test_old_cleanup_cannot_disable_new_owner_even_after_lease_release(self):
        calls = []
        # Separate controllers simulate separate processes sharing the flock.
        old = self.manual(transport=lambda action: calls.append(("old", action)) or accepted(action))
        self.assertTrue(old._action("typing_on"))
        old.stop()
        newer = self.manual(transport=lambda action: calls.append(("new", action)) or accepted(action))
        self.assertTrue(newer._action("typing_on"))
        self.assertTrue(newer._action("typing_off"))
        # cleanup guard returns true, including the empty-lease case. Retained
        # generation still prevents the older worker from doing any I/O.
        self.assertFalse(old._action("typing_off"))
        self.assertEqual(calls, [("old", "typing_on"), ("new", "typing_on"), ("new", "typing_off")])

    def test_late_cleanup_still_runs_if_successor_exited_without_presence(self):
        calls = []
        handle = self.manual(transport=lambda action: calls.append(action) or accepted(action))
        self.assertTrue(handle._action("typing_on"))
        handle.stop()
        handle.controller._sessions.pop(handle.key)
        self.assertTrue(handle._action("typing_off"))
        self.assertEqual(calls, ["typing_on", "typing_off"])

    def test_pause_epoch_or_owner_loss_prevents_refresh_but_allows_own_cleanup(self):
        allowed = [True]
        calls = []
        handle = self.manual(
            guard=lambda cleanup: cleanup or allowed[0],
            transport=lambda action: calls.append(action) or accepted(action),
        )
        self.assertTrue(handle._action("typing_on"))
        allowed[0] = False
        self.assertFalse(handle._action("typing_on"))
        self.assertTrue(handle._action("typing_off"))
        self.assertEqual(calls, ["typing_on", "typing_off"])

    def test_repeated_words_update_same_generation_and_watermark(self):
        calls = []
        handle = self.manual(transport=lambda action: calls.append(action) or accepted(action))
        generation = handle.generation
        for value in (10, 12, 11, 50):
            handle.update(source_watermark=value)
        self.assertEqual(handle.source_watermark, 50)
        self.assertEqual(handle.generation, generation)
        self.assertEqual(calls, [])

    def test_success_resets_consecutive_failure_count(self):
        outcomes = iter([False, False, True, False, False, False])
        handle = self.manual(transport=lambda action: presence.SenderActionResult(next(outcomes), 503, "provider", action))
        self.assertEqual([handle._action("typing_on") for _ in range(6)], [True, True, True, True, True, False])
        self.assertEqual(handle.consecutive_failures, 3)

    def test_unsupported_missing_token_and_429_cooldown_survive_new_controller(self):
        for index, kind in enumerate(("unsupported_or_denied", "missing_token", "rate_limited")):
            with self.subTest(kind=kind):
                old = self.manual(transport=lambda action: presence.SenderActionResult(False, 400, kind, action))
                old.key += str(index)
                old.controller._sessions[old.key] = old
                self.assertFalse(old._action("mark_seen"))
                transport = Mock(side_effect=accepted)
                newer = self.manual(transport=transport)
                newer.key = old.key
                newer.controller._sessions[newer.key] = newer
                self.assertFalse(newer._action("mark_seen"))
                transport.assert_not_called()

    def test_capacity_is_bounded_without_a_pending_queue(self):
        entered, release = threading.Event(), threading.Event()

        def transport(action):
            entered.set()
            release.wait(2)
            return accepted(action)

        controller = presence.PresenceController(max_workers=1)
        handle = self.start(transport, controller=controller)
        self.assertTrue(entered.wait(1))
        try:
            self.assertIsNone(self.start(transport, controller=controller))
        finally:
            handle.stop()
            release.set()

    def test_default_does_not_guess_instagram_refresh_ttl(self):
        calls, started = [], threading.Event()

        def transport(action):
            calls.append(action)
            if action == "typing_on":
                started.set()
            return accepted(action)

        handle = self.start(transport)
        self.assertTrue(started.wait(1))
        self.assertEqual(handle.refresh_seconds, 0)
        handle.stop()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen", "typing_on", "typing_off"])

    def test_nominal_lifetime_expiry_stops_further_actions(self):
        transport = Mock(side_effect=accepted)
        handle = self.manual(transport=transport)
        handle.started -= presence.MAX_SESSION_SECONDS + 1
        self.assertFalse(handle._action("typing_on"))
        transport.assert_not_called()

    def test_file_lock_is_bounded_and_never_blocks_a_reply_thread(self):
        entered, release = threading.Event(), threading.Event()

        def occupied_lane():
            with presence._channel_state("instagram:account:client"):
                entered.set()
                release.wait(2)

        holder = threading.Thread(target=occupied_lane)
        holder.start()
        self.assertTrue(entered.wait(1))
        try:
            transport = Mock(side_effect=accepted)
            started = time.monotonic()
            handle = self.start(transport)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(handle.finished.wait(1))
            transport.assert_not_called()
        finally:
            release.set()
            holder.join(1)

    def test_suspended_handoff_retains_generation_without_old_token_requests(self):
        calls, started = [], threading.Event()
        owner_valid = [True]

        def transport(action):
            calls.append(action)
            if action == "typing_on":
                started.set()
            return accepted(action)

        handle = self.start(transport, owner_check=lambda: owner_valid[0])
        self.assertTrue(started.wait(1))
        generation = handle.generation
        handle.suspend()
        owner_valid[0] = False
        self.assertTrue(handle._allowed(check_owner=False))
        handle.update(source_watermark=25, owner_check=lambda: True, resume=True)
        self.assertEqual(handle.generation, generation)
        self.assertTrue(handle._allowed())
        handle.stop()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen", "typing_on", "typing_off"])

    def test_fast_sent_before_thread_start_still_marks_seen_without_typing(self):
        calls = []
        controller = presence.PresenceController()
        with patch.object(presence.threading.Thread, "start"):
            handle = controller.start(
                key="instagram:account:client", source_watermark=25,
                transport=lambda action: calls.append(action) or accepted(action),
                guard=lambda cleanup: False, completion_guard=lambda: True,
            )
        handle.begin_dispatch()
        handle.complete_delivery()
        handle.stop()  # Normal process finally cannot cancel confirmed seen.
        handle._run()
        self.assertEqual(calls, ["mark_seen"])

    def test_fast_cancel_before_thread_start_has_no_sender_actions(self):
        calls = []
        controller = presence.PresenceController()
        with patch.object(presence.threading.Thread, "start"):
            handle = controller.start(
                key="instagram:account:client", source_watermark=25,
                transport=lambda action: calls.append(action) or accepted(action),
                guard=lambda cleanup: True, completion_guard=lambda: True,
            )
        handle.begin_dispatch()
        handle.stop()
        handle._run()
        self.assertEqual(calls, [])

    def test_completed_seen_never_bypasses_changed_permission_or_new_source(self):
        transport = Mock(side_effect=accepted)
        handle = self.manual(transport=transport, completion_guard=lambda: False)
        handle.begin_dispatch()
        handle.complete_delivery()
        self.assertFalse(handle._action("mark_seen", completed_seen=True))
        transport.assert_not_called()

    def test_completed_seen_can_replace_only_an_older_source_generation(self):
        old = self.manual()
        old._action("mark_seen")
        newer = self.manual(completion_guard=lambda: True)
        newer.source_watermark = 10
        newer.begin_dispatch()
        newer.complete_delivery()
        self.assertTrue(newer._action("mark_seen", completed_seen=True))
        old.completion_guard = lambda: True
        old.begin_dispatch()
        old.complete_delivery()
        self.assertFalse(old._action("mark_seen", completed_seen=True))

    def test_unsupported_seen_does_not_prevent_supported_typing(self):
        calls, entered = [], threading.Event()

        def transport(action):
            calls.append(action)
            if action == "mark_seen":
                return presence.SenderActionResult(False, 400, "unsupported_or_denied", action)
            if action == "typing_on":
                entered.set()
            return accepted(action)

        handle = self.start(transport)
        self.assertTrue(entered.wait(1))
        handle.stop()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen", "typing_on", "typing_off"])

    def test_long_generation_refreshes_only_after_accepted_typing(self):
        calls, refreshed = [], threading.Event()
        report = Mock()

        def transport(action):
            calls.append(action)
            if calls.count("typing_on") >= 2:
                refreshed.set()
            return accepted(action)

        handle = self.start(
            transport, refresh_seconds=0.01, report=report,
            refresh_authorized=lambda: True,
        )
        self.assertTrue(refreshed.wait(1))
        handle.begin_dispatch()
        handle.complete_delivery()
        self.assertTrue(handle.finished.wait(1))
        count = calls.count("typing_on")
        self.assertGreaterEqual(count, 2)
        self.assertEqual(calls[-1], "typing_off")
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0]["our_mark_seen"], "accepted")
        self.assertEqual(report.call_args.args[0]["typing_requests"], count)

    def test_initial_typing_has_no_artificial_seen_gap(self):
        handle = self.manual()
        handle._action("mark_seen")
        with patch.object(handle.stopped, "wait", wraps=handle.stopped.wait) as wait:
            self.assertTrue(handle._action("typing_on"))
        wait.assert_not_called()

    def test_definite_typing_rejection_does_not_issue_useless_cleanup(self):
        calls = []

        def rejected(action):
            calls.append(action)
            return presence.SenderActionResult(False, 400, "unsupported_or_denied", action)

        handle = self.start(rejected, refresh_seconds=0.01)
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls, ["mark_seen", "typing_on"])
        self.assertFalse(handle.typing_attempted)

    def test_periodic_refresh_does_not_run_without_durable_authorization(self):
        calls, first_typing = [], threading.Event()

        def transport(action):
            calls.append(action)
            if action == "typing_on":
                first_typing.set()
            return accepted(action)

        refresh_authorized = Mock(return_value=False)
        persistence = Mock()
        handle = self.start(
            transport, refresh_seconds=0.01, refresh_authorized=refresh_authorized,
            typing_result_callback=persistence,
        )
        self.assertTrue(first_typing.wait(1))
        time.sleep(0.35)
        handle.stop()
        self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls[:2], ["mark_seen", "typing_on"])
        self.assertEqual(calls.count("typing_on"), 1)
        refresh_authorized.assert_called()
        persistence.assert_not_called()

    def test_capability_database_failure_does_not_delay_initial_actions_or_reply(self):
        calls, initial_typing = [], threading.Event()
        profile = presence.PresenceCapability(
            "instagram_login", "v25.0", "a" * 64, "b" * 64, 0.01,
        )

        def transport(action):
            calls.append(action)
            if action == "typing_on":
                initial_typing.set()
            return accepted(action)

        with patch("management.models.IgPresenceCapability.objects.filter", side_effect=DatabaseError("unavailable")):
            handle = self.start(
                transport, refresh_seconds=0.01,
                refresh_authorized=lambda: presence.typing_refresh_authorized(profile),
            )
            self.assertTrue(initial_typing.wait(1))
            substantive_send = Mock(return_value="sent")
            started = time.monotonic()
            self.assertEqual(substantive_send(), "sent")
            self.assertLess(time.monotonic() - started, 0.1)
            time.sleep(0.35)
            handle.stop()
            self.assertTrue(handle.finished.wait(1))
        self.assertEqual(calls.count("typing_on"), 1)

    def test_completion_seen_authority_expires_even_after_stuck_http_returns(self):
        transport = Mock(side_effect=accepted)
        handle = self.manual(transport=transport, completion_guard=lambda: True)
        handle.begin_dispatch()
        handle.complete_delivery()
        handle.delivery_confirmed_at -= presence.DELIVERY_SEEN_WAIT_SECONDS + 1
        self.assertFalse(handle._action("mark_seen", completed_seen=True))
        transport.assert_not_called()

    def test_completion_seen_slow_permission_check_cannot_extend_deadline(self):
        for slow_call in (1, 2):
            with self.subTest(slow_call=slow_call):
                clock, checks = [100.0], [0]
                transport = Mock(side_effect=accepted)

                def slow_guard():
                    checks[0] += 1
                    if checks[0] == slow_call:
                        clock[0] += presence.DELIVERY_SEEN_WAIT_SECONDS + 1
                    return True

                with patch.object(presence.time, "monotonic", side_effect=lambda: clock[0]):
                    handle = self.manual(transport=transport, completion_guard=slow_guard)
                    handle.begin_dispatch()
                    handle.complete_delivery()
                    self.assertFalse(handle._action("mark_seen", completed_seen=True))
                self.assertEqual(checks[0], slow_call)
                transport.assert_not_called()

    def test_completion_seen_rechecks_owner_after_last_permission_read(self):
        transport = Mock(side_effect=accepted)
        controller = presence.PresenceController()
        checks = [0]

        def replacing_guard():
            checks[0] += 1
            if checks[0] == 2:
                self.manual(controller=controller)
            return True

        handle = self.manual(controller=controller, transport=transport, completion_guard=replacing_guard)
        handle.begin_dispatch()
        handle.complete_delivery()
        self.assertFalse(handle._action("mark_seen", completed_seen=True))
        transport.assert_not_called()


class PresenceTransportTests(SimpleTestCase):
    def setUp(self):
        self.settings_row = SimpleNamespace(ig_user_id="17841400000000001", page_id="page")
        self.route = patch("management.services.instagram_bot.provider_transport", return_value="instagram_login")
        self.route.start()
        self.addCleanup(self.route.stop)

    @patch.object(presence, "_cached_token", return_value="private-token")
    @patch.object(presence.requests, "Session")
    def test_short_transport_budgets_no_redirects_and_no_body_read(self, session, _token):
        response = session.return_value.__enter__.return_value.post.return_value.__enter__.return_value
        response.status_code = 200
        result = presence.send_sender_action(self.settings_row, "recipient", "typing_on")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "accepted")
        post = session.return_value.__enter__.return_value.post
        self.assertEqual(post.call_args.args[0], "https://graph.instagram.com/v25.0/17841400000000001/messages")
        self.assertEqual(post.call_args.kwargs["timeout"], (0.5, 1.0))
        self.assertEqual(post.call_args.kwargs["json"]["sender_action"], "TYPING_ON")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertTrue(post.call_args.kwargs["stream"])
        response.json.assert_not_called()

    @patch.object(presence, "_cached_token", return_value="private-token")
    @patch.object(presence.requests, "Session")
    def test_timeout_is_advisory_and_diagnostics_do_not_expose_body_or_token(self, session, _token):
        session.return_value.__enter__.return_value.post.side_effect = requests.Timeout("private-token recipient raw-body")
        with self.assertLogs(presence.logger, level="INFO") as logs:
            result = presence.send_sender_action(self.settings_row, "recipient", "typing_on")
        self.assertEqual(result.kind, "timeout")
        self.assertNotIn("private-token", " ".join(logs.output))
        self.assertNotIn("recipient", " ".join(logs.output))

    @patch.object(presence, "_cached_token", return_value="")
    @patch.object(presence.requests, "Session")
    def test_missing_token_does_not_construct_http_session(self, session, _token):
        self.assertEqual(presence.send_sender_action(self.settings_row, "recipient", "mark_seen").kind, "missing_token")
        session.assert_not_called()

    @patch("management.services.instagram_bot.resolve_instagram_login_token", return_value="configured-token")
    @patch.object(presence.cache, "get", return_value=None)
    @patch("management.services.instagram_bot._effective_instagram_login_token")
    def test_instagram_login_works_after_restart_without_token_refresh(self, refresh, _cache, _raw):
        self.assertEqual(presence._cached_token(self.settings_row), "configured-token")
        refresh.assert_not_called()

    @patch("management.services.instagram_bot.resolve_direct_token", return_value="user-token")
    @patch("management.services.instagram_bot._page_token_cache_keys", return_value=("page-cache", "cooldown"))
    @patch.object(presence.cache, "get", return_value=None)
    @patch("management.services.instagram_bot.get_page_token")
    def test_legacy_cold_page_cache_skips_discovery(self, discovery, _cache, _keys, _raw):
        with patch("management.services.instagram_bot.provider_transport", return_value="legacy_page"):
            self.assertEqual(presence._cached_token(self.settings_row), "")
        discovery.assert_not_called()

    @patch.object(presence, "_cached_token", return_value="private-token")
    @patch.object(presence.requests, "Session")
    def test_legacy_messenger_keeps_lowercase_sender_action(self, session, _token):
        response = session.return_value.__enter__.return_value.post.return_value.__enter__.return_value
        response.status_code = 200
        with patch("management.services.instagram_bot.provider_transport", return_value="legacy_page"):
            self.assertTrue(presence.send_sender_action(self.settings_row, "recipient", "mark_seen").ok)
        post = session.return_value.__enter__.return_value.post
        self.assertEqual(post.call_args.kwargs["json"]["sender_action"], "mark_seen")

    def test_refresh_is_disabled_by_default_and_requires_explicit_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            profile = presence.capability_profile(self.settings_row)
            self.assertEqual(profile.refresh_seconds, 0)
            self.assertEqual(profile.visibility, "ui_unverified_refresh_disabled")
        with patch.dict("os.environ", {"IG_PRESENCE_REFRESH_SECONDS": "5"}):
            self.assertEqual(presence.capability_profile(self.settings_row).refresh_seconds, 5)
        with patch.dict("os.environ", {"IG_PRESENCE_REFRESH_SECONDS": "0"}):
            self.assertEqual(presence.capability_profile(self.settings_row).refresh_seconds, 0)

    def test_retired_legacy_pulse_and_visibility_delay_are_removed(self):
        from management.services import instagram_bot as bot

        self.assertIs(bot.send_sender_action, presence.send_sender_action)
        self.assertFalse(hasattr(bot, "_TypingPulse"))
        with patch.object(bot, "_renew_client_automation_lease", return_value=True), patch.object(bot, "_reply_permission_is_current", return_value=True), patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot._wait_for_typing_window(None, None, "lease", None, "reply", typing_started_at=1), "allowed")
        sleep.assert_not_called()


class DurablePresenceGuardTests(TestCase):
    def setUp(self):
        from management.models import IgClient, InstagramBotSettings
        from management.services.ig_reply_boundary import capture_reply_permission

        self.settings_row = InstagramBotSettings.load()
        self.settings_row.is_enabled = True
        self.settings_row.allowed_senders = ""
        self.settings_row.ig_user_id = "17841400000000001"
        self.settings_row.save()
        self.client = IgClient.objects.create(
            igsid="presence-test-recipient", automation_lease_token="owner-one",
            automation_lease_until=timezone.now() + timedelta(seconds=60),
        )
        self.route = patch("management.services.instagram_bot.provider_transport", return_value="instagram_login")
        self.route.start()
        self.addCleanup(self.route.stop)
        self.permission = capture_reply_permission(self.settings_row.pk, self.client.pk)
        self.completion_valid = True
        with patch.object(presence._controller, "start") as start:
            presence.start_presence(
                self.settings_row, client_id=self.client.pk, recipient_id=self.client.igsid,
                owner_token="owner-one", source_watermark=1, permission=self.permission,
                completion_check=lambda: self.completion_valid,
            )
        self.guard = start.call_args.kwargs["guard"]
        self.completed_guard = start.call_args.kwargs["completion_guard"]

    def test_committed_pause_and_resume_do_not_revive_old_epoch(self):
        self.assertTrue(self.guard(False))
        self.assertTrue(self.completed_guard())
        self.client.bot_paused = True
        self.client.reply_permission_epoch += 1
        self.client.save()
        self.assertFalse(self.guard(False))
        self.assertFalse(self.completed_guard())
        self.assertTrue(self.guard(True))
        self.client.bot_paused = False
        self.client.reply_permission_epoch += 1
        self.client.save()
        self.assertFalse(self.guard(False))

    def test_new_durable_owner_fences_refresh_and_cleanup(self):
        self.client.automation_lease_token = "owner-two"
        self.client.save()
        self.assertFalse(self.guard(False))
        self.assertFalse(self.guard(True))
        self.assertFalse(self.completed_guard())

    def test_completed_seen_rechecks_source_completion_and_permission_after_release(self):
        self.client.automation_lease_token = ""
        self.client.save()
        self.assertTrue(self.completed_guard())
        self.completion_valid = False
        self.assertFalse(self.completed_guard())

    def test_lease_expiry_stops_refresh(self):
        self.client.automation_lease_until = timezone.now() - timedelta(seconds=1)
        self.client.save()
        self.assertFalse(self.guard(False))

    def test_erasure_and_opt_out_deny_new_advisory_effects(self):
        self.client.privacy_erasure_started_at = timezone.now()
        self.client.save()
        self.assertFalse(self.guard(False))
        self.client.privacy_erasure_started_at = None
        self.client.opted_out_at = timezone.now()
        self.client.save()
        self.assertFalse(self.guard(False))

    def test_global_epoch_and_account_change_are_rechecked(self):
        self.settings_row.reply_permission_epoch += 1
        self.settings_row.save()
        self.assertFalse(self.guard(False))
        self.settings_row.ig_user_id = "different-account"
        self.settings_row.save()
        self.assertFalse(self.guard(True))


class PresenceCapabilityTests(TestCase):
    def setUp(self):
        from management.models import InstagramBotSettings

        self.settings_row = InstagramBotSettings.load()
        self.settings_row.ig_user_id = "17841400000000001"
        self.settings_row.save(update_fields=["ig_user_id"])
        self.operator = get_user_model().objects.create_user(
            username="presence-capability-staff", password="fixture", is_staff=True,
        )
        self.route = patch("management.services.instagram_bot.provider_transport", return_value="instagram_login")
        self.token = patch("management.services.instagram_bot.resolve_instagram_login_token", return_value="configured-test-token")
        self.refresh = patch.dict("os.environ", {"IG_PRESENCE_REFRESH_SECONDS": "5"}, clear=False)
        self.route.start()
        self.token.start()
        self.refresh.start()
        self.addCleanup(self.route.stop)
        self.addCleanup(self.token.stop)
        self.addCleanup(self.refresh.stop)
        self.profile = presence.capability_profile(self.settings_row)

    def verify(self, *, now=None):
        return presence.verify_typing_refresh_capability(
            self.settings_row,
            verified_by_id=self.operator.pk,
            evidence_kind=presence.VERIFICATION_EVIDENCE_KIND,
            evidence_ref="sha256:" + hashlib.sha256(b"presence-test-evidence").hexdigest(),
            now=now,
        )

    def test_zero_cadence_does_not_query_capability_storage(self):
        disabled = replace(self.profile, refresh_seconds=0)
        with self.assertNumQueries(0):
            self.assertFalse(presence.typing_refresh_authorized(disabled))

    def test_profile_uses_full_namespaced_account_hash_and_token_fingerprint(self):
        expected = hashlib.sha256(
            b"ig-presence-account-v1\0" + b"17841400000000001"
        ).hexdigest()
        self.assertEqual(self.profile.account_key, expected)
        self.assertEqual(len(self.profile.account_key), 64)
        self.assertEqual(len(self.profile.config_fingerprint), 64)

    def test_verification_requires_operator_and_opaque_evidence(self):
        with self.assertRaises(ValueError):
            presence.verify_typing_refresh_capability(
                self.settings_row, verified_by_id=0,
                evidence_kind=presence.VERIFICATION_EVIDENCE_KIND,
                evidence_ref="sha256:" + hashlib.sha256(b"presence-test-evidence").hexdigest(),
            )
        with self.assertRaises(ValueError):
            presence.verify_typing_refresh_capability(
                self.settings_row, verified_by_id=self.operator.pk,
                evidence_kind="free_form", evidence_ref="not-opaque",
            )
        capability = self.verify()
        self.assertEqual(capability.status, capability.Status.VERIFIED)
        self.assertEqual(capability.verified_by_id, self.operator.pk)
        self.assertTrue(presence.typing_refresh_authorized(self.profile))

    def test_verification_has_exact_24_hour_ttl(self):
        now = timezone.now()
        capability = self.verify(now=now)
        self.assertEqual(capability.verified_at, now)
        self.assertEqual(capability.expires_at, now + timedelta(hours=24))

    def test_verification_rejects_inactive_or_nonstaff_actors_and_malformed_evidence(self):
        nonstaff = get_user_model().objects.create_user(
            username="presence-capability-nonstaff", password="fixture",
        )
        inactive = get_user_model().objects.create_user(
            username="presence-capability-inactive", password="fixture", is_staff=True,
            is_active=False,
        )
        valid_ref = "sha256:" + hashlib.sha256(b"presence-test-evidence").hexdigest()
        for actor_id in (nonstaff.pk, inactive.pk):
            with self.subTest(actor_id=actor_id), self.assertRaises(ValueError):
                presence.verify_typing_refresh_capability(
                    self.settings_row, verified_by_id=actor_id,
                    evidence_kind=presence.VERIFICATION_EVIDENCE_KIND, evidence_ref=valid_ref,
                )
        with self.assertRaises(ValueError):
            presence.verify_typing_refresh_capability(
                self.settings_row, verified_by_id=self.operator.pk,
                evidence_kind=presence.VERIFICATION_EVIDENCE_KIND,
                evidence_ref="operator@example.test",
            )

    def test_expiry_config_mismatch_and_invalidation_block_refresh(self):
        now = timezone.now()
        capability = self.verify(now=now)
        capability.expires_at = now - timedelta(seconds=1)
        capability.save(update_fields=["expires_at"])
        self.assertFalse(presence.typing_refresh_authorized(self.profile, now=now))
        capability = self.verify(now=now)
        self.assertFalse(presence.typing_refresh_authorized(
            replace(self.profile, config_fingerprint="0" * 64), now=now,
        ))
        presence.invalidate_typing_refresh_capability(
            self.settings_row, invalidated_by_id=self.operator.pk,
            reason="operator revocation", now=now,
        )
        capability.refresh_from_db()
        self.assertEqual(capability.status, capability.Status.INVALIDATED)
        self.assertFalse(presence.typing_refresh_authorized(self.profile, now=now))

    def test_accepted_sender_action_never_creates_verification(self):
        from management.models import IgPresenceCapability

        presence._record_typing_refresh_outcome(
            self.profile, presence.SenderActionResult(True, 200, "accepted", "typing_on"),
        )
        self.assertEqual(IgPresenceCapability.objects.count(), 0)

    def test_mark_seen_denial_is_not_typing_capability_denial(self):
        from management.models import IgPresenceCapability

        presence._record_typing_refresh_outcome(
            self.profile, presence.SenderActionResult(False, 400, "unsupported_or_denied", "mark_seen"),
        )
        self.assertEqual(IgPresenceCapability.objects.count(), 0)

    def test_definitive_denial_is_durable_across_new_controller(self):
        now = timezone.now()
        presence._record_typing_refresh_outcome(
            self.profile, presence.SenderActionResult(False, 403, "unsupported_or_denied", "typing_on"), now=now,
        )
        from management.models import IgPresenceCapability

        capability = IgPresenceCapability.objects.get()
        self.assertEqual(capability.status, capability.Status.DENIED)
        self.assertEqual(capability.denied_until, now + timedelta(hours=24))
        new_session = presence.PresenceSession(
            presence.PresenceController(), key="new-controller", transport=accepted,
            guard=lambda cleanup: True, source_watermark=1,
            refresh_seconds=self.profile.refresh_seconds,
            refresh_authorized=lambda: presence.typing_refresh_authorized(self.profile, now=now),
        )
        self.assertFalse(new_session.refresh_authorized())

    def test_rate_limit_uses_short_cooldown_without_losing_verified_state(self):
        now = timezone.now()
        capability = self.verify(now=now)
        expires_at = capability.expires_at
        presence._record_typing_refresh_outcome(
            self.profile, presence.SenderActionResult(False, 429, "rate_limited", "typing_on"), now=now,
        )
        capability.refresh_from_db()
        self.assertEqual(capability.status, capability.Status.VERIFIED)
        self.assertEqual(capability.expires_at, expires_at)
        self.assertEqual(capability.denied_until, now + timedelta(minutes=15))
        self.assertFalse(presence.typing_refresh_authorized(self.profile, now=now))
        self.assertTrue(presence.typing_refresh_authorized(self.profile, now=now + timedelta(minutes=15, seconds=1)))

    def test_timeout_transport_and_provider_leave_verified_record_untouched(self):
        now = timezone.now()
        capability = self.verify(now=now)
        original = (capability.status, capability.expires_at, capability.last_error_kind)
        for kind, status in (("timeout", -1), ("transport", -1), ("provider", 503)):
            presence._record_typing_refresh_outcome(
                self.profile, presence.SenderActionResult(False, status, kind, "typing_on"), now=now,
            )
        capability.refresh_from_db()
        self.assertEqual((capability.status, capability.expires_at, capability.last_error_kind), original)

    def test_unique_identity_prevents_duplicate_capability_rows(self):
        from django.db import IntegrityError
        from management.models import IgPresenceCapability

        self.verify()
        with self.assertRaises(IntegrityError):
            IgPresenceCapability.objects.create(
                transport=self.profile.route,
                graph_version=self.profile.api_version,
                account_key=self.profile.account_key,
                capability=presence.CAPABILITY_TYPING_REFRESH,
                config_fingerprint=self.profile.config_fingerprint,
            )

    def test_migration_is_independent_from_untracked_trace_refresh(self):
        migration = importlib.import_module(
            "management.migrations.0210_ig_presence_capability"
        ).Migration
        self.assertEqual(migration.dependencies, [("management", "0208_journey_trace_snapshots")])
        self.assertEqual(migration.run_before, [("management", "0209_journey_trace_refresh")])

    def test_capability_database_failure_fails_closed(self):
        from management.models import IgPresenceCapability

        with patch.object(IgPresenceCapability.objects, "filter", side_effect=DatabaseError("unavailable")):
            self.assertFalse(presence.typing_refresh_authorized(self.profile))


@override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00", GOOGLE_INDEXING_ENABLED=False)
class CanonicalPresenceIntegrationTests(TransactionTestCase):
    def setUp(self):
        # Reuse the canonical suite's real publication/source fixture without
        # importing its test class into module discovery or rerunning its tests.
        from management.tests_ig_revision_live import RevisionLiveTests

        self._message = MethodType(RevisionLiveTests._message, self)
        self._prepare = MethodType(RevisionLiveTests._prepare, self)
        RevisionLiveTests.setUp(self)
        self.settings.ai_enabled = False
        self.settings.trigger_text = self.source.text
        self.settings.reply_text = "Вітаємо! Чим можемо допомогти?"
        self.settings.save()
        self.handle = SimpleNamespace(
            finished=threading.Event(), stopped=threading.Event(),
            suspend=Mock(), update=Mock(),
        )
        self.handle.stop = Mock(side_effect=self.handle.stopped.set)
        self.handle.begin_dispatch = Mock(side_effect=self.handle.stopped.set)
        self.handle.complete_delivery = Mock()

    def test_canonical_preparation_execution_and_send_share_one_lifecycle(self):
        from management.services import ig_revision_live as live

        def send(*_args, **_kwargs):
            self.assertTrue(self.handle.stopped.is_set(), "dispatch must not wait for or refresh presence")
            return 200, '{"message_id":"canonical-presence-test"}'

        with patch.object(presence, "start_presence", return_value=self.handle) as start, patch.object(live, "capture_revision_source"), patch("management.services.instagram_bot.get_page_token", return_value="test-token"), patch("management.services.instagram_bot._provider_http", side_effect=send) as http, patch("management.services.instagram_bot._register_outgoing_message"), patch("management.services.instagram_bot.send_sender_action") as legacy:
            handled = live.process_pending_revisions(self.settings, max_items=1)
        self.assertEqual(handled, 1)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.handle.suspend.assert_called_once()
        self.assertTrue(self.handle.update.call_args.kwargs["resume"])
        self.handle.stop.assert_called()
        self.handle.complete_delivery.assert_called_once()
        legacy.assert_not_called()
        completed_guard = start.call_args.kwargs["completion_check"]
        self.assertTrue(completed_guard())
        self._message("А ще одне питання", "newer-after-sent")
        self.assertFalse(completed_guard())

    def test_no_reply_input_never_starts_presence_in_preparation_or_execution(self):
        from management.services import ig_revision_live as live
        from management.services.ig_turn_revisions import create_collecting_revision

        self.source.text = "👍"
        self.source.save(update_fields=["text"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        with patch.object(presence, "start_presence") as start, patch.object(live, "capture_revision_source"), patch("management.services.instagram_bot._provider_http") as send:
            self.assertEqual(live.process_pending_revisions(self.settings, max_items=1), 1)
        start.assert_not_called()
        send.assert_not_called()

    def test_execution_exception_stops_started_presence(self):
        from management.services import ig_revision_live as live

        self._prepare()
        with patch.object(presence, "start_presence", return_value=self.handle), patch.object(live, "_execute_deterministic_input", side_effect=RuntimeError("generation failed")):
            with self.assertRaises(RuntimeError):
                live.execute_claimed_revision(self.revision.pk, self.token, self.settings, presence_scope={"lease": "caller-lease", "handle": None})
        self.handle.stop.assert_called_once()

    def test_standalone_execution_without_owned_client_lease_skips_presence(self):
        from management.services import ig_revision_live as live

        self._prepare()
        with patch.object(presence, "start_presence") as start, patch.object(live, "_execute_deterministic_input", return_value=live.RevisionLiveResult(self.revision.pk, "completed")):
            self.assertEqual(live.execute_claimed_revision(self.revision.pk, self.token, self.settings).state, "completed")
        start.assert_not_called()
