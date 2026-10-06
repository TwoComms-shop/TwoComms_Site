"""Actual private expiry, monotonic capture and pause-safe maintenance contracts."""
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import ig_private_media as media
from management.services import instagram_bot as bot
from management.services.ig_media_recovery import owned_part_updates, prepared_blob_descriptor
from management.management.commands import run_instagram_bot as runner


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class PrivateMediaExpiryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        os.chmod(root, 0o700)
        override = override_settings(IG_PRIVATE_MEDIA_ROOT=str(root))
        override.enable()
        self.addCleanup(override.disable)
        cache.delete(media.DEADLINE_CURSOR_KEY)
        self.addCleanup(cache.delete, media.DEADLINE_CURSOR_KEY)
        runner.reset_service_lanes()
        self.addCleanup(runner.reset_service_lanes)
        self.user = get_user_model().objects.create_superuser("expiry-reviewer", "expiry@example.test", "unused")
        self.client.force_login(self.user)

    def message(self, *, deadline=None, part_deadlines=None, mime="audio/ogg"):
        now = timezone.now()
        client = IgClient.objects.create(igsid="expiry-" + str(InstagramBotMessage.objects.count()))
        parts = []
        for index, due in enumerate(part_deadlines or [now + timedelta(days=1)]):
            raw = ("private-part-" + str(index)).encode()
            storage_name = media.private_media_storage().save(f"expiry/{client.pk}/{index}.bin", ContentFile(raw))
            parts.append({"source_part_id": "mp1_" + format(index + 1, "032x"), "original_index": index,
                "status": "owned", "private_storage": True, "storage_name": storage_name,
                "mime": mime, "content_hash": hashlib.sha256(raw).hexdigest(),
                "delete_after": due.isoformat() if due else ""})
        return InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user", source="webhook",
            private_media_state="active", private_media_delete_after=deadline, attachment_media=parts)

    def test_capture_second_part_preserves_earlier_message_and_part_deadline(self):
        now = timezone.now()
        first_due, newer_due = now + timedelta(days=2), now + timedelta(days=60)
        row = self.message(deadline=first_due, part_deadlines=[first_due])
        pending = {"source_part_id": "mp1_" + "f" * 32, "original_index": 1,
                   "status": "acquiring", "capture_token": "capture-b", "url": "https://example.test/b"}
        row.attachment_media.append(pending)
        row.save(update_fields=["attachment_media"])
        bot._finish_media_capture(row.pk, pending["source_part_id"], "capture-b",
            {"status": "owned", "private_storage": True, "storage_name": "expiry/later.bin", "mime": "image/jpeg",
             "delete_after": newer_due.isoformat()})
        row.refresh_from_db()
        self.assertEqual(row.private_media_delete_after, first_due)
        self.assertEqual(row.attachment_media[0]["delete_after"], first_due.isoformat())

    def test_stale_capture_token_cannot_change_retention_or_accept_part(self):
        due = timezone.now() + timedelta(days=1)
        row = self.message(deadline=due)
        part = row.attachment_media[0]
        part["capture_token"] = "current-token"
        row.save(update_fields=["attachment_media"])
        bot._finish_media_capture(row.pk, part["source_part_id"], "stale-token",
            {"status": "owned", "delete_after": (due - timedelta(hours=1)).isoformat(), "storage_name": "wrong.bin"})
        row.refresh_from_db()
        self.assertEqual(row.private_media_delete_after, due)
        self.assertEqual(row.attachment_media[0]["storage_name"], part["storage_name"])
        self.assertEqual(row.attachment_media[0]["capture_token"], "current-token")

    def test_new_capture_repairs_legacy_extended_message_deadline_to_earliest_part(self):
        now = timezone.now()
        old_due = now + timedelta(hours=2)
        row = self.message(deadline=now + timedelta(days=60), part_deadlines=[old_due])
        pending = {"source_part_id": "mp1_" + "f" * 32, "original_index": 1,
                   "status": "acquiring", "capture_token": "current-token"}
        row.attachment_media.append(pending)
        row.save(update_fields=["attachment_media"])
        bot._finish_media_capture(row.pk, pending["source_part_id"], "current-token",
            {"status": "owned", "private_storage": True, "storage_name": "expiry/new.bin", "mime": "image/jpeg",
             "delete_after": (now + timedelta(days=60)).isoformat()})
        row.refresh_from_db()
        self.assertEqual(row.private_media_delete_after, old_due)

    def test_actual_preview_overdue_message_or_part_denies_before_file_io(self):
        now = timezone.now()
        rows = [self.message(deadline=now - timedelta(seconds=1)),
                self.message(deadline=now + timedelta(days=2), part_deadlines=[now - timedelta(seconds=1)]),
                self.message(deadline=None, part_deadlines=[None])]
        before = AdminAuditLog.objects.filter(action="ig_private_media.preview").count()
        for row in rows:
            with self.subTest(row=row.pk), patch("management.ig_private_media_views._read_current_bytes") as read, patch("requests.post") as provider:
                response = self.client.get(reverse("management_bot_private_media_preview",
                    args=[row.pk, row.attachment_media[0]["source_part_id"]]))
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response["Cache-Control"])
            read.assert_not_called()
            provider.assert_not_called()
            row.refresh_from_db()
            self.assertEqual(row.private_media_use_token, "")
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_private_media.preview").count(), before)

    def test_lease_admission_uses_wall_clock_after_row_lock(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(seconds=1))
        with patch.object(media.timezone, "now", return_value=now + timedelta(seconds=2)) as clock:
            self.assertEqual(media.acquire_blob_use(row.pk), "")
        clock.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.private_media_use_token, "")

    def test_preview_rechecks_expiry_after_a_preexpiry_lease(self):
        from management.ig_private_media_views import PrivateMediaUnavailable, _safe_part
        now = timezone.now()
        row = self.message(deadline=now + timedelta(seconds=10))
        token = media.acquire_blob_use(row.pk, seconds=120)
        self.assertTrue(token)
        row.refresh_from_db()
        with patch.object(media.timezone, "now", return_value=now + timedelta(seconds=11)):
            with self.assertRaises(PrivateMediaUnavailable):
                _safe_part(row, row.client, row.attachment_media[0]["source_part_id"], use_token=token)
        media.release_blob_use(row.pk, token)

    def test_expiry_defers_physical_delete_for_existing_lease_then_purges_all_parts(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(minutes=2), part_deadlines=[now + timedelta(minutes=2)] * 2)
        token = media.acquire_blob_use(row.pk)
        self.assertTrue(token)
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=now - timedelta(seconds=1))
        self.assertEqual(media.acquire_blob_use(row.pk), "")
        self.assertEqual(media.purge_due(now=now, limit=1), 0)
        self.assertTrue(all(media.private_media_storage().exists(part["storage_name"]) for part in row.attachment_media))
        media.release_blob_use(row.pk, token)
        self.assertEqual(media.purge_due(now=now, limit=1), 1)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleted")
        self.assertTrue(all("storage_name" not in part for part in row.attachment_media))

    def test_legacy_extension_reconciliation_is_bounded_fair_idempotent_and_never_extends(self):
        now = timezone.now()
        rows = [self.message(deadline=now + timedelta(days=60), part_deadlines=[now + timedelta(hours=index + 1)]) for index in range(3)]
        with self.assertNumQueries(5):
            self.assertEqual(media.reconcile_private_media_deadlines(limit=1), 1)
        self.assertEqual(cache.get(media.DEADLINE_CURSOR_KEY), rows[0].pk)
        self.assertEqual(media.reconcile_private_media_deadlines(limit=1), 1)
        self.assertEqual(media.reconcile_private_media_deadlines(limit=1), 1)
        self.assertEqual(media.reconcile_private_media_deadlines(limit=1), 0)
        self.assertEqual(cache.get(media.DEADLINE_CURSOR_KEY), rows[0].pk)
        for index, row in enumerate(rows):
            row.refresh_from_db()
            self.assertEqual(row.private_media_delete_after, now + timedelta(hours=index + 1))

    def test_failed_reconciliation_page_does_not_advance_and_retries_without_starvation(self):
        now = timezone.now()
        rows = [self.message(deadline=now + timedelta(days=60), part_deadlines=[now + timedelta(hours=1)]) for _ in range(3)]
        save = InstagramBotMessage.save

        def fail_second(instance, *args, **kwargs):
            if instance.pk == rows[1].pk:
                raise RuntimeError("interrupted deadline repair")
            return save(instance, *args, **kwargs)

        with patch.object(InstagramBotMessage, "save", autospec=True, side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                media.reconcile_private_media_deadlines(limit=3)
        self.assertEqual(cache.get(media.DEADLINE_CURSOR_KEY, 0), 0)
        self.assertEqual(media.reconcile_private_media_deadlines(limit=3), 2)
        self.assertEqual(cache.get(media.DEADLINE_CURSOR_KEY), rows[-1].pk)
        self.assertEqual(media.reconcile_private_media_deadlines(limit=3), 0)

    def test_purge_repairs_preexisting_extended_deadline_and_physically_deletes_expired_blob(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(days=60), part_deadlines=[now - timedelta(seconds=1)])
        name = row.attachment_media[0]["storage_name"]
        self.assertEqual(media.purge_due(now=now, limit=1), 1)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleted")
        self.assertFalse(media.private_media_storage().exists(name))

    def test_delete_failure_keeps_failed_state_and_retry_due_then_finishes_remaining_blobs(self):
        now = timezone.now()
        row = self.message(deadline=now - timedelta(seconds=1), part_deadlines=[now - timedelta(seconds=1)] * 2)
        storage = media.private_media_storage()
        names = [part["storage_name"] for part in row.attachment_media]
        real_delete = storage.delete

        def fail_second(name):
            if name == names[1]:
                raise OSError("unlink failed")
            return real_delete(name)

        with patch.object(media, "private_media_storage", return_value=storage), patch.object(storage, "delete", side_effect=fail_second):
            self.assertEqual(media.purge_due(now=now, limit=1), 0)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "delete_failed")
        self.assertFalse(storage.exists(names[0]))
        self.assertTrue(storage.exists(names[1]))
        self.assertEqual(media.acquire_blob_use(row.pk), "")
        self.assertEqual(media.purge_due(now=now, limit=1), 0)
        self.assertEqual(media.purge_due(now=now + timedelta(seconds=61), limit=1), 1)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleted")
        self.assertFalse(storage.exists(names[1]))

    def test_unknown_legacy_deadline_does_not_invent_retention_or_extend_existing_boundary(self):
        due = timezone.now() + timedelta(hours=1)
        row = SimpleNamespace(private_media_delete_after=due, attachment_media=[
            {"private_storage": True, "delete_after": "unknown"},
            {"private_storage": True, "delete_after": "2099-01-01T00:00:00"},
            {"private_storage": False, "delete_after": (due - timedelta(days=1)).isoformat()}])
        self.assertEqual(media.earliest_private_media_deadline(row), due)
        row.private_media_delete_after = None
        self.assertIsNone(media.earliest_private_media_deadline(row))

    @override_settings(IG_WEBHOOK_INBOX_ENABLED=False)
    def test_actual_disabled_daemon_cycle_purges_in_isolated_and_legacy_modes_with_zero_provider(self):
        settings = InstagramBotSettings(is_enabled=False, receive_via_poll=False)
        for isolated in (True, False):
            runner.reset_service_lanes()
            row = self.message(deadline=timezone.now() - timedelta(seconds=1))
            with (self.subTest(isolated=isolated), patch.object(runner, "require_database_ready"),
                  patch.object(runner, "maintenance_status", return_value={"active": False}),
                  patch.object(runner, "_revision_receipt_tick"),
                  patch.object(runner, "_service_task_isolation_enabled", return_value=isolated),
                  patch.object(runner, "_run_service_lane", side_effect=lambda _name, work, **_kwargs: work() or True),
                  patch.object(runner, "_reclaim_lease_guard"), patch.object(bot, "drain_manager_notifications"),
                  patch.object(bot, "refresh_profiles_batch"), patch.object(bot, "purge_expired_failed_media_url_metadata"),
                  patch.object(bot, "process_pending") as customer, patch("requests.post") as provider):
                self.assertEqual(runner._run_work_cycle(settings, 17.0), (False, 17.0))
            row.refresh_from_db()
            self.assertEqual(row.private_media_state, "deleted")
            customer.assert_not_called()
            provider.assert_not_called()

    def test_maintenance_or_db_gate_blocks_tick_before_cleanup_and_file_io(self):
        from management.services.ig_db_circuit import DbCircuitOpen
        with patch.object(runner, "maintenance_status", return_value={"active": True}), patch.object(media, "purge_due") as purge:
            self.assertFalse(runner._private_media_cleanup_tick())
        purge.assert_not_called()
        with patch.object(runner, "maintenance_status", return_value={"active": False}), patch.object(runner, "require_database_ready", side_effect=DbCircuitOpen("closed")), patch.object(media, "purge_due") as purge:
            with self.assertRaises(DbCircuitOpen):
                runner._private_media_cleanup_tick()
        purge.assert_not_called()

    def test_gate_closing_during_page_preserves_cursor_and_stops_before_file_io(self):
        now = timezone.now()
        row = self.message(deadline=now - timedelta(seconds=1))
        calls = 0

        def can_work():
            nonlocal calls
            calls += 1
            return calls < 4  # Closes after the repair row lock, before mutation.

        with patch.object(media, "delete_claimed_blob") as delete:
            self.assertEqual(media.purge_due(now=now, limit=1, can_work=can_work), 0)
        delete.assert_not_called()
        self.assertEqual(cache.get(media.DEADLINE_CURSOR_KEY, 0), 0)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "active")

    def test_maintenance_after_delete_claim_stops_file_io_and_stale_claim_is_recoverable(self):
        now = timezone.now()
        row = self.message(deadline=now - timedelta(seconds=1))
        calls = 0

        def can_work():
            nonlocal calls
            calls += 1
            return calls < 7  # Admission closes immediately after claim CAS.

        with patch.object(media, "delete_claimed_blob") as delete:
            self.assertEqual(media.purge_due(now=now, limit=1, can_work=can_work), 0)
        delete.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleting")
        self.assertTrue(media.private_media_storage().exists(row.attachment_media[0]["storage_name"]))
        self.assertEqual(media.purge_due(now=now + timedelta(seconds=301), limit=1), 1)

    def test_existing_retention_cap_is_sixty_days_and_configured_shorter_value_is_kept(self):
        for configured, expected in ((90 * 86400, 60 * 86400), (3 * 86400, 3 * 86400), (0, 3600)):
            with self.subTest(configured=configured), override_settings(IG_PRIVATE_MEDIA_RETENTION_SECONDS=configured):
                self.assertEqual(bot._private_media_retention_seconds(), expected)

    def prepared_sibling(self, row):
        """Use the canonical capture owner before writing interrupted bytes."""
        row.media_capture_eligible = True
        row.attachment_media.append({"source_part_id": "mp1_" + "f" * 32,
            "original_index": 1, "provenance": "live_webhook", "status": "pending",
            "url": "https://lookaside.fbsbx.com/interrupted.jpg"})
        row.save(update_fields=["media_capture_eligible", "attachment_media"])
        token, item, use = bot._claim_media_capture(row.pk, row.attachment_media[-1]["source_part_id"])
        raw = b"interrupted-private-bytes"
        digest = hashlib.sha256(raw).hexdigest()
        descriptor = prepared_blob_descriptor(storage_name=f"ig_message_media/{row.pk}/{digest[:32]}.jpg",
            mime_type="image/jpeg", body_bytes=raw)
        self.assertIsNotNone(bot._persist_prepared_media_blob(row.pk, item["source_part_id"], token, use, descriptor))
        media.private_media_storage().save(descriptor["storage_name"], ContentFile(raw))
        row.refresh_from_db()
        return token, use, descriptor, raw

    def test_expired_failed_and_unknown_owned_capture_cannot_touch_storage_or_mint_lease(self):
        now = timezone.now()
        for state, due, part_due in (("active", now - timedelta(seconds=1), now),
                                   ("delete_failed", now + timedelta(seconds=60), now),
                                   ("active", None, None)):
            with self.subTest(state=state, due=due):
                row = self.message(deadline=now + timedelta(days=1))
                token, use, descriptor, _raw = self.prepared_sibling(row)
                media.release_blob_use(row.pk, use)
                row.refresh_from_db()
                row.private_media_state, row.private_media_delete_after = state, due
                row.attachment_media[0]["delete_after"] = part_due.isoformat() if part_due else ""
                row.save(update_fields=["private_media_state", "private_media_delete_after", "attachment_media"])
                before = InstagramBotMessage.objects.values("attachment_media").get(pk=row.pk)["attachment_media"]
                with patch.object(bot, "_private_media_storage") as storage, patch.object(bot, "download_image") as download:
                    self.assertIsNone(bot._claim_media_capture(row.pk, row.attachment_media[-1]["source_part_id"]))
                    bot._capture_message_media(row)
                storage.assert_not_called()
                download.assert_not_called()
                row.refresh_from_db()
                self.assertEqual(row.private_media_use_token, "")
                self.assertEqual(row.private_media_state, state)
                self.assertEqual(row.attachment_media[-1]["prepared_blob"], descriptor)
                self.assertEqual(row.attachment_media, before)

    def test_prepared_resume_rechecks_current_expiry_before_any_file_operation(self):
        now = timezone.now()
        for state in ("active", "delete_failed"):
            with self.subTest(state=state):
                row = self.message(deadline=now + timedelta(hours=1))
                token, use, descriptor, _raw = self.prepared_sibling(row)
                part = dict(row.attachment_media[-1])
                retry = now + timedelta(seconds=60) if state == "delete_failed" else now - timedelta(seconds=1)
                InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_state=state, private_media_delete_after=retry)
                storage = media.private_media_storage()
                with patch.object(storage, "exists") as exists, patch.object(storage, "open") as opened, patch.object(bot, "_consume_prepared_refetch_attempt") as refetch:
                    result, _parts, _retry = bot._resume_prepared_media_blob(row, part, token=token, use_token=use, private_storage=storage)
                self.assertEqual(result, "failed")
                exists.assert_not_called()
                opened.assert_not_called()
                refetch.assert_not_called()
                row.refresh_from_db()
                self.assertEqual(row.private_media_state, state)
                self.assertEqual(row.private_media_delete_after, retry)
                self.assertEqual(row.attachment_media[-1]["prepared_blob"], descriptor)
                self.assertEqual(row.private_media_use_token, "")

    def test_prepared_persist_and_refetch_recheck_expiry_without_consuming_attempt(self):
        row = self.message(deadline=timezone.now() + timedelta(hours=1))
        token, use, descriptor, _raw = self.prepared_sibling(row)
        attempts = row.attachment_media[-1]["capture_attempts"]
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(bot._persist_prepared_media_blob(row.pk, row.attachment_media[-1]["source_part_id"], token, use, descriptor))
        self.assertIsNone(bot._consume_prepared_refetch_attempt(row.pk, row.attachment_media[-1]["source_part_id"], token, use))
        row.refresh_from_db()
        self.assertEqual(row.attachment_media[-1]["capture_attempts"], attempts)
        self.assertEqual(row.attachment_media[-1]["prepared_blob"], descriptor)
        self.assertEqual(row.private_media_use_token, use)

    def test_capture_finish_after_expiry_keeps_new_verified_bytes_as_actual_deletion_debt(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(hours=1))
        token, use, descriptor, raw = self.prepared_sibling(row)
        old_name = row.attachment_media[0]["storage_name"]
        expired = now - timedelta(seconds=1)
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=expired)
        bot._finish_media_capture(row.pk, row.attachment_media[-1]["source_part_id"], token,
            {**owned_part_updates(descriptor, verified_body_bytes=raw), "delete_after": (now + timedelta(days=60)).isoformat()}, use_token=use)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "delete_pending")
        self.assertEqual(row.private_media_delete_after, expired)
        self.assertEqual(row.attachment_media[-1]["status"], "delete_pending")
        self.assertEqual(row.attachment_media[-1]["storage_name"], descriptor["storage_name"])
        self.assertEqual(media.acquire_blob_use(row.pk), "")
        self.assertEqual(media.purge_due(now=now, limit=1), 1)
        self.assertFalse(media.private_media_storage().exists(old_name))
        self.assertFalse(media.private_media_storage().exists(descriptor["storage_name"]))

    def test_capture_finish_preserves_failed_deletion_retry_and_never_reopens_access(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(hours=1))
        token, use, descriptor, raw = self.prepared_sibling(row)
        retry = now + timedelta(seconds=60)
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_state="delete_failed", private_media_delete_after=retry)
        bot._finish_media_capture(row.pk, row.attachment_media[-1]["source_part_id"], token,
            {**owned_part_updates(descriptor, verified_body_bytes=raw), "delete_after": (now + timedelta(days=60)).isoformat()}, use_token=use)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "delete_failed")
        self.assertEqual(row.private_media_delete_after, retry)
        self.assertEqual(media.acquire_blob_use(row.pk), "")
        self.assertEqual(media.purge_due(now=now, limit=1), 0)
        self.assertEqual(media.purge_due(now=retry, limit=1), 1)
        self.assertFalse(media.private_media_storage().exists(descriptor["storage_name"]))

    def test_canonical_prepared_debt_defers_existing_lease_then_physically_purges(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(hours=1))
        _token, use, descriptor, _raw = self.prepared_sibling(row)
        old_name = row.attachment_media[0]["storage_name"]
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=now - timedelta(seconds=1))
        self.assertEqual(media.purge_due(now=now, limit=1), 0)
        self.assertTrue(media.private_media_storage().exists(descriptor["storage_name"]))
        media.release_blob_use(row.pk, use)
        self.assertEqual(media.purge_due(now=now, limit=1), 1)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleted")
        self.assertNotIn("prepared_blob", row.attachment_media[-1])
        self.assertFalse(media.private_media_storage().exists(old_name))
        self.assertFalse(media.private_media_storage().exists(descriptor["storage_name"]))

    def test_unproven_prepared_path_owner_or_hash_never_unlinks_or_confirms_deletion(self):
        for variant in ("arbitrary_path", "foreign_message", "foreign_hash", "foreign_owner", "unknown_version"):
            with self.subTest(variant=variant):
                row = self.message(deadline=timezone.now() + timedelta(hours=1))
                _token, use, descriptor, _raw = self.prepared_sibling(row)
                media.release_blob_use(row.pk, use)
                row.refresh_from_db()
                if variant == "arbitrary_path": row.attachment_media[-1]["prepared_blob"]["storage_name"] = "foreign/customer.jpg"
                elif variant == "foreign_message": row.attachment_media[-1]["prepared_blob"]["storage_name"] = descriptor["storage_name"].replace(f"/{row.pk}/", f"/{row.pk + 1000}/")
                elif variant == "foreign_hash": row.attachment_media[-1]["prepared_blob"]["content_hash"] = "a" * 64
                elif variant == "foreign_owner": row.sender_id = "another-client"
                else: row.attachment_media[-1]["prepared_blob"]["version"] = "future-unknown"
                row.private_media_delete_after = timezone.now() - timedelta(seconds=1)
                row.save(update_fields=["attachment_media", "sender_id", "private_media_delete_after"])
                with patch.object(media, "private_media_storage") as storage:
                    self.assertEqual(media.purge_due(limit=1), 0)
                storage.assert_not_called()
                row.refresh_from_db()
                self.assertEqual(row.private_media_state, "delete_failed")
                self.assertIsNotNone(row.private_media_delete_after)
                self.assertIn("prepared_blob", row.attachment_media[-1])
                self.assertTrue(media.private_media_storage().exists(descriptor["storage_name"]))

    def test_new_prepared_debt_after_claim_cannot_be_falsely_finalized_as_deleted(self):
        row = self.message(deadline=timezone.now() + timedelta(hours=1))
        _token, use, descriptor, _raw = self.prepared_sibling(row)
        media.release_blob_use(row.pk, use)
        InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=timezone.now() - timedelta(seconds=1))
        claim = media.claim_deletion(row.pk)
        self.assertIsNotNone(claim)
        row.refresh_from_db()
        row.attachment_media[-1]["prepared_blob"]["storage_name"] = "unproven/changed.bin"
        row.save(update_fields=["attachment_media"])
        self.assertFalse(media.delete_claimed_blob(claim))
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "delete_failed")
        self.assertIn("prepared_blob", row.attachment_media[-1])

    def test_actual_symlink_parent_or_leaf_blocks_purge_then_retry_deletes_only_private_blob(self):
        for kind in ("parent", "leaf"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as foreign_directory:
                now = timezone.now()
                row = self.message(deadline=now + timedelta(hours=1))
                _token, use, descriptor, _raw = self.prepared_sibling(row)
                media.release_blob_use(row.pk, use)
                storage = media.private_media_storage()
                target = Path(storage.location) / descriptor["storage_name"]
                foreign = Path(foreign_directory) / target.name
                foreign.write_bytes(b"foreign-owner-keep")
                original = target.parent if kind == "parent" else target
                held = original.with_name(original.name + ".held")
                original.rename(held)
                original.symlink_to(Path(foreign_directory) if kind == "parent" else foreign,
                                    target_is_directory=kind == "parent")
                InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_delete_after=now - timedelta(seconds=1))
                self.assertEqual(media.purge_due(now=now, limit=1), 0)
                row.refresh_from_db()
                self.assertEqual(row.private_media_state, "delete_failed")
                self.assertEqual(foreign.read_bytes(), b"foreign-owner-keep")
                self.assertTrue(original.is_symlink())
                original.unlink()
                held.rename(original)
                self.assertEqual(media.purge_due(now=now + timedelta(seconds=61), limit=1), 1)
                row.refresh_from_db()
                self.assertEqual(row.private_media_state, "deleted")
                self.assertFalse(target.exists())
                self.assertEqual(foreign.read_bytes(), b"foreign-owner-keep")

    def test_guarded_unlink_rejects_directory_and_missing_path_is_idempotent(self):
        storage = media.private_media_storage()
        directory = Path(storage.location) / "directory-target"
        directory.mkdir(mode=0o700)
        with self.assertRaises(PermissionError):
            storage.delete("directory-target")
        self.assertTrue(directory.is_dir())
        storage.delete("missing-parent/missing-file.jpg")
        name = storage.save("idempotent/file.jpg", ContentFile(b"private"))
        storage.delete(name)
        storage.delete(name)
        self.assertFalse(storage.exists(name))

    def test_actual_capture_caller_keeps_failed_retry_when_fence_wins_after_storage(self):
        now = timezone.now()
        customer = IgClient.objects.create(igsid="late-capture-fence")
        row = InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
            role="user", source="webhook", media_capture_eligible=True,
            attachment_media=[{"url": "https://lookaside.fbsbx.com/fresh.jpg",
                "status": "pending", "provenance": "live_webhook"}])
        retry = now + timedelta(seconds=60)
        policy = media.current_private_media_retention_policy

        def failed_during_capture():
            captured = policy()
            InstagramBotMessage.objects.filter(pk=row.pk).update(private_media_state="delete_failed", private_media_delete_after=retry)
            return captured

        with patch.object(bot, "download_image", return_value=("image/jpeg", b"late-private-body")), patch.object(media, "current_private_media_retention_policy", side_effect=failed_during_capture):
            bot._capture_message_media(row)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "delete_failed")
        self.assertEqual(row.private_media_delete_after, retry)
        self.assertEqual(row.attachment_media[0]["status"], "delete_pending")
        name = row.attachment_media[0]["storage_name"]
        self.assertTrue(media.private_media_storage().exists(name))
        self.assertEqual(media.purge_due(now=now, limit=1), 0)
        self.assertEqual(media.purge_due(now=retry, limit=1), 1)
        self.assertFalse(media.private_media_storage().exists(name))

    def test_current_private_without_url_survives_fresh_capture_and_actual_purge(self):
        now = timezone.now()
        due = now + timedelta(hours=1)
        row = self.message(deadline=due, part_deadlines=[due])
        original = dict(row.attachment_media[0])
        self.assertNotIn("url", original)
        row.media_capture_eligible = True
        row.attachment_media.append({"source_part_id": "mp1_" + "f" * 32,
            "original_index": 1, "provenance": "live_webhook", "status": "pending",
            "url": "https://lookaside.fbsbx.com/new-sibling.jpg"})
        row.save(update_fields=["media_capture_eligible", "attachment_media"])
        with patch.object(bot, "download_image", return_value=("image/jpeg", b"new-sibling-private")):
            bot._capture_message_media(row)
        row.refresh_from_db()
        retained = next(part for part in row.attachment_media if part["source_part_id"] == original["source_part_id"])
        for key, value in original.items():
            self.assertEqual(retained.get(key), value)
        self.assertNotIn("url", retained)
        self.assertNotIn("url_metadata_expired", retained)
        self.assertEqual(row.private_media_delete_after, due)
        new_part = next(part for part in row.attachment_media if part["source_part_id"] != original["source_part_id"])
        self.assertEqual(new_part["status"], "owned")
        names = [original["storage_name"], new_part["storage_name"]]
        self.assertEqual(media.purge_due(now=due, limit=1), 1)
        row.refresh_from_db()
        self.assertEqual(row.private_media_state, "deleted")
        self.assertTrue(all(not media.private_media_storage().exists(name) for name in names))


@skipUnless(connection.vendor == "mysql", "requires actual InnoDB row-lock contention")
class NativePrivateMediaExpiryTests(TransactionTestCase):
    message = PrivateMediaExpiryTests.message

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        os.chmod(root, 0o700)
        override = override_settings(IG_PRIVATE_MEDIA_ROOT=str(root))
        override.enable()
        self.addCleanup(override.disable)
        cache.delete(media.DEADLINE_CURSOR_KEY)
        self.addCleanup(cache.delete, media.DEADLINE_CURSOR_KEY)

    def contend(self, row, work, *, after_wait):
        import threading

        arrived = threading.Event()
        outputs, failures, arrivals = [], [], []

        def worker():
            close_old_connections()

            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper() and InstagramBotMessage._meta.db_table in sql:
                    arrivals.append(timezone.now())
                    arrived.set()
                return execute(sql, params, many, context)

            try:
                with connections["default"].execute_wrapper(observe):
                    outputs.append(work())
            except Exception as exc:
                failures.append(exc)
            finally:
                close_old_connections()

        with transaction.atomic():
            locked = InstagramBotMessage.objects.select_for_update().get(pk=row.pk)
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(arrived.wait(5), "reader did not reach the real row-lock boundary")
            after_wait(locked, arrivals)
        thread.join(10)
        self.assertFalse(thread.is_alive(), "contended worker did not finish")
        self.assertEqual(failures, [])
        return outputs

    def test_new_reader_waiting_on_row_lock_uses_postwait_expiry_clock(self):
        import time

        due = timezone.now() + timedelta(seconds=2)
        row = self.message(deadline=due, part_deadlines=[due])

        def expire_while_locked(_row, arrivals):
            self.assertLess(arrivals[0], due, "fixture must reach the lock before expiry")
            time.sleep(max(0, (due - timezone.now()).total_seconds()) + 0.03)

        self.assertEqual(self.contend(row, lambda: media.acquire_blob_use(row.pk),
                                     after_wait=expire_while_locked), [""])
        row.refresh_from_db()
        self.assertEqual(row.private_media_use_token, "")

    def test_deadline_repair_rechecks_changed_part_under_actual_row_lock(self):
        now = timezone.now()
        row = self.message(deadline=now + timedelta(days=60), part_deadlines=[now + timedelta(days=20)])
        earlier = now + timedelta(hours=1)

        def change_part_while_locked(locked, _arrivals):
            locked.attachment_media[0]["delete_after"] = earlier.isoformat()
            locked.save(update_fields=["attachment_media"])

        self.assertEqual(self.contend(row, lambda: media.reconcile_private_media_deadlines(limit=1),
                                     after_wait=change_part_while_locked), [1])
        row.refresh_from_db()
        self.assertEqual(row.private_media_delete_after, earlier)
