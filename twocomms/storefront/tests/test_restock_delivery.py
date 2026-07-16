import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from fable5.models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    SizeGridProfile,
    VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.admin import RestockSubscriptionAdmin
from storefront.models import (
    Catalog,
    Category,
    Product,
    ProductFitOption,
    RestockSubscription,
    SizeGrid,
)


@override_settings(SITE_BASE_URL="https://twocomms.shop")
class RestockDeliveryTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Restock", slug="delivery-restock")
        self.product = Product.objects.create(
            title="Limited hoodie",
            slug="limited-delivery-hoodie",
            category=self.category,
            price=1500,
            status="published",
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
            slug="black-delivery",
            is_default=True,
        )
        self.rule = VariantSizeRule.objects.create(
            variant=self.variant,
            fit_code="classic",
            size="M",
            is_enabled=True,
            stock=2,
        )

    def subscription(self, **overrides):
        values = {
            "product": self.product,
            "color_variant": self.variant,
            "size": "M",
            "option_values": {},
            "option_labels": {},
            "channel": RestockSubscription.Channel.EMAIL,
            "status": RestockSubscription.Status.ACTIVE,
            "name": "Buyer",
            "contact": "buyer@example.com",
            "normalized_contact": "buyer@example.com",
            "fingerprint": f"delivery-{RestockSubscription.objects.count()}",
            "next_attempt_at": timezone.now(),
        }
        values.update(overrides)
        return RestockSubscription.objects.create(**values)

    def test_subscription_availability_uses_variant_stock_and_size_grid(self):
        from storefront.services.restock import subscription_is_available

        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Classic",
            is_active=True,
            is_default=True,
        )
        subscription = self.subscription(option_values={"fit": "classic"})
        self.assertTrue(subscription_is_available(subscription))

        self.rule.stock = 0
        self.rule.save(update_fields=["stock"])
        self.assertFalse(subscription_is_available(subscription))

        self.rule.stock = 2
        self.rule.save(update_fields=["stock"])
        catalog = Catalog.objects.create(name="Delivery grids", slug="delivery-grids")
        self.product.catalog = catalog
        self.product.save(update_fields=["catalog"])
        grid = SizeGrid.objects.create(
            catalog=catalog,
            name="Classic delivery",
            guide_data={"columns": [], "rows": [{"size": "M"}, {"size": "L"}]},
            is_active=True,
        )
        SizeGridProfile.objects.create(size_grid=grid, option_key="fit=classic")
        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=classic",
            size_grid=grid,
        )
        ProductSizeRule.objects.create(
            product=self.product,
            option_key="fit=classic",
            size="M",
            is_enabled=False,
        )
        self.assertFalse(subscription_is_available(subscription))

    def test_unpublished_product_and_missing_variant_are_never_available(self):
        from storefront.services.restock import subscription_is_available

        subscription = self.subscription()
        self.product.status = "draft"
        self.product.save(update_fields=["status"])
        self.assertFalse(subscription_is_available(subscription))

        self.product.status = "published"
        self.product.save(update_fields=["status"])
        subscription.color_variant = None
        subscription.save(update_fields=["color_variant"])
        self.assertFalse(subscription_is_available(subscription))

    def test_schedule_wakes_only_automatic_active_and_failed_channels(self):
        from storefront.services.restock import schedule_restock_scan

        email = self.subscription(next_attempt_at=None)
        telegram = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            normalized_contact="123",
            telegram_chat_id=123,
            status=RestockSubscription.Status.FAILED,
            next_attempt_at=timezone.now() + timedelta(hours=2),
        )
        phone = self.subscription(
            channel=RestockSubscription.Channel.PHONE,
            normalized_contact="+380501112233",
            next_attempt_at=None,
        )
        whatsapp = self.subscription(
            channel=RestockSubscription.Channel.WHATSAPP,
            normalized_contact="+380501112234",
            next_attempt_at=None,
        )
        draft = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            status=RestockSubscription.Status.DRAFT,
            next_attempt_at=None,
        )

        before = timezone.now()
        self.assertEqual(schedule_restock_scan(self.product.pk, self.variant.pk), 2)
        for row in (email, telegram):
            row.refresh_from_db()
            self.assertGreaterEqual(row.next_attempt_at, before)
        for row in (phone, whatsapp, draft):
            row.refresh_from_db()
            self.assertIsNone(row.next_attempt_at)

    @patch("storefront.services.restock.TelegramBot")
    def test_manual_channels_and_telegram_draft_are_never_auto_sent(self, bot_class):
        phone = self.subscription(
            channel=RestockSubscription.Channel.PHONE,
            normalized_contact="+380501112233",
        )
        whatsapp = self.subscription(
            channel=RestockSubscription.Channel.WHATSAPP,
            normalized_contact="+380501112234",
        )
        draft = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            status=RestockSubscription.Status.DRAFT,
            telegram_chat_id=999,
            normalized_contact="999",
        )

        call_command("process_restock_notifications")

        for row, expected in (
            (phone, RestockSubscription.Status.ACTIVE),
            (whatsapp, RestockSubscription.Status.ACTIVE),
            (draft, RestockSubscription.Status.DRAFT),
        ):
            row.refresh_from_db()
            self.assertEqual(row.status, expected)
            self.assertEqual(row.notification_attempts, 0)
        bot_class.return_value.send_message.assert_not_called()

    @patch("storefront.services.restock.TelegramBot")
    def test_telegram_is_sent_once_to_exact_customer_chat(self, bot_class):
        bot_class.return_value.send_message.return_value = True
        subscription = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            normalized_contact="777",
            telegram_chat_id=777,
        )

        call_command("process_restock_notifications")
        call_command("process_restock_notifications")

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        self.assertIsNotNone(subscription.customer_notified_at)
        bot_class.return_value.send_message.assert_called_once()
        self.assertEqual(bot_class.return_value.send_message.call_args.args[0], 777)
        self.assertIsNone(
            bot_class.return_value.send_message.call_args.kwargs["parse_mode"]
        )

    @patch("storefront.services.restock.TelegramBot")
    def test_missing_telegram_chat_fails_and_is_retained_for_retry(self, bot_class):
        subscription = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            normalized_contact="777",
            telegram_chat_id=None,
        )

        call_command("process_restock_notifications")

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.FAILED)
        self.assertIn("chat", subscription.last_error.lower())
        self.assertIsNotNone(subscription.next_attempt_at)
        bot_class.return_value.send_message.assert_not_called()

    def test_email_is_multipart_and_escapes_customer_content(self):
        self.product.title = "<Limited & Rare>"
        self.product.save(update_fields=["title"])
        subscription = self.subscription(
            option_labels={"Посадка": "<Оверсайз>"},
        )

        call_command("process_restock_notifications")

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["buyer@example.com"])
        self.assertIn("https://twocomms.shop/product/limited-delivery-hoodie/", message.body)
        self.assertIn("<Limited & Rare>", message.body)
        self.assertEqual(len(message.alternatives), 1)
        html = message.alternatives[0].content
        self.assertIn("&lt;Limited &amp; Rare&gt;", html)
        self.assertIn("&lt;Оверсайз&gt;", html)
        self.assertNotIn("<Limited & Rare>", html)

    @patch("storefront.services.restock.TelegramBot")
    def test_failure_gets_exponential_retry_then_success(self, bot_class):
        bot_class.return_value.send_message.side_effect = [False, True]
        subscription = self.subscription(
            channel=RestockSubscription.Channel.TELEGRAM,
            normalized_contact="888",
            telegram_chat_id=888,
        )

        first_started = timezone.now()
        call_command("process_restock_notifications")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.FAILED)
        self.assertEqual(subscription.notification_attempts, 1)
        self.assertGreater(subscription.next_attempt_at, first_started)

        subscription.next_attempt_at = timezone.now()
        subscription.save(update_fields=["next_attempt_at"])
        call_command("process_restock_notifications")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        self.assertEqual(subscription.notification_attempts, 2)
        self.assertEqual(bot_class.return_value.send_message.call_count, 2)

    def test_claim_is_exclusive_and_wrong_token_cannot_finalize(self):
        from storefront.services.restock import claim_due_subscription, finalize_delivery

        subscription = self.subscription()
        claimed = claim_due_subscription()
        self.assertEqual(claimed.pk, subscription.pk)
        self.assertEqual(claimed.status, RestockSubscription.Status.SENDING)
        self.assertIsNone(claim_due_subscription())
        self.assertFalse(finalize_delivery(subscription.pk, "00000000-0000-0000-0000-000000000000", success=True))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.SENDING)

    def test_stale_sending_is_recovered(self):
        from storefront.services.restock import recover_stale_sending

        subscription = self.subscription(
            status=RestockSubscription.Status.SENDING,
            delivery_token="11111111-1111-1111-1111-111111111111",
            last_attempt_at=timezone.now() - timedelta(minutes=16),
            next_attempt_at=None,
        )

        self.assertEqual(recover_stale_sending(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.FAILED)
        self.assertIsNone(subscription.delivery_token)
        self.assertIsNotNone(subscription.next_attempt_at)

    @patch("storefront.services.restock.TelegramBot")
    def test_dry_run_does_not_mutate_or_send(self, bot_class):
        subscription = self.subscription()
        output = io.StringIO()

        call_command("process_restock_notifications", "--dry-run", stdout=output)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.ACTIVE)
        self.assertEqual(subscription.notification_attempts, 0)
        bot_class.return_value.send_message.assert_not_called()
        self.assertIn("dry-run", output.getvalue().lower())

    def test_cron_fallback_delivers_after_direct_stock_change(self):
        self.rule.stock = 0
        self.rule.save(update_fields=["stock"])
        subscription = self.subscription(next_attempt_at=None)
        self.rule.stock = 5
        self.rule.save(update_fields=["stock"])

        call_command("process_restock_notifications")

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        self.assertEqual(len(mail.outbox), 1)

    def test_cron_fallback_respects_subscription_filter(self):
        target = self.subscription(next_attempt_at=None)
        untouched = self.subscription(
            next_attempt_at=None,
            normalized_contact="other@example.com",
            contact="other@example.com",
        )

        call_command(
            "process_restock_notifications",
            subscription_id=target.pk,
        )

        target.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(target.status, RestockSubscription.Status.NOTIFIED)
        self.assertEqual(untouched.status, RestockSubscription.Status.ACTIVE)
        self.assertIsNone(untouched.next_attempt_at)

    def test_dry_run_scans_past_unavailable_rows_until_limit_available(self):
        VariantSizeRule.objects.create(
            variant=self.variant,
            size="S",
            is_enabled=False,
            stock=0,
        )
        unavailable = self.subscription(size="S", next_attempt_at=None)
        available = self.subscription(size="M", next_attempt_at=None)
        output = io.StringIO()

        call_command(
            "process_restock_notifications",
            "--limit",
            "1",
            "--dry-run",
            stdout=output,
        )

        self.assertIn("dry-run: 1 available", output.getvalue().lower())
        for row in (unavailable, available):
            row.refresh_from_db()
            self.assertEqual(row.status, RestockSubscription.Status.ACTIVE)
            self.assertIsNone(row.next_attempt_at)
            self.assertEqual(row.notification_attempts, 0)

    def test_non_positive_command_filters_are_rejected_without_processing(self):
        subscription = self.subscription(next_attempt_at=None)

        for option in ("product_id", "variant_id", "subscription_id"):
            with self.subTest(option=option):
                with self.assertRaises(CommandError):
                    call_command("process_restock_notifications", **{option: 0})

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.ACTIVE)
        self.assertIsNone(subscription.next_attempt_at)
        self.assertEqual(subscription.notification_attempts, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_dry_run_models_stale_recovery_without_mutating_then_real_run_delivers(self):
        token = "22222222-2222-2222-2222-222222222222"
        subscription = self.subscription(
            status=RestockSubscription.Status.SENDING,
            delivery_token=token,
            notification_attempts=1,
            last_attempt_at=timezone.now() - timedelta(minutes=16),
            next_attempt_at=None,
        )
        output = io.StringIO()

        call_command(
            "process_restock_notifications",
            dry_run=True,
            limit=1,
            subscription_id=subscription.pk,
            stdout=output,
        )

        subscription.refresh_from_db()
        self.assertIn("dry-run: 1 available", output.getvalue().lower())
        self.assertEqual(subscription.status, RestockSubscription.Status.SENDING)
        self.assertEqual(str(subscription.delivery_token), token)
        self.assertEqual(subscription.notification_attempts, 1)
        self.assertIsNone(subscription.next_attempt_at)

        call_command(
            "process_restock_notifications",
            subscription_id=subscription.pk,
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        self.assertEqual(subscription.notification_attempts, 2)
        self.assertEqual(len(mail.outbox), 1)

    def test_sending_status_participates_in_subscription_deduplication(self):
        from storefront.services.restock import create_subscription

        VariantSizeRule.objects.create(
            variant=self.variant,
            size="XL",
            is_enabled=False,
        )
        with self.captureOnCommitCallbacks(execute=False):
            first, created = create_subscription(
                product=self.product,
                variant=self.variant,
                size="XL",
                option_values={},
                channel=RestockSubscription.Channel.EMAIL,
                name="Buyer",
                contact="buyer@example.com",
                browser_key="browser",
            )
        self.assertTrue(created)
        first.status = RestockSubscription.Status.SENDING
        first.save(update_fields=["status"])

        second, created = create_subscription(
            product=self.product,
            variant=self.variant,
            size="XL",
            option_values={},
            channel=RestockSubscription.Channel.EMAIL,
            name="Buyer",
            contact="buyer@example.com",
            browser_key="browser",
        )
        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)


class RestockVariantSaveAndAdminTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="restock-admin",
            password="password",
            is_staff=True,
        )
        self.category = Category.objects.create(name="Editor restock", slug="editor-restock")
        self.product = Product.objects.create(
            title="Editor product",
            slug="editor-delivery-product",
            category=self.category,
            price=1000,
            status="published",
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="White", primary_hex="#ffffff"),
            slug="white-delivery",
        )
        self.client.force_login(self.staff)

    def _payload(self, **overrides):
        payload = {
            "product_id": self.product.pk,
            "id": self.variant.pk,
            "color": {"id": self.variant.color_id},
        }
        payload.update(overrides)
        return payload

    @patch("storefront.services.restock.schedule_restock_scan")
    def test_variant_size_save_schedules_only_after_commit(self, schedule):
        import json
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                reverse("fable5_api_variant_save"),
                data=json.dumps(self._payload(sizes=[{
                    "fit_code": "",
                    "size": "M",
                    "is_enabled": True,
                    "stock": 4,
                }])),
                content_type="application/json",
            )
            schedule.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(callbacks), 1)
        for callback in callbacks:
            callback()
        schedule.assert_called_once_with(self.product.pk, self.variant.pk)

    @patch("storefront.services.restock.schedule_restock_scan")
    def test_variant_save_without_sizes_does_not_schedule(self, schedule):
        import json
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("fable5_api_variant_save"),
                data=json.dumps(self._payload(sku="UNCHANGED-STOCK")),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        schedule.assert_not_called()

    @patch("storefront.services.restock.schedule_restock_scan")
    @patch("fable5.views._variant_payload", side_effect=RuntimeError("serialize failed"))
    def test_rolled_back_variant_size_save_does_not_schedule(self, _payload, schedule):
        import json
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(
                reverse("fable5_api_variant_save"),
                data=json.dumps(self._payload(sizes=[{
                    "fit_code": "",
                    "size": "M",
                    "is_enabled": True,
                    "stock": 4,
                }])),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(callbacks, [])
        self.assertFalse(VariantSizeRule.objects.filter(variant=self.variant).exists())
        schedule.assert_not_called()

    def test_admin_retry_action_only_queues_delivery(self):
        subscription = RestockSubscription.objects.create(
            product=self.product,
            color_variant=self.variant,
            size="M",
            channel=RestockSubscription.Channel.EMAIL,
            status=RestockSubscription.Status.FAILED,
            normalized_contact="buyer@example.com",
            fingerprint="admin-retry",
            last_error="SMTP unavailable",
        )
        model_admin = RestockSubscriptionAdmin(RestockSubscription, admin.site)
        request = RequestFactory().post("/admin/storefront/restocksubscription/")
        request.user = self.staff

        with patch("storefront.services.restock.send_claimed_subscription") as send:
            model_admin.retry_notifications(request, RestockSubscription.objects.filter(pk=subscription.pk))

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.ACTIVE)
        self.assertIsNotNone(subscription.next_attempt_at)
        send.assert_not_called()
