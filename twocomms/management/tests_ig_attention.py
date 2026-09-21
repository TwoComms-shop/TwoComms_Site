from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from management.services.ig_attention import attention_snapshot


class AttentionSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_reply_debt_owns_frame_over_payment_and_post_sale(self):
        result = attention_snapshot(
            now=self.now,
            response_debt={
                "required": True,
                "label": "Потрібна відповідь команди",
                "reason_label": "Невизначена доставка",
                "since": (self.now - timedelta(minutes=7)).isoformat(),
            },
            manager_action_required=True,
            post_sale_needs_action=True,
            commercial_visual_state="paid",
        )
        self.assertEqual(result["owner"], "reply_debt")
        self.assertEqual(result["reason"], "Невизначена доставка")
        self.assertEqual(result["waiting_for"], "team")
        self.assertEqual(result["observed_seconds"], 420)
        self.assertEqual(result["commercial_state"], "paid")

    def test_overdue_followup_is_visible_only_without_stronger_obligation(self):
        result = attention_snapshot(
            now=self.now,
            next_followup_at=self.now - timedelta(minutes=3),
        )
        self.assertEqual(result["owner"], "overdue")
        self.assertTrue(result["overdue"])
        self.assertEqual(result["observed_seconds"], 180)

    def test_paused_manager_view_has_no_false_attention(self):
        result = attention_snapshot(
            now=self.now,
            bot_paused=True,
            manager_takeover=True,
        )
        self.assertEqual(result["owner"], "none")
        self.assertEqual(result["mode"], "manager")
        self.assertEqual(result["waiting_for"], "customer")
