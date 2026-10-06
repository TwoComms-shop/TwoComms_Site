"""Owner privacy fences reject operator writes to a real UNKNOWN human send."""
from copy import deepcopy
from datetime import timedelta
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.ig_human_reply_models import HumanReplyPart
from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import create_human_reply_command, resolve_unknown_human_reply
from management.services.ig_human_reply_delivery import claim_next_human_part, record_human_part_started, settle_human_part
from management.services.ig_human_reply_transport import ensure_planned_human_reconciliation


class HumanReplyResolutionPrivacyTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.namespace = "instagram_login:human-privacy-owner"
        env = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.http = patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("privacy test provider I/O")).start()
        self.addCleanup(patch.stopall)
        self.actor = get_user_model().objects.create_superuser(username="human-privacy-actor", password="x")
        InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="human-privacy-owner", page_id="human-privacy-owner")
        self.customer = IgClient.objects.create(igsid="human-privacy-customer")
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", provider_namespace=self.namespace,
            text="Original source", mid="human-privacy-source", status="done",
            provider_created_at=self.now - timedelta(minutes=2))
        self.command = create_human_reply_command(self.customer.pk, actor=self.actor,
            text="Original manager reply", context_message_id=self.source.pk, now=self.now).command
        claim = claim_next_human_part(self.command.pk, now=self.now)
        self.assertTrue(claim.ready, claim.reason)
        self.assertTrue(record_human_part_started(claim.part.pk, claim.token, now=self.now).ready)
        settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace,
            transport_outcome="timeout", now=self.now)
        self.command.refresh_from_db()
        self.assertEqual(self.command.state, "unknown")
        self.task = ensure_planned_human_reconciliation(self.command, now=self.now)
        self.assertIsNotNone(self.task)
        self.task.manager_context = {**self.task.manager_context, "manager_note": "Keep original private note"}
        self.task.save(update_fields=["manager_context", "updated_at"])

    def tearDown(self):
        self.http.assert_not_called()
        super().tearDown()

    def assert_rejects_owner_fence_without_writes(self, field):
        IgClient.objects.filter(pk=self.customer.pk).update(**{field: self.now})
        original_context = deepcopy(self.task.manager_context)
        original_payload = deepcopy(self.task.event_payload)
        original_audits = AdminAuditLog.objects.count()
        original_part = HumanReplyPart.objects.get(command=self.command)
        original_token = original_part.claim_token
        for outcome in ("delivered", "not_delivered", "handled"):
            with self.subTest(outcome=outcome), CaptureQueriesContext(connection) as captured:
                result = resolve_unknown_human_reply(self.task.pk, client_id=self.customer.pk,
                    actor=self.actor, outcome=outcome, note="Must never replace the manager note", now=self.now)
            self.assertEqual(result, {"ok": False, "error": "client_unavailable", "status": 410})
            writes = [query["sql"] for query in captured.captured_queries
                if re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", query["sql"], re.I)]
            self.assertEqual(writes, [], "Rejected operator resolution executed database mutations")
            self.task.refresh_from_db()
            self.command.refresh_from_db()
            original_part.refresh_from_db()
            self.assertEqual(self.task.manager_context, original_context)
            self.assertEqual(self.task.event_payload, original_payload)
            self.assertEqual(self.task.status, "skipped")
            self.assertIsNone(self.task.manager_approval_actor_id)
            self.assertEqual(self.command.state, "unknown")
            self.assertEqual(self.command.context_message_id, self.source.pk)
            self.assertEqual((original_part.state, original_part.claim_token), ("unknown", original_token))
            self.assertIsNone(original_part.provider_message_id)
            self.assertEqual(AdminAuditLog.objects.count(), original_audits)
            self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_hidden_owner_rejects_resolution_without_dml_or_private_note_change(self):
        self.assert_rejects_owner_fence_without_writes("hidden_at")

    def test_erasure_owner_rejects_resolution_without_dml_or_private_note_change(self):
        self.assert_rejects_owner_fence_without_writes("privacy_erasure_started_at")
