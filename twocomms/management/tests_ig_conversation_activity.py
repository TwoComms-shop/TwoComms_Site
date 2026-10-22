"""Conversation order follows observed event time, independent of card state."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.bot_views import bot_client_detail_api
from management.models import IgClient, InstagramBotMessage
from management.services.ig_conversation_activity import (
    activity_payload, detail_revision, with_conversation_activity,
)


class ConversationActivityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.a = IgClient.objects.create(igsid="activity-a", last_message_at=self.now)
        self.b = IgClient.objects.create(igsid="activity-b", last_message_at=self.now - timedelta(hours=1))

    def message(self, client, *, role="user", at=None, **kwargs):
        return InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role=role,
            provider_created_at=at or self.now, **kwargs,
        )

    def activity(self, client):
        return with_conversation_activity(IgClient.objects.filter(pk=client.pk)).get()

    def ordered_ids(self):
        return list(with_conversation_activity(IgClient.objects.all()).order_by(
            "-activity_at", "-activity_message_id", "-id",
        ).values_list("id", flat=True))

    def test_inbound_or_delivered_manager_model_and_native_voice_promote_by_event_time(self):
        self.message(self.a, at=self.now)
        for offset, role in enumerate(["user", "model", "manager"], start=1):
            with self.subTest(role=role):
                row = self.message(self.b, role=role, at=self.now + timedelta(minutes=offset),
                                   source="echo" if role == "manager" else "webhook",
                                   attachment_media=[{"type": "audio"}] if role == "manager" else [])
                projected = self.activity(self.b)
                self.assertEqual(projected.activity_message_id, row.pk)
                self.assertEqual(self.ordered_ids()[0], self.b.pk)
                self.b.refresh_from_db()
                self.assertEqual(self.b.last_message_at, self.now - timedelta(hours=1))

    def test_historical_import_uses_provider_time_and_missing_provider_time_never_promotes(self):
        self.message(self.a, at=self.now)
        old = self.message(self.b, at=self.now - timedelta(days=10), source="manual_refresh")
        InstagramBotMessage.objects.create(client=self.b, sender_id=self.b.igsid,
                                          role="manager", source="poll_history")
        InstagramBotMessage.objects.create(client=self.b, sender_id=self.b.igsid,
                                          role="manager", source="poll", mid="undated-poll-history")
        self.assertEqual(self.activity(self.b).activity_message_id, old.pk)
        self.assertEqual(self.ordered_ids(), [self.a.pk, self.b.pk])

    def test_failed_or_pending_outgoing_rows_do_not_claim_delivered_activity(self):
        observed = self.message(self.b, at=self.now - timedelta(days=1))
        for state in ["sending", "unknown", "failed", "cancelled", "duplicate"]:
            self.message(self.b, role="model", send_state=state)
        self.message(self.b, role="manager", status="pending")
        self.assertEqual(self.activity(self.b).activity_message_id, observed.pk)

    def test_outgoing_delivery_completion_precedes_local_draft_creation_fallback(self):
        sent_at = self.now + timedelta(hours=2)
        row = InstagramBotMessage.objects.create(client=self.b, sender_id=self.b.igsid,
                                                role="manager", send_state="sent", send_completed_at=sent_at)
        self.assertEqual(self.activity(self.b).activity_at, sent_at)
        self.assertEqual(activity_payload(self.activity(self.b))["activity_message_id"], row.pk)

    def test_inbound_processing_completion_is_not_its_message_event_time(self):
        row = InstagramBotMessage.objects.create(client=self.b, sender_id=self.b.igsid, role="user",
                                                send_completed_at=self.now + timedelta(hours=2))
        self.assertEqual(self.activity(self.b).activity_at, row.created_at)

    def test_ties_are_stable_and_profile_card_changes_do_not_reorder(self):
        self.message(self.a, at=self.now)
        latest = self.message(self.b, at=self.now)
        order = self.ordered_ids()
        IgClient.objects.filter(pk=self.a.pk).update(display_name="Changed", stage="cold", manager_takeover=True)
        self.assertEqual(self.ordered_ids(), order)
        self.assertEqual(order, [self.b.pk, self.a.pk])
        self.assertEqual(activity_payload(self.activity(self.b))["activity_message_id"], latest.pk)

    def test_legacy_timestamp_fallback_only_when_there_is_no_transcript(self):
        self.assertEqual(self.activity(self.b).activity_at, self.b.last_message_at)
        InstagramBotMessage.objects.create(client=self.b, sender_id=self.b.igsid,
                                          role="manager", source="manual_refresh")
        self.assertIsNone(self.activity(self.b).activity_at)

    def test_projection_reads_all_rows_in_one_query(self):
        self.message(self.a)
        with self.assertNumQueries(1):
            rows = list(with_conversation_activity(IgClient.objects.all()))
            for row in rows:
                activity_payload(row)


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class ConversationDetailRevisionTests(TestCase):
    def setUp(self):
        self.person = IgClient.objects.create(igsid="detail-revision")

    def revision(self):
        client = with_conversation_activity(IgClient.objects.filter(pk=self.person.pk)).get()
        with self.assertNumQueries(1):
            return detail_revision(client)

    def test_revision_is_stable_without_mutations_and_ignores_clock_age(self):
        first = self.revision()
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=3)):
            self.assertEqual(self.revision(), first)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_client_bulk_update_without_mutation_timestamp_changes_revision(self):
        first = self.revision()
        IgClient.objects.filter(pk=self.person.pk).update(display_name="New profile", manager_takeover=True)
        self.assertNotEqual(self.revision(), first)

    def test_latest_related_bulk_update_without_mutation_timestamp_changes_revision(self):
        from management.ig_bot_models import IgFollowState

        follow = IgFollowState.objects.create(client=self.person)
        first = self.revision()
        IgFollowState.objects.filter(pk=follow.pk).update(state="following")
        self.assertNotEqual(self.revision(), first)

    def test_current_payment_bulk_update_changes_revision(self):
        from management.ig_bot_models import IgDeal, IgPaymentProjection

        deal = IgDeal.objects.create(client=self.person)
        projection = IgPaymentProjection.objects.create(client=self.person, deal=deal)
        first = self.revision()
        IgPaymentProjection.objects.filter(pk=projection.pk).update(truth="confirmed")
        self.assertNotEqual(self.revision(), first)

    def test_older_manager_task_bulk_transition_changes_revision(self):
        from management.ig_bot_models import IgFollowUpTask

        old = IgFollowUpTask.objects.create(client=self.person, due_at=timezone.now(), kind="manager_task")
        IgFollowUpTask.objects.create(client=self.person, due_at=timezone.now(), kind="manager_task", status="completed")
        first = self.revision()
        IgFollowUpTask.objects.filter(pk=old.pk).update(status="completed")
        self.assertNotEqual(self.revision(), first)

    def test_older_semantic_row_update_timestamp_changes_revision(self):
        from management.ig_bot_models import IgDeal

        old = IgDeal.objects.create(client=self.person)
        IgDeal.objects.create(client=self.person)
        first = self.revision()
        IgDeal.objects.filter(pk=old.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        self.assertNotEqual(self.revision(), first)

    def test_linked_order_bulk_shipping_update_changes_revision(self):
        from management.ig_bot_models import IgOrderAttribution
        from orders.models import Order

        order = Order.objects.create(full_name="Activity test", order_number="activity-test-order")
        IgOrderAttribution.objects.create(client=self.person, order=order, creation_mode="linked_existing")
        first = self.revision()
        Order.objects.filter(pk=order.pk).update(status="ship", tracking_number="test-tracking")
        shipped = self.revision()
        self.assertNotEqual(shipped, first)
        Order.objects.filter(pk=order.pk).update(total_sum="850.00", pay_type="cod")
        self.assertNotEqual(self.revision(), shipped)

    def test_explicit_zero_cursor_poll_has_incremental_shape_and_skips_full_assembly(self):
        import json

        request = RequestFactory().get("/bot/api/clients/1/", {"poll": "1", "after_id": "0"})
        request.user = get_user_model().objects.create_superuser("activity-test-admin", password="x")
        with patch("management.bot_views._client_card", side_effect=AssertionError("full card assembled")), \
             patch("management.services.ig_journey_snapshot.build_journey_snapshot", side_effect=AssertionError("journey assembled")), \
             patch("management.services.ig_commerce_projection.captured_selection_for", side_effect=AssertionError("selection assembled")), \
             CaptureQueriesContext(connection) as captured:
            response = bot_client_detail_api(request, self.person.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["last_message_id"], 0)
        self.assertNotIn("client", data)
        self.assertIn("detail_revision", data)
        self.assertIn("activity_at", data)
        self.assertLessEqual(len(captured), 20)

    def test_full_detail_and_idle_poll_share_revision(self):
        import json

        user = get_user_model().objects.create_superuser("revision-idle-admin", password="x")
        factory = RequestFactory()
        full_request = factory.get("/bot/api/clients/1/")
        full_request.user = user
        full = json.loads(bot_client_detail_api(full_request, self.person.pk).content)
        poll_request = factory.get("/bot/api/clients/1/", {"poll": "1", "after_id": "0"})
        poll_request.user = user
        incremental = json.loads(bot_client_detail_api(poll_request, self.person.pk).content)
        self.assertEqual(full["detail_revision"], incremental["detail_revision"])
        self.assertEqual(full["activity_at"], incremental["activity_at"])

    def test_actual_full_poll_reads_stay_stable_without_semantic_writes(self):
        from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION
        from management.tests_ig_clients_ui import _operator

        operator = _operator("activity-read-sequence", VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(operator)
        # Match the existing native ClientDetailCursorTests history shape;
        # exercise real authorized HTTP view assembly, without mocking card,
        # journey, selection, follow state or reply-debt helpers.
        messages = [InstagramBotMessage.objects.create(
            client=self.person, sender_id=self.person.igsid, role=role,
            text=text, mid=f"stable-read-{index}",
        ) for index, (role, text) in enumerate([
            ("user", "First question"), ("user", "Second question"),
            ("model", "Delivered response"),
        ])]
        url = reverse("management_bot_client_detail_api", args=[self.person.pk])
        poll = {"poll": "1", "after_id": str(messages[-1].pk)}
        # Settings/projection bootstrap is permitted once, outside the
        # read-purity assertion; further DTO reads must not touch semantic data.
        self.assertEqual(self.client.get(url).status_code, 200)

        def read_sequence():
            revisions = []
            with CaptureQueriesContext(connection) as queries:
                for query in ({}, poll, {}, poll):
                    response = self.client.get(url, query)
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertEqual("client" in data, not bool(query))
                    revisions.append(data["detail_revision"])
            semantic_writes = [
                item["sql"].lstrip().split(None, 1)[0].upper()
                for item in queries
                if item["sql"].lstrip().split(None, 1)[0].upper()
                in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "TRUNCATE"}
                and "django_session" not in item["sql"].lower()
            ]
            self.assertEqual(semantic_writes, [])
            self.assertEqual(len(set(revisions)), 1)
            return revisions[0]

        initial = read_sequence()
        IgClient.objects.filter(pk=self.person.pk).update(
            display_name="A profile changed outside model.save", manager_takeover=True,
        )
        changed = read_sequence()
        self.assertNotEqual(changed, initial)
        self.assertEqual(read_sequence(), changed)
