"""Offline evidence/denominator/read-only contracts for episode audit stats."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION
from management.models import (
    IgClient, IgCommercialEpisode, IgCommercialEpisodeEvent,
    IgConversationAnalysisSnapshot, IgFunnelResetAudit,
    IgPaymentConfirmationReview, IgPaymentReviewDecision,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_conversion_audit import normalize_conversion_audit
from management.services.ig_conversion_audit_stats import (
    MAX_PAGE, QUERY_CAP, build_conversion_audit_stats, conversion_audit_payload,
)
from orders.models import Order


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ConversionAuditStatsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.ordinal = 0
        InstagramBotSettings.objects.update_or_create(pk=1, defaults={"ig_user_id": "owner-1"})
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)

    def episode(self, text="Не куплю, бо дорого.", *, audit=True, classification="retail"):
        self.ordinal += 1
        client = IgClient.objects.create(igsid=f"audit-stats-{self.ordinal}")
        message = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            status="done", mid=f"audit-stats-{self.ordinal}", text=text)
        episode = IgCommercialEpisode.objects.create(client=client, sequence=1,
            materialization_key=f"audit-stats-episode-{self.ordinal}", opened_watermark_message_id=message.pk)
        IgCommercialEpisode.objects.filter(pk=episode.pk).update(opened_at=self.now - timedelta(hours=1))
        client.current_commercial_episode = episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        IgCommercialEpisodeEvent.objects.create(episode=episode,
            dedupe_key=f"audit-stats-inquiry-{self.ordinal}", event_type="commercial_inquiry_observed",
            evidence={"commercial_inquiry_v1": {"classification": classification,
                "source_message_id": message.pk, "occurred_at": self.now.isoformat()}})
        review = {"schema_version": 1, "client_id": client.pk, "episode_id": episode.pk,
            "namespace": message.provider_namespace, "reset_floor": message.pk,
            "client_permission_epoch": 0, "watermark_message_id": message.pk,
            "coverage": "reply_not_yet_observable", "digest": "1" * 64,
            "diagnostics": [], "diagnostic_refs": []}
        value = {"schema_version": 1, "motivation": [], "readiness_evidence": [],
            "nonconversion_reasons": [{"code": "price", "basis": "reported_by_customer",
                "confidence": "high", "evidence": [{"message_id": message.pk, "quote": text}]}]}
        normalized = normalize_conversion_audit(value, {message.pk: {"role": "user", "text": text}}, review)
        normalized["_response_review"] = review
        snapshot = IgConversationAnalysisSnapshot.objects.create(client=client, commercial_episode=episode,
            last_analyzed_message=message, score_band="exploring", interaction_type="product_interest",
            purchase_probability="0.6", confidence="0.8", dedupe_key=f"audit-stats-snapshot-{self.ordinal}",
            conversion_audit=normalized if audit else {})
        return client, episode, message, snapshot

    def report(self, **kwargs):
        return build_conversion_audit_stats(since=self.now - timedelta(days=7),
            until=self.now, observed_at=self.now, **kwargs)

    def test_customer_reported_price_and_possible_objection_remain_separate_and_not_forecasts(self):
        self.episode()
        self.episode("Для мене дорого.")
        self.episode(audit=False)
        result = self.report()
        self.assertEqual(result["counts"]["qualifying_episodes"], 3)
        self.assertEqual(result["counts"]["observed_audits"], 2)
        self.assertEqual(result["counts"]["missing_audit"], 1)
        self.assertEqual(result["causes"], [{"code": "price", "label": "Ціна",
            "outcome": "no_confirmed_purchase", "customer_reported_count": 1, "possible_count": 1}])
        self.assertIsNone(result["interpretation"]["conversion_forecast"])
        self.assertIsNone(result["observed_facts"]["reply_latency_seconds"])
        self.assertNotIn("rate_pct", json.dumps(result))

    def test_schema_default_is_distinct_empty_log_and_old_snapshots_are_missing(self):
        first = IgConversationAnalysisSnapshot._meta.get_field("conversion_audit").get_default()
        second = IgConversationAnalysisSnapshot._meta.get_field("conversion_audit").get_default()
        self.assertEqual(first, {})
        self.assertIsNot(first, second)
        self.episode(audit=False)
        result = self.report()
        self.assertEqual(result["counts"]["observed_audits"], 0)
        self.assertEqual(result["counts"]["missing_audit"], 1)
        self.assertEqual(result["causes"], [])

    def test_unpaid_order_and_pending_screenshot_review_never_become_purchase(self):
        client, episode, _, _ = self.episode()
        order = Order.objects.create(full_name="Audit", phone="380501112233", total_sum="1000", payment_status="unpaid")
        review = IgPaymentConfirmationReview.objects.create(client=client, order=order,
            dedupe_key="audit-pending", status="pending")
        episode.intended_order = order
        episode.primary_payment_review = review
        episode.save(update_fields=["intended_order", "primary_payment_review", "updated_at"])
        self.assertEqual(self.report()["counts"]["paid_observed"], 0)
        review.status = "confirmed"
        review.save(update_fields=["status", "updated_at"])
        self.assertEqual(self.report()["counts"]["paid_observed"], 0)
        IgPaymentReviewDecision.objects.create(client=client, review=review, decision="manager_verified",
            verification_source="manager", verification_scope="prepayment", confirmed_amount="200",
            actor_source="management_user", actor_external_id="1")
        result = self.report()
        self.assertEqual(result["counts"]["paid_observed"], 1)
        self.assertEqual(result["causes"][0]["outcome"], "confirmed_purchase")

    def test_stale_new_source_is_coverage_unknown_not_current_loss_reason(self):
        client, _, _, _ = self.episode()
        InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", text="Передумав, купую")
        result = self.report()
        self.assertEqual(result["counts"]["stale_audit"], 1)
        self.assertEqual(result["causes"], [])

    def test_foreign_evidence_namespace_epoch_and_reset_fail_closed(self):
        client, episode, message, snapshot = self.episode()
        foreign, _, foreign_message, _ = self.episode()
        original = deepcopy(snapshot.conversion_audit)
        cases = [
            ("foreign", lambda value: value["nonconversion_reasons"][0]["evidence"][0].update(message_id=foreign_message.pk)),
            ("namespace", lambda value: value["_source_scope"].update(source_namespace="instagram_login:foreign")),
            ("epoch", lambda value: value["_source_scope"].update(client_permission_epoch=999)),
            ("reset", lambda value: value["_source_scope"].update(reset_floor=999)),
        ]
        for name, mutation in cases:
            value = deepcopy(original)
            mutation(value)
            snapshot.conversion_audit = value
            snapshot.save(update_fields=["conversion_audit"])
            result = self.report()
            self.assertEqual(result["counts"]["unobservable_audit"], 1, name)
            self.assertEqual(sum(row["customer_reported_count"] for row in result["causes"]), 1, name)
        snapshot.conversion_audit = original
        snapshot.save(update_fields=["conversion_audit"])
        IgFunnelResetAudit.objects.create(client=client, reset_after_message_id=message.pk,
            previous_state={}, resulting_stage="new", reason="new run")
        self.assertEqual(self.report()["counts"]["unobservable_audit"], 1)

    def test_quote_tamper_is_not_customer_reported_and_malformed_schema_is_explicit(self):
        _, _, _, snapshot = self.episode()
        value = deepcopy(snapshot.conversion_audit)
        value["nonconversion_reasons"][0]["evidence"][0]["quote"] = "fabricated customer refusal"
        snapshot.conversion_audit = value
        snapshot.save(update_fields=["conversion_audit"])
        self.assertEqual(self.report()["causes"], [])
        snapshot.conversion_audit = {"schema_version": 999}
        snapshot.save(update_fields=["conversion_audit"])
        self.assertEqual(self.report()["counts"]["invalid_audit"], 1)

    def test_page_denominator_hidden_clients_and_privacy_erasure_are_explicit(self):
        first, _, _, _ = self.episode()
        self.episode()
        third, _, _, _ = self.episode()
        first.hidden_at = self.now
        first.save(update_fields=["hidden_at", "updated_at"])
        third.privacy_erasure_started_at = self.now
        third.save(update_fields=["privacy_erasure_started_at", "updated_at"])
        result = self.report(limit=1)
        self.assertEqual(result["counts"]["qualifying_episodes"], 2)
        self.assertEqual(result["counts"]["page_episodes"], 1)
        self.assertTrue(result["pagination"]["has_more"])
        next_page = self.report(limit=1, before_episode_id=result["pagination"]["next_before_episode_id"])
        self.assertEqual(next_page["counts"]["page_episodes"], 1)
        self.assertFalse(next_page["pagination"]["has_more"])
        self.assertTrue(result["scope"]["includes_hidden_clients"])

    def test_noncommercial_episodes_do_not_enter_commercial_denominator(self):
        self.episode(classification="service")
        self.episode(classification="collaboration")
        self.assertEqual(self.report()["counts"]["qualifying_episodes"], 0)

    def test_constant_query_budget_no_provider_no_writes_and_bounded_window(self):
        for _ in range(8):
            self.episode()
        with (CaptureQueriesContext(connection) as queries,
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no provider")),
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no provider"))):
            report = build_conversion_audit_stats(observed_at=self.now)
        self.assertLessEqual(len(queries), QUERY_CAP)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        self.assertTrue(report["scope"]["window_bounded"])
        self.assertEqual(report["counts"]["page_episodes"], 8)
        for kwargs in ({"limit": MAX_PAGE + 1}, {"limit": True}, {"before_episode_id": 0}):
            with self.assertRaises(ValueError):
                self.report(**kwargs)

    def test_client_wrapper_never_exposes_stale_hypotheses_or_noncommercial_zero_probability(self):
        client, _, _, snapshot = self.episode()
        payload = conversion_audit_payload(snapshot, client, fresh=False)
        self.assertEqual(payload["state"], "stale")
        self.assertEqual(payload["data"], {})
        self.assertIsNone(payload["intent_assessment"]["value"])
        snapshot.interaction_type = "collaboration"
        with patch("management.services.ig_analysis_reply_review.public_conversion_audit", return_value=snapshot.conversion_audit):
            payload = conversion_audit_payload(snapshot, client, fresh=True)
        self.assertIsNone(payload["intent_assessment"]["value"])
        self.assertEqual(payload["intent_assessment"]["calibration_status"], "uncalibrated")
        self.assertNotIn("_source_scope", payload["data"])

    def test_lightweight_metadata_does_not_call_full_serializer_or_query(self):
        client, _, _, snapshot = self.episode()
        with (self.assertNumQueries(0), patch("management.services.ig_analysis_reply_review.public_conversion_audit",
            side_effect=AssertionError("list metadata must not rebuild native context"))):
            payload = conversion_audit_payload(snapshot, client, fresh=True, load=False)
        self.assertEqual(payload["state"], "not_loaded")
        self.assertTrue(payload["source_fresh"])
        self.assertEqual(payload["data"], {})


@override_settings(ROOT_URLCONF="twocomms.urls_management", ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)
class ConversionAuditStatsApiTests(TestCase):
    episode = ConversionAuditStatsTests.episode

    def setUp(self):
        ConversionAuditStatsTests.setUp(self)
        user = get_user_model().objects.create_user("audit-stats-api", is_staff=True)
        app, code = VIEW_IG_CONVERSATION_PII_PERMISSION.split(".", 1)
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        self.client.force_login(user)

    def test_stats_api_adds_aggregate_without_raw_quotes_or_provider_ids(self):
        self.episode()
        response = self.client.get(reverse("management_bot_stats_api") + "?days=0&conversion_limit=1")
        self.assertEqual(response.status_code, 200)
        data = response.json()["conversion_audit_stats"]
        self.assertEqual(data["counts"]["page_episodes"], 1)
        self.assertNotIn("Не куплю", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("provider_message_id", json.dumps(data))
        self.assertIn("conversion_cohorts", response.json())

    def test_stats_api_pagination_and_permission_fail_before_exposure(self):
        response = self.client.get(reverse("management_bot_stats_api") + "?conversion_limit=500")
        self.assertEqual(response.status_code, 400)
        user = get_user_model().objects.create_user("audit-stats-no-pii", is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("management_bot_stats_api")).status_code, 403)


from management import tests_ig_conversion_audit_analysis_20261022 as analysis_fixtures


@override_settings(IG_ANALYSIS_V2_MODE="off", IG_ANALYSIS_MATERIALITY_MODE="off",
    GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class ConversionAuditNativeReceiptProofTests(TransactionTestCase):
    setUp = analysis_fixtures.ConversionAuditAnalyzerTests.setUp
    _payload = analysis_fixtures.ConversionAuditAnalyzerTests._payload
    _plan = analysis_fixtures.ConversionAuditAnalyzerTests._plan
    _send = analysis_fixtures.ConversionAuditAnalyzerTests._send
    _finish = analysis_fixtures.ConversionAuditAnalyzerTests._finish
    _parsed = analysis_fixtures.ConversionAuditAnalyzerTests._parsed
    _run = analysis_fixtures.ConversionAuditAnalyzerTests._run

    def _native_snapshot(self):
        episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=1,
            materialization_key="stats-native-proof", opened_watermark_message_id=self.source.pk)
        self.client_row.current_commercial_episode = episode
        self.client_row.save(update_fields=["current_commercial_episode"])
        self.revision.turn.episode = episode
        self.revision.turn.save(update_fields=["episode"])
        IgCommercialEpisodeEvent.objects.create(episode=episode, dedupe_key="stats-native-inquiry",
            event_type="commercial_inquiry_observed", evidence={"commercial_inquiry_v1": {"classification": "retail"}})
        claim = self._send()
        self._finish(claim)
        return self._run(self.source, self._parsed(self.source))[0]

    def test_edited_sealed_source_removes_current_native_audit_and_latency(self):
        snapshot = self._native_snapshot()
        now = timezone.now()
        arguments = {"since": now - timedelta(days=1), "observed_at": now}
        with CaptureQueriesContext(connection) as queries:
            before = build_conversion_audit_stats(**arguments)
        self.assertLessEqual(len(queries), QUERY_CAP)
        self.assertEqual(before["observed_facts"]["reply_latency_sample"], 1)
        self.source.text = "edited current customer source"
        self.source.save(update_fields=["text"])
        with CaptureQueriesContext(connection) as queries:
            after = build_conversion_audit_stats(**arguments)
        self.assertLessEqual(len(queries), QUERY_CAP)
        self.assertEqual(after["observed_facts"]["reply_latency_sample"], 0)
        self.assertEqual(after["counts"]["observed_audits"], 0)
        self.assertEqual(after["counts"]["unobservable_audit"], 1)
        self.assertEqual(after["causes"], [])

    def test_prefetched_canonical_proof_matches_default_and_performs_zero_queries(self):
        self._native_snapshot()
        from management.models import IgCustomerTurnRevision
        from management.services.ig_revision_execution import _whole_sent_proof
        from management.services.ig_revision_outbox import revision_sources_current

        revision = IgCustomerTurnRevision.objects.select_related("client").get(pk=self.revision.pk)
        effects = list(revision.delivery_effects.order_by("order_index", "pk"))
        sources = list(revision.sources.select_related("message__client").order_by("ordinal", "pk"))
        expected = _whole_sent_proof(revision, effects)
        self.assertTrue(expected[0])
        with self.assertNumQueries(0):
            self.assertEqual(_whole_sent_proof(revision, effects, source_rows=sources), expected)
            self.assertTrue(revision_sources_current(revision, source_rows=sources))
        for attribute, changed in (("recipient_igsid", "foreign"), ("payload_digest", "0" * 64),
            ("part_count", 2), ("revision_snapshot_digest", "0" * 64),
            ("provider_namespace", ""), ("provider_message_id", ""), ("terminal_at", None),
            ("state", "unknown"), ("source_message_id", self.source.pk + 1000)):
            original = getattr(effects[0], attribute)
            setattr(effects[0], attribute, changed)
            expected = _whole_sent_proof(revision, effects)
            self.assertFalse(expected[0], attribute)
            with self.assertNumQueries(0):
                self.assertEqual(_whole_sent_proof(revision, effects, source_rows=sources), expected)
            setattr(effects[0], attribute, original)

    def test_persisted_latency_is_recomputed_from_native_receipts(self):
        snapshot = self._native_snapshot()
        now = timezone.now()
        arguments = {"since": now - timedelta(days=1), "observed_at": now}
        before = build_conversion_audit_stats(**arguments)
        diagnostic = snapshot.conversion_audit["_response_review"]["diagnostics"][0]
        diagnostic.update(response_seconds=999999, media_observed=True, time_basis="invented")
        snapshot.save(update_fields=["conversion_audit"])
        after = build_conversion_audit_stats(**arguments)
        self.assertEqual(after["observed_facts"]["reply_latency_seconds"],
            before["observed_facts"]["reply_latency_seconds"])
        self.assertEqual(after["observed_facts"]["reply_latency_sample"], 1)

    def test_native_receipt_cannot_borrow_unrelated_duplicate_or_reversed_declared_sources(self):
        self._native_snapshot()
        later = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", status="done",
            mid="stats-later-independent-source", text="Another independent question",
            provider_created_at=self.source.created_at + timedelta(milliseconds=1))
        snapshot, _ = self._run(later, self._parsed(later))
        now = timezone.now()
        arguments = {"since": now - timedelta(days=1), "observed_at": now}
        original = deepcopy(snapshot.conversion_audit)
        self.assertEqual(original["_response_review"]["diagnostics"][0]["source_message_ids"], [self.source.pk])
        self.assertEqual(build_conversion_audit_stats(**arguments)["observed_facts"]["reply_latency_sample"], 1)
        for label, source_ids in (("unrelated", [self.source.pk, later.pk]),
            ("duplicate", [self.source.pk, self.source.pk]), ("reversed", [later.pk, self.source.pk])):
            with self.subTest(label=label):
                value = deepcopy(original)
                value["_response_review"]["diagnostics"][0]["source_message_ids"] = source_ids
                snapshot.conversion_audit = value
                snapshot.save(update_fields=["conversion_audit"])
                with CaptureQueriesContext(connection) as queries:
                    result = build_conversion_audit_stats(**arguments)
                self.assertLessEqual(len(queries), QUERY_CAP)
                self.assertEqual(result["counts"]["unobservable_audit"], 1)
                self.assertEqual(result["counts"]["observed_audits"], 0)
                self.assertEqual(result["observed_facts"]["reply_latency_sample"], 0)
