from decimal import Decimal

from django.test import TestCase

from management.models import IgClient, InstagramBotMessage
from management.services.bot_sales_classifier import classify_message


class ConversationIntelligenceSnapshotTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="intelligence-user")

    def message(self, text, role=InstagramBotMessage.Role.USER):
        return InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=role,
            text=text,
            status=InstagramBotMessage.Status.DONE,
        )

    def test_user_message_persists_evidence_bound_snapshot(self):
        message = self.message("Який розмір M і чи є чорний?")

        result = classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(result["analysis_snapshot_id"], snapshot.id)
        self.assertEqual(snapshot.score_band, "exploring")
        self.assertEqual(snapshot.purchase_probability, Decimal("0.28"))
        self.assertGreaterEqual(snapshot.confidence, Decimal("0.55"))
        self.assertEqual(snapshot.analysis_model, "rules")
        self.assertEqual(snapshot.rules_version, "2026-07-23.v1")
        self.assertEqual(snapshot.evidence[0]["source_role"], "user")
        self.assertIn("product", snapshot.uncertainties)

    def test_payment_signal_is_high_intent_but_not_paid(self):
        message = self.message("Дайте посилання на оплату")

        classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.score_band, "high_intent")
        self.assertLess(snapshot.purchase_probability, Decimal("1.00"))
        self.assertIn("payment_unverified", snapshot.uncertainties)

    def test_manager_evidence_is_labeled_and_deduplicated(self):
        message = self.message("Підкажемо клієнту розмір", InstagramBotMessage.Role.MANAGER)

        classify_message(self.client, message=message)
        classify_message(self.client, message=message)

        self.assertEqual(self.client.analysis_snapshots.count(), 1)
        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.evidence[0]["source_role"], "manager")
        self.assertTrue(snapshot.evidence[0]["manager_evidence"])
        self.client.refresh_from_db()
        self.assertEqual(self.client.buying_readiness, 0)
        self.assertEqual(snapshot.purchase_probability, Decimal("0.00"))

    def test_explicit_no_buy_is_lost_not_high_probability(self):
        message = self.message("Більше не пишіть, купувати не буду")

        classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.score_band, "lost")
        self.assertEqual(snapshot.purchase_probability, Decimal("0.00"))

    def test_new_snapshot_fks_are_cross_engine_safe(self):
        from management.models import IgConversationAnalysisSnapshot

        self.assertFalse(IgConversationAnalysisSnapshot._meta.get_field("client").db_constraint)
        self.assertFalse(
            IgConversationAnalysisSnapshot._meta.get_field("last_analyzed_message").db_constraint
        )
