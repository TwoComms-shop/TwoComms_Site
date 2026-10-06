"""Actual producer/accounting boundary with deterministic operational fixtures."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import GeminiRequest, GeminiRequestAttempt, GeminiQuotaState, IgClient
from management.services import gemini_accounting_runtime as runtime
from management.services import ig_memory_producer as producer
from management import tests_ig_memory_producer as memory_cases
from management import tests_ig_nonlive_admission as admission_cases


@override_settings(**admission_cases.ENFORCE, IG_MEMORY_GENERATION_ENABLED=True,
                   IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True)
class MemoryProviderGateTests(TransactionTestCase):
    source = memory_cases.CapturedMemoryProducerTests.source
    _profile = admission_cases.NonliveFinalAdmissionTests._profile
    _facade = admission_cases.NonliveFinalAdmissionTests._facade

    def setUp(self):
        memory_cases.CapturedMemoryProducerTests.setUp(self)
        # Other daemon suites own filesystem/circuit fixtures. This boundary
        # test starts with operational admission open; source/lease and actual
        # accounting checks remain active throughout generation.
        for target, value in (
            ("management.services.ig_maintenance.maintenance_status", {"active": False}),
            ("management.services.ig_db_circuit.circuit_status", {"open": False, "failures": 0}),
        ):
            operational = patch(target, return_value=value)
            operational.start()
            self.addCleanup(operational.stop)
        self.profile_sequence = 0
        self.profile = self._profile()
        source = self.source("current permitted customer preference")
        self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock).queued)

    def test_admitted_summary_uses_actual_facade_and_publishes_captured_head(self):
        with self._facade() as (post, cancel):
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=4))
        self.assertEqual((result["claimed"], result["published"], result["failed"]), (1, 1, 0))
        post.assert_called_once()
        cancel.assert_not_called()
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 1)
        self.assertEqual(producer.read_memory_summary(self.client).reason, "current")
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.accounting_mode, "enforced")
        self.assertEqual(graph.attempts.filter(provider_started_at__isnull=False).count(), 1)
        self.assertEqual(GeminiQuotaState.objects.get().rpd_dispatched, 1)

    def test_source_erasure_after_planning_stops_actual_http_and_added_spend(self):
        validate = runtime.RequestObserver._validate_boundary

        def erase_after_planning(observer, boundary):
            admitted = validate(observer, boundary)
            IgClient.objects.filter(pk=self.client.pk).update(privacy_erasure_started_at=timezone.now())
            return admitted

        with self._facade() as (post, _cancel), patch.object(
            runtime.RequestObserver, "_validate_boundary", erase_after_planning
        ):
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=4))
        self.assertEqual((result["claimed"], result["published"]), (1, 0), result)
        post.assert_not_called()
        self.client.refresh_from_db()
        self.assertFalse(self.client.memory_summary)
        rows = GeminiRequestAttempt.objects.filter(request_graph=GeminiRequest.objects.get())
        self.assertFalse(rows.filter(provider_started_at__isnull=False).exists())
        self.assertTrue(rows.filter(failure_kind="source_admission_denied").exists())
        self.assertFalse(GeminiQuotaState.objects.filter(rpd_dispatched__gt=0).exists())
