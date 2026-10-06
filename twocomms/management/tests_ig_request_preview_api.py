"""Narrow authenticated preview route; transport and producers stay unused."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.http import HttpResponse
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path, reverse
from django.utils import timezone

from management.bot_access import (
    META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
)
from management.bot_request_preview_views import bot_request_preview_api
from management.models import GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurnRevision, InstagramBotSettings
from management.services.gemini_accounting_runtime import revision_request_execution
from management.services.ig_request_manifest import capture_dispatch_context, capture_request_context
from management.tests_ig_request_context_manifest import policy
from management import tests_ig_revision_live as live_fixture


urlpatterns = [
    path("login/", lambda request: HttpResponse("login"), name="management_login"),
    path("bot/api/clients/<int:client_id>/turn-revisions/<int:revision_id>/request-preview/",
        bot_request_preview_api, name="management_bot_request_preview_api"),
]


@override_settings(ROOT_URLCONF="management.tests_ig_request_preview_api", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class RequestPreviewApiTests(TransactionTestCase):
    def setUp(self):
        self.case = live_fixture.RevisionLiveTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._prepare()
        self.actor = get_user_model().objects.create_user(username="request-preview-operator")
        self.grant(self.actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(self.actor)
        payload = {"contents": [{"role": "user", "parts": [{"text": "synthetic private prompt"}]}]}
        self.context = capture_request_context(payload=payload, metadata={
            "revision_id": self.case.revision.pk, "client_id": self.case.customer.pk,
            "source_message_ids": [self.case.source.pk], "bundle_digest": self.case.revision.snapshot_digest,
            "builder_version": "ig-turn-intelligence.v1", "effective_mode": "unified",
            "selected_block_ids": ["history"], "view_versions": {"memory_head_version": "head:8"},
        })
        execution = f"ig-revision:{self.case.revision.pk}"
        with revision_request_execution(self.case.revision.pk, self.case.token,
            settings_id=self.case.settings.pk, settings_permission_epoch=self.case.settings.reply_permission_epoch):
            self.graph = GeminiRequest.objects.create(
                request_id="synthetic-preview-request", client_id=self.case.customer.pk,
                source_message_id=self.case.source.pk, lane="live", task_class="ordinary_live",
                logical_turn_id=execution, source_execution_key=execution, accounting_mode="shadow",
                policy_manifest={**policy(), "request_context": self.context},
            )
        self.attempt = GeminiRequestAttempt.objects.create(
            request_graph=self.graph, request_id=self.graph.request_id, role="chat", key_name="synthetic-key",
            client_id=self.graph.client_id, source_message_id=self.graph.source_message_id,
            logical_turn_id=execution, lane="live", model="gemini-fallback", attempt_index=1,
            candidate_index=1, outcome="failed", fsm_state="failed", http_code=503,
            provider_started_at=timezone.now(), dispatch_pacific_day=timezone.localdate(),
            dispatch_manifest=capture_dispatch_context(payload=b'{"contents": []}', context=self.context,
                attempt_index=1, model="gemini-fallback"),
        )
        self.graph.terminal_resolution = "failed"
        self.graph.terminal_reason = "provider_outage"
        self.graph.save(update_fields=["terminal_resolution", "terminal_reason", "updated_at"])

    @staticmethod
    def grant(actor, *permissions):
        actor.user_permissions.add(*(Permission.objects.get(
            content_type__app_label=dotted.split(".")[0], codename=dotted.split(".")[1],
        ) for dotted in permissions))

    def url(self, *, client_id=None, revision_id=None):
        return reverse("management_bot_request_preview_api", kwargs={
            "client_id": self.case.customer.pk if client_id is None else client_id,
            "revision_id": self.case.revision.pk if revision_id is None else revision_id,
        })

    def test_anonymous_redirect_and_each_capability_boundary_before_reader(self):
        self.client.logout()
        with patch("management.services.ig_request_manifest.actual_request_preview") as reader:
            self.assertEqual(self.client.get(self.url()).status_code, 302)
            for index, permissions in enumerate(((), (OPERATE_IG_BOT_PERMISSION,), (VIEW_IG_CONVERSATION_PII_PERMISSION,))):
                actor = get_user_model().objects.create_user(username=f"preview-capability-{index}", is_staff=True)
                self.grant(actor, *permissions)
                self.client.force_login(actor)
                self.assertEqual(self.client.get(self.url()).status_code, 403)
            reader.assert_not_called()

    def test_reviewer_dominant_deny_even_for_superuser(self):
        reviewer = get_user_model().objects.create_superuser(username="preview-reviewer", password="synthetic-password")
        reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
        self.client.force_login(reviewer)
        with patch("management.services.ig_request_manifest.actual_request_preview") as reader:
            self.assertEqual(self.client.get(self.url()).status_code, 403)
        reader.assert_not_called()

    def test_inactive_principal_cannot_reach_reader(self):
        actor = get_user_model().objects.create_user(username="inactive-preview", is_active=False)
        self.grant(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(actor)
        with patch("management.services.ig_request_manifest.actual_request_preview") as reader:
            self.assertIn(self.client.get(self.url()).status_code, (302, 403))
        reader.assert_not_called()

    def test_get_only_no_cache_and_default_protected_digest_suppression(self):
        self.assertEqual(self.client.post(self.url()).status_code, 405)
        response = self.client.get(self.url(), {"include_protected": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        data = response.json()
        self.assertEqual(data["mode"], "actual_request")
        self.assertEqual(data["terminal_resolution"], "failed")
        self.assertEqual(data["attempts"][0]["http_code"], 503)
        self.assertEqual(data["attempts"][0]["dispatch_manifest"]["payload_stage"], "http_dispatch")
        for key in ("request_digest", "context_digest", "bundle_digest"):
            self.assertNotIn(key, data["manifest"]["request_context"])
        self.assertNotIn("request_digest", data["attempts"][0]["dispatch_manifest"])
        self.assertNotIn("logical_request_digest", data["attempts"][0]["dispatch_manifest"])
        self.assertNotIn("synthetic private prompt", response.content.decode())

    def test_historical_preview_ignores_changed_customer_and_current_policy(self):
        IgClient.objects.filter(pk=self.case.customer.pk).update(language="en")
        InstagramBotSettings.objects.filter(pk=self.case.settings.pk).update(system_prompt="synthetic different current policy")
        data = self.client.get(self.url()).json()
        self.assertEqual(data["manifest"]["content_hash"], "b" * 64)
        self.assertEqual(data["manifest"]["request_context"]["view_versions"]["memory_head_version"], "head:8")
        self.assertEqual(data["reconstruction"], "not_reconstructable")

    def test_successful_actual_fallback_uses_existing_winner_evidence(self):
        GeminiRequestAttempt.objects.filter(pk=self.attempt.pk).update(
            fsm_state="succeeded", outcome="succeeded", winner_claimed=True,
        )
        self.attempt.refresh_from_db()
        self.graph.winner_attempt = self.attempt
        self.graph.terminal_resolution = "succeeded"
        self.graph.terminal_reason = ""
        self.graph.save(update_fields=["winner_attempt", "terminal_resolution", "terminal_reason", "updated_at"])
        data = self.client.get(self.url()).json()
        self.assertEqual(data["actual_model"], "gemini-fallback")
        self.assertTrue(data["attempts"][0]["winner"])

    def test_missing_foreign_and_invalid_identity_never_select_current_graph(self):
        for kwargs, status, reason in (
            ({"client_id": self.case.customer.pk + 999}, 404, "request_missing"),
            ({"revision_id": self.case.revision.pk + 999}, 404, "request_missing"),
            ({"revision_id": 0}, 400, "invalid_identity"),
            ({"client_id": 2**63}, 400, "invalid_identity"),
        ):
            with self.subTest(kwargs=kwargs):
                response = self.client.get(self.url(**kwargs))
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["reason"], reason)
                self.assertEqual(response.json()["manifest"], {})

    def test_fenced_and_deleted_owner_report_not_reconstructable(self):
        IgClient.objects.filter(pk=self.case.customer.pk).update(privacy_erasure_started_at=timezone.now())
        data = self.client.get(self.url()).json()
        self.assertEqual(data["reason"], "privacy_erasure")
        self.assertEqual(data["manifest"], {})
        IgClient.objects.filter(pk=self.case.customer.pk).delete()
        data = self.client.get(self.url()).json()
        self.assertEqual(data["reason"], "owner_missing")
        self.assertEqual(data["attempts"], [])

    def test_ambiguous_lookup_is_bounded_and_does_not_invoke_reader(self):
        query = MagicMock()
        query.only.return_value.order_by.return_value.__getitem__.return_value = [self.graph, self.graph]
        with patch("management.models.GeminiRequest.objects.filter", return_value=query) as lookup, patch(
            "management.services.ig_request_manifest.actual_request_preview") as reader:
            response = self.client.get(self.url())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "request_ambiguous")
        reader.assert_not_called()
        query.only.return_value.order_by.return_value.__getitem__.assert_called_once_with(slice(None, 2))
        self.assertEqual(lookup.call_args.kwargs["client_id"], self.case.customer.pk)
        self.assertEqual(lookup.call_args.kwargs["source_execution_key"], f"ig-revision:{self.case.revision.pk}")

    def test_repeated_gets_are_select_only_and_call_no_mutating_producers(self):
        models = (GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurnRevision, InstagramBotSettings)
        before = [model.objects.count() for model in models]
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, patch(
            "management.models.InstagramBotSettings.load") as bootstrap, patch(
            "management.services.bot_memory.memory_note") as memory, patch(
            "management.services.instagram_bot.build_prompt_snapshot") as current_prompt, CaptureQueriesContext(connection) as queries:
            for _ in range(3):
                self.assertEqual(self.client.get(self.url()).status_code, 200)
        for producer in (provider, bootstrap, memory, current_prompt):
            producer.assert_not_called()
        self.assertEqual(before, [model.objects.count() for model in models])
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries),
            [row["sql"] for row in queries])
