"""Independent hash identities and strictly read-only publication readiness."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from management.bot_access import EDIT_IG_PROMPT_PERMISSION
from management.models import BotPolicyPublication, BotPromptRevision, InstagramBotSettings
from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY, CORE_POLICY_SHA256, CORE_POLICY_VERSION, core_policy_hash
from management.services.ig_policy_parity import read_policy_parity
from management.tests_ig_policy_helpers import ensure_test_instruction_publication


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False)
class PolicyParityTests(TestCase):
    def setUp(self):
        self.row = InstagramBotSettings.load()
        ensure_test_instruction_publication()
        self.row.refresh_from_db()

    def test_separate_identities_match_actual_request_metadata_without_writes(self):
        from management.services.instagram_bot import _assemble_system_instruction
        from management.services.ig_policy_publication import load_active_policy_snapshot
        with CaptureQueriesContext(connection) as queries, patch(
            "management.services.call_ai_analysis.gemini_generate_text") as provider:
            parity = read_policy_parity()
        provider.assert_not_called()
        metadata = {}
        _assemble_system_instruction(self.row, compiled_metadata=metadata,
            instruction_publication=load_active_policy_snapshot(settings_obj=self.row))
        self.assertTrue(parity["ready"], parity["readiness"])
        self.assertTrue(all(item["sql"].lstrip().upper().startswith("SELECT") for item in queries), [item["sql"] for item in queries])
        self.assertEqual(parity["core"]["canonical_raw_hash"], CORE_POLICY_SHA256)
        self.assertEqual(parity["core"]["effective_hash"], metadata["core"]["prompt_hash"])
        self.assertEqual(parity["knowledge"]["uk"]["content_hash"], metadata["knowledge_hash"])
        self.assertEqual(parity["instruction_publication"]["snapshot_hash"], metadata["instruction_publication"]["hash"])
        self.assertNotEqual(parity["instruction_publication"]["snapshot_hash"], parity["core"]["canonical_raw_hash"])
        self.assertEqual(set(parity["facts"]), {"uk","ru","en"})

    def test_same_version_label_with_different_core_body_reports_actual_diff(self):
        BotPromptRevision.objects.create(target="system_prompt",target_id=1,title=CORE_POLICY_VERSION,
            body="different reviewed core", kind="edit")
        self.row.system_prompt = "different reviewed core"
        self.row.save(update_fields=["system_prompt"])
        parity = read_policy_parity()
        self.assertFalse(parity["core"]["matches_raw"])
        self.assertFalse(parity["core"]["matches_effective"])
        self.assertEqual(parity["core"]["effective_version"], "custom")
        self.assertEqual(parity["core"]["raw_hash"], core_policy_hash(self.row.system_prompt))
        self.assertIn("+different reviewed core", parity["core"]["diff"])

    def test_raw_whitespace_and_effective_compiler_hash_are_distinct(self):
        self.row.system_prompt = " \n"+CANONICAL_IG_CORE_POLICY+"\n "
        self.row.save(update_fields=["system_prompt"])
        parity = read_policy_parity()
        self.assertFalse(parity["core"]["matches_raw"])
        self.assertTrue(parity["core"]["matches_effective"])
        self.assertNotEqual(parity["core"]["raw_hash"], parity["core"]["effective_hash"])

    def test_missing_or_corrupt_publication_is_not_ready(self):
        self.row.active_instruction_publication = None
        self.row.save(update_fields=["active_instruction_publication"])
        missing = read_policy_parity()
        self.assertFalse(missing["ready"])
        self.assertIn("active_publication_missing", missing["readiness"])
        ensure_test_instruction_publication()
        self.row.refresh_from_db()
        table = connection.ops.quote_name(BotPolicyPublication._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET snapshot_hash=%s WHERE id=%s", ["0"*64,self.row.active_instruction_publication_id])
        corrupt = read_policy_parity()
        self.assertFalse(corrupt["ready"])
        self.assertIn("active_publication_hash_mismatch", corrupt["readiness"])

    def test_facts_hash_changes_even_if_version_label_is_unchanged(self):
        from management.services import approved_public_facts
        before = read_policy_parity()["facts"]["uk"]
        with patch.object(approved_public_facts,"ORDINARY_DISPATCH_WINDOW_DAYS",(2,4)):
            after = read_policy_parity()["facts"]["uk"]
        self.assertEqual(before["version"],after["version"])
        self.assertNotEqual(before["content_hash"],after["content_hash"])

    def test_ui_missing_settings_does_not_create_row_or_history(self):
        InstagramBotSettings.objects.all().delete()
        actor = get_user_model().objects.create_user(username="parity-editor")
        actor.user_permissions.add(Permission.objects.get(content_type__app_label="management",
            codename=EDIT_IG_PROMPT_PERMISSION.split(".")[1]))
        self.client.force_login(actor)
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            response = self.client.get(reverse("management_bot_kb_api"))
            history = self.client.get(reverse("management_bot_policy_history_api"))
        provider.assert_not_called()
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()["policy"]["ready"])
        self.assertEqual(history.status_code,200)
        self.assertFalse(InstagramBotSettings.objects.exists())
        self.assertFalse(BotPromptRevision.objects.exists())

    def test_kb_ui_validates_corrupt_head_instead_of_head_existence(self):
        actor = get_user_model().objects.create_user(username="parity-corrupt-editor")
        actor.user_permissions.add(Permission.objects.get(content_type__app_label="management",
            codename=EDIT_IG_PROMPT_PERMISSION.split(".")[1]))
        self.client.force_login(actor)
        table = connection.ops.quote_name(BotPolicyPublication._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET snapshot_hash=%s WHERE id=%s",["0"*64,self.row.active_instruction_publication_id])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("management_bot_kb_api"))
        self.assertEqual(response.status_code,200)
        policy = response.json()["policy"]
        self.assertIsNotNone(policy["active_publication"])
        self.assertFalse(policy["ready"])
        self.assertEqual(policy["readiness_code"], "active_publication_hash_mismatch")
        verbs = [item["sql"].lstrip().split(None, 1)[0].upper() for item in queries]
        self.assertTrue(set(verbs) <= {"SELECT", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"}, verbs)
        writes = [item["sql"] for item in queries if item["sql"].lstrip().split(None, 1)[0].upper()
            in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "TRUNCATE"}]
        self.assertEqual(writes, [])
