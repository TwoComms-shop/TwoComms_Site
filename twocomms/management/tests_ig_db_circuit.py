from contextlib import nullcontext
from io import StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import secrets
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.services.ig_db_circuit import (
    DbActiveCapacityError,
    DbCircuitOpen,
    active_slot_cap,
    admit_read_probe,
    db_active_slot,
    record_db_failure,
    record_read_success,
    release_idle_connection,
    retain_session_connection,
    require_database_ready,
)


class DbCircuitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = override_settings(IG_DB_CIRCUIT_LOCK_DIR=directory.name)
        override.enable()
        self.addCleanup(override.disable)

    def test_disconnect_opens_shared_circuit_and_one_probe_recovers(self):
        error = Exception(2006, "gone")
        with patch("management.services.ig_db_circuit.random.random", return_value=0):
            self.assertTrue(record_db_failure(error, scope="default", lane="media", now=10))
        with self.assertRaises(DbCircuitOpen):
            admit_read_probe("default", now=10.5)
        token = admit_read_probe("default", now=11)
        self.assertTrue(token)
        with self.assertRaises(DbCircuitOpen):
            admit_read_probe("default", now=11)
        record_read_success("default", probe_token=token)
        self.assertFalse(admit_read_probe("default", now=11))

    def test_non_disconnect_write_failure_is_not_recorded_or_retried(self):
        self.assertFalse(record_db_failure(Exception(1062, "duplicate"), scope="default", lane="outbox", now=1))

    def test_unknown_capacity_defaults_to_one_and_slots_are_exclusive(self):
        self.assertEqual(active_slot_cap(configured_cap=4, measured_user_cap=None), 1)
        self.assertEqual(active_slot_cap(configured_cap=4, measured_user_cap=3), 0)
        with db_active_slot(configured_cap=1, measured_user_cap=8):
            with self.assertRaises(DbActiveCapacityError):
                with db_active_slot(configured_cap=1, measured_user_cap=8):
                    pass

    @patch("management.services.ig_db_circuit.connections")
    def test_provider_boundary_closes_only_non_atomic_connection(self, connections):
        db = connections.__getitem__.return_value
        db.in_atomic_block = False
        self.assertTrue(release_idle_connection())
        db.close.assert_called_once_with()
        db.close.reset_mock()
        db.in_atomic_block = True
        self.assertFalse(release_idle_connection())
        db.close.assert_not_called()

    def test_session_connection_retention_is_nested_alias_scoped_and_exception_safe(self):
        default, other = MagicMock(), MagicMock()
        default.in_atomic_block = other.in_atomic_block = False
        with patch("management.services.ig_db_circuit.connections", {"default": default, "other": other}):
            with retain_session_connection():
                self.assertFalse(release_idle_connection())
                with retain_session_connection():
                    self.assertFalse(release_idle_connection())
                self.assertFalse(release_idle_connection())
                self.assertTrue(release_idle_connection(using="other"))
            self.assertTrue(release_idle_connection())
            default.close.assert_called_once()
            other.close.assert_called_once()
            default.close.reset_mock()
            with self.assertRaises(ValueError):
                with retain_session_connection():
                    raise ValueError("synthetic")
            self.assertTrue(release_idle_connection())
            default.close.assert_called_once()

    def test_commercial_advisory_owner_survives_provider_boundary_and_reentrant_scope(self):
        from management.services import ig_commercial_episodes as episodes

        db = MagicMock()
        db.vendor, db.alias, db.in_atomic_block = "mysql", "default", False
        db.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
        with patch.object(episodes, "connection", db), \
             patch("management.services.ig_db_circuit.connections", {"default": db}):
            with episodes.commercial_episode_client_lock(42):
                self.assertFalse(release_idle_connection())
                with episodes.commercial_episode_client_lock(42):
                    self.assertFalse(release_idle_connection())
                db.close.assert_not_called()
            self.assertTrue(release_idle_connection())
        db.close.assert_called_once()
        self.assertEqual(db.cursor.return_value.__enter__.return_value.execute.call_count, 2)

    def test_reentrant_commercial_owner_rejects_replaced_physical_connection(self):
        from management.services import ig_commercial_episodes as episodes

        db = MagicMock()
        db.vendor, db.alias, db.in_atomic_block = "mysql", "default", False
        owner = db.connection
        db.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
        with patch.object(episodes, "connection", db), \
             patch("management.services.ig_db_circuit.connections", {"default": db}):
            with episodes.commercial_episode_client_lock(42):
                db.connection = None
                with self.assertRaises(episodes.SessionLockOwnershipLost):
                    with episodes.commercial_episode_client_lock(42):
                        self.fail("A lost advisory owner must not enter")
                self.assertFalse(release_idle_connection())
                db.connection = owner
            self.assertTrue(release_idle_connection())
        self.assertEqual(db.cursor.return_value.__enter__.return_value.execute.call_count, 2)

    def test_cached_capacity_cannot_heal_a_failed_half_open_probe(self):
        db = MagicMock()
        db.settings_dict = {"HOST": "db", "PORT": "3306", "USER": "worker"}
        db.in_atomic_block = False
        db.cursor.return_value.__enter__.return_value.execute.side_effect = OperationalError(2002, "offline")
        with patch("management.services.ig_db_circuit.connections", {"default": db}), patch("management.services.ig_db_circuit.time.monotonic", return_value=10), patch("management.services.ig_db_circuit.random.random", return_value=0):
            record_db_failure(OperationalError(2006, "gone"))
        with patch("management.services.ig_db_circuit.connections", {"default": db}), patch("management.services.ig_db_circuit.time.monotonic", return_value=11), patch("management.services.ig_db_circuit.capacity_snapshot", return_value={"known": True, "user_cap": 20}) as capacity:
            with self.assertRaises(DbCircuitOpen):
                require_database_ready()
            capacity.assert_not_called()
            db.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")
            with self.assertRaises(DbCircuitOpen):
                admit_read_probe()

    def test_ordinary_or_stale_probe_success_cannot_clear_a_new_failure(self):
        record_db_failure(OperationalError(2006, "old"), now=10)
        token = admit_read_probe(now=12)
        record_db_failure(OperationalError(2013, "new"), now=12)
        record_read_success()
        record_read_success(probe_token=token)
        with self.assertRaises(DbCircuitOpen):
            admit_read_probe(now=12)

    def test_daemon_and_periodic_gate_before_work_when_database_is_deferred(self):
        from management.management.commands import run_instagram_bot as daemon
        from management.management.commands import run_instagram_periodic_jobs as periodic

        with patch.object(daemon, "require_database_ready", side_effect=DbCircuitOpen("wait")), patch.object(daemon.bot, "_provider_account_id") as owner:
            with self.assertRaises(DbCircuitOpen):
                daemon._run_work_cycle(SimpleNamespace(is_enabled=True), 0)
            owner.assert_not_called()
        output = StringIO()
        with patch("management.services.ig_db_circuit.require_database_ready", side_effect=DbCircuitOpen("wait")), patch.object(periodic, "due_periodic_lanes") as due:
            periodic.Command(stdout=output).handle(budget_seconds=10)
            due.assert_not_called()
        self.assertIn("deferred=db_circuit", output.getvalue())

    def test_inbox_disconnect_stops_once_without_a_retry_write(self):
        from management.models import IgWebhookInboxEvent
        from management.services.ig_webhook_inbox import drain_webhook_inbox

        with patch("management.services.ig_webhook_inbox._namespace", return_value=("legacy_page:owner", "owner")), patch("management.services.ig_db_circuit.db_active_slot", return_value=nullcontext()), patch("management.services.ig_webhook_inbox.transaction.atomic", return_value=nullcontext()), patch.object(IgWebhookInboxEvent.objects, "select_for_update") as select, patch.object(IgWebhookInboxEvent.objects, "filter") as retry_write, patch("management.services.ig_db_circuit.release_idle_connection"):
            claim = select.return_value.filter.return_value.filter.return_value.order_by.return_value.first
            claim.side_effect = OperationalError(2006, "gone")
            with self.assertRaises(DbCircuitOpen):
                drain_webhook_inbox(object(), limit=25)
            claim.assert_called_once()
            retry_write.assert_not_called()

    def test_verified_capacity_defaults_to_four_but_missing_evidence_to_one(self):
        self.assertEqual(active_slot_cap(measured_user_cap=20), 4)
        with patch("management.services.ig_db_circuit.capacity_snapshot", return_value={"known": False}):
            self.assertEqual(active_slot_cap(), 1)


@skipUnless(connection.vendor == "mysql", "Requires root-owned disposable MariaDB advisory locks")
class SessionConnectionNativeTests(TransactionTestCase):
    def test_provider_boundary_preserves_real_get_lock_until_owner_release(self):
        from management.services.ig_commercial_episodes import commercial_episode_client_lock

        client_id = 10000000 + secrets.randbelow(10000000)
        lock_name = f"twocomms:ig-episode:{client_id}"
        competitor = connection.copy(alias="episode_native_competitor")
        try:
            competitor.ensure_connection()
            with commercial_episode_client_lock(client_id):
                owner = connection.connection
                with connection.cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    owner_id = cursor.fetchone()[0]
                self.assertFalse(release_idle_connection())
                self.assertIs(connection.connection, owner)
                with competitor.cursor() as cursor:
                    cursor.execute("SELECT GET_LOCK(%s, 0)", [lock_name])
                    self.assertEqual(cursor.fetchone()[0], 0)
                    cursor.execute("SELECT IS_USED_LOCK(%s)", [lock_name])
                    self.assertEqual(cursor.fetchone()[0], owner_id)
            with competitor.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0)", [lock_name])
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
                self.assertEqual(cursor.fetchone()[0], 1)
        finally:
            competitor.close()

    def test_closed_native_connection_cannot_pretend_to_reenter_its_old_advisory_lock(self):
        from management.services.ig_commercial_episodes import commercial_episode_client_lock, SessionLockOwnershipLost

        client_id = 10000000 + secrets.randbelow(10000000)
        lock_name = f"twocomms:ig-episode:{client_id}"
        with commercial_episode_client_lock(client_id):
            connection.close()
            with self.assertRaises(SessionLockOwnershipLost):
                with commercial_episode_client_lock(client_id):
                    self.fail("Closed physical session must fail closed")
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0)", [lock_name])
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
            self.assertEqual(cursor.fetchone()[0], 1)
