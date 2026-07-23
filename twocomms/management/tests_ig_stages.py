"""Тести Phase 3 / Task 12 — машина стадій воронки + таймлайн подій.

Модель додає у відповідь приховані керуючі теги ([STAGE:x], [MANAGER], …),
воркер їх парсить, вирізає з тексту і просуває клієнта по воронці, фіксуючи
кожен перехід у IgClientStageEvent (для кружечків прогресу в картці).
"""
from django.test import SimpleTestCase, TestCase

from management.models import IgClient, IgClientStageEvent, InstagramBotSettings
from management.services import instagram_bot as bot


class ExtractControlTests(SimpleTestCase):
    def test_manager_tag(self):
        clean, ctrl = bot._extract_control("Передаю менеджеру. [MANAGER]")
        self.assertTrue(ctrl.get("manager"))
        self.assertNotIn("[MANAGER]", clean)

    def test_stage_tag(self):
        clean, ctrl = bot._extract_control("Чудовий вибір! [STAGE:checkout]")
        self.assertEqual(ctrl.get("stage"), "checkout")
        self.assertNotIn("STAGE", clean)

    def test_plain_text_no_tags(self):
        clean, ctrl = bot._extract_control("Привіт, як справи?")
        self.assertEqual(ctrl, {})
        self.assertEqual(clean, "Привіт, як справи?")

    def test_cyrillic_brackets_preserved(self):
        clean, ctrl = bot._extract_control("Це [приклад] тексту")
        self.assertIn("[приклад]", clean)
        self.assertEqual(ctrl, {})


class ApplyStageTests(TestCase):
    def test_default_prompt_reserves_hard_stages_for_system_truth(self):
        prompt = InstagramBotSettings.load().system_prompt

        self.assertIn("Ніколи не став paid/order_created/done", prompt)
        self.assertNotIn(
            "payment_pending, paid, order_created, done, lead_manager",
            prompt,
        )

    def test_apply_valid_stage(self):
        c = IgClient.get_or_create_for_sender("s1")
        self.assertTrue(bot._apply_stage(c, "checkout"))
        c.refresh_from_db()
        self.assertEqual(c.stage, "checkout")

    def test_apply_invalid_stage_noop(self):
        c = IgClient.get_or_create_for_sender("s2")
        self.assertFalse(bot._apply_stage(c, "not-a-stage"))
        c.refresh_from_db()
        self.assertEqual(c.stage, IgClient.Stage.NEW)

    def test_apply_same_stage_noop(self):
        c = IgClient.get_or_create_for_sender("s3")
        self.assertFalse(bot._apply_stage(c, "new"))

    def test_model_cannot_claim_payment_or_fulfilment_stages(self):
        c = IgClient.get_or_create_for_sender("s-hard-targets")

        for stage in (IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE):
            with self.subTest(stage=stage):
                self.assertFalse(bot._apply_stage(c, stage))
                c.refresh_from_db()
                self.assertEqual(c.stage, IgClient.Stage.NEW)

    def test_model_cannot_regress_a_hard_stage(self):
        c = IgClient.get_or_create_for_sender("s-hard-current")
        c.stage = IgClient.Stage.PAID
        c.save(update_fields=["stage", "updated_at"])

        self.assertFalse(bot._apply_stage(c, IgClient.Stage.QUALIFYING))
        c.refresh_from_db()
        self.assertEqual(c.stage, IgClient.Stage.PAID)


class StageEventTests(TestCase):
    def test_set_stage_creates_event(self):
        c = IgClient.get_or_create_for_sender("s4")
        c.set_stage(IgClient.Stage.CHECKOUT, reason="bot")
        ev = IgClientStageEvent.objects.filter(client=c).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.to_stage, "checkout")
        self.assertEqual(ev.from_stage, "new")
        self.assertEqual(ev.reason, "bot")
