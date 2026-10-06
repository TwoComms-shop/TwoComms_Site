"""Required declarations use the existing draft editor and its authority/CAS."""
from contextlib import contextmanager
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from management.bot_access import (
    EDIT_IG_PROMPT_PERMISSION, META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION,
)
from management.models import AdminAuditLog, BotInstruction, BotPolicyPublication, InstagramBotSettings
from management.services.ig_required_policy import REQUIRED_KEY, SHOOTING_METADATA
from management.tests_ig_policy_helpers import ensure_test_instruction_publication


_DML = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)


@override_settings(
    ALLOWED_HOSTS=["management.twocomms.shop", "testserver"],
    ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
)
class RequiredPolicyEditorApiTests(TestCase):
    host = "management.twocomms.shop"

    def setUp(self):
        BotInstruction.objects.all().delete()
        ensure_test_instruction_publication()
        self.editor_operator = self.principal("required-editor-operator", EDIT_IG_PROMPT_PERMISSION, OPERATE_IG_BOT_PERMISSION)
        self.client.force_login(self.editor_operator)

    def principal(self, name, *capabilities):
        user = get_user_model().objects.create_user(username=name, password="unused-test-password")
        user.user_permissions.add(*(Permission.objects.get(content_type__app_label=capability.split(".", 1)[0],
            codename=capability.split(".", 1)[1]) for capability in capabilities))
        return user

    @contextmanager
    def no_dml(self):
        def guard(execute, sql, params, many, context):
            if _DML.match(sql):
                self.fail("API issued a database write: " + sql.split(None, 1)[0])
            return execute(sql, params, many, context)
        with connection.execute_wrapper(guard):
            yield

    def get(self):
        return self.client.get(reverse("management_bot_kb_api"), HTTP_HOST=self.host, secure=True)

    def state(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        return response.json()

    def post(self, data):
        return self.client.post(reverse("management_bot_kb_save_api"), data,
            HTTP_HOST=self.host, secure=True, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def payload(self, *, state=None, identity=None, **changes):
        state = state or self.state()["policy"]
        values = {"type": "instruction", "title": "Reviewed guidance", "body": "Existing reviewed guidance.",
            "intent_tags": "global", "trigger_codes": "size_question", "is_active": "1",
            "priority": "30", "locale": "uk", "trust_scope": "public_policy", "allowed_actions": "",
            "draft_revision": state["draft_revision"], "draft_hash": state["draft_hash"]}
        if identity is not None:
            values["id"] = identity
        values.update(changes)
        return values

    def create(self, required="ugc,size,size"):
        response = self.post(self.payload(required_for_scenarios=required))
        self.assertEqual(response.status_code, 200)
        return BotInstruction.objects.get(pk=response.json()["id"]), response.json()

    def fingerprint(self):
        return {
            "instructions": list(BotInstruction.objects.order_by("pk").values("pk", "title", "body", "programme_metadata")),
            "settings": list(InstagramBotSettings.objects.order_by("pk").values("pk", "instruction_draft_revision", "active_instruction_publication_id")),
            "publications": list(BotPolicyPublication.objects.order_by("pk").values("pk", "snapshot_hash", "snapshot")),
            "audit": list(AdminAuditLog.objects.order_by("pk").values_list("pk", flat=True)),
        }

    def test_declare_save_get_uses_canonical_list_and_advances_exact_draft_cas(self):
        before = self.state()["policy"]
        response = self.post(self.payload(state=before, required_for_scenarios="ugc,size,size"))
        self.assertEqual(response.status_code, 200)
        saved = response.json()
        self.assertEqual(saved["draft_revision"], before["draft_revision"] + 1)
        self.assertNotEqual(saved["draft_hash"], before["draft_hash"])
        actual = self.state()
        self.assertEqual(actual["policy"]["draft_revision"], saved["draft_revision"])
        self.assertEqual(actual["policy"]["draft_hash"], saved["draft_hash"])
        instruction = next(item for item in actual["instructions"] if item["id"] == saved["id"])
        self.assertEqual(instruction[REQUIRED_KEY], ["size", "ugc"])
        self.assertIs(instruction["required_scenarios_valid"], True)
        self.assertEqual(BotInstruction.objects.get(pk=saved["id"]).programme_metadata, {REQUIRED_KEY: ["size", "ugc"]})

    def test_stale_revision_or_hash_cannot_overwrite_declaration_or_body(self):
        stale = self.state()["policy"]
        instruction, _saved = self.create("size")
        current = self.state()["policy"]
        before = self.fingerprint()
        for state in (stale, {**current, "draft_hash": stale["draft_hash"]}):
            with self.subTest(state=state), self.no_dml():
                response = self.post(self.payload(state=state, identity=instruction.pk,
                    body="Stale replacement", required_for_scenarios="ugc"))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "draft_revision_conflict")
            self.assertEqual(self.fingerprint(), before)

    def test_old_editor_omission_preserves_existing_declaration(self):
        instruction, _saved = self.create("size,ugc")
        response = self.post(self.payload(identity=instruction.pk, body="An ordinary draft edit."))
        self.assertEqual(response.status_code, 200)
        instruction.refresh_from_db()
        self.assertEqual(instruction.body, "An ordinary draft edit.")
        self.assertEqual(instruction.programme_metadata, {REQUIRED_KEY: ["size", "ugc"]})
        self.assertEqual(self.state()["instructions"][0][REQUIRED_KEY], ["size", "ugc"])

    def test_explicit_empty_removes_key_and_get_returns_virtual_empty_list(self):
        instruction, _saved = self.create("size")
        response = self.post(self.payload(identity=instruction.pk, required_for_scenarios=""))
        self.assertEqual(response.status_code, 200)
        instruction.refresh_from_db()
        self.assertEqual(instruction.programme_metadata, {})
        actual = self.state()["instructions"][0]
        self.assertEqual(actual[REQUIRED_KEY], [])
        self.assertIs(actual["required_scenarios_valid"], True)

    def test_invalid_scenario_unknown_code_or_overlong_list_returns_400_zero_writes(self):
        instruction, _saved = self.create("size")
        before = self.fingerprint()
        state = self.state()["policy"]
        for raw in ("money", "Size", "size,arbitrary_customer_text", "size,size,size,size,size,size"):
            with self.subTest(raw=raw), self.no_dml():
                response = self.post(self.payload(state=state, identity=instruction.pk,
                    body="Must not replace", required_for_scenarios=raw))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json().get("code", response.json().get("error")), "invalid_required_scenarios")
            self.assertEqual(self.fingerprint(), before)

    def test_save_changes_draft_without_auto_publishing_or_creating_publication(self):
        before = self.state()["policy"]["active_publication"]
        old_publications = list(BotPolicyPublication.objects.order_by("pk").values("pk", "snapshot", "snapshot_hash"))
        self.create("size")
        actual = self.state()["policy"]
        self.assertEqual(actual["active_publication"], before)
        self.assertTrue(actual["has_unpublished_changes"])
        self.assertEqual(list(BotPolicyPublication.objects.order_by("pk").values("pk", "snapshot", "snapshot_hash")), old_publications)

    def test_actual_api_get_is_zero_dml_and_does_not_bootstrap_missing_settings(self):
        self.create("size")
        self.assertTrue(reverse("management_bot_kb_api").endswith("/bot/api/kb/"))
        for mode in ("existing", "missing_head", "missing_settings"):
            if mode == "missing_head":
                InstagramBotSettings.objects.filter(pk=1).update(active_instruction_publication_id=None)
            elif mode == "missing_settings":
                InstagramBotSettings.objects.all().delete()
            before = self.fingerprint()
            with self.subTest(mode=mode), self.no_dml():
                response = self.get()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["instructions"][0][REQUIRED_KEY], ["size"])
            self.assertEqual(self.fingerprint(), before)
            if mode != "existing":
                self.assertIsNone(response.json()["policy"]["active_publication"])
            if mode == "missing_settings":
                self.assertFalse(InstagramBotSettings.objects.exists())

    def test_declared_shooting_programme_preserves_exact_subset_when_old_editor_omits_required(self):
        response = self.post(self.payload(programme_kind="shooting_prize", required_for_scenarios="ugc"))
        self.assertEqual(response.status_code, 200)
        identity = response.json()["id"]
        self.assertEqual(BotInstruction.objects.get(pk=identity).programme_metadata, {**SHOOTING_METADATA, REQUIRED_KEY: ["ugc"]})
        response = self.post(self.payload(identity=identity, programme_kind="shooting_prize"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BotInstruction.objects.get(pk=identity).programme_metadata, {**SHOOTING_METADATA, REQUIRED_KEY: ["ugc"]})
        actual = self.state()["instructions"][0]
        self.assertEqual(actual[REQUIRED_KEY], ["ugc"])
        self.assertEqual(actual["programme_kind"], "shooting_prize")

    def test_edit_only_and_editor_operator_keep_access_but_operator_only_is_denied(self):
        state = self.state()["policy"]
        operator = self.principal("required-operator-only", OPERATE_IG_BOT_PERMISSION)
        self.client.force_login(operator)
        before = self.fingerprint()
        with self.no_dml():
            read = self.get()
            write = self.post(self.payload(state=state, required_for_scenarios="size"))
        self.assertEqual(read.status_code, 403)
        self.assertEqual(write.status_code, 403)
        self.assertEqual(self.fingerprint(), before)
        editor = self.principal("required-editor-only", EDIT_IG_PROMPT_PERMISSION)
        self.client.force_login(editor)
        instruction, _saved = self.create("size")
        self.assertEqual(self.state()["instructions"][0]["id"], instruction.pk)

    def test_reviewer_with_both_capabilities_is_dominant_deny(self):
        state = self.state()["policy"]
        reviewer = self.principal("required-reviewer", EDIT_IG_PROMPT_PERMISSION, OPERATE_IG_BOT_PERMISSION)
        reviewer.groups.add(Group.objects.get_or_create(name=META_REVIEWER_GROUP_NAME)[0])
        self.client.force_login(reviewer)
        before = self.fingerprint()
        with self.no_dml():
            read = self.get()
            write = self.post(self.payload(state=state, required_for_scenarios="size"))
        self.assertEqual(read.status_code, 403)
        self.assertEqual(write.status_code, 403)
        self.assertEqual(self.fingerprint(), before)
