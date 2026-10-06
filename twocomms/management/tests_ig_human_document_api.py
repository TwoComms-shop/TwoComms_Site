"""Private document API scoping, read purity, CAS and committed draft submit."""
from datetime import timedelta
import hashlib
import json
import os
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection, transaction
from django.http import HttpResponse
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path, reverse
from django.utils import timezone

from management.bot_access import META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
from management import bot_human_document_views as api
from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPrivateDocument
from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply_delivery import create_private_document


urlpatterns = [
    path("login/", lambda request: HttpResponse("login"), name="management_login"),
    # Management runs at the subdomain root; its real /bot/api/ prefix bypasses
    # storefront analytics writes while leaving the production middleware active.
    path("bot/api/clients/<int:client_id>/documents/", api.bot_human_documents_api, name="documents"),
    path("bot/api/clients/<int:client_id>/documents/create/", api.bot_human_document_create_api, name="create"),
    path("bot/api/clients/<int:client_id>/documents/<uuid:document_id>/", api.bot_human_document_detail_api, name="detail"),
    path("bot/api/clients/<int:client_id>/documents/<uuid:document_id>/update/", api.bot_human_document_update_api, name="update"),
    path("bot/api/clients/<int:client_id>/documents/<uuid:document_id>/submit/", api.bot_human_document_submit_api, name="submit"),
]


@override_settings(ROOT_URLCONF="management.tests_ig_human_document_api", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False)
class HumanDocumentApiTests(TransactionTestCase):
    def test_final_private_read_fences_erasure_or_hide_after_owner_lookup(self):
        document = self.document(text="private plaintext must never escape erasure")
        original = api._authorised_owner
        for field in ("privacy_erasure_started_at", "hidden_at"):
            for endpoint in ("documents", "detail"):
                with self.subTest(field=field, endpoint=endpoint):
                    IgClient.objects.filter(pk=self.customer.pk).update(
                        privacy_erasure_started_at=None, hidden_at=None)

                    def concurrent_fence(request, client_id):
                        owner = original(request, client_id)
                        # Simulate another committed actor after the initial
                        # owner SELECT, before the private document SELECT.
                        IgClient.objects.filter(pk=owner.pk).update(**{field: timezone.now()})
                        return owner

                    with patch.object(api, "_authorised_owner", side_effect=concurrent_fence):
                        response = self.client.get(self.url(endpoint, document if endpoint == "detail" else None))
                    self.assertNotIn(document.text, response.content.decode())
                    if endpoint == "detail":
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(response.json()["code"], "private_document_missing")
                    else:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["documents"], [])

    def setUp(self):
        env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.actor = get_user_model().objects.create_user(username="private-document-owner")
        self.grant(self.actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(self.actor)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="owner-1", page_id="owner-1")
        self.customer = IgClient.objects.create(igsid="synthetic-private-document-customer")
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", status="done",
            text="synthetic customer context", mid="synthetic-doc-inbound", provider_created_at=timezone.now() - timedelta(minutes=2))

    @staticmethod
    def grant(actor, *names):
        actor.user_permissions.add(*(Permission.objects.get(content_type__app_label=name.split(".")[0],
            codename=name.split(".")[1]) for name in names))

    def url(self, name, document=None, client_id=None):
        kwargs = {"client_id": self.customer.pk if client_id is None else client_id}
        if document is not None:
            kwargs["document_id"] = document.document_id if hasattr(document, "document_id") else document
        return reverse(name, kwargs=kwargs)

    def post(self, name, body, document=None, **kwargs):
        return self.client.post(self.url(name, document, **kwargs), json.dumps(body), content_type="application/json")

    def document(self, kind="reply_draft", text="synthetic saved manager text", actor=None):
        return create_private_document(self.customer.pk, actor=actor or self.actor, kind=kind, text=text,
            context_message_id=self.source.pk, provider_namespace=self.source.provider_namespace,
            document_id=uuid.uuid4(), expected_permission_epoch=self.customer.reply_permission_epoch)

    def test_each_capability_and_reviewer_denied_before_document_service(self):
        with patch("management.services.ig_human_reply_delivery.create_private_document") as create:
            for index, permissions in enumerate(((), (OPERATE_IG_BOT_PERMISSION,), (VIEW_IG_CONVERSATION_PII_PERMISSION,))):
                actor = get_user_model().objects.create_user(username=f"document-missing-cap-{index}", is_staff=True)
                self.grant(actor, *permissions)
                self.client.force_login(actor)
                self.assertEqual(self.client.get(self.url("documents")).status_code, 403)
                self.assertEqual(self.post("create", {}).status_code, 403)
            reviewer = get_user_model().objects.create_superuser(username="private-reviewer", password="synthetic")
            reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
            self.client.force_login(reviewer)
            self.assertEqual(self.client.get(self.url("documents")).status_code, 403)
            self.assertEqual(self.post("create", {}).status_code, 403)
        create.assert_not_called()
        self.client.logout()
        self.assertEqual(self.client.get(self.url("documents")).status_code, 302)

    def test_note_save_and_update_never_take_over_send_or_change_client_activity(self):
        original = IgClient.objects.values().get(pk=self.customer.pk)
        document_id = str(uuid.uuid4())
        with patch("management.services.ig_human_reply.dispatch_human_reply_command") as dispatch, patch(
            "management.services.instagram_bot.send_text") as send, patch(
            "management.services.call_ai_analysis.gemini_generate_text") as generation, patch(
            "management.models.InstagramBotSettings.load") as bootstrap:
            response = self.post("create", {"document_id": document_id, "kind": "internal_note",
                "text": "  synthetic private note  ", "context_message_id": self.source.pk})
            self.assertEqual(response.status_code, 200)
            saved = response.json()["document"]
            self.assertEqual(saved["text_hash"], hashlib.sha256(b"synthetic private note").hexdigest())
            updated = self.post("update", {"expected_version": saved["version"], "expected_hash": saved["text_hash"],
                "text": "synthetic updated note"}, document_id)
            self.assertEqual(updated.status_code, 200)
            for producer in (dispatch, send, generation, bootstrap):
                producer.assert_not_called()
        self.assertEqual(original, IgClient.objects.values().get(pk=self.customer.pk))
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_create_identity_replay_and_permission_revocation_are_rechecked_by_service(self):
        body = {"document_id": str(uuid.uuid4()), "kind": "internal_note", "text": "synthetic saved note",
            "context_message_id": self.source.pk}
        first = self.post("create", body)
        second = self.post("create", body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["document"]["document_id"], second.json()["document"]["document_id"])
        self.assertEqual(HumanReplyPrivateDocument.objects.count(), 1)
        self.assertEqual(self.post("create", {**body, "text": "conflicting replay"}).status_code, 409)

        def revoked(*args, **kwargs):
            self.actor.user_permissions.clear()
            return create_private_document(*args, **kwargs)

        with patch("management.services.ig_human_reply_delivery.create_private_document", side_effect=revoked):
            denied = self.post("create", {**body, "document_id": str(uuid.uuid4())})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(HumanReplyPrivateDocument.objects.count(), 1)

    def test_get_list_detail_are_bounded_no_cache_select_only_without_producers(self):
        doc = self.document()
        other = get_user_model().objects.create_user(username="private-other-manager")
        self.grant(other, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.document(actor=other, text="synthetic other actor text")
        self.assertTrue(self.url("documents").startswith("/bot/api/clients/"))
        self.assertTrue(self.url("detail", doc).startswith("/bot/api/clients/"))
        with patch("management.services.ig_human_reply_delivery.create_private_document") as create, patch(
            "management.services.ig_human_reply_delivery.update_private_document") as update, patch(
            "management.models.InstagramBotSettings.load") as bootstrap, patch(
            "management.services.ig_human_reply.dispatch_human_reply_command") as dispatch, CaptureQueriesContext(connection) as queries:
            listing = self.client.get(self.url("documents"), {"limit": "1"})
            detail = self.client.get(self.url("detail", doc))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["documents"]), 1)
        self.assertNotIn("text", listing.json()["documents"][0])
        self.assertEqual(detail.json()["document"]["text"], doc.text)
        self.assertIn("no-store", detail.headers["Cache-Control"])
        for producer in (create, update, bootstrap, dispatch):
            producer.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries), [row["sql"] for row in queries])

    def test_actor_client_and_filter_scope_do_not_leak_documents(self):
        doc = self.document()
        other = get_user_model().objects.create_user(username="private-reader-other")
        self.grant(other, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url("detail", doc)).status_code, 404)
        self.assertEqual(self.client.get(self.url("documents")).json()["documents"], [])
        self.assertEqual(self.post("update", {"expected_version": 1, "expected_hash": doc.text_hash, "text": "other actor"}, doc).status_code, 404)
        self.client.force_login(self.actor)
        foreign = IgClient.objects.create(igsid="synthetic-document-foreign-owner")
        self.assertEqual(self.client.get(self.url("detail", doc, client_id=foreign.pk)).status_code, 404)
        for query in ({"limit": "51"}, {"limit": "01"}, {"before_id": "bad"}, {"state": "deleted"}, {"kind": "recipient"}, {"actor_id": other.pk}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url("documents"), query).status_code, 400)

    def test_two_tabs_cas_archive_and_unknown_fields_fail_closed(self):
        doc = self.document()
        original = {"expected_version": doc.version, "expected_hash": doc.text_hash}
        self.assertEqual(self.post("update", {**original, "text": "first tab"}, doc).status_code, 200)
        self.assertEqual(self.post("update", {**original, "text": "stale tab"}, doc).status_code, 409)
        doc.refresh_from_db()
        self.assertEqual(self.post("update", {"expected_version": doc.version, "expected_hash": doc.text_hash, "archive": True}, doc).status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.state, "archived")
        self.assertEqual(self.post("update", {"expected_version": doc.version, "expected_hash": doc.text_hash, "text": "closed"}, doc).status_code, 409)
        self.assertEqual(self.post("create", {"recipient_igsid": "synthetic override"}).status_code, 400)

    def test_privacy_erasure_and_deleted_client_prevent_reads_or_resurrection(self):
        doc = self.document()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        with patch("management.services.ig_human_reply_delivery.create_private_document") as create:
            self.assertEqual(self.client.get(self.url("detail", doc)).status_code, 404)
            self.assertEqual(self.post("create", {}).status_code, 404)
        create.assert_not_called()
        owner_id = self.customer.pk
        IgClient.objects.filter(pk=owner_id).delete()
        self.assertEqual(self.client.get(self.url("documents", client_id=owner_id)).status_code, 404)
        self.assertFalse(HumanReplyPrivateDocument.objects.filter(client_id=owner_id).exists())

    def test_regular_csrf_protection_and_method_guards(self):
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.actor)
        self.assertEqual(protected.post(self.url("create"), {}).status_code, 403)
        self.assertEqual(self.client.get(self.url("create")).status_code, 405)
        self.assertEqual(self.client.post(self.url("documents"), {}).status_code, 405)

    def test_note_submit_never_invokes_dispatch_or_takeover(self):
        note = self.document(kind="internal_note")
        with patch("management.services.ig_human_reply.dispatch_human_reply_command") as dispatch:
            response = self.post("submit", {"operation_id": str(uuid.uuid4()), "expected_version": note.version,
                "expected_hash": note.text_hash}, note)
        self.assertEqual(response.status_code, 409)
        dispatch.assert_not_called()
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)

    def test_submit_uses_server_text_after_commit_and_same_operation_retry_is_idempotent(self):
        doc = self.document()
        request_body = {"operation_id": str(uuid.uuid4()), "expected_version": doc.version, "expected_hash": doc.text_hash}
        dispatched = []

        def dispatcher(command_id):
            self.assertFalse(connection.in_atomic_block)
            command = HumanReplyCommand.objects.get(pk=command_id)
            self.assertEqual(command.text, "synthetic saved manager text")
            dispatched.append(command_id)
            return SimpleNamespace(pk=command.pk, operation_id=command.operation_id, state="sent", failure_code="", State=command.State)

        with patch("management.services.ig_human_reply.dispatch_human_reply_command", side_effect=dispatcher), patch(
            "management.services.instagram_bot.send_text") as transport:
            first = self.post("submit", request_body, doc)
            second = self.post("submit", request_body, doc)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(dispatched[0], dispatched[1])
        self.assertEqual(HumanReplyCommand.objects.count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").count(), 1)
        doc.refresh_from_db()
        self.assertEqual(doc.state, "consumed")
        transport.assert_not_called()
        self.assertEqual(self.post("submit", {**request_body, "text": "browser replacement"}, doc).status_code, 400)

    def test_submit_rejects_api_outer_transaction_before_consumption(self):
        doc = self.document()
        with transaction.atomic(), patch("management.services.ig_human_reply.create_human_reply_command_from_draft") as consume:
            response = self.post("submit", {"operation_id": str(uuid.uuid4()), "expected_version": 1,
                "expected_hash": doc.text_hash}, doc)
        self.assertEqual(response.status_code, 503)
        consume.assert_not_called()
        self.assertFalse(HumanReplyCommand.objects.exists())

    def test_dispatch_failure_keeps_committed_command_identity_and_source_text_private(self):
        doc = self.document()
        op = str(uuid.uuid4())
        with patch("management.services.ig_human_reply.dispatch_human_reply_command", side_effect=RuntimeError("synthetic saved manager text")):
            response = self.post("submit", {"operation_id": op, "expected_version": doc.version, "expected_hash": doc.text_hash}, doc)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["operation_id"], op)
        self.assertNotIn("synthetic saved manager text", response.content.decode())
        self.assertEqual(HumanReplyCommand.objects.count(), 1)
        doc.refresh_from_db()
        self.assertEqual(doc.state, "consumed")
