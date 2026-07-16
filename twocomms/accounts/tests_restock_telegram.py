from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import TelegramVerificationSession
from accounts.telegram_bot import TelegramBot
from productcolors.models import Color, ProductColorVariant
from fable5.models import VariantSizeRule
from storefront.models import Category, Product


class RestockTelegramVerificationTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            title="Telegram restock",
            slug="telegram-restock",
            category=Category.objects.create(name="Telegram", slug="telegram-restock"),
            price=1000,
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
        )

    def make_subscription(self):
        from storefront.models import RestockSubscription

        browser_session = self.client.session
        browser_session["restock-test"] = True
        browser_session.save()
        return RestockSubscription.objects.create(
            product=self.product,
            color_variant=self.variant,
            size="M",
            channel=RestockSubscription.Channel.TELEGRAM,
            status=RestockSubscription.Status.DRAFT,
            name="Telegram Buyer",
            browser_session_key=browser_session.session_key,
        )

    @override_settings(TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_BOT_USERNAME="twc_test_bot")
    def test_start_accepts_anonymous_restock_purpose_and_binds_metadata(self):
        subscription = self.make_subscription()

        response = self.client.post(
            reverse("telegram_verify_start"),
            {"purpose": "restock", "restock_id": subscription.id},
        )

        self.assertEqual(response.status_code, 200)
        session = TelegramVerificationSession.objects.get(token=response.json()["token"])
        self.assertEqual(session.purpose, "restock")
        self.assertEqual(session.metadata["restock_id"], subscription.id)

    def test_shared_telegram_frontend_passes_restock_id_to_start_endpoint(self):
        javascript = (
            Path(__file__).resolve().parents[1]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "telegram-verify.js"
        ).read_text(encoding="utf-8")

        self.assertIn("restockId: opts && opts.restockId", javascript)
        self.assertIn("reqPayload.restock_id = currentRequest.restockId", javascript)

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_bot_completion_activates_restock_subscription(self, notify_mock):
        from storefront.models import RestockSubscription

        subscription = self.make_subscription()
        session = TelegramVerificationSession.objects.create(
            token="restock-verify-token",
            purpose="restock",
            status=TelegramVerificationSession.STATUS_VERIFIED,
            expires_at=timezone.now() + timedelta(minutes=5),
            completed_at=timezone.now(),
            telegram_user_id=123456,
            telegram_username="buyer",
            telegram_first_name="Test",
            phone="+380991234567",
            chat_id=123456,
            session_key=subscription.browser_session_key,
            metadata={"restock_id": subscription.id},
        )

        with self.captureOnCommitCallbacks(execute=True):
            TelegramBot()._post_verify_purpose_action(session)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.ACTIVE)
        self.assertEqual(subscription.telegram_user_id, 123456)
        self.assertEqual(subscription.telegram_chat_id, 123456)
        self.assertEqual(subscription.normalized_contact, "+380991234567")
        self.assertIsNotNone(subscription.next_attempt_at)
        notify_mock.assert_called_once_with(subscription)

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    @patch("storefront.services.restock.TelegramBot")
    def test_available_during_draft_is_delivered_after_verification(
        self, delivery_bot, _notify
    ):
        from storefront.models import RestockSubscription

        self.product.status = "published"
        self.product.save(update_fields=["status"])
        rule = VariantSizeRule.objects.create(
            variant=self.variant,
            size="M",
            is_enabled=False,
            stock=0,
        )
        subscription = self.make_subscription()
        session = TelegramVerificationSession.objects.create(
            token="restock-late-availability",
            purpose="restock",
            status=TelegramVerificationSession.STATUS_VERIFIED,
            expires_at=timezone.now() + timedelta(minutes=5),
            completed_at=timezone.now(),
            telegram_user_id=654321,
            chat_id=654321,
            session_key=subscription.browser_session_key,
            metadata={"restock_id": subscription.id},
        )
        rule.is_enabled = True
        rule.stock = 3
        rule.save(update_fields=["is_enabled", "stock"])
        delivery_bot.return_value.send_message.return_value = True

        with self.captureOnCommitCallbacks(execute=True):
            TelegramBot()._post_verify_purpose_action(session)
        call_command(
            "process_restock_notifications",
            subscription_id=subscription.pk,
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.NOTIFIED)
        delivery_bot.return_value.send_message.assert_called_once()

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_late_completion_does_not_reopen_closed_subscription(self, notify):
        from storefront.models import RestockSubscription
        from storefront.services.restock import activate_telegram_subscription

        subscription = self.make_subscription()
        subscription.status = RestockSubscription.Status.CLOSED
        subscription.save(update_fields=["status"])
        session = TelegramVerificationSession.objects.create(
            token="restock-late-closed",
            purpose="restock",
            status=TelegramVerificationSession.STATUS_VERIFIED,
            expires_at=timezone.now() + timedelta(minutes=5),
            telegram_user_id=999,
            chat_id=999,
            session_key=subscription.browser_session_key,
            metadata={"restock_id": subscription.pk},
        )

        self.assertIsNone(activate_telegram_subscription(session))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.CLOSED)
        self.assertIsNone(subscription.telegram_chat_id)
        notify.assert_not_called()

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_wrong_browser_session_does_not_activate_subscription(self, notify):
        from storefront.models import RestockSubscription
        from storefront.services.restock import activate_telegram_subscription

        subscription = self.make_subscription()
        session = TelegramVerificationSession.objects.create(
            token="restock-wrong-browser",
            purpose="restock",
            status=TelegramVerificationSession.STATUS_VERIFIED,
            expires_at=timezone.now() + timedelta(minutes=5),
            telegram_user_id=998,
            chat_id=998,
            session_key="another-browser-session",
            metadata={"restock_id": subscription.pk},
        )

        self.assertIsNone(activate_telegram_subscription(session))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, RestockSubscription.Status.DRAFT)
        self.assertIsNone(subscription.telegram_chat_id)
        notify.assert_not_called()

    @patch.object(TelegramBot, "send_message", return_value=True)
    def test_contact_completes_only_the_session_opened_by_same_chat(self, _send):
        first = TelegramVerificationSession.objects.create(
            token="first-chat-token",
            purpose="login",
            status=TelegramVerificationSession.STATUS_PENDING,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        second = TelegramVerificationSession.objects.create(
            token="second-chat-token",
            purpose="login",
            status=TelegramVerificationSession.STATUS_PENDING,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        bot = TelegramBot()
        bot.handle_verification_start(user_id=111, username="first", token=first.token)
        bot.handle_verification_start(user_id=222, username="second", token=second.token)

        bot.process_contact_message(
            user_id=111,
            username="first",
            contact={"user_id": 111, "phone_number": "+380991111111"},
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, TelegramVerificationSession.STATUS_VERIFIED)
        self.assertEqual(second.status, TelegramVerificationSession.STATUS_BOT_OPENED)

    @patch.object(TelegramBot, "send_message", return_value=True)
    def test_contact_owned_by_another_telegram_user_is_rejected(self, _send):
        session = TelegramVerificationSession.objects.create(
            token="foreign-contact-token",
            purpose="login",
            status=TelegramVerificationSession.STATUS_PENDING,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        bot = TelegramBot()
        bot.handle_verification_start(user_id=111, username="first", token=session.token)

        result = bot.process_contact_message(
            user_id=111,
            username="first",
            contact={"user_id": 222, "phone_number": "+380992222222"},
        )

        session.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(session.status, TelegramVerificationSession.STATUS_BOT_OPENED)
