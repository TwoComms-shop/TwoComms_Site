"""Task 6 — policy-safe shipment notifications in Instagram Direct."""
import json
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, IgDeal, IgFollowUpTask
from management.services import bot_orders
from management.services import instagram_bot as bot


def _order(status="ship", ttn="59000111222"):
    from orders.models import Order

    return Order.objects.create(
        full_name="Тест", phone="0501112233", city="Київ", np_office="Відділення 1",
        status=status, tracking_number=ttn, total_sum=950,
    )


class SendTextTaggedTests(TestCase):
    @patch("management.services.instagram_bot._http")
    @patch("management.services.instagram_bot.get_page_token")
    def test_uses_message_tag_human_agent(self, mock_pt, mock_http):
        from management.models import InstagramBotSettings

        mock_pt.return_value = "PT"
        mock_http.return_value = (200, '{"message_id":"m"}')
        ok, kind, hint = bot.send_text_tagged(
            InstagramBotSettings.load(),
            "u1",
            "Відправлено",
            human_authored=True,
        )
        self.assertTrue(ok)
        body = mock_http.call_args.kwargs.get("data")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["messaging_type"], "MESSAGE_TAG")
        self.assertEqual(payload["tag"], "HUMAN_AGENT")
        self.assertEqual(payload["recipient"]["id"], "u1")

    @patch("management.services.instagram_bot._http")
    @patch("management.services.instagram_bot.get_page_token")
    def test_rejects_automated_human_agent_tag_before_provider_call(self, mock_pt, mock_http):
        from management.models import InstagramBotSettings

        ok, kind, hint = bot.send_text_tagged(
            InstagramBotSettings.load(),
            "u1",
            "Автоматичне нагадування",
        )

        self.assertFalse(ok)
        self.assertEqual(kind, "policy")
        self.assertIn("human", hint.lower())
        mock_pt.assert_not_called()
        mock_http.assert_not_called()

    @patch("management.services.instagram_bot.get_page_token")
    def test_no_token_permanent(self, mock_pt):
        from management.models import InstagramBotSettings

        mock_pt.return_value = ""
        ok, kind, hint = bot.send_text_tagged(
            InstagramBotSettings.load(),
            "u1",
            "Х",
            human_authored=True,
        )
        self.assertFalse(ok)
        self.assertEqual(kind, "permanent")


class NotifyShippedDealsTests(TestCase):
    @patch("management.services.bot_orders.notify_manager")
    @patch("management.services.instagram_bot.send_text_tagged")
    @patch("management.services.bot_orders.send_text", create=True)
    def test_uses_standard_response_inside_window_once(
        self, mock_send, mock_tagged, mock_notify
    ):
        mock_send.return_value = (True, "", "")
        c = IgClient.get_or_create_for_sender("sh1")
        c.last_message_at = timezone.now()
        c.save(update_fields=["last_message_at", "updated_at"])
        order = _order(ttn="59000111222")
        IgDeal.objects.create(client=c, status=IgDeal.Status.ORDER_CREATED, order=order)
        n = bot_orders.notify_shipped_deals()
        self.assertEqual(n, 1)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args.args[2]
        self.assertIn("59000111222", sent_text)
        mock_tagged.assert_not_called()
        mock_notify.assert_not_called()
        # ідемпотентність — другий прогін не дублює
        n2 = bot_orders.notify_shipped_deals()
        self.assertEqual(n2, 0)

    @patch("management.services.bot_orders.send_text", create=True)
    def test_skips_when_not_shipped_or_no_ttn(self, mock_send):
        c = IgClient.get_or_create_for_sender("sh2")
        order = _order(status="new", ttn="")
        IgDeal.objects.create(client=c, status=IgDeal.Status.ORDER_CREATED, order=order)
        self.assertEqual(bot_orders.notify_shipped_deals(), 0)
        mock_send.assert_not_called()

    @patch("management.services.bot_orders.notify_manager")
    @patch("management.services.instagram_bot.send_text_tagged")
    @patch("management.services.bot_orders.send_text", create=True)
    def test_outside_window_creates_one_human_task_without_tagged_send(
        self, mock_send, mock_tagged, mock_notify
    ):
        c = IgClient.get_or_create_for_sender("sh3")
        order = _order(ttn="59000999888")
        deal = IgDeal.objects.create(client=c, status=IgDeal.Status.ORDER_CREATED, order=order)

        self.assertEqual(bot_orders.notify_shipped_deals(), 0)
        self.assertEqual(bot_orders.notify_shipped_deals(), 0)

        deal.refresh_from_db()
        self.assertIsNone(deal.shipped_notified_at)
        mock_send.assert_not_called()
        mock_tagged.assert_not_called()
        task = IgFollowUpTask.objects.get(
            deal=deal,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="shipment_human_review",
        )
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.skip_reason, "human_agent_required")
        self.assertIn("59000999888", task.message_text)
        self.assertEqual(
            IgFollowUpTask.objects.filter(
                deal=deal,
                reason="shipment_human_review",
            ).count(),
            1,
        )
        self.assertEqual(mock_notify.call_count, 2)

    @patch("management.services.bot_orders.notify_manager")
    @patch("management.services.bot_orders.send_text", create=True)
    def test_ambiguous_standard_send_creates_task_and_is_not_retried(
        self, mock_send, mock_notify
    ):
        mock_send.return_value = (False, "unknown", "delivery unknown")
        c = IgClient.get_or_create_for_sender("sh4")
        c.last_message_at = timezone.now()
        c.save(update_fields=["last_message_at", "updated_at"])
        order = _order(ttn="59000444444")
        deal = IgDeal.objects.create(client=c, status=IgDeal.Status.ORDER_CREATED, order=order)

        self.assertEqual(bot_orders.notify_shipped_deals(), 0)
        self.assertEqual(bot_orders.notify_shipped_deals(), 0)

        self.assertEqual(mock_send.call_count, 1)
        self.assertTrue(
            IgFollowUpTask.objects.filter(
                deal=deal,
                reason="shipment_delivery_review",
            ).exists()
        )
        self.assertEqual(mock_notify.call_count, 2)
