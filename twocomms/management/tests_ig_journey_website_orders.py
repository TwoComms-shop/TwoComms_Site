from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase
from management.models import IgClient, InstagramBotMessage, IgFunnelResetAudit
from management.services.ig_journey_website_orders import (
    append_website_order_reports, is_website_order_statement, channel_statement,
)


class WebsiteOrderStatementTests(SimpleTestCase):
    def test_reports_and_negatives(self):
        for text in [
            "Я заказывал с сайта и хочу уточнить что по моему заказу",
            "Я замовила на вашому сайті, хочу уточнити доставку",
            "Я заказал на сайте, но не получил заказ",
            "I ordered on your website. Where is my order?",
        ]:
            self.assertTrue(is_website_order_statement(text), text)
        for text in [
            "Хочу заказать с сайта", "Я не заказал на сайте",
            "Если я заказал на сайте, можно написать вам?",
            "I never ordered on your website", "Вы заказали на сайте",
            "Я заказал в другом магазине. У вас на сайте есть футболки?",
        ]:
            self.assertFalse(is_website_order_statement(text), text)

    def test_channel_request_is_not_completed_handoff(self):
        self.assertEqual(channel_statement("Я вже написав вам у телеграм"), "reported")
        self.assertEqual(channel_statement("Давайте перейдем в Telegram"), "requested")
        for text in ["Я не написал в телеграм", "Если я написал в телеграм", "У вас є Telegram?", "Канал Telegram"]:
            self.assertIsNone(channel_statement(text), text)


class WebsiteOrderProjectionTests(TestCase):
    def setUp(self):
        self.client = IgClient.get_or_create_for_sender("website-report")
        self.other = IgClient.get_or_create_for_sender("website-other")
        self.graph = {"nodes": [{"id": "in", "semantic_key": "inbound", "current": True}],
                      "edges": [], "overview_node_ids": ["in"], "coverage": {}}

    def message(self, text, client=None, role="user", status="done"):
        return InstagramBotMessage.objects.create(client=client or self.client,
            sender_id=str((client or self.client).pk), role=role, text=text, status=status)

    def project(self, **kwargs):
        return append_website_order_reports(kwargs.pop("graph", self.graph),
            client_id=self.client.pk, is_history=kwargs.pop("is_history", False), **kwargs)

    def test_source_bound_context_without_order_ownership_or_business_transition(self):
        msg = self.message("Я заказал на сайте, хочу уточнить доставку")
        before = deepcopy(self.graph)
        result = self.project()
        node = result["nodes"][-1]
        self.assertEqual(node["verification"], "needs_order_match")
        self.assertEqual(node["evidence_refs"], [{"kind": "message", "id": msg.pk}])
        self.assertEqual(result["edges"][0]["relation"], "client_report_context")
        self.assertIsNone(node["episode_id"])
        self.assertFalse(node["current"])
        self.assertNotIn("order_id", node)
        self.assertEqual(self.graph, before)
        self.assertEqual(self.project(graph=result), result)
        from management.services.ig_journey_snapshot import build_journey_snapshot
        snapshot = build_journey_snapshot(self.client)
        self.assertIn(node["id"], {n["id"] for n in snapshot["graph"]["nodes"]})

    def test_isolation_history_failed_and_reset(self):
        text = "Я заказал с сайта"
        self.message(text, client=self.other)
        self.message(text, role="model")
        self.message(text, status="failed")
        self.assertEqual(len(self.project()["nodes"]), 1)
        msg = self.message(text)
        self.assertEqual(len(self.project(is_history=True)["nodes"]), 1)
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=msg.id)
        self.assertEqual(len(self.project()["nodes"]), 1)

    def test_repeated_reports_are_one_card_and_keep_source_refs(self):
        self.message("Я заказал с сайта")
        self.message("Я замовив на сайті")
        result = self.project()
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(len(result["nodes"][-1]["evidence_refs"]), 2)

    def test_channel_context_is_source_bound_idempotent_and_reset_scoped(self):
        msg = self.message("Давайте перейдем в Telegram")
        result = self.project()
        node = result["nodes"][-1]
        self.assertEqual(node["channel_report"]["status"], "requested")
        self.assertNotIn("channel_handoff", node)
        self.assertEqual(node["evidence_refs"], [{"kind": "message", "id": msg.pk}])
        self.assertEqual(self.project(graph=result), result)
        self.assertEqual(len(self.project(is_history=True)["nodes"]), 1)
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=msg.pk)
        self.assertEqual(len(self.project()["nodes"]), 1)
