"""Physical assisted evidence guards seeded by actual source/manager producers."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from importlib import import_module
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import (
    IgAssistedCommerceRequest as Request, IgAssistedCommerceDecision as Decision,
    IgAssistedCommerceExecution as Execution, IgAssistedCommerceImage as Image,
    IgAssistedCommerceStockSync as StockSync,
)
from management.models import IgClient, IgDeal, IgFollowUpTask
from management.services import ig_assisted_commerce as assisted
from management.tests_ig_assisted_commerce import AssistedCommerceFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class NativeAssistedCommerceModelsTests(AssistedCommerceFixture, TransactionTestCase):
    reset_sequences = True

    def produced_request(self):
        request, _revision = self.request_case()
        self.assertTrue(assisted._verified(request, "request"))
        return request

    def produced_decision(self, request=None):
        request = request or self.produced_request()
        draft, created = self.record(request, items=self.items_for(request, unit_price="790.00"))
        self.assertTrue(created)
        decision, created = self.record(request, status="approved", latest=draft)
        self.assertTrue(created)
        self.assertTrue(assisted._verified(decision, "decision"))
        return request, decision

    def raw_clone(self, row, overrides, *, drop_signed=()):
        """Exercise native ownership even with internally consistent signed columns.

The starting evidence comes from real producers. Re-signing a deliberately
invalid copy models a service writer bug: SQL must independently reject the
foreign owner/reference. This helper grants no runtime checkout authority.
"""
        values = {field.attname: deepcopy(getattr(row, field.attname)) for field in row._meta.concrete_fields}
        values["id"] = uuid.uuid4()
        if "operation_id" in values:
            values["operation_id"] = uuid.uuid4()
        values.update(overrides)
        clone = type(row)(**values)
        if isinstance(clone, Image):
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(clone.mime_type, ".webp")
            clone.storage_key = f"assisted/{clone.request_id.hex}/{clone.id.hex}{extension}"
        clone.envelope = deepcopy(row.envelope)
        clone.envelope["columns"] = assisted._columns(clone)
        for key in drop_signed:
            clone.envelope["columns"].pop(key, None)
        kind = clone.envelope["kind"]
        clone.envelope_hmac = assisted._sign(kind, clone.envelope, clone.key_id)
        columns = [field.column for field in clone._meta.concrete_fields]
        prepared = [field.get_db_prep_save(getattr(clone, field.attname), connection)
            for field in clone._meta.concrete_fields]
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO " + connection.ops.quote_name(clone._meta.db_table)
                + " (" + ",".join(connection.ops.quote_name(name) for name in columns) + ") VALUES ("
                + ",".join(["%s"] * len(columns)) + ")", prepared)

    def rejected_clone(self, row, overrides, **kwargs):
        with self.assertRaisesRegex(DatabaseError, "Assisted immutable/owner/privacy/lifecycle guard"):
            with transaction.atomic():
                self.raw_clone(row, overrides, **kwargs)

    def raw_update(self, row, **changes):
        values = []
        for name, value in changes.items():
            field = row._meta.get_field(name)
            values.append(field.get_db_prep_save(value, connection))
        values.append(row._meta.pk.get_db_prep_value(row.pk, connection))
        with connection.cursor() as cursor:
            cursor.execute("UPDATE " + connection.ops.quote_name(row._meta.db_table) + " SET "
                + ",".join(connection.ops.quote_name(row._meta.get_field(name).column) + "=%s" for name in changes)
                + " WHERE id=%s", values)

    def test_native_producers_are_append_only_and_do_not_create_payment_or_stock_effects(self):
        from orders.models import Order, PaymentAttempt
        from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, decision = self.produced_decision()
        provider.assert_not_called()
        self.assertEqual(decision.approved_total, Decimal("790.00"))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        for row in (request, decision):
            with self.subTest(model=type(row).__name__), self.assertRaises(ValueError):
                row.save()
            with self.subTest(model=type(row).__name__), self.assertRaises(DatabaseError):
                with transaction.atomic():
                    self.raw_update(row, envelope_hmac=row.envelope_hmac)

    def test_native_request_rejects_foreign_client_source_revision_task_and_episode(self):
        request = self.produced_request()
        foreign = IgClient.objects.create(igsid="assisted-native-foreign-owner",
            reply_permission_epoch=3, last_user_message_at=self.now)
        foreign_source = self.source("Хочу купити", client=foreign)
        foreign_revision, _ = self.revision(foreign_source)
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        foreign_deal = IgDeal.objects.create(client=foreign)
        foreign_episode = ensure_episode_for_deal(foreign_deal)
        task = IgFollowUpTask.objects.create(client=foreign, kind="manager_task", reason="assisted-native-foreign",
            due_at=self.now, status="skipped")
        for values in ({"client_id": foreign.pk}, {"source_id": foreign_source.pk}, {"task_id": task.pk},
                {"revision_id": foreign_revision.pk}, {"episode_id": foreign_episode.pk}, {"deal_id": foreign_deal.pk}):
            with self.subTest(values=values):
                self.rejected_clone(request, values)
        self.rejected_clone(request, {"revision_id": None, "source_id": None})

    def test_explicit_signed_null_cannot_be_omitted_for_nullable_native_owner(self):
        request = self.produced_request()
        self.assertIsNone(request.task_id)
        self.rejected_clone(request, {}, drop_signed=("task_id",))

    def test_request_same_intention_cannot_move_to_another_client(self):
        request = self.produced_request()
        foreign = IgClient.objects.create(igsid="assisted-native-intention-owner")
        source = self.source("Хочу купити", client=foreign)
        self.rejected_clone(request, {"client_id": foreign.pk, "source_id": source.pk,
            "revision_id": None, "episode_id": None, "deal_id": None, "task_id": None, "version": 2})

    def test_raw_request_and_decision_version_jumps_cannot_create_an_owner_fork(self):
        request, decision = self.produced_decision()
        self.rejected_clone(request, {"intention_id": uuid.uuid4(), "version": 2})
        self.rejected_clone(request, {"version": request.version + 2})
        self.rejected_clone(decision, {"version": decision.version + 2})

    def test_native_decision_rejects_foreign_request_owner_and_nonstaff_actor(self):
        request, decision = self.produced_decision()
        foreign = IgClient.objects.create(igsid="assisted-native-decision-owner")
        viewer = get_user_model().objects.create_user(username="assisted-native-viewer", password="test")
        for values in ({"client_id": foreign.pk}, {"actor_id": viewer.pk}, {"request_id": uuid.uuid4()}):
            with self.subTest(values=values):
                self.rejected_clone(decision, dict(values, version=decision.version + 1))

    def test_native_money_capacity_currency_and_physical_bounds_match_ordinary_checkout(self):
        _, decision = self.produced_decision()
        for values in ({"approved_total": Decimal("1000000.01")}, {"approved_custom_quantity": 51},
                {"currency": "USD"}, {"expires_at": None},
                {"approved_custom_quantity": 1, "physical_checked_at": None}):
            with self.subTest(values=values):
                self.rejected_clone(decision, dict(values, version=decision.version + 1))

    def test_decision_parent_digest_and_snapshot_columns_cannot_drift(self):
        _, decision = self.produced_decision()
        self.rejected_clone(decision, {"request_digest": "0" * 64, "version": decision.version + 1})
        self.rejected_clone(decision, {"full_capture_digest": "0" * 64, "version": decision.version + 1})
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                self.raw_update(decision, draft_digest="0" * 64)

    def test_native_delete_requires_actual_persisted_exact_owner_erasure_fence(self):
        request, decision = self.produced_decision()
        for row in (request, decision):
            with self.subTest(model=type(row).__name__), self.assertRaises(DatabaseError):
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM " + connection.ops.quote_name(row._meta.db_table) + " WHERE id=%s",
                        [row._meta.pk.get_db_prep_value(row.pk, connection)])
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        with transaction.atomic(), connection.cursor() as cursor:
            for model in (Decision, Request):
                cursor.execute("DELETE FROM " + connection.ops.quote_name(model._meta.db_table) + " WHERE client_id=%s",
                    [self.client_row.pk])
        self.assertFalse(Request.objects.exists())
        self.assertFalse(Decision.objects.exists())

    def test_additive_native_tables_and_guards_are_transactional(self):
        if connection.vendor == "mysql":
            with connection.cursor() as cursor:
                for model in (Request, Decision, Execution, Image, StockSync):
                    cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                        [model._meta.db_table])
                    self.assertEqual(cursor.fetchone(), ("InnoDB",))
                cursor.execute("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE 'ig_assist_%'")
                names = {row[0] for row in cursor.fetchall()}
        else:
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ig_assist_%'")
                names = {row[0] for row in cursor.fetchall()}
        self.assertEqual(names, {f"ig_assist_{kind}_{event}" for kind in ("request", "decision", "execution", "image", "stock_sync")
            for event in ("insert", "update", "delete")})

    def test_native_legacy_auth_engine_is_allowed_without_weakening_business_parents(self):
        if connection.vendor != "mysql":
            self.skipTest("MariaDB engine boundary")
        from django.apps import apps
        from django.conf import settings
        migration = import_module("management.migrations.0233_assisted_commerce")
        table = "ig_assist_auth_engine_test_" + uuid.uuid4().hex[:12]
        quoted = connection.ops.quote_name(table)
        # A fresh test database may have auth foreign keys that production's
        # legacy MyISAM schema never had. Exercise a physical isolated actor
        # table shape without altering any actual authentication parent.
        actor_table = SimpleNamespace(_meta=SimpleNamespace(db_table=table))
        registry = SimpleNamespace(get_model=lambda app, model=None:
            actor_table if model is None and app == settings.AUTH_USER_MODEL else apps.get_model(app, model))
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {quoted} LIKE {connection.ops.quote_name(get_user_model()._meta.db_table)}")
            try:
                cursor.execute(f"ALTER TABLE {quoted} ENGINE=MyISAM")
                migration.ensure_transactions(registry, SimpleNamespace(connection=connection))
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                self.assertEqual(cursor.fetchone(), ("MyISAM",))
                request, decision = self.produced_decision()
                viewer = get_user_model().objects.create_user(username="assisted-native-legacy-viewer", password="test")
                self.rejected_clone(decision, {"actor_id": viewer.pk, "version": decision.version + 1})
                self.assertEqual(request.client_id, decision.client_id)
            finally:
                cursor.execute(f"DROP TABLE {quoted}")

    def produced_image(self, request):
        from PIL import Image as RasterImage
        from management.services.ig_assisted_commerce_media import upload_assisted_image
        buffer = BytesIO()
        RasterImage.new("RGB", (2, 2), color=(140, 40, 180)).save(buffer, format="PNG")
        image, created = upload_assisted_image(request.pk, actor=self.manager,
            operation_id=uuid.uuid4(), expected_request_version=request.version,
            expected_request_digest=request.envelope_hmac, expected_full_capture_digest=request.full_capture_digest,
            expected_decision_version=0, expected_draft_digest="",
            upload=SimpleUploadedFile("purple-hoodie.png", buffer.getvalue(), content_type="image/png"), now=self.now)
        self.assertTrue(created)
        self.assertTrue(assisted._verified(image, "image"))
        return image

    def test_native_legacy_product_metadata_engine_is_allowed_without_conversion(self):
        if connection.vendor != "mysql":
            self.skipTest("MariaDB metadata engine boundary")
        from django.apps import apps
        from storefront.models import Product
        migration = import_module("management.migrations.0233_assisted_commerce")
        table = "ig_assist_product_engine_test_" + uuid.uuid4().hex[:12]
        quoted = connection.ops.quote_name(table)
        metadata_table = SimpleNamespace(_meta=SimpleNamespace(db_table=table))
        registry = SimpleNamespace(get_model=lambda app, model=None:
            metadata_table if (app, model) == ("storefront", "Product") else apps.get_model(app, model))
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {quoted} LIKE {connection.ops.quote_name(Product._meta.db_table)}")
            try:
                cursor.execute(f"ALTER TABLE {quoted} ENGINE=MyISAM")
                migration.ensure_transactions(registry, SimpleNamespace(connection=connection))
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                self.assertEqual(cursor.fetchone(), ("MyISAM",))
                for app, model in migration.OWNER_MODELS:
                    cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                        [apps.get_model(app, model)._meta.db_table])
                    self.assertEqual(cursor.fetchone(), ("InnoDB",))
            finally:
                cursor.execute(f"DROP TABLE {quoted}")

    def test_actual_owned_raster_upload_has_no_public_url_and_foreign_owner_is_rejected(self):
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-image-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            self.assertEqual((image.request_id, image.client_id, image.uploader_id),
                (request.pk, request.client_id, self.manager.pk))
            self.assertIn(image.mime_type, {"image/jpeg", "image/png"})
            with self.assertRaises(ValueError):
                _ = image.url
            foreign = IgClient.objects.create(igsid="assisted-native-image-owner")
            viewer = get_user_model().objects.create_user(username="assisted-native-image-viewer", password="test")
            for values in ({"client_id": foreign.pk}, {"request_id": uuid.uuid4()}, {"uploader_id": viewer.pk}):
                with self.subTest(values=values):
                    self.rejected_clone(image, values)

    def test_image_native_metadata_rewrite_public_key_and_raster_bounds_are_denied(self):
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-image-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            for values in ({"mime_type": "image/svg+xml"}, {"byte_size": 6291457}, {"byte_size": 0},
                    {"width": 24000001}, {"height": 0}, {"delete_after": self.now - timedelta(seconds=1)}):
                with self.subTest(values=values):
                    self.rejected_clone(image, values)
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    self.raw_update(image, content_digest="0" * 64)
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    self.raw_update(image, storage_key="https://example.invalid/customer.png")

    def test_owned_image_erasure_is_monotonic_and_preserves_approved_identity(self):
        from management.services.ig_assisted_commerce_media import delete_asset_bytes
        from management.services.ig_private_media import private_media_storage
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-image-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            identity = (image.storage_key, image.content_digest, image.envelope_hmac)
            for state in (Image.State.DELETE_PENDING, Image.State.DELETING):
                image.state = state
                image.save(update_fields=["state"])
            delete_asset_bytes(image)
            self.assertFalse(private_media_storage().exists(image.storage_key))
            image.state, image.erased_at = Image.State.DELETED, self.now
            image.save(update_fields=["state", "erased_at"])
            image.refresh_from_db()
            self.assertEqual((image.storage_key, image.content_digest, image.envelope_hmac), identity)
            self.assertTrue(assisted._verified(image, "image"))
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    self.raw_update(image, state=Image.State.ACTIVE, erased_at=None)

    def test_privacy_purge_removes_owned_bytes_before_metadata_and_retries_failures(self):
        from management.services.ig_fulfillment_privacy import purge_fulfillment_data
        from management.services.ig_private_media import private_media_storage
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-privacy-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            _, decision = self.produced_decision(request)
            with self.assertRaises(ValueError), transaction.atomic():
                purge_fulfillment_data([request.client_id])
            self.assertTrue(private_media_storage().exists(image.storage_key))
            IgClient.objects.filter(pk=request.client_id).update(privacy_erasure_started_at=self.now)
            with patch("management.services.ig_assisted_commerce_media.delete_asset_bytes", side_effect=OSError("fixture_failure")):
                with self.assertRaises(OSError), transaction.atomic():
                    purge_fulfillment_data([request.client_id])
            self.assertTrue(Image.objects.filter(pk=image.pk).exists())
            self.assertTrue(Decision.objects.filter(pk=decision.pk).exists())
            self.assertTrue(Request.objects.filter(pk=request.pk).exists())
            self.assertTrue(private_media_storage().exists(image.storage_key))
            with transaction.atomic():
                purge_fulfillment_data([request.client_id])
            self.assertFalse(private_media_storage().exists(image.storage_key))
            self.assertFalse(Image.objects.filter(pk=image.pk).exists())
            self.assertFalse(Decision.objects.filter(request_id=request.pk).exists())
            self.assertFalse(Request.objects.filter(pk=request.pk).exists())

    def test_existing_expiry_worker_deletes_real_unconverted_image_and_keeps_retry_pointer(self):
        from management.services.ig_private_media import purge_due, private_media_storage
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-expiry-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            due = image.delete_after + timedelta(seconds=1)
            with patch("management.services.ig_assisted_commerce_media.delete_asset_bytes", side_effect=OSError("fixture_failure")):
                self.assertEqual(purge_due(now=due), 0)
            image.refresh_from_db()
            self.assertEqual(image.state, Image.State.DELETE_FAILED)
            self.assertEqual(image.failure_code, "OSError")
            self.assertIsNone(image.erased_at)
            self.assertTrue(private_media_storage().exists(image.storage_key))
            self.assertTrue(assisted._verified(image, "image"))
            self.assertEqual(purge_due(now=due + timedelta(minutes=1)), 1)
            image.refresh_from_db()
            self.assertEqual(image.state, Image.State.DELETED)
            self.assertFalse(private_media_storage().exists(image.storage_key))
            self.assertTrue(Request.objects.filter(pk=request.pk).exists())

    def test_privacy_fence_erases_image_even_before_immutable_expiry(self):
        from management.services.ig_private_media import purge_due_assisted_images, private_media_storage
        request = self.produced_request()
        with TemporaryDirectory(prefix="assisted-model-early-privacy-") as private_root, override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            image = self.produced_image(request)
            self.assertGreater(image.delete_after, self.now)
            self.assertEqual(purge_due_assisted_images(now=self.now), 0)
            IgClient.objects.filter(pk=request.client_id).update(privacy_erasure_started_at=self.now)
            self.assertEqual(purge_due_assisted_images(now=self.now), 1)
            image.refresh_from_db()
            self.assertEqual(image.state, Image.State.DELETED)
            self.assertFalse(private_media_storage().exists(image.storage_key))
