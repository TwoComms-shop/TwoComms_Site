"""Real MariaDB source admission and migration constraints; no provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgCommerceSelectionTransition, IgCommerceTurnDecision,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_commerce import reduce_inbound_commerce_source


@skipUnless(connection.vendor == "mysql", "requires disposable MariaDB")
@override_settings(GOOGLE_INDEXING_ENABLED=False)
class MariaDbSourceAdmissionTests(TransactionTestCase):
    reset_sequences = True

    def test_competing_intake_workers_keep_one_source_decision(self):
        settings = InstagramBotSettings.objects.create(ig_user_id="90000001")
        client = IgClient.get_or_create_for_sender("90000002")
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", source="webhook",
            text="Хочу замовити футболку розмір L", mid="r30-maria-owned-source",
            provider_namespace="r30-owned-page", provider_created_at=timezone.now(),
        )
        barrier = Barrier(2)

        def reduce():
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    InstagramBotSettings.objects.select_for_update().get(pk=settings.pk)
                    locked = IgClient.objects.select_for_update().get(pk=client.pk)
                    result = reduce_inbound_commerce_source(
                        locked, source, expected_provider_namespace="r30-owned-page",
                    )
                    self.assertTrue(result.ready, result.reason)
                    return result.decisions[0]["decision_id"]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: reduce(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(source_message=source).count(), 1)
        decision = IgCommerceTurnDecision.objects.select_related("session").get(pk=results[0])
        self.assertEqual(decision.session.revision, 1)
        self.assertEqual(decision.session.lines[0]["size"], "L")
        self.assertTrue(decision.session.query_constraints["purchase_requested"])

    def test_source_action_migration_preserves_decision_and_revision_uniqueness(self):
        def constraints(model):
            with connection.cursor() as cursor:
                return connection.introspection.get_constraints(cursor, model._meta.db_table)

        transitions = constraints(IgCommerceSelectionTransition)
        decisions = constraints(IgCommerceTurnDecision)
        self.assertFalse(any(row["unique"] and row["columns"] == ["source_message_id"]
                             for row in transitions.values()))
        self.assertTrue(any(row["unique"] and row["columns"] == ["session_id", "to_revision"]
                            for row in transitions.values()))
        self.assertTrue(any(row["unique"] and row["columns"] == ["source_message_id"]
                            for row in decisions.values()))
