"""Failed-provider replies and finite parked holding admission."""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings as django_settings
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management.models import (
    GeminiRequest, IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask,
    IgBotNotification, InstagramBotMessage,
)
from management.services.ig_revision_holding import (
    PARKED_DISPOSITION_KEY, claim_parked_collaboration_revision, parked_collaboration_revision_ids,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class HoldingReliabilityTests(TransactionTestCase):
    reset_sequences = True
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._generate
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record

    def test_positive_failed_generation_holds_without_optional_flag_and_replays_no_http(self):
        with override_settings():
            if hasattr(django_settings, "IG_REVISION_OPTIONAL_HOLDING_ENABLED"):
                del django_settings.IG_REVISION_OPTIONAL_HOLDING_ENABLED
            self.assertFalse(self._fit_fixture(source_texts=["Сколько стоит эта футболка?"]).ready)
            first, generate, http = self._execute()
            self.assertEqual(first.reasons, ("technical_holding_sent",))
            self.assertEqual(http.call_count, 1)
            generate.assert_not_called()
            self.revision.refresh_from_db()
            receipt = self.revision.action_receipts["technical_holding"]
            self.assertEqual(self.revision.recovery_state, "manual")
            self.assertIsNone(self.revision.lease_until)
            self.assertTrue(IgFollowUpTask.objects.filter(pk=receipt["task_id"]).exists())
            self.assertTrue(IgBotNotification.objects.filter(pk=receipt["notification_id"]).exists())
            cases = IgFollowUpTask.objects.count()
            notices = IgBotNotification.objects.count()
            _, retry_generate, retry_http = self._execute()
            retry_generate.assert_not_called()
            retry_http.assert_not_called()
            self.assertEqual(IgFollowUpTask.objects.count(), cases)
            self.assertEqual(IgBotNotification.objects.count(), notices)

    @override_settings(IG_REVISION_OPTIONAL_HOLDING_ENABLED=False)
    def test_neutral_failed_generation_stays_optional(self):
        self._fit_fixture(source_texts=["Дякую!"])
        _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()

    @override_settings(IG_REVISION_OPTIONAL_HOLDING_ENABLED=False)
    def test_fresh_positive_generation_failure_also_sends_one_guarded_holding(self):
        from management.services.ig_turn_revisions import create_collecting_revision

        self.revision.delete()
        self.source.text = "Сколько стоит эта футболка?"
        self.source.save(update_fields=["text"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        self.provider_failure = True
        with patch.object(self, "_generate", side_effect=lambda *args, **kwargs:
                live_fixtures.RevisionLiveTests._generate(self, *args, **kwargs)):
            result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.delivery_effects.get().purpose, "technical_holding")

    def _parked(self, index, text):
        from management.services.ig_turn_revisions import _create_source_rows, _digest, _source_payload

        client = IgClient.objects.create(igsid=f"parked-{index}", language="uk")
        source = InstagramBotMessage.objects.create(client=client, role="user", text=text,
            provider_namespace="instagram_login:owner-1", mid=f"parked-source-{index}",
            sender_id=client.igsid, source="webhook", status="pending")
        turn = IgCustomerTurn.objects.create(client=client, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        payload = _source_payload(source, ordinal=1)
        snapshot = {"sources": [payload]}
        revision = IgCustomerTurnRevision.objects.create(
            client=client, turn=turn, revision=1, state="claimed", active_slot=1,
            recovery_state="manual", recovery_code="provider_dispatch_budget",
            overall_deadline=timezone.now() - timedelta(seconds=1),
            quiet_started_at=timezone.now(), quiet_deadline=timezone.now(), quiet_cap_at=timezone.now(),
            snapshot_digest=_digest(snapshot), source_count=1, bundle_snapshot=snapshot,
        )
        _create_source_rows(revision, [payload])
        GeminiRequest.objects.create(request_id=f"parked-graph-{revision.pk}",
            logical_turn_id=f"ig-revision:{revision.pk}", client_id=client.pk,
            source_message_id=source.pk, terminal_resolution="failed")
        return revision

    def test_noncreator_rejections_settle_once_and_cannot_starve_later_offer(self):
        rows = [self._parked(index, "Звичайне питання") for index in range(4)]
        valid = self._parked(5, "Пропоную співпрацю, я блогер")
        for _ in range(2):
            ids = parked_collaboration_revision_ids(limit=2)
            self.assertEqual(len(ids), 2)
            for pk in ids:
                claimed, reason = claim_parked_collaboration_revision(pk)
                self.assertIsNone(claimed)
                self.assertEqual(reason, "parked_collaboration_not_creator_offer")
        self.assertEqual(parked_collaboration_revision_ids(limit=2), [valid.pk])
        for row in rows:
            row.refresh_from_db()
            self.assertIn(PARKED_DISPOSITION_KEY, row.action_receipts)
            self.assertIsNone(row.lease_until)
        task_count = IgFollowUpTask.objects.count()
        for _ in range(3):
            self.assertEqual(parked_collaboration_revision_ids(limit=2), [valid.pk])
        self.assertEqual(IgFollowUpTask.objects.count(), task_count)

    def test_manager_blocked_old_rows_are_filtered_before_limit(self):
        blocked = [self._parked(index, "Пропоную співпрацю") for index in range(3)]
        IgClient.objects.filter(pk__in=[row.client_id for row in blocked]).update(manager_takeover=True)
        valid = self._parked(4, "Пропоную співпрацю")
        self.assertEqual(parked_collaboration_revision_ids(limit=1), [valid.pk])
