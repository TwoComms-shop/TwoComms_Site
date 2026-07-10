"""Тести Phase 0 / Task 2 — картка IG-клієнта (IgClient) і прив'язка історії.

IgClient — це B2C-картка співрозмовника в Instagram Direct (НЕ B2B Client
холодного обзвону). Кожне повідомлення прив'язується до картки; enqueue
створює/оновлює картку (first_contact_at, last_message_at).
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import (
    IgClient,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import instagram_bot as bot


class IgClientModelTests(TestCase):
    def test_get_or_create_idempotent_default_stage(self):
        c1 = IgClient.get_or_create_for_sender("igsid123")
        c2 = IgClient.get_or_create_for_sender("igsid123")
        self.assertEqual(c1.pk, c2.pk)
        self.assertEqual(IgClient.objects.count(), 1)
        self.assertEqual(c1.stage, IgClient.Stage.NEW)

    def test_set_stage_updates_value_and_timestamp(self):
        c = IgClient.get_or_create_for_sender("s2")
        c.set_stage(IgClient.Stage.QUALIFYING)
        c.refresh_from_db()
        self.assertEqual(c.stage, IgClient.Stage.QUALIFYING)
        self.assertIsNotNone(c.stage_updated_at)

    def test_phone_normalized_on_save(self):
        c = IgClient.get_or_create_for_sender("s3")
        c.phone = "0931112233"
        c.save()
        c.refresh_from_db()
        self.assertTrue(c.phone_normalized.startswith("+380"))

    def test_funnel_order_contains_main_stages(self):
        order = IgClient.FUNNEL_ORDER
        self.assertEqual(order[0], IgClient.Stage.NEW)
        self.assertIn(IgClient.Stage.PAID, order)
        self.assertIn(IgClient.Stage.ORDER_CREATED, order)


class IgClientMessageLinkTests(TestCase):
    def test_message_has_client_fk(self):
        c = IgClient.get_or_create_for_sender("s1")
        m = InstagramBotMessage.objects.create(
            sender_id="s1", role="user", text="hi", client=c
        )
        self.assertEqual(m.client_id, c.pk)

    def test_enqueue_links_client_and_sets_contact_times(self):
        s = InstagramBotSettings.load()
        s.is_enabled = True
        s.allowed_senders = ""  # дозволяємо всім (для тесту)
        s.save()
        ok = bot.enqueue_inbound(s, sender_id="abc", text="привіт", mid="m1")
        self.assertTrue(ok)
        msg = InstagramBotMessage.objects.get(mid="m1")
        self.assertIsNotNone(msg.client_id)
        self.assertEqual(msg.client.igsid, "abc")
        self.assertIsNotNone(msg.client.first_contact_at)
        self.assertIsNotNone(msg.client.last_message_at)


class HiddenClientIngressTests(TestCase):
    @patch("management.services.bot_sales_classifier.classify_message")
    @patch("management.services.bot_followups.schedule_after_inbound")
    def test_hidden_client_is_not_enqueued_or_classified(self, schedule_followup, classify_message):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save()
        client = IgClient.get_or_create_for_sender("hidden_ingress")
        client.hidden_at = timezone.now()
        client.save(update_fields=["hidden_at", "updated_at"])

        queued = bot.enqueue_inbound(
            settings, sender_id=client.igsid, text="не обробляйте", mid="hidden-mid"
        )

        self.assertFalse(queued)
        self.assertFalse(InstagramBotMessage.objects.filter(mid="hidden-mid").exists())
        schedule_followup.assert_not_called()
        classify_message.assert_not_called()
        settings.refresh_from_db()
        self.assertIsNone(settings.last_inbound_at)

    def test_overview_counters_exclude_hidden_clients_and_messages(self):
        active = IgClient.get_or_create_for_sender("active_overview")
        hidden = IgClient.get_or_create_for_sender("hidden_overview")
        hidden.hidden_at = timezone.now()
        hidden.save(update_fields=["hidden_at", "updated_at"])
        InstagramBotMessage.objects.create(
            sender_id=active.igsid,
            client=active,
            role=InstagramBotMessage.Role.USER,
            text="активне повідомлення",
            status=InstagramBotMessage.Status.PENDING,
        )
        InstagramBotMessage.objects.create(
            sender_id=hidden.igsid,
            client=hidden,
            role=InstagramBotMessage.Role.USER,
            text="приховане повідомлення",
            status=InstagramBotMessage.Status.PENDING,
        )

        self.assertEqual(bot.pending_count(), 1)
        self.assertEqual(bot.unique_senders_count(), 1)
        snapshot = bot.status_snapshot()
        self.assertEqual(snapshot["pending"], 1)
        self.assertEqual(snapshot["unique_senders"], 1)


class BackfillOrphanMessagesTests(TestCase):
    def test_links_orphan_messages_and_sets_times(self):
        # Легасі-повідомлення без картки (як у старій історії за sender_id).
        m1 = InstagramBotMessage.objects.create(sender_id="legacy1", role="user", text="a")
        InstagramBotMessage.objects.create(sender_id="legacy1", role="model", text="b")
        m3 = InstagramBotMessage.objects.create(sender_id="legacy2", role="user", text="c")

        created = bot.link_orphan_messages_to_clients()
        self.assertEqual(created, 2)  # дві унікальні картки

        m1.refresh_from_db()
        m3.refresh_from_db()
        self.assertEqual(m1.client.igsid, "legacy1")
        self.assertEqual(m3.client.igsid, "legacy2")

        c1 = IgClient.objects.get(igsid="legacy1")
        self.assertIsNotNone(c1.first_contact_at)
        self.assertIsNotNone(c1.last_message_at)

    def test_idempotent_second_run_links_nothing_new(self):
        InstagramBotMessage.objects.create(sender_id="legacy3", role="user", text="x")
        self.assertEqual(bot.link_orphan_messages_to_clients(), 1)
        self.assertEqual(bot.link_orphan_messages_to_clients(), 0)
