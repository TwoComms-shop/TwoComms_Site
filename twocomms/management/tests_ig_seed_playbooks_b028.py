"""Focused B02.8 contracts for the managed sales-playbook seed."""
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from management.models import BotInstruction, InstagramBotSettings


class B028SeedPlaybookTests(TestCase):
    def _item(self, title):
        from management.management.commands.seed_ig_bot_sales_playbooks import PLAYBOOKS

        return next(item for item in PLAYBOOKS if item["title"] == title)

    def test_managed_bodies_use_scoped_facts_and_do_not_advertise_unavailable_offers(self):
        from management.management.commands.seed_ig_bot_sales_playbooks import PLAYBOOKS

        combined = "\n".join(item["body"] for item in PLAYBOOKS)
        for forbidden in (
            "автоматична система окремо дасть 5%",
            "10% тільки як фінальний",
            "майже будь-який DTF",
            "щільна тканина, якісний DTF-друк і власне виробництво",
            "Нової пошти 1–3 дні",
            "[OBJHANDLE:",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)

        dispatch = self._item("IG Objection · Термін доставки")["body"]
        self.assertIn("після підтвердження оплати", dispatch)
        self.assertIn("не строк перевезення", dispatch)
        self.assertIn('"kind":"objhandle"', dispatch)

    def test_every_b028_legacy_body_is_upgraded_but_admin_body_is_preserved(self):
        from management.management.commands.seed_ig_bot_sales_playbooks import (
            B02_8_MANAGED_TITLES,
            LEGACY_PLAYBOOK_BODIES,
        )

        for title in B02_8_MANAGED_TITLES:
            if title == "Custom Print Handoff":
                continue
            with self.subTest(title=title):
                BotInstruction.objects.create(
                    title=title,
                    body=next(iter(LEGACY_PLAYBOOK_BODIES[title])),
                )
        BotInstruction.objects.create(
            title="Custom Print Handoff",
            body="ADMINISTRATOR-OWNED BODY",
        )

        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())

        for title in B02_8_MANAGED_TITLES:
            with self.subTest(title=title):
                row = BotInstruction.objects.filter(title=title).order_by("id").first()
                if title == "Custom Print Handoff":
                    self.assertEqual(row.body, "ADMINISTRATOR-OWNED BODY")
                else:
                    self.assertEqual(row.body, self._item(title)["body"])

    def test_known_legacy_marker_body_upgrades_to_typed_control_guidance(self):
        from management.management.commands.seed_ig_bot_sales_playbooks import (
            LEGACY_PLAYBOOK_BODIES,
        )

        title = "IG Objection · Подумаю"
        old_body = next(
            body for body in LEGACY_PLAYBOOK_BODIES[title] if "[OBJHANDLE:" in body
        )
        BotInstruction.objects.create(title=title, body=old_body)

        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())

        body = BotInstruction.objects.get(title=title).body
        self.assertNotIn("[OBJHANDLE:", body)
        self.assertIn('"kind":"objhandle"', body)

    @patch("management.services.ig_policy_publication.publish_instruction_policy")
    def test_publish_is_opt_in_and_uses_post_seed_whole_set_cas(self, publish):
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication

        baseline = ensure_test_instruction_publication()
        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())
        publish.assert_not_called()

        # Re-bootstrap after the draft-only seed so the pre-publish set is a
        # reviewed head, as production requires.
        settings_obj = InstagramBotSettings.load()
        settings_obj.active_instruction_publication = None
        settings_obj.save(update_fields=["active_instruction_publication", "updated_at"])
        baseline = ensure_test_instruction_publication()
        publish.return_value = type("Publication", (), {
            "changed": True,
            "publication": type("Head", (), {"version": baseline.version + 1})(),
        })()

        call_command("seed_ig_bot_sales_playbooks", "--publish", stdout=StringIO())
        kwargs = publish.call_args.kwargs
        self.assertEqual(kwargs["expected_head_id"], baseline.publication_id)
        self.assertEqual(kwargs["expected_head_hash"], baseline.snapshot_hash)
        self.assertEqual(kwargs["expected_draft_revision"], settings_obj.instruction_draft_revision)
        self.assertEqual(len(kwargs["expected_draft_hash"]), 64)
        self.assertEqual(kwargs["note"], "seed_ig_bot_sales_playbooks")

    def test_publish_refuses_unreviewed_draft_and_rolls_back_seed(self):
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication

        ensure_test_instruction_publication()
        BotInstruction.objects.create(title="Operator draft", body="unpublished change")

        with self.assertRaises(CommandError):
            call_command("seed_ig_bot_sales_playbooks", "--publish", stdout=StringIO())

        self.assertEqual(BotInstruction.objects.filter(title="IG Core Sales").count(), 0)
        self.assertEqual(BotInstruction.objects.get(title="Operator draft").body, "unpublished change")

    def test_publish_does_not_initialize_a_missing_head(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.active_instruction_publication = None
        settings_obj.save(update_fields=["active_instruction_publication", "updated_at"])

        with self.assertRaises(CommandError):
            call_command("seed_ig_bot_sales_playbooks", "--publish", stdout=StringIO())

        self.assertEqual(BotInstruction.objects.count(), 0)

    def test_owner_payment_seed_upgrades_exact_old_body_without_publishing(self):
        from management.management.commands.seed_ig_bot_sales_playbooks import LEGACY_PLAYBOOK_BODIES
        from management.knowledge.publication_compat import REVIEWED_BODIES
        from management.models import BotPolicyPublication

        old = next(body for body in LEGACY_PLAYBOOK_BODIES["Prepayment Objection"]
                   if body.startswith("Передоплата — індивідуально погоджена сума"))
        row = BotInstruction.objects.create(title="Prepayment Objection", body=old)
        publications = BotPolicyPublication.objects.count()
        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.body, REVIEWED_BODIES["Prepayment Objection"])
        self.assertEqual(BotPolicyPublication.objects.count(), publications)
        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.body, REVIEWED_BODIES["Prepayment Objection"])
        self.assertEqual(BotPolicyPublication.objects.count(), publications)

    def test_owner_payment_seed_preserves_administrator_variation(self):
        from management.management.commands.seed_ig_bot_sales_playbooks import LEGACY_PLAYBOOK_BODIES

        old = next(body for body in LEGACY_PLAYBOOK_BODIES["Prepayment Objection"]
                   if body.startswith("Передоплата — індивідуально погоджена сума"))
        body = old + " Умова адміністратора для окремого випадку."
        row = BotInstruction.objects.create(title="Prepayment Objection", body=body)
        call_command("seed_ig_bot_sales_playbooks", stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.body, body)
