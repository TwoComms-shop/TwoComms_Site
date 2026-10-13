"""Real source capture and operator permissions through the assisted API."""
from copy import deepcopy
import json
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from management.bot_access import OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION, META_REVIEWER_GROUP_NAME
from management.ig_assisted_commerce_models import IgAssistedCommerceRequest as Request, IgAssistedCommerceDecision as Decision, IgAssistedCommerceExecution as Execution
from management.models import IgClient, IgCheckoutProposal
from management.tests_ig_clients_ui import _operator
from management.tests_ig_restock_subscriptions import _RestockFixture


class _AssistedApiFixture(_RestockFixture):
    def setUp(self):
        super().setUp()
        self.manager = _operator("assisted-api-manager", OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.manager)
        self.revision_row, _token = self.revision(self.choice)
        self.private_root = tempfile.TemporaryDirectory(prefix="twc-assisted-photo-",
            dir=str(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.private_root.cleanup)
        os.chmod(self.private_root.name, 0o700)
        private = override_settings(IG_PRIVATE_MEDIA_ROOT=self.private_root.name)
        private.enable()
        self.addCleanup(private.disable)

    def endpoint(self, client=None):
        return reverse("management_assisted_commerce_api", args=[(client or self.client_row).pk])

    def read(self):
        response = self.client.get(self.endpoint())
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["commerce"]

    def post(self, payload):
        return self.client.post(self.endpoint(), json.dumps(payload), content_type="application/json")

    def produced_request(self):
        source = self.read()["source_cas"]
        self.assertTrue(source["ready"], source)
        payload = {"action": "request", "operation_id": str(uuid.uuid4()),
            **{key: source[key] for key in ("revision_id", "expected_revision_digest", "expected_full_capture_digest")}}
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        return Request.objects.get(pk=response.json()["request_id"]), payload, response.json()["commerce"]

    def draft_items(self, request):
        from management.services.ig_assisted_commerce import _editable_items
        snapshot = request.envelope["snapshot"]
        items = _editable_items(snapshot)
        for item in items:
            item.update(availability="ready", checked_quantity=item["quantity"],
                supply_basis="warehouse_checked", source_location="Manager checked")
            item["question_answers"] = [
                {"question_id": question["question_id"], "state": "confirmed",
                    "answer": "Менеджер перевірив потрібні параметри цієї позиції."}
                for question in snapshot["questions"] if question["line_id"] == item["line_id"]]
        return items


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class AssistedCommerceApiTests(_AssistedApiFixture, TestCase):
    def test_native_request_and_draft_approve_cas_replay_have_no_provider_io(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, original, context = self.produced_request()
            replay = self.post(original)
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertFalse(replay.json()["created"])
            self.assertEqual(Request.objects.count(), 1)
            draft = {"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
                **context["cas"], "items": self.draft_items(request)}
            response = self.post(draft)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(self.post(draft).status_code, 200)
            self.assertEqual(Decision.objects.count(), 1)
            approved = {"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
                **response.json()["commerce"]["cas"]}
            result = self.post(approved)
            self.assertEqual(result.status_code, 200, result.content)
            self.assertEqual(result.json()["commerce"]["approved_head"]["status"], "approved")
            self.assertEqual(self.post(approved).status_code, 200)
            self.assertEqual(Decision.objects.count(), 2)
            changed = deepcopy(approved);changed["action"] = "reject";changed["rejection_reason"] = "Changed replay"
            self.assertEqual(self.post(changed).status_code, 409)
            self.assertEqual(Decision.objects.count(), 2)
            provider.assert_not_called()

    def test_raw_source_rows_nonfinite_duplicate_keys_and_foreign_request_do_not_write(self):
        request, original, context = self.produced_request()
        bad = {**original, "operation_id": str(uuid.uuid4()), "sources": [{"text": "I buy"}]}
        self.assertEqual(self.post(bad).status_code, 400)
        for body in ('{"action":"request","action":"approve"}', '{"quantity":NaN}'):
            self.assertEqual(self.client.post(self.endpoint(), body, content_type="application/json").status_code, 400)
        foreign = IgClient.objects.create(igsid="assisted-api-foreign")
        body = {"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk), **context["cas"]}
        self.assertEqual(self.client.post(self.endpoint(foreign), json.dumps(body), content_type="application/json").status_code, 404)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Decision.objects.count(), 0)

    def test_stale_source_and_draft_cas_cannot_silently_replace_current_terms(self):
        request, original, context = self.produced_request()
        payload = {"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **context["cas"], "items": self.draft_items(request)}
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        stale = {"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk), **context["cas"]}
        self.assertEqual(self.post(stale).status_code, 409)
        self.assertEqual(Decision.objects.count(), 1)
        self.canonical("Ні, хочу розмір L")
        replay = self.post(original)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(Request.objects.count(), 1)

    def test_permissions_csrf_and_erasure_block_native_case_without_provider(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, _original, context = self.produced_request()
            payload = {"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk), **context["cas"]}
            viewer = _operator("assisted-view-only", VIEW_IG_CONVERSATION_PII_PERMISSION)
            reviewer = _operator("assisted-meta-reviewer", OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
            reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
            for actor in (viewer, reviewer):
                self.client.force_login(actor)
                self.assertEqual(self.client.get(self.endpoint()).status_code, 403)
                self.assertEqual(self.post(payload).status_code, 403)
            strict = Client(enforce_csrf_checks=True, HTTP_HOST="management.twocomms.shop")
            strict.force_login(self.manager)
            self.assertEqual(strict.post(self.endpoint(), json.dumps(payload), content_type="application/json").status_code, 403)
            self.client.force_login(self.manager)
            self.client_row.privacy_erasure_started_at = self.now
            self.client_row.save(update_fields=["privacy_erasure_started_at"])
            self.assertEqual(self.client.get(self.endpoint()).status_code, 410)
            self.assertEqual(self.post(payload).status_code, 410)
            self.assertEqual(Decision.objects.count(), 0)
            provider.assert_not_called()

    def test_operator_business_capture_remains_available_while_bot_is_paused(self):
        self.settings.is_enabled = False
        self.settings.save(update_fields=["is_enabled"])
        request, _payload, context = self.produced_request()
        self.assertEqual(context["status"], "current")
        self.assertFalse(context["bot_effects_allowed"])
        self.assertEqual(request.client_id, self.client_row.pk)

    def test_manager_quote_approval_without_native_purchase_does_not_create_proposal(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, _original, context = self.produced_request()
            draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()),
                "request_id": str(request.pk), **context["cas"], "items": self.draft_items(request)})
            self.assertEqual(draft.status_code, 200, draft.content)
            approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()),
                "request_id": str(request.pk), **draft.json()["commerce"]["cas"]})
            self.assertEqual(approved.status_code, 200, approved.content)
            decision = Decision.objects.get(pk=approved.json()["decision_id"])
            before = (IgCheckoutProposal.objects.count(), Execution.objects.count())
            result = self.post({"action": "prepare", "operation_id": str(uuid.uuid4()),
                "request_id": str(request.pk), "decision_id": str(decision.pk),
                "expected_request_digest": request.envelope_hmac,
                "expected_decision_digest": decision.envelope_hmac})
            self.assertEqual(result.status_code, 409, result.content)
            self.assertIn(result.json()["checkout"]["reason"], {"customer_purchase_required", "checkout_purchase_authority_missing"})
            self.assertEqual((IgCheckoutProposal.objects.count(), Execution.objects.count()), before)
            provider.assert_not_called()
