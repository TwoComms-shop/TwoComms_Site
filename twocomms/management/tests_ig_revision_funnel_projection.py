"""New-plan episode ownership and event-time first replies from exact SENT proof."""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCustomerTurn, IgCustomerTurnRevision,
    IgFunnelDropOff, IgFunnelStepEvent, IgTurnMessage, InstagramBotMessage,
)
from management import tests_ig_revision_reply_projection as projection_fixtures
from management.services.ig_funnel_analytics import build_funnel_analytics, canonical_first_reply_events
from management.services.ig_revision_reply_projection import (
    ADMISSION_KEY, RECEIPT_KEY, admission_binding, funnel_admission_binding, project_sent_reply,
)
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionFunnelProjectionTests(TransactionTestCase):
    _prepare = projection_fixtures.RevisionReplyProjectionTests._prepare
    _execute = projection_fixtures.RevisionReplyProjectionTests._execute
    _generate = projection_fixtures.RevisionReplyProjectionTests._generate
    _parts = projection_fixtures.RevisionReplyProjectionTests._parts
    _assert_count = projection_fixtures.RevisionReplyProjectionTests._assert_count

    def setUp(self):
        projection_fixtures.RevisionReplyProjectionTests.setUp(self)
        self.episode = self._episode()

    def _message(self, text, mid):
        return projection_fixtures.RevisionReplyProjectionTests._message(
            self, "Яка ціна цієї футболки?" if mid == "first" else text, mid,
        )

    def _episode(self):
        sequence = IgCommercialEpisode.objects.filter(client=self.customer).count() + 1
        episode = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=sequence, materialization_key=f"projection-episode:{sequence}",
        )
        self.customer.current_commercial_episode = episode
        self.customer.stage = "qualifying"
        self.customer.save(update_fields=["current_commercial_episode", "stage"])
        return episode

    def _admit(self, revision, effect, *, facts=()):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=self.customer.pk)
            admission = admission_binding(revision, plan_digest=effect.plan_digest, settings_id=effect.settings_id_snapshot)
            admission["funnel"] = funnel_admission_binding(
                revision, client, plan_digest=effect.plan_digest, settings_id=effect.settings_id_snapshot,
                actor=effect.actor, purpose=effect.purpose, fact_bindings=facts,
            )
            revision.action_receipts = {**revision.action_receipts, ADMISSION_KEY: admission}
            revision.save(update_fields=["action_receipts"])

    def _another_reply(self, *, at, text="Допоможіть підібрати футболку", purpose="normal_reply"):
        self.revision.refresh_from_db()
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None)
        self.source = self._message(text, f"next:{self.revision.pk}")
        self.turn = IgCustomerTurn.objects.create(
            client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        return self._parts(at=at, purpose=purpose)[0]

    def _events(self):
        return IgFunnelStepEvent.objects.filter(event_type="bot_replied_first")

    def test_actual_live_plan_admits_episode_and_completed_reply_projects_once(self):
        # A normal explicit selection answer avoids claiming an unbound price.
        self.parsed = {"reply_text": "Який стиль футболки вам подобається?", "controls": []}
        self._prepare()
        result, _, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[ADMISSION_KEY]["funnel"]["episode_id"], self.episode.pk)
        receipt = self.revision.action_receipts[RECEIPT_KEY]
        self.assertEqual(receipt["funnel_projection"]["outcome"], "projected")
        with patch("management.services.instagram_bot._provider_http") as send:
            self.assertEqual(project_sent_reply(self.revision.pk), receipt)
        send.assert_not_called()
        self.assertEqual(self._events().count(), 1)

    def test_actual_http_receipt_after_episode_reset_uses_pre_send_admission(self):
        self.parsed = {"reply_text": "Який стиль футболки вам подобається?", "controls": []}
        self._prepare()
        def provider_response(*args, **kwargs):
            admitted = IgCustomerTurnRevision.objects.get(pk=self.revision.pk).action_receipts[ADMISSION_KEY]["funnel"]
            self.assertEqual(admitted["episode_id"], self.episode.pk)
            self.assertEqual(admitted["outcome"], "admitted")
            IgCommercialEpisode.objects.filter(pk=self.episode.pk).update(open_slot=None, state="cancelled")
            self._episode()
            return 200, '{"message_id":"confirmed-after-reset"}'
        result, _, http = self._execute(send_results=provider_response)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self._events().get().episode_id, self.episode.pk)
        self.assertEqual(IgCommercialEpisode.objects.count(), 2)

    def test_multipart_waits_for_whole_sent_and_uses_complete_reply_time(self):
        parts = self._parts(("sent", "unknown"))
        self.assertEqual(project_sent_reply(self.revision.pk), {})
        self.assertFalse(self._events().exists())
        parts[1].state = "sent"
        parts[1].provider_message_id = "late-confirmed-second-part"
        parts[1].save(update_fields=["state", "provider_message_id"])
        first = project_sent_reply(self.revision.pk)
        self.assertEqual(project_sent_reply(self.revision.pk), first)
        event = self._events().get()
        self.assertEqual(event.occurred_at, parts[1].terminal_at)
        self.assertEqual(event.evidence["sent_effect_ids"], [part.pk for part in parts])
        self._assert_count(1)

    def test_episode_reset_after_admission_does_not_move_historical_reply(self):
        self._parts()
        IgCommercialEpisode.objects.filter(pk=self.episode.pk).update(open_slot=None, state="cancelled", closed_at=timezone.now())
        current = self._episode()
        IgClient.objects.filter(pk=self.customer.pk).update(stage="checkout", reply_permission_epoch=2, bot_paused=True)
        project_sent_reply(self.revision.pk)
        event = self._events().get()
        self.assertEqual(event.episode_id, self.episode.pk)
        self.assertEqual(event.stage, "qualifying")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_commercial_episode_id, current.pk)
        self.assertEqual(self.customer.stage, "checkout")

    def test_late_earlier_receipt_appends_correction_before_cohort_filtering(self):
        now = timezone.now()
        self._parts(at=now - timedelta(hours=1))
        original = project_sent_reply(self.revision.pk)
        previous = self._events().get()
        self._another_reply(at=now - timedelta(hours=3))
        correction = project_sent_reply(self.revision.pk)
        self.assertEqual(correction["funnel_projection"].get("supersedes_event_id"), previous.pk, correction)
        previous.refresh_from_db()
        self.assertEqual(previous.occurred_at, now - timedelta(hours=1))
        canonical = canonical_first_reply_events(self._events()).get()
        self.assertEqual(canonical.occurred_at, now - timedelta(hours=3))
        self.assertFalse(canonical_first_reply_events(self._events()).filter(occurred_at__gte=now - timedelta(hours=2)).exists())
        result = build_funnel_analytics(since=now - timedelta(hours=2), until=now, client_ids=[self.customer.pk])
        step = next(row for row in result["steps"] if row["step"] == "bot_replied_first")
        self.assertEqual(step["entered"], 0)
        self.assertEqual(self._events().count(), 2)
        self.assertEqual(original["funnel_projection"]["event_id"], previous.pk)
        from management.services.ig_journey_snapshot import _history
        history = _history(self.episode.pk, self.customer.pk)
        self.assertEqual(history["total"], 1)
        self.assertEqual([event["id"] for event in history["events"]], [f"funnel_event:{canonical.pk}"])
        self.assertLessEqual(len(history["visits"]), 1)

    def test_later_reply_does_not_append_another_first_or_recover_future_dropoff(self):
        now = timezone.now()
        self._parts(at=now - timedelta(hours=3))
        drop_event = IgFunnelStepEvent.objects.create(
            episode=self.episode, event_type="drop_off", event_key="future-drop",
            occurred_at=now - timedelta(hours=1), stage="qualifying", actor="system",
        )
        drop = IgFunnelDropOff.objects.create(
            episode=self.episode, step_event=drop_event, kind="silence", is_recoverable=True,
            occurred_at=drop_event.occurred_at, stage_at_drop="qualifying",
        )
        project_sent_reply(self.revision.pk)
        self._another_reply(at=now)
        result = project_sent_reply(self.revision.pk)
        self.assertEqual(result["funnel_projection"]["outcome"], "first_reply_already_recorded")
        self.assertEqual(self._events().count(), 1)
        drop.refresh_from_db()
        self.assertIsNone(drop.recovered_at)
        self.assertFalse(IgFunnelStepEvent.objects.filter(event_type="recovered").exists())

    def test_projection_crash_rolls_back_event_counter_and_transcript_without_resend(self):
        self._parts()
        original = IgCustomerTurnRevision.save
        def crash(instance, *args, **kwargs):
            if RECEIPT_KEY in (instance.action_receipts or {}):
                raise RuntimeError("projection persistence unavailable")
            return original(instance, *args, **kwargs)
        with patch.object(IgCustomerTurnRevision, "save", crash):
            with self.assertRaises(RuntimeError):
                project_sent_reply(self.revision.pk)
        self.assertFalse(self._events().exists())
        self.assertFalse(InstagramBotMessage.objects.filter(source="revision_reply").exists())
        self._assert_count(0)
        with patch("management.services.instagram_bot._provider_http") as send:
            project_sent_reply(self.revision.pk)
        send.assert_not_called()
        self.assertEqual(self._events().count(), 1)
        self._assert_count(1)

    def test_wrong_episode_authority_is_unsupported_without_losing_reply_count(self):
        effect = self._parts(admitted=False)[0]
        self._admit(self.revision, effect, facts=[{"episode_id": self.episode.pk + 100}])
        result = project_sent_reply(self.revision.pk)
        self.assertEqual(result["funnel_projection"]["reason"], "original_episode_authority_mismatch")
        self.assertFalse(self._events().exists())
        self._assert_count(1)

    def test_missing_episode_is_unsupported_without_creating_one(self):
        IgClient.objects.filter(pk=self.customer.pk).update(current_commercial_episode_id=None)
        self._parts()
        result = project_sent_reply(self.revision.pk)
        self.assertEqual(result["funnel_projection"]["reason"], "original_episode_missing")
        self.assertEqual(IgCommercialEpisode.objects.count(), 1)
        self._assert_count(1)

    def test_episode_reassigned_to_another_client_is_not_projected(self):
        self._parts()
        other = IgClient.objects.create(igsid="different-episode-owner")
        IgCommercialEpisode.objects.filter(pk=self.episode.pk).update(client=other)
        receipt = project_sent_reply(self.revision.pk)
        self.assertEqual(receipt["funnel_projection"]["reason"], "original_episode_owner_changed")
        self.assertFalse(self._events().exists())
        self._assert_count(1)

    def test_media_only_never_means_product_selection_or_price_quote(self):
        self._parts(group="catalog_media")
        project_sent_reply(self.revision.pk)
        self.assertFalse(IgFunnelStepEvent.objects.exists())

    def test_legacy_admitted_plan_does_not_gain_funnel_ownership_on_projection(self):
        self._parts(admitted=False)
        effect = self.revision.delivery_effects.get()
        projection_fixtures.RevisionReplyProjectionTests._admit(self, self.revision, effect)
        first = project_sent_reply(self.revision.pk)
        self.assertEqual(first["funnel_projection"]["reason"], "original_episode_binding_not_projected")
        self.assertEqual(project_sent_reply(self.revision.pk), first)
        self.assertFalse(self._events().exists())
        self._assert_count(1)

    def test_noncommercial_ack_does_not_become_a_sales_milestone(self):
        self._another_reply(at=timezone.now(), text="Дякую, гарного дня!")
        result = project_sent_reply(self.revision.pk)
        self.assertEqual(result["funnel_projection"]["reason"], "noncommercial_source_purpose")
        self.assertFalse(self._events().exists())
        self._assert_count(1)

    def test_holding_and_followup_have_no_first_reply_event(self):
        self._parts(purpose="technical_holding", origin="holding")
        project_sent_reply(self.revision.pk)
        self._another_reply(at=timezone.now(), purpose="followup")
        project_sent_reply(self.revision.pk)
        self.assertFalse(self._events().exists())
