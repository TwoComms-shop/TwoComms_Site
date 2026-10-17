"""Exact native-template receipts bypass empty manager-echo body parsing."""
from copy import deepcopy
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_echo as fixtures
from management.models import IgDeferredEcho, IgPermissionTransitionJob, IgWebhookInboxEvent, InstagramBotMessage
from management.services import instagram_bot as bot
from management.services.ig_revision_outbox import claim_next_effect, finish_effect, mark_provider_started
from management.services.ig_webhook_inbox import (
    _owned_template_echo_effect, accept_webhook, drain_webhook_inbox, reconcile_owned_template_echo,
)
from management import tests_ig_revision_echo_integration as integration
from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart, human_payload_digest


@override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    SITE_BASE_URL="https://twocomms.test")
class OwnedTemplateEchoTests(TransactionTestCase):
    setUp = fixtures.RevisionEchoTests.setUp
    _plan = fixtures.RevisionEchoTests._plan
    _accept_echo = integration.RevisionEchoIntegrationTests._accept_echo
    template = {"type": "template", "payload": {"template_type": "generic", "elements": [{"title": "Футболка"}]}}

    def sent_template(self):
        payload = {"recipient": {"id": self.client_row.igsid}, "message": {"attachment": self.template}}
        self._plan([{"group": "template", "kind": "template", "payload": payload}])
        claim = claim_next_effect(self.revision.pk, self.revision_token, "template")
        self.assertEqual(mark_provider_started(claim.effect.pk, claim.token, self.revision_token).reason, "provider_started")
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200,
            provider_message_id="native-template-mid")
        return claim.effect

    def test_exact_sent_template_materializes_without_manager_parser_or_network(self):
        effect = self.sent_template()
        # The live incident had an earlier manually prepared reply. Its part
        # makes the manager echo lane active even for later bot template MIDs.
        command = HumanReplyCommand.objects.create(client=self.client_row, recipient_igsid=self.client_row.igsid,
            provider_namespace=self.namespace, text="Earlier manager answer", state="sent")
        payload = {"recipient": {"id": self.client_row.igsid}, "message": {"text": command.text}}
        HumanReplyPart.objects.create(command=command, client=self.client_row,
            operation_id_snapshot=command.operation_id, actor_id_snapshot=1, context_message_id_snapshot=1,
            permission_epoch=0, recipient_igsid=self.client_row.igsid, provider_namespace=self.namespace,
            ordinal=0, part_count=1, plan_digest="a" * 64, payload=payload, payload_digest=human_payload_digest(payload),
            state="sent", provider_message_id="earlier-human-mid", provider_started_at=timezone.now(), terminal_at=timezone.now())
        self._accept_echo("native-template-mid", [self.template])
        with patch.object(bot, "handle_webhook_payload") as parser, patch.object(bot, "_provider_http") as network:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        parser.assert_not_called()
        network.assert_not_called()
        row = IgWebhookInboxEvent.objects.get()
        self.assertIsNotNone(row.processed_at)
        self.assertEqual(row.reason, "owned_template_receipt")
        self.assertEqual(row.payload["owned_template_receipt"]["effect_id"], effect.pk)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.assertFalse(IgDeferredEcho.objects.exists())
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertTrue(InstagramBotMessage.objects.filter(role="model", provider_message_id="native-template-mid").exists())

    def test_uncommitted_or_corrupt_effect_cannot_supply_template_authority(self):
        effect = self.sent_template()
        self._accept_echo("native-template-mid", [self.template])
        row = IgWebhookInboxEvent.objects.get()
        from management.models import IgRevisionDeliveryEffect

        for fields in ({"state": "unknown"}, {"terminal_at": None}):
            with self.subTest(fields=fields):
                with transaction.atomic():
                    IgRevisionDeliveryEffect.objects.filter(pk=effect.pk).update(**fields)
                    self.assertIsNone(_owned_template_echo_effect(row, self.namespace, "owner-1"))
                    transaction.set_rollback(True)
        with patch("management.services.ig_revision_outbox._digest", return_value="0" * 64):
            self.assertIsNone(_owned_template_echo_effect(row, self.namespace, "owner-1"))

    def test_one_shot_repairs_only_named_blocked_receipt_and_is_idempotent(self):
        effect = self.sent_template()
        self._accept_echo("native-template-mid", [self.template])
        own = IgWebhookInboxEvent.objects.get()
        IgWebhookInboxEvent.objects.filter(pk=own.pk).update(decision="blocked", reason="echo_empty", last_error="echo_empty", attempts=1)
        self._accept_echo("unknown-old-template", [self.template])
        unknown = IgWebhookInboxEvent.objects.exclude(pk=own.pk).get()
        IgWebhookInboxEvent.objects.filter(pk=unknown.pk).update(decision="blocked", reason="echo_empty", last_error="echo_empty", attempts=1)
        with patch.object(bot, "_provider_http") as network:
            result = reconcile_owned_template_echo(own.pk, self.settings)
            self.assertTrue(result["reconciled"], result)
            self.assertEqual(result["effect_id"], effect.pk)
            self.assertFalse(reconcile_owned_template_echo(own.pk, self.settings)["reconciled"])
            self.assertFalse(reconcile_owned_template_echo(unknown.pk, self.settings)["reconciled"])
            self.assertEqual(drain_webhook_inbox(self.settings, limit=2), 0)
        network.assert_not_called()
        own.refresh_from_db()
        unknown.refresh_from_db()
        self.assertIsNotNone(own.processed_at)
        self.assertEqual((unknown.decision, unknown.reason, unknown.attempts, unknown.processed_at), ("blocked", "echo_empty", 1, None))
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", provider_message_id="native-template-mid").count(), 1)

    def test_unknown_template_still_blocks_instead_of_inventing_own_or_manager_authority(self):
        self._accept_echo("unproven-template", [self.template])
        with patch.object(bot, "_provider_http") as network:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        network.assert_not_called()
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual((row.decision, row.reason), ("blocked", "echo_empty"))
        self.assertFalse(InstagramBotMessage.objects.filter(role__in=("manager", "model")).exists())

    def test_exact_mid_cannot_override_user_foreign_owner_or_recipient(self):
        self.sent_template()
        self._accept_echo("native-template-mid", [self.template])
        original = IgWebhookInboxEvent.objects.get()
        for change in ("user", "owner", "recipient", "namespace"):
            with self.subTest(change=change):
                row = deepcopy(original)
                event = row.payload["entry"][0]["messaging"][0]
                if change == "user":
                    event["message"]["is_echo"] = False
                elif change == "owner":
                    event["sender"]["id"] = "foreign-owner"
                elif change == "recipient":
                    event["recipient"]["id"] = "foreign-customer"
                else:
                    row.namespace = "instagram_login:foreign-owner"
                self.assertIsNone(_owned_template_echo_effect(row, self.namespace, "owner-1"))
