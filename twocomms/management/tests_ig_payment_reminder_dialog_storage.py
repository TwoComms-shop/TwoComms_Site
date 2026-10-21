"""Physical guards and fenced erasure for standalone reminder time questions."""
from importlib import import_module
import uuid

from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings

from management.ig_payment_reminder_dialog_models import IgPaymentReminderTimeRequest as TimeRequest
from management.models import IgClient, IgPaymentReminderInvitation
from management.services import ig_payment_reminders as reminders
from management.services.ig_payment_reminder_privacy import purge_payment_reminder_data
from management import tests_ig_payment_reminders as fixtures


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    IG_PAYMENT_REMINDERS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class PaymentReminderTimeStorageTests(TransactionTestCase):
    """Borrow producer fixtures, without collecting their unrelated test methods."""
    reset_sequences = True
    quoted = fixtures.RequestedPaymentReminderTests.quoted
    source = fixtures.RequestedPaymentReminderTests.source
    revision = fixtures.RequestedPaymentReminderTests.revision

    def setUp(self):
        self.guard_migration = import_module("management.migrations.0243_payment_reminder_time_dialog")
        # test_settings creates SQLite tables without migration operations.
        # Native MariaDB runs the real graph: never replace or remove its guards.
        if connection.vendor == "sqlite":
            with connection.schema_editor() as editor:
                self.guard_migration.install_guards(None, editor)
            self.addCleanup(self.remove_sqlite_guards)
        fixtures.RequestedPaymentReminderTests.setUp(self)

    def remove_sqlite_guards(self):
        with connection.schema_editor() as editor:
            self.guard_migration.remove_guards(None, editor)

    def tearDown(self):
        # Native delete guards must remain installed through test teardown.
        # Dispose of our rows through the real persisted-owner privacy fence.
        owners = list(TimeRequest.objects.values_list("client_id", flat=True).distinct())
        if owners:
            IgClient.objects.filter(pk__in=owners).update(privacy_erasure_started_at=self.now)
            with transaction.atomic():
                purge_payment_reminder_data(owners)
        super().tearDown()

    def standalone(self):
        source = self.source("Нагадай про оплату")
        revision, _token = self.revision(source)
        result = reminders.prepare_request(revision, source, settings_obj=self.settings, now=self.now)
        self.assertTrue(result.handled, result)
        self.assertIsNone(result.invitation_id)
        row = TimeRequest.objects.get(request_source=source)
        self.assertEqual(result.reply_text, row.body)
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())
        return row

    def spare_source(self):
        source = self.source("Нагадай про оплату")
        revision, _token = self.revision(source)
        return source, revision

    def insert_sql(self, row, **changes):
        """Clone columns to isolate SQL ownership guards, not HMAC admission."""
        values = {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields}
        values.update(id=uuid.uuid4(), **changes)
        fields = row._meta.concrete_fields
        columns = ", ".join(connection.ops.quote_name(field.column) for field in fields)
        placeholders = ", ".join(["%s"] * len(fields))
        params = [field.get_db_prep_save(values[field.attname], connection) for field in fields]
        return f"INSERT INTO {connection.ops.quote_name(row._meta.db_table)} ({columns}) VALUES ({placeholders})", params

    def assert_trigger_denies(self, sql, params):
        # A unique-source collision or another table's guard is not evidence
        # that the new time-request trigger checked the requested ownership.
        with self.assertRaisesMessage(DatabaseError, "Payment reminder time immutable/source guard"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, params)

    def test_orm_and_raw_sql_cannot_rewrite_or_delete_unfenced_question(self):
        row = self.standalone()
        body = row.body
        with self.assertRaises(ValueError):
            row.save()
        with self.assertRaises(ValueError):
            TimeRequest.objects.filter(pk=row.pk).update(body="rewritten")
        with self.assertRaises(ValueError):
            row.delete()
        with self.assertRaises(ValueError):
            TimeRequest.objects.filter(pk=row.pk).delete()
        prepared_id = TimeRequest._meta.pk.get_db_prep_value(row.pk, connection)
        self.assert_trigger_denies(f"UPDATE {self.guard_migration.TABLE} SET body=%s WHERE id=%s",
            ["rewritten", prepared_id])
        self.assert_trigger_denies(f"DELETE FROM {self.guard_migration.TABLE} WHERE id=%s", [prepared_id])
        row.refresh_from_db()
        self.assertEqual(row.body, body)

    def test_raw_insert_rejects_foreign_owner_and_source_outside_revision(self):
        row = self.standalone()
        source, revision = self.spare_source()
        foreign = IgClient.objects.create(igsid="foreign-time-question-owner")
        sql, params = self.insert_sql(row, client_id=foreign.pk,
            request_source_id=source.pk, revision_id=revision.pk)
        self.assert_trigger_denies(sql, params)
        sql, params = self.insert_sql(row, request_source_id=source.pk, revision_id=row.revision_id)
        self.assert_trigger_denies(sql, params)
        self.assertEqual(TimeRequest.objects.count(), 1)

    def test_raw_insert_requires_case_exact_revision_source_namespace(self):
        row = self.standalone()
        source, revision = self.spare_source()
        original_namespace = source.provider_namespace
        source.provider_namespace = "instagram_login:OWNER-1"
        self.assertNotEqual(source.provider_namespace, original_namespace)
        self.assertEqual(source.provider_namespace.casefold(), original_namespace.casefold())
        source.save(update_fields=["provider_namespace"])
        sql, params = self.insert_sql(row, request_source_id=source.pk, revision_id=revision.pk)
        self.assert_trigger_denies(sql, params)
        self.assertEqual(TimeRequest.objects.count(), 1)

    def test_sql_guard_accepts_exact_source_but_rejects_malformed_hmac(self):
        row = self.standalone()
        source, revision = self.spare_source()
        sql, params = self.insert_sql(row, request_source_id=source.pk,
            revision_id=revision.pk, envelope_hmac="A" * 64)
        self.assert_trigger_denies(sql, params)
        sql, params = self.insert_sql(row, request_source_id=source.pk, revision_id=revision.pk)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
        self.assertEqual(TimeRequest.objects.count(), 2)

    def test_purge_requires_exact_persisted_fences_without_any_invitation(self):
        row = self.standalone()
        foreign = IgClient.objects.create(igsid="unfenced-time-question-owner")
        # An in-memory owner change is not the database privacy fence.
        self.client_row.privacy_erasure_started_at = self.now
        with self.assertRaises(ValueError), transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk])
        self.assertTrue(TimeRequest.objects.filter(pk=row.pk).exists())
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        with self.assertRaises(ValueError), transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk, foreign.pk])
        self.assertTrue(TimeRequest.objects.filter(pk=row.pk).exists())
        with transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk])
        self.assertFalse(TimeRequest.objects.exists())
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())
        self.assertTrue(IgClient.objects.filter(pk=foreign.pk, privacy_erasure_started_at__isnull=True).exists())

    def test_purge_outside_atomic_phase_is_denied_even_with_persisted_fence(self):
        row = self.standalone()
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        self.assertFalse(connection.in_atomic_block)
        with self.assertRaises(ValueError):
            purge_payment_reminder_data([self.client_row.pk])
        self.assertTrue(TimeRequest.objects.filter(pk=row.pk).exists())
