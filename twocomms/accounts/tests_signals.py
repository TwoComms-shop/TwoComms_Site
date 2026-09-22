import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import override_settings
from django.utils import timezone

from accounts import signals as account_signals
from accounts.signals import notify_admins_on_registration
from management.models import IgBotNotification


_REGISTRATION_CHECKPOINT_KEY = "internal:registration-reconcile-checkpoint"


def _set_registration_checkpoint(user_id):
    return IgBotNotification.objects.create(
        dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
        event_type="registration_reconcile_checkpoint",
        payload={"last_user_id": int(user_id)},
        status=IgBotNotification.Status.RESOLVED,
    )


def _registration_checkpoint():
    return IgBotNotification.objects.get(
        event_type="registration_reconcile_checkpoint"
    )


class RegistrationNotificationSignalTests(TestCase):
    def test_test_runs_do_not_start_detached_database_notifier(self):
        user = User(username="signal-test-user", email="signal@example.com")

        with patch("accounts.signals._notify_admins") as notify:
            notify_admins_on_registration(User, user, created=True)

        notify.assert_not_called()

    @override_settings(TESTING=False)
    def test_registration_intent_is_written_inside_outer_transaction(self):
        with transaction.atomic():
            user = User.objects.create_user(
                username="signal-commit-user",
                email="commit@example.com",
            )
            self.assertTrue(
                IgBotNotification.objects.filter(
                    dedupe_key=f"registration:{user.pk}"
                ).exists()
            )
        self.assertIsNotNone(
            IgBotNotification.objects.get(
                dedupe_key=f"registration:{user.pk}"
            ).next_attempt_at
        )

    @override_settings(TESTING=False)
    def test_rolled_back_registration_never_queues_notification(self):
        username = "signal-rollback-user"
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email="rollback@example.com",
                )
                raise RuntimeError("force outer rollback")

        self.assertFalse(User.objects.filter(username=username).exists())
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{user.pk}"
            ).exists()
        )

    @override_settings(TESTING=False)
    @patch.dict(
        "os.environ",
        {
            "TELEGRAM_BOT_TOKEN": "site-token",
            "TELEGRAM_ADMIN_ID": "111;222",
            "TELEGRAM_CHAT_ID": "333",
            "MANAGEMENT_TG_BOT_TOKEN": "management-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "999",
        },
        clear=False,
    )
    @patch("management.services.instagram_bot._http")
    def test_outbox_defers_social_lookup_and_uses_site_telegram_transport(self, http):
        from social_django.models import UserSocialAuth
        from management.services import instagram_bot

        http.side_effect = [
            (200, json.dumps({"ok": True, "result": {"message_id": 71}})),
            (200, json.dumps({"ok": True, "result": {"message_id": 72}})),
        ]
        with self.captureOnCommitCallbacks(execute=True):
            user = User.objects.create_user(
                username="signal-social-user",
                email="social@example.com",
            )
        row = IgBotNotification.objects.get(dedupe_key=f"registration:{user.pk}")
        self.assertEqual(row.status, IgBotNotification.Status.PENDING)
        self.assertEqual(http.call_count, 0)

        # The association happens after User creation and before a worker
        # processes the durable intent.
        UserSocialAuth.objects.create(
            user=user,
            provider="google-oauth2",
            uid="signal-social-uid",
        )

        notify_admins_on_registration(User, user, created=True)
        self.assertEqual(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{user.pk}"
            ).count(),
            1,
        )
        row.refresh_from_db()
        self.assertEqual(row.payload["transport"], "site_registration")
        self.assertEqual(row.payload["registration_user_id"], str(user.pk))
        self.assertNotIn(user.username, json.dumps(row.payload))
        self.assertNotIn(user.email, json.dumps(row.payload))
        self.assertEqual(http.call_count, 0)

        self.assertTrue(instagram_bot._deliver_manager_notification_unlocked(row.dedupe_key))
        self.assertEqual(http.call_count, 2)
        self.assertTrue(all("botsite-token/sendMessage" in call.args[0] for call in http.call_args_list))
        deliveries = [json.loads(call.kwargs["data"]) for call in http.call_args_list]
        self.assertEqual([item["chat_id"] for item in deliveries], ["111", "222"])
        self.assertTrue(all("Спосіб: Google" in item["text"] for item in deliveries))
        self.assertTrue(all(item["parse_mode"] == "HTML" for item in deliveries))
        self.assertIn("<b>Нова реєстрація на сайті</b>", deliveries[0]["text"])

        # A retry after confirmed delivery is idempotent and does not re-enter
        # the transport boundary.
        self.assertTrue(instagram_bot._deliver_manager_notification_unlocked(row.dedupe_key))
        self.assertEqual(http.call_count, 2)

    @override_settings(TESTING=False)
    def test_registration_notification_escapes_user_supplied_html(self):
        user = User.objects.create_user(
            username="angle<bracket",
            email="person+<tag>@example.com",
        )

        text = account_signals.registration_notification_text(user.pk)

        self.assertIn("Користувач: <b>angle&lt;bracket</b>", text)
        self.assertIn("Email: person+&lt;tag&gt;@example.com", text)


class RegistrationNotificationAutocommitTests(TransactionTestCase):
    @override_settings(TESTING=False)
    @patch("management.services.instagram_bot._deliver_manager_notification")
    def test_autocommit_persists_intent_without_immediate_delivery(self, deliver):
        user = User.objects.create_user(
            username="signal-autocommit-user",
            email="autocommit@example.com",
        )

        row = IgBotNotification.objects.get(dedupe_key=f"registration:{user.pk}")
        self.assertEqual(row.status, IgBotNotification.Status.PENDING)
        self.assertIsNotNone(row.next_attempt_at)
        deliver.assert_not_called()


class RegistrationNotificationDatabaseFailureTests(TransactionTestCase):
    @override_settings(TESTING=False)
    def test_signal_fast_path_swallows_registration_persistence_database_error(self):
        real_save = IgBotNotification.save

        def fail_registration_save(notification, *args, **kwargs):
            if notification.dedupe_key.startswith("registration:"):
                raise DatabaseError("private@example.com token=do-not-log")
            return real_save(notification, *args, **kwargs)

        with patch.object(IgBotNotification, "save", new=fail_registration_save):
            user = User.objects.create_user(
                username="signal-explicit-database-error-user",
                email="signal-explicit-database-error@example.com",
            )

        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{user.pk}"
            ).exists()
        )
        self.assertFalse(connection.needs_rollback)

    @override_settings(TESTING=False)
    @patch("management.services.instagram_bot._deliver_manager_notification")
    @patch("management.services.instagram_bot._http")
    def test_notification_persistence_failure_does_not_abort_registration(
        self,
        http,
        deliver,
    ):
        username = "signal-persistence-failure-user"
        persistence_errors = []
        real_save = IgBotNotification.save

        def fail_notification_save(notification, *args, **kwargs):
            notification.status = None
            try:
                return real_save(notification, *args, **kwargs)
            except DatabaseError as error:
                persistence_errors.append(error)
                raise

        with patch.object(
            IgBotNotification,
            "save",
            new=fail_notification_save,
        ):
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email="persistence-failure@example.com",
                )
                self.assertTrue(User.objects.filter(pk=user.pk).exists())
                self.assertFalse(
                    IgBotNotification.objects.filter(
                        dedupe_key=f"registration:{user.pk}"
                    ).exists()
                )
                self.assertFalse(connection.needs_rollback)

        self.assertEqual(len(persistence_errors), 1)
        self.assertTrue(User.objects.filter(username=username).exists())
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{user.pk}"
            ).exists()
        )
        self.assertFalse(connection.needs_rollback)
        next_user = User.objects.create_user(
            username="signal-after-persistence-failure-user",
            email="after-persistence-failure@example.com",
        )
        self.assertTrue(User.objects.filter(pk=next_user.pk).exists())
        deliver.assert_not_called()
        http.assert_not_called()


class RegistrationNotificationReconciliationTests(TransactionTestCase):
    def test_test_runs_do_not_initialize_reconciliation_checkpoint(self):
        User.objects.create_user(username="reconcile-test-guard-user")

        self.assertEqual(
            account_signals.reconcile_registration_notification_intents(limit=20),
            0,
        )

        self.assertFalse(
            IgBotNotification.objects.filter(
                event_type="registration_reconcile_checkpoint"
            ).exists()
        )

    @patch("management.services.instagram_bot._deliver_manager_notification")
    @patch("management.services.instagram_bot._http")
    @patch("accounts.signals._detect_method")
    def test_reconcile_is_bounded_idempotent_and_keeps_payload_scalar_only(
        self,
        detect_method,
        http,
        deliver,
    ):
        anchor = User.objects.create_user(username="reconcile-anchor")
        checkpoint = _set_registration_checkpoint(anchor.pk)
        users = [
            User.objects.create_user(
                username=f"reconcile-user-{index}",
                email=f"reconcile-{index}@example.com",
                password=f"raw-secret-{index}",
            )
            for index in range(3)
        ]

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                2,
            )

        self.assertEqual(
            set(
                IgBotNotification.objects.filter(event_type="registration")
                .values_list("dedupe_key", flat=True)
            ),
            {f"registration:{users[0].pk}", f"registration:{users[1].pk}"},
        )
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{users[2].pk}"
            ).exists()
        )
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload["last_user_id"], users[1].pk)

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                1,
            )
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                0,
            )

        self.assertEqual(
            IgBotNotification.objects.filter(event_type="registration").count(),
            3,
        )
        for user in users:
            row = IgBotNotification.objects.get(dedupe_key=f"registration:{user.pk}")
            self.assertEqual(row.payload["registration_user_id"], str(user.pk))
            serialized = json.dumps(row.payload)
            self.assertNotIn(user.username, serialized)
            self.assertNotIn(user.email, serialized)
            self.assertNotIn("raw-secret", serialized)
        detect_method.assert_not_called()
        deliver.assert_not_called()
        http.assert_not_called()

    @patch("accounts.signals._notify_admins", side_effect=DatabaseError("outbox unavailable"))
    def test_reconcile_raises_without_advancing_cursor_on_database_error(self, notify):
        anchor = User.objects.create_user(username="reconcile-error-anchor")
        missing = User.objects.create_user(username="reconcile-error-user")
        checkpoint = _set_registration_checkpoint(anchor.pk)

        with override_settings(TESTING=False):
            with self.assertRaises(DatabaseError):
                account_signals.reconcile_registration_notification_intents(limit=20)

        notify.assert_called_once_with(missing.pk, raise_errors=True)
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload["last_user_id"], anchor.pk)
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{missing.pk}"
            ).exists()
        )

    def test_first_reconcile_backfills_only_bounded_recent_window(self):
        old = User.objects.create_user(username="reconcile-old-user")
        User.objects.filter(pk=old.pk).update(
            date_joined=timezone.now() - timedelta(minutes=16)
        )
        recent = User.objects.create_user(username="reconcile-recent-user")

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint = _registration_checkpoint()
        self.assertEqual(checkpoint.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(checkpoint.payload, {"last_user_id": recent.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{recent.pk}"
            ).exists()
        )

    def test_existing_checkpoint_recovers_user_regardless_of_age(self):
        anchor = User.objects.create_user(username="reconcile-aged-anchor")
        _set_registration_checkpoint(anchor.pk)
        missed = User.objects.create_user(username="reconcile-aged-user")
        User.objects.filter(pk=missed.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{missed.pk}"
            ).exists()
        )
        self.assertEqual(
            _registration_checkpoint().payload,
            {"last_user_id": missed.pk},
        )

    def test_malformed_checkpoint_restarts_with_bounded_recent_bootstrap(self):
        old = User.objects.create_user(username="reconcile-malformed-old")
        User.objects.filter(pk=old.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        recent = User.objects.create_user(username="reconcile-malformed-recent")
        checkpoint = IgBotNotification.objects.create(
            dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
            event_type="registration_reconcile_checkpoint",
            payload={"last_user_id": "not-an-integer"},
            status=IgBotNotification.Status.RESOLVED,
        )

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": recent.pk})
        self.assertEqual(
            IgBotNotification.objects.filter(
                event_type="registration_reconcile_checkpoint"
            ).count(),
            1,
        )
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{recent.pk}"
            ).exists()
        )

    def test_cursor_above_current_user_high_watermark_restarts_bootstrap(self):
        anchor = User.objects.create_user(username="reconcile-corrupt-anchor")
        checkpoint = _set_registration_checkpoint(anchor.pk + 1000)
        User.objects.filter(pk=anchor.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        missed = User.objects.create_user(username="reconcile-corrupt-missed")
        self.assertLess(missed.pk, checkpoint.payload["last_user_id"])

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": missed.pk})
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{missed.pk}"
            ).exists()
        )

    def test_non_positive_limit_returns_without_checkpoint_side_effects(self):
        with (
            override_settings(TESTING=False),
            patch.object(IgBotNotification.objects, "get_or_create") as get_or_create,
        ):
            for limit in (0, -1):
                with self.subTest(limit=limit):
                    self.assertEqual(
                        account_signals.reconcile_registration_notification_intents(
                            limit=limit
                        ),
                        0,
                    )

        get_or_create.assert_not_called()
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=_REGISTRATION_CHECKPOINT_KEY
            ).exists()
        )

    def test_limit_is_capped_at_500_before_user_slice(self):
        anchor = User.objects.create_user(username="reconcile-limit-anchor")
        _set_registration_checkpoint(anchor.pk)

        filter_patcher = patch.object(User.objects, "filter")
        filter_chain = filter_patcher.start()
        filtered_ordered = filter_chain.return_value.order_by.return_value
        filtered_values = filtered_ordered.values_list.return_value
        filtered_values.__getitem__.return_value = []
        self.addCleanup(filter_patcher.stop)

        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=999),
                0,
            )

        filter_chain.assert_called_once_with(pk__gt=anchor.pk)
        filtered_ordered.values_list.assert_called_once_with("pk", "date_joined")
        filtered_values.__getitem__.assert_called_once_with(slice(None, 500))

    @patch(
        "accounts.signals.reconcile_registration_notification_intents",
        side_effect=DatabaseError("checkpoint unavailable"),
    )
    @patch("management.services.instagram_bot._monitor_terminal_notifications")
    def test_existing_drain_owner_survives_reconcile_database_error(
        self,
        monitor,
        reconcile,
    ):
        from management.services.instagram_bot import drain_manager_notifications

        self.assertEqual(drain_manager_notifications(limit=3), 0)

        reconcile.assert_called_once_with(limit=3)
        monitor.assert_called_once_with()

    @patch("management.services.instagram_bot._monitor_terminal_notifications")
    @patch("management.services.ig_alerts.throttle_gate", return_value=(True, 0))
    @patch(
        "management.services.instagram_bot._deliver_manager_notification",
        return_value=True,
    )
    @patch("management.services.instagram_bot.log")
    def test_drain_logs_redacted_checkpoint_database_error_and_continues(
        self,
        log,
        deliver,
        throttle,
        monitor,
    ):
        from management.services.instagram_bot import drain_manager_notifications

        due = IgBotNotification.objects.create(
            dedupe_key="existing-due-after-reconcile-failure",
            event_type="test_due",
            payload={"text": "existing due"},
        )
        sensitive_error = DatabaseError(
            "checkpoint failed for private@example.com token=do-not-log"
        )

        with (
            override_settings(TESTING=False),
            patch.object(
                IgBotNotification.objects,
                "get_or_create",
                side_effect=sensitive_error,
            ),
        ):
            self.assertEqual(drain_manager_notifications(limit=3), 1)

        deliver.assert_called_once_with(due.dedupe_key)
        throttle.assert_called_once_with()
        monitor.assert_called_once_with()
        log.assert_called_once_with(
            "error",
            "registration_notification_reconcile_failed",
            "DatabaseError",
        )
        serialized_log = repr(log.call_args)
        self.assertNotIn("private@example.com", serialized_log)
        self.assertNotIn("do-not-log", serialized_log)

    @patch("management.services.instagram_bot._monitor_terminal_notifications")
    @patch("management.services.ig_alerts.throttle_gate", return_value=(True, 0))
    @patch(
        "management.services.instagram_bot._deliver_manager_notification",
        return_value=True,
    )
    @patch("management.services.instagram_bot.log")
    def test_drain_logs_real_registration_persistence_failure_without_advancing_cursor(
        self,
        log,
        deliver,
        throttle,
        monitor,
    ):
        from management.services.instagram_bot import drain_manager_notifications

        anchor = User.objects.create_user(username="reconcile-persistence-anchor")
        checkpoint = _set_registration_checkpoint(anchor.pk)
        candidate = User.objects.create_user(
            username="reconcile-persistence-candidate"
        )
        due = IgBotNotification.objects.create(
            dedupe_key="existing-due-after-registration-persistence-failure",
            event_type="test_due",
            payload={"text": "existing due"},
        )
        real_save = IgBotNotification.save

        def fail_registration_save(notification, *args, **kwargs):
            if notification.dedupe_key == f"registration:{candidate.pk}":
                raise DatabaseError("private@example.com token=do-not-log")
            return real_save(notification, *args, **kwargs)

        with (
            override_settings(TESTING=False),
            patch.object(IgBotNotification, "save", new=fail_registration_save),
        ):
            self.assertEqual(drain_manager_notifications(limit=3), 1)

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": anchor.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{candidate.pk}"
            ).exists()
        )
        deliver.assert_called_once_with(due.dedupe_key)
        throttle.assert_called_once_with()
        monitor.assert_called_once_with()
        log.assert_called_once_with(
            "error",
            "registration_notification_reconcile_failed",
            "DatabaseError",
        )
        serialized_log = repr(log.call_args)
        self.assertNotIn("private@example.com", serialized_log)
        self.assertNotIn("do-not-log", serialized_log)

    @patch("management.services.instagram_bot._monitor_terminal_notifications")
    @patch("accounts.signals._detect_method")
    @patch("management.services.instagram_bot._http")
    @patch("management.services.instagram_bot._deliver_manager_notification")
    def test_resolved_checkpoint_is_never_selected_for_delivery(
        self,
        deliver,
        http,
        detect_method,
        monitor,
    ):
        from management.services.instagram_bot import drain_manager_notifications

        checkpoint = _set_registration_checkpoint(0)
        checkpoint.last_attempt_at = timezone.now() - timedelta(hours=1)
        checkpoint.next_attempt_at = timezone.now() - timedelta(hours=1)
        checkpoint.save(update_fields=["last_attempt_at", "next_attempt_at"])

        self.assertEqual(drain_manager_notifications(limit=20), 0)

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.status, IgBotNotification.Status.RESOLVED)
        deliver.assert_not_called()
        http.assert_not_called()
        detect_method.assert_not_called()
        monitor.assert_called_once_with()

    @patch("management.services.instagram_bot._monitor_terminal_notifications")
    @patch("management.services.ig_alerts.throttle_gate", return_value=(True, 0))
    @patch(
        "management.services.instagram_bot._deliver_manager_notification",
        return_value=False,
    )
    def test_existing_due_checkpoint_is_normalized_before_delivery(
        self,
        deliver,
        throttle,
        monitor,
    ):
        from management.services.instagram_bot import drain_manager_notifications

        anchor = User.objects.create_user(username="reconcile-due-checkpoint-anchor")
        checkpoint = _set_registration_checkpoint(anchor.pk)
        checkpoint.event_type = "wrong_checkpoint_type"
        checkpoint.status = IgBotNotification.Status.FAILED
        checkpoint.last_attempt_at = timezone.now() - timedelta(hours=1)
        checkpoint.next_attempt_at = timezone.now() - timedelta(minutes=1)
        checkpoint.save(
            update_fields=[
                "event_type",
                "status",
                "last_attempt_at",
                "next_attempt_at",
            ]
        )

        with override_settings(TESTING=False):
            self.assertEqual(drain_manager_notifications(limit=20), 0)

        checkpoint.refresh_from_db()
        self.assertEqual(
            checkpoint.event_type,
            "registration_reconcile_checkpoint",
        )
        self.assertEqual(checkpoint.status, IgBotNotification.Status.RESOLVED)
        self.assertIsNone(checkpoint.next_attempt_at)
        deliver.assert_not_called()
        throttle.assert_not_called()
        monitor.assert_called_once_with()

    def test_bootstrap_scans_only_newest_window_across_bounded_batches(self):
        outside_old = User.objects.create_user(username="reconcile-outside-old")
        outside_recent = User.objects.create_user(username="reconcile-outside-recent")
        first_recent = User.objects.create_user(username="reconcile-window-recent-1")
        first_old = User.objects.create_user(username="reconcile-window-old-1")
        second_old = User.objects.create_user(username="reconcile-window-old-2")
        second_recent = User.objects.create_user(username="reconcile-window-recent-2")
        User.objects.filter(
            pk__in=[outside_old.pk, first_old.pk, second_old.pk]
        ).update(date_joined=timezone.now() - timedelta(days=1))

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 4),
        ):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                1,
            )
            checkpoint = _registration_checkpoint()
            self.assertEqual(checkpoint.payload["last_user_id"], first_old.pk)
            self.assertEqual(
                checkpoint.payload["bootstrap_until_user_id"],
                second_recent.pk,
            )

            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                1,
            )
            checkpoint.refresh_from_db()
            self.assertEqual(checkpoint.payload, {"last_user_id": second_recent.pk})

        registration_keys = set(
            IgBotNotification.objects.filter(event_type="registration")
            .values_list("dedupe_key", flat=True)
        )
        self.assertEqual(
            registration_keys,
            {
                f"registration:{first_recent.pk}",
                f"registration:{second_recent.pk}",
            },
        )
        self.assertNotIn(f"registration:{outside_old.pk}", registration_keys)
        self.assertNotIn(f"registration:{outside_recent.pk}", registration_keys)

        steady_state = User.objects.create_user(username="reconcile-steady-old")
        User.objects.filter(pk=steady_state.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        with override_settings(TESTING=False):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=2),
                1,
            )

        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{steady_state.pk}"
            ).exists()
        )

    def test_bootstrap_retry_keeps_original_recent_eligibility_boundary(self):
        bootstrap_time = timezone.now()
        retry_time = bootstrap_time + timedelta(minutes=16)
        old = User.objects.create_user(username="reconcile-retry-old")
        candidate = User.objects.create_user(username="reconcile-retry-candidate")
        User.objects.filter(pk=old.pk).update(
            date_joined=bootstrap_time - timedelta(days=1)
        )
        User.objects.filter(pk=candidate.pk).update(
            date_joined=bootstrap_time - timedelta(minutes=1)
        )
        real_save = IgBotNotification.save

        def fail_candidate_save(notification, *args, **kwargs):
            if notification.dedupe_key == f"registration:{candidate.pk}":
                raise DatabaseError("temporary registration outbox failure")
            return real_save(notification, *args, **kwargs)

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 2),
            patch("accounts.signals.timezone.now", return_value=bootstrap_time),
            patch.object(IgBotNotification, "save", new=fail_candidate_save),
        ):
            with self.assertRaises(DatabaseError):
                account_signals.reconcile_registration_notification_intents(limit=20)

        checkpoint = _registration_checkpoint()
        self.assertLess(checkpoint.payload.get("last_user_id", 0), candidate.pk)
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{candidate.pk}"
            ).exists()
        )

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 2),
            patch("accounts.signals.timezone.now", return_value=retry_time),
        ):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": candidate.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{candidate.pk}"
            ).exists()
        )

    def test_missing_or_non_integer_bootstrap_cutoff_restarts_bootstrap(self):
        missing = object()
        for marker_kind, cutoff_marker in (
            ("missing", missing),
            ("numeric-string", "123456789"),
            ("boolean", False),
            ("float", 123456789.0),
        ):
            with self.subTest(marker_kind=marker_kind):
                try:
                    old = User.objects.create_user(
                        username=f"reconcile-{marker_kind}-cutoff-old"
                    )
                    recent = User.objects.create_user(
                        username=f"reconcile-{marker_kind}-cutoff-recent"
                    )
                    User.objects.filter(pk=old.pk).update(
                        date_joined=timezone.now() - timedelta(days=1)
                    )
                    payload = {
                        "last_user_id": old.pk,
                        "bootstrap_until_user_id": recent.pk,
                    }
                    if cutoff_marker is not missing:
                        payload["bootstrap_recent_cutoff_us"] = cutoff_marker
                    checkpoint = IgBotNotification.objects.create(
                        dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
                        event_type="registration_reconcile_checkpoint",
                        payload=payload,
                        status=IgBotNotification.Status.RESOLVED,
                    )

                    with (
                        override_settings(TESTING=False),
                        patch.object(
                            account_signals,
                            "REGISTRATION_RECONCILE_INITIAL_WINDOW",
                            2,
                        ),
                    ):
                        self.assertEqual(
                            account_signals.reconcile_registration_notification_intents(
                                limit=1
                            ),
                            0,
                        )

                    checkpoint.refresh_from_db()
                    self.assertEqual(checkpoint.payload["last_user_id"], old.pk)
                    self.assertEqual(
                        checkpoint.payload["bootstrap_until_user_id"],
                        recent.pk,
                    )
                    self.assertIs(
                        type(checkpoint.payload["bootstrap_recent_cutoff_us"]),
                        int,
                    )
                    self.assertFalse(
                        IgBotNotification.objects.filter(
                            event_type="registration"
                        ).exists()
                    )
                finally:
                    IgBotNotification.objects.all().delete()
                    User.objects.all().delete()

    def test_present_malformed_bootstrap_high_watermark_restarts_bootstrap(self):
        anchor = User.objects.create_user(username="reconcile-malformed-high-anchor")
        checkpoint = IgBotNotification.objects.create(
            dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
            event_type="registration_reconcile_checkpoint",
            payload={
                "last_user_id": anchor.pk,
                "bootstrap_until_user_id": "not-an-integer",
            },
            status=IgBotNotification.Status.RESOLVED,
        )
        old = User.objects.create_user(username="reconcile-malformed-high-old")
        User.objects.filter(pk=old.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        recent = User.objects.create_user(username="reconcile-malformed-high-recent")

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 2),
        ):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": recent.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{recent.pk}"
            ).exists()
        )

    def test_invalid_bootstrap_high_watermark_relation_restarts_bootstrap(self):
        anchor = User.objects.create_user(username="reconcile-invalid-high-anchor")
        checkpoint = IgBotNotification.objects.create(
            dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
            event_type="registration_reconcile_checkpoint",
            payload={
                "last_user_id": anchor.pk,
                "bootstrap_until_user_id": anchor.pk,
            },
            status=IgBotNotification.Status.RESOLVED,
        )
        old = User.objects.create_user(username="reconcile-invalid-high-old")
        User.objects.filter(pk=old.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        recent = User.objects.create_user(username="reconcile-invalid-high-recent")

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 2),
        ):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": recent.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{recent.pk}"
            ).exists()
        )

    def test_boolean_last_user_id_restarts_bounded_recent_bootstrap(self):
        old = User.objects.create_user(username="reconcile-bool-cursor-old")
        User.objects.filter(pk=old.pk).update(
            date_joined=timezone.now() - timedelta(days=1)
        )
        recent = User.objects.create_user(
            username="reconcile-bool-cursor-recent"
        )
        checkpoint = IgBotNotification.objects.create(
            dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
            event_type="registration_reconcile_checkpoint",
            payload={"last_user_id": False},
            status=IgBotNotification.Status.RESOLVED,
        )

        with (
            override_settings(TESTING=False),
            patch.object(account_signals, "REGISTRATION_RECONCILE_INITIAL_WINDOW", 2),
        ):
            self.assertEqual(
                account_signals.reconcile_registration_notification_intents(limit=20),
                1,
            )

        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.payload, {"last_user_id": recent.pk})
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{old.pk}"
            ).exists()
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=f"registration:{recent.pk}"
            ).exists()
        )

    def test_non_integer_bootstrap_marker_restarts_bounded_recent_bootstrap(self):
        for marker_kind in ("numeric-string", "boolean"):
            with self.subTest(marker_kind=marker_kind):
                try:
                    old = User.objects.create_user(
                        username=f"reconcile-{marker_kind}-bootstrap-old"
                    )
                    User.objects.filter(pk=old.pk).update(
                        date_joined=timezone.now() - timedelta(days=1)
                    )
                    recent = User.objects.create_user(
                        username=f"reconcile-{marker_kind}-bootstrap-recent"
                    )
                    marker = str(old.pk) if marker_kind == "numeric-string" else True
                    checkpoint = IgBotNotification.objects.create(
                        dedupe_key=_REGISTRATION_CHECKPOINT_KEY,
                        event_type="registration_reconcile_checkpoint",
                        payload={
                            "last_user_id": 0,
                            "bootstrap_until_user_id": marker,
                        },
                        status=IgBotNotification.Status.RESOLVED,
                    )

                    with (
                        override_settings(TESTING=False),
                        patch.object(
                            account_signals,
                            "REGISTRATION_RECONCILE_INITIAL_WINDOW",
                            2,
                        ),
                    ):
                        self.assertEqual(
                            account_signals.reconcile_registration_notification_intents(
                                limit=20
                            ),
                            1,
                        )

                    checkpoint.refresh_from_db()
                    self.assertEqual(
                        checkpoint.payload,
                        {"last_user_id": recent.pk},
                    )
                    self.assertFalse(
                        IgBotNotification.objects.filter(
                            dedupe_key=f"registration:{old.pk}"
                        ).exists()
                    )
                    self.assertTrue(
                        IgBotNotification.objects.filter(
                            dedupe_key=f"registration:{recent.pk}"
                        ).exists()
                    )
                finally:
                    IgBotNotification.objects.all().delete()
                    User.objects.all().delete()
