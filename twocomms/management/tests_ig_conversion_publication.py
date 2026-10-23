from django.test import TestCase

from management.models import BotInstruction, InstagramBotSettings
from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
from management.services.ig_policy_publication import (
    PolicyPublicationError, draft_state, publish_instruction_policy, save_instruction_draft,
)


class OwnerScenarioPublicationTests(TestCase):
    def setUp(self):
        InstagramBotSettings.load()
        self.existing = BotInstruction.objects.create(title="Existing unrelated policy", body="Keep this policy.", is_active=True)
        state = draft_state()
        publish_instruction_policy(expected_draft_revision=state.revision, expected_draft_hash=state.snapshot_hash,
            expected_head_id=None, expected_head_hash="", note="disposable baseline")

    def test_dry_run_does_not_publish_and_apply_preserves_existing_then_is_idempotent(self):
        before = BotInstruction.objects.count()
        plan = publish_reviewed_owner_scenarios()
        self.assertEqual(BotInstruction.objects.count(), before)
        self.assertEqual([item["action"] for item in plan["changes"]], ["create"] * 8)
        result = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=plan["draft_hash"], expected_head_hash=plan["head_hash"])
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.body, "Keep this policy.")
        self.assertEqual(len(result["coverage"]), 8)
        for scenario in ("custom_print", "collaboration", "ugc", "size", "objection"):
            self.assertEqual(result["coverage"][scenario]["status"], "declared_covered")
        for language in ("uk", "ru", "en"):
            self.assertTrue(result["coverage"]["business_cooperation_" + language]["required_id"])
        again = publish_reviewed_owner_scenarios()
        replay = publish_reviewed_owner_scenarios(apply=True,
            expected_draft_hash=again["draft_hash"], expected_head_hash=again["head_hash"])
        self.assertEqual(replay["head_id"], result["head_id"])
        self.assertFalse(replay["changed"])

    def test_unrelated_draft_is_rejected_without_saving_managed_rows(self):
        state = draft_state()
        save_instruction_draft(expected_revision=state.revision, expected_snapshot_hash=state.snapshot_hash,
            instruction_id=self.existing.pk, values={"title": self.existing.title, "body": "Other owner's pending draft.",
                "active": True, "priority": 100, "locale": "all", "tags": [], "triggers": [],
                "programme_metadata": {}, "allowed_actions": [], "trust_scope": "public_policy"})
        with self.assertRaises(PolicyPublicationError) as raised:
            publish_reviewed_owner_scenarios()
        self.assertEqual(raised.exception.code, "unrelated_instruction_draft_changes")
        self.assertEqual(BotInstruction.objects.count(), 1)

    def test_stale_hash_cannot_apply(self):
        plan = publish_reviewed_owner_scenarios()
        with self.assertRaises(PolicyPublicationError) as raised:
            publish_reviewed_owner_scenarios(apply=True, expected_draft_hash="0" * 64,
                expected_head_hash=plan["head_hash"])
        self.assertEqual(raised.exception.code, "owner_publication_cas_changed")
        self.assertEqual(BotInstruction.objects.count(), 1)
