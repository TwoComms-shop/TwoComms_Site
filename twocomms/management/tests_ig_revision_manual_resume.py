"""Focused contracts for the B02.10 manual-resume successor producer."""
from datetime import timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from management.models import AdminAuditLog, IgClient, IgCustomerTurn, IgTurnMessage, InstagramBotMessage, InstagramBotSettings
from management.services.ig_revision_manual_resume import (
    MANUAL_RESUME_ORIGIN,
    MANUAL_RESUME_REASON,
    create_manual_resume_successor,
)


class ManualResumeSuccessorTests(TestCase):
    def _source(self, *, at=None, suffix="one"):
        now = at or timezone.now()
        settings_obj = InstagramBotSettings.load()
        settings_obj.page_id = "owner"
        settings_obj.save(update_fields=["page_id"])
        from management.services.instagram_bot import ingress_provider_namespace
        namespace = ingress_provider_namespace(settings_obj)
        client = IgClient.objects.create(igsid=f"manual-resume-client-{suffix}", reply_permission_epoch=7)
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER, status=InstagramBotMessage.Status.DONE,
            text="Підкажіть, будь ласка, розмір", mid=f"manual-resume-source-{suffix}",
            provider_namespace=namespace, provider_created_at=now,
        )
        turn = IgCustomerTurn.objects.create(
            client=client, primary_source_message=source,
            window_started_at=now, window_deadline=now,
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role=source.role)
        return client, source, turn

    def _create(self, client, source, *, now=None):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.page_id = "owner"
        settings_obj.save(update_fields=["is_enabled", "page_id"])
        actor, _created = get_user_model().objects.get_or_create(
            username=f"resume-{client.pk}",
            defaults={"is_staff": True, "is_superuser": True, "is_active": True},
        )
        audit = AdminAuditLog.objects.create(
            actor=actor, actor_role="prompt_editor", action="ig_bot.manual_resume",
            entity_type="IgClient", entity_id=str(client.pk),
            before={"permission_epoch": client.reply_permission_epoch - 1, "bot_paused": True, "manager_takeover": False},
            after={"permission_epoch": client.reply_permission_epoch, "bot_paused": False, "manager_takeover": False},
        )
        return create_manual_resume_successor(
            client, settings_obj=settings_obj, audit_id=audit.pk,
            source_message_id=source.pk, now=now or timezone.now(),
        )

    def test_creates_one_fresh_collecting_execution_without_reopening_turn(self):
        now = timezone.now()
        client, source, turn = self._source(at=now)

        result = self._create(client, source, now=now)

        self.assertTrue(result.created, result.reason)
        child = result.revision
        self.assertEqual(child.origin, MANUAL_RESUME_ORIGIN)
        self.assertEqual(child.successor_reason, MANUAL_RESUME_REASON)
        self.assertEqual(child.turn_id, turn.pk)
        self.assertEqual(child.state, child.State.COLLECTING)
        self.assertEqual(child.permission_epoch, client.reply_permission_epoch)
        self.assertEqual(child.quiet_deadline, now)
        self.assertEqual(child.overall_deadline, now + timedelta(seconds=45))
        self.assertEqual(child.sources.count(), 1)
        turn.refresh_from_db()
        self.assertEqual(turn.primary_source_message_id, source.pk)

        repeated = self._create(client, source, now=now + timedelta(seconds=1))
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.revision.pk, child.pk)

    def test_expired_source_and_newer_inbound_are_refused(self):
        now = timezone.now()
        client, source, _turn = self._source(at=now - timedelta(hours=23, seconds=1))
        self.assertEqual(self._create(client, source, now=now).reason, "manual_source_window_expired")

        client, source, _turn = self._source(at=now, suffix="two")
        InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.DONE, text="Ще одне питання", mid="manual-newer",
        )
        self.assertEqual(self._create(client, source, now=now).reason, "manual_newer_inbound")

    def test_question_plus_latest_emoji_in_same_turn_still_creates_one_child(self):
        now = timezone.now()
        client, question, turn = self._source(at=now, suffix="bundle")
        emoji = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.DONE, text="👍", mid="manual-emoji",
            provider_namespace=question.provider_namespace, provider_created_at=now,
        )
        IgTurnMessage.objects.create(turn=turn, message=emoji, ordinal=2, role=emoji.role)

        result = self._create(client, emoji, now=now)

        self.assertTrue(result.created, result.reason)
        self.assertEqual(
            list(result.revision.sources.values_list("message_id", flat=True)),
            [question.pk, emoji.pk],
        )

    def test_takeover_optout_and_manager_answer_remain_guards(self):
        now = timezone.now()
        client, source, _turn = self._source(at=now)
        client.manager_takeover = True
        client.save(update_fields=["manager_takeover", "updated_at"])
        self.assertEqual(self._create(client, source, now=now).reason, "manual_client_ineligible")

        client.manager_takeover = False
        client.opted_out_at = now
        client.save(update_fields=["manager_takeover", "opted_out_at", "updated_at"])
        self.assertEqual(self._create(client, source, now=now).reason, "manual_client_ineligible")

        client.opted_out_at = None
        client.save(update_fields=["opted_out_at", "updated_at"])
        InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role=InstagramBotMessage.Role.MANAGER,
            status=InstagramBotMessage.Status.DONE, text="Менеджер уже відповів", mid="manual-manager",
        )
        self.assertEqual(self._create(client, source, now=now).reason, "manual_manager_answered")
