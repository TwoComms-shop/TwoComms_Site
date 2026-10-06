"""Bounded real-store cohorts, independent denominators and protected exports."""
import csv
import io
import json
import shutil
import subprocess
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.http import HttpResponse
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path, reverse
from django.utils import timezone

from management import tests_ig_decision_trace_read_model as trace_fixtures
from management.bot_access import META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
from management.bot_quality_observation_views import bot_quality_observations_api, bot_quality_observations_export_api
from management.models import IgClient, IgCustomerTurnRevision, IgTurnRevisionSource
from management.services.ig_decision_trace_read_model import read_revision_decision_trace
from management.services.ig_quality_observations import (
    MAX_PAGE, QUERY_CAP, build_quality_observation_page, quality_observation_csv,
)
from management.services.ig_revision_input import decide_revision_input
from management.services.ig_revision_reply_projection import project_sent_reply

urlpatterns = [
    path("login/", lambda request: HttpResponse("login"), name="management_login"),
    path("bot/api/quality-observations/", bot_quality_observations_api, name="management_bot_quality_observations_api"),
    path("bot/api/quality-observations/export/", bot_quality_observations_export_api, name="management_bot_quality_observations_export_api"),
]


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class QualityObservationTests(TransactionTestCase):
    def setUp(self):
        method = "test_no_reply_and_static_decisions_remain_in_revision_index" if self._testMethodName == "test_canonical_no_reply_is_not_applicable_to_delivery_or_generation" else "runTest"
        self.case = trace_fixtures.DecisionTraceReadModelTests(methodName=method)
        self.case.setUp(); self.addCleanup(self.case.doCleanups)

    def page(self, **kwargs):
        return build_quality_observation_page(since=timezone.now() - timedelta(days=7), until=timezone.now(), **kwargs)

    @staticmethod
    def metric(payload, key):
        return next(row for row in payload["metrics"] if row["key"] == key)

    def test_attempt_winner_delivery_and_semantics_have_independent_unknown_bins(self):
        data = self.page()
        self.assertEqual(data["cohort"]["population_count_at_read_start"], 1)
        self.assertEqual(data["sample"]["count"], 1)
        self.assertTrue(data["sample"]["whole_window_sampled"])
        self.assertEqual(self.metric(data, "generation_winner")["numerator"], 1)
        self.assertEqual(self.metric(data, "normal_reply_sent")["unknown_count"], 1)
        self.assertEqual(self.metric(data, "semantic_reply_complete")["unknown_count"], 1)
        self.assertEqual(self.metric(data, "local_semantic_rejection")["denominator"], 2)
        self.assertEqual(self.metric(data, "local_semantic_rejection")["numerator"], 1)
        self.assertEqual(data["usage"]["total_tokens_sum"], 60)
        self.assertIsNone(data["usage"]["monetary_cost"])
        self.assertIsNone(data["interpretation"]["quality_score"])
        self.assertFalse(data["interpretation"]["population_extrapolation"])

    def test_partial_unknown_is_not_a_proven_delivery_failure(self):
        self.case._effects(("sent", "unknown", "planned"))
        self.case._coverage(disposition="recovery", remaining=("synthetic:answer",))
        data = self.page()
        normal = self.metric(data, "normal_reply_sent")
        self.assertEqual((normal["numerator"], normal["negative_count"], normal["unknown_count"]), (0, 0, 1))
        receipts = self.metric(data, "receipt_confirmed")
        self.assertEqual((receipts["denominator"], receipts["numerator"], receipts["unknown_count"]), (3, 1, 2))
        self.assertEqual(self.metric(data, "semantic_reply_complete")["negative_count"], 1)
        self.assertEqual(self.metric(data, "transcript_parity")["unknown_count"], 1)

    def test_exact_receipts_and_projection_complete_without_equating_attempts(self):
        self.case._effects(("sent", "sent")); self.case._coverage()
        project_sent_reply(self.case.revision.pk)
        data = self.page()
        self.assertEqual(self.metric(data, "normal_reply_sent")["numerator"], 1)
        self.assertEqual(self.metric(data, "semantic_reply_complete")["numerator"], 1)
        self.assertEqual(self.metric(data, "receipt_confirmed")["denominator"], 2)
        self.assertEqual(self.metric(data, "transcript_parity")["numerator"], 2)
        self.assertEqual(data["sample"]["count"], 1)

    def test_holding_does_not_count_as_complete_customer_reply(self):
        self.case._effects(purpose="technical_holding"); self.case._coverage()
        data = self.page()
        self.assertEqual(self.metric(data, "receipt_confirmed")["numerator"], 1)
        self.assertEqual(self.metric(data, "normal_reply_sent")["negative_count"], 1)
        self.assertEqual(self.metric(data, "semantic_reply_complete")["negative_count"], 1)

    def test_canonical_no_reply_is_not_applicable_to_delivery_or_generation(self):
        self.case.settings.ai_enabled = False; self.case.settings.trigger_text = "absent"
        self.case.settings.reply_text = "PRIVATE STATIC RESPONSE"
        self.case.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        decision = decide_revision_input(self.case.revision.pk, self.case.token, settings_id=self.case.settings.pk)
        self.assertTrue(decision.ready, decision.reason); self.assertEqual(decision.origin, "no_reply")
        data = self.page()
        self.assertEqual(self.metric(data, "no_reply_decision")["numerator"], 1)
        for key in ("generation_winner", "normal_reply_sent", "semantic_reply_complete"):
            self.assertEqual(self.metric(data, key)["denominator"], 0)
            self.assertEqual(self.metric(data, key)["not_applicable_count"], 1)
        self.assertEqual(data["counts"]["recorded_attempts"], 0)

    def test_source_change_is_unknown_not_dropped_from_eligible_sample(self):
        self.case.source.text = "PRIVATE CHANGED TEXT"; self.case.source.save(update_fields=["text"])
        data = self.page()
        self.assertEqual(data["sample"]["count"], 1)
        self.assertEqual(data["counts"]["unavailable_trace_revisions"], 1)
        self.assertEqual(data["stage_coverage"]["context"]["unknown"], 1)
        self.assertEqual(self.metric(data, "generation_winner")["unknown_count"], 1)
        self.assertEqual(data["counts"]["physical_part_population_unknown_revisions"], 1)
        self.assertNotIn("PRIVATE", json.dumps(data))

    def _variants(self, number):
        original = self.case.revision
        source = original.sources.get()
        for index in range(1, number + 1):
            now = timezone.now()
            variant = IgCustomerTurnRevision.objects.create(
                client=self.case.client_row, turn=original.turn, parent=original,
                revision=original.revision + index, origin="auto_refresh", state="sealed", active_slot=None,
                quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now, overall_deadline=now,
                permission_epoch=original.permission_epoch, source_count=original.source_count,
                bundle_snapshot=deepcopy(original.bundle_snapshot), snapshot_digest=original.snapshot_digest,
            )
            values = {field.attname: getattr(source, field.attname) for field in source._meta.concrete_fields
                      if field.name not in {"id", "revision", "created_at"}}
            IgTurnRevisionSource.objects.create(revision=variant, **values)

    def test_five_revision_cap_cursor_and_population_do_not_count_unique_turns(self):
        self._variants(5)
        with CaptureQueriesContext(connection) as queries:
            first = self.page()
        self.assertEqual((first["sample"]["count"], first["cohort"]["population_count_at_read_start"]), (5, 6))
        self.assertTrue(first["sample"]["has_more"]); self.assertFalse(first["sample"]["whole_window_sampled"])
        self.assertIsNone(first["interpretation"]["logical_customer_turns"])
        self.assertIsNone(first["interpretation"]["economic_roots"])
        self.assertLessEqual(len(queries), QUERY_CAP)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        second = self.page(before_revision_id=first["sample"]["next_before_revision_id"])
        self.assertEqual(second["sample"]["count"], 1); self.assertFalse(second["sample"]["whole_window_sampled"])

    def test_empty_erased_and_hidden_population_never_claims_quality(self):
        IgClient.objects.filter(pk=self.case.client_row.pk).update(hidden_at=timezone.now())
        data = self.page()
        self.assertEqual((data["sample"]["count"], data["cohort"]["population_count_at_read_start"]), (0, 0))
        self.assertIsNone(data["usage"]["total_tokens_sum"])
        self.assertIsNone(data["interpretation"]["quality_score"])
        IgClient.objects.filter(pk=self.case.client_row.pk).update(hidden_at=None, privacy_erasure_started_at=timezone.now())
        self.assertEqual(self.page()["sample"]["count"], 0)

    def test_erasure_after_trace_before_buffered_export_fails_closed(self):
        from management.services import ig_quality_observations as reader
        actual = reader.read_revision_decision_trace
        def fence(**kwargs):
            result = actual(**kwargs)
            IgClient.objects.filter(pk=self.case.client_row.pk).update(privacy_erasure_started_at=timezone.now())
            return result
        with patch.object(reader, "read_revision_decision_trace", side_effect=fence):
            result = self.page()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("metrics", result)
        with self.assertRaises(ValueError): quality_observation_csv(result)

    def test_window_and_page_limits_are_strict_aware_and_not_future(self):
        now = timezone.now()
        for kwargs in ({"since": now, "until": now}, {"since": now - timedelta(days=32), "until": now},
                       {"since": now - timedelta(days=1), "until": now + timedelta(hours=1)},
                       {"since": now.replace(tzinfo=None) - timedelta(days=1), "until": now}):
            with self.assertRaises(ValueError): build_quality_observation_page(**kwargs)
        for limit in (True, 0, MAX_PAGE + 1):
            with self.assertRaises(ValueError): self.page(limit=limit)
        for cursor in (True, 0, -1, 2**63):
            with self.assertRaises(ValueError): self.page(before_revision_id=cursor)

    def test_current_observation_interval_and_durable_trace_timestamps_not_historical_replay(self):
        effects = self.case._effects(("sent", "unknown"))
        trace = read_revision_decision_trace(client_id=self.case.client_row.pk, revision_id=self.case.revision.pk)
        self.assertEqual(trace["revision"]["created_at"], self.case.revision.created_at.isoformat())
        request = trace["generation"]["requests"][0]
        self.assertEqual(request["resolved_at"], self.case.graph.resolved_at.isoformat())
        self.assertEqual(request["attempts"][0]["created_at"], self.case.failed.created_at.isoformat())
        self.assertEqual(trace["delivery"]["effects"][0]["receipt_finalized_at"], effects[0].terminal_at.isoformat())
        self.assertIsNone(trace["delivery"]["effects"][1]["receipt_finalized_at"])
        until = self.case.revision.created_at + timedelta(microseconds=1)
        data = build_quality_observation_page(since=until - timedelta(days=7), until=until)
        self.assertFalse(data["observation"]["historical_as_of_supported"])
        self.assertTrue(data["observation"]["outcomes_may_postdate_cohort_end"])
        self.assertEqual(data["cohort"]["time_field"], "IgCustomerTurnRevision.created_at")

    def test_aggregate_csv_excludes_identifiers_prose_and_hides_no_unknown_bins(self):
        data = self.page(); output = quality_observation_csv(data)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 19)
        self.assertTrue(all(row["sample_revisions"] == "1" for row in rows))
        self.assertTrue(all(row["historical_as_of_supported"] == "False" for row in rows))
        self.assertTrue(all(row["monetary_cost"] == "unknown" for row in rows))
        self.assertEqual(next(row for row in rows if row["metric"] == "normal_reply_sent")["unknown_count"], "1")
        self.assertEqual(next(row for row in rows if row["metric"] == "stage_known_semantic")["unknown_count"], "1")
        for private in (self.case.client_row.igsid, self.case.request_id, "trace-mid", "gemini-", "private-key-alias", "Що на фото?", self.case.revision.snapshot_digest):
            self.assertNotIn(private, output)
        forged = deepcopy(data); forged["cohort"]["start"] = "=UNTRUSTED()"
        with self.assertRaises(ValueError): quality_observation_csv(forged)
        forged = deepcopy(data); forged["metrics"][0]["numerator"] = "=UNTRUSTED()"
        with self.assertRaises(ValueError): quality_observation_csv(forged)

    def test_zero_dml_no_provider_no_bootstrap_and_strict_page_query_budget(self):
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("provider forbidden")) as provider, \
             patch("management.models.InstagramBotSettings.load", side_effect=AssertionError("bootstrap forbidden")) as bootstrap, \
             CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.page()["status"], "available")
        self.assertLessEqual(len(queries), QUERY_CAP)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        provider.assert_not_called(); bootstrap.assert_not_called()


@override_settings(ROOT_URLCONF="management.tests_ig_quality_observations", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class QualityObservationApiTests(TransactionTestCase):
    def setUp(self):
        self.case = trace_fixtures.DecisionTraceReadModelTests(methodName="runTest")
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.actor = get_user_model().objects.create_user(username="quality-observation-operator")
        self.grant(self.actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(self.actor)

    @staticmethod
    def grant(actor, *permissions):
        actor.user_permissions.add(*(Permission.objects.get(content_type__app_label=value.split(".")[0], codename=value.split(".")[1]) for value in permissions))

    def urls(self):
        return [reverse("management_bot_quality_observations_api"), reverse("management_bot_quality_observations_export_api")]

    def test_anonymous_each_capability_and_reviewer_deny_before_reader(self):
        self.client.logout()
        with patch("management.services.ig_quality_observations.build_quality_observation_page") as reader:
            for url in self.urls(): self.assertEqual(self.client.get(url).status_code, 302)
            for number, permissions in enumerate(((), (OPERATE_IG_BOT_PERMISSION,), (VIEW_IG_CONVERSATION_PII_PERMISSION,))):
                actor = get_user_model().objects.create_user(username=f"quality-cap-{number}", is_staff=True)
                self.grant(actor, *permissions); self.client.force_login(actor)
                for url in self.urls(): self.assertEqual(self.client.get(url).status_code, 403)
            actor = get_user_model().objects.create_superuser(username="quality-reviewer", password="synthetic-password")
            actor.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME)); self.client.force_login(actor)
            for url in self.urls(): self.assertEqual(self.client.get(url).status_code, 403)
        reader.assert_not_called()

    def test_get_only_no_store_and_aggregate_csv_attachment(self):
        report, export = self.urls()
        for url in self.urls(): self.assertEqual(self.client.post(url).status_code, 405)
        response = self.client.get(export, {"days": 7, "include_protected": 1, "raw_response": 1})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn(self.case.client_row.igsid, response.content.decode())
        self.assertNotIn(self.case.request_id, response.content.decode())
        self.assertIn("sample_revisions", response.content.decode())
        self.assertEqual(self.client.get(report).json()["sample"]["maximum"], 5)

    def test_api_rejects_oversize_ambiguous_naive_future_windows_and_cursor(self):
        for query in ("days=32", "limit=6", "limit=01", "limit=1&limit=2", "before_revision_id=-1",
                      "since=2026-01-01&until=2026-01-02", "since=2099-01-01T00:00:00Z&until=2099-01-02T00:00:00Z",
                      "days=7&since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z"):
            for url in self.urls(): self.assertEqual(self.client.get(url + "?" + query).status_code, 400)

    def test_authenticated_json_and_csv_are_select_only_no_provider_no_bootstrap(self):
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("provider forbidden")), \
             patch("management.models.InstagramBotSettings.load", side_effect=AssertionError("bootstrap forbidden")):
            for url in self.urls():
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self.client.get(url).status_code, 200)
                self.assertLessEqual(len(queries), QUERY_CAP + 8)
                self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))


class QualityObservationRendererTests(SimpleTestCase):
    def test_actual_widget_denominators_unknowns_and_fixed_window_next_page(self):
        source = (Path(__file__).parent / "static/management/ig_quality_observations.js").read_text()
        program = r'''
const assert=require('node:assert/strict');
class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;this.textContent='';}setAttribute(k,v){this.attrs[k]=v;}addEventListener(){}remove(){}}
global.document={createElement:tag=>new Element(tag)};global.window={location:{href:'https://twocomms.test/management/bot/'}};
''' + source + r'''
function text(node){return node.textContent+' '+node.children.map(text).join(' ');}
const data={success:true,schema_version:'ig-quality-observations.v1',cohort:{start:'2026-09-29T00:00:00+00:00',end_exclusive:'2026-10-06T00:00:00+00:00',population_count_at_read_start:50},observation:{read_finished_at:'2026-10-06T12:00:00+00:00'},sample:{count:5,whole_window_sampled:false,has_more:true,next_before_revision_id:20},metrics:[{key:'generation_winner',label:'Прийнята генерація',numerator:4,denominator:5,denominator_unit:'generation_applicable_revisions',unknown_count:1,not_applicable_count:0},{key:'normal_reply_sent',label:'Усі звичайні частини доставлено',numerator:1,denominator:4,denominator_unit:'reply_applicable_revisions',unknown_count:2,not_applicable_count:1},{key:'semantic_reply_complete',label:'Повну відповідь підтверджено',numerator:0,denominator:4,denominator_unit:'reply_applicable_revisions',unknown_count:3,not_applicable_count:1}],stage_coverage:{semantic:{unknown:3,denominator:5}},usage:{total_tokens_sum:null,observed_attempts:0,unknown_attempts:7}};
const calls=[];window.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,json:async()=>data};};
(async()=>{const component=window.IgQualityObservations.create(new Element('div'));assert.equal(calls.length,0);await component.render({days:7});
 const shown=text(component.root);assert.match(shown,/5 \/ 50/);assert.match(shown,/не повний звіт/);assert.match(shown,/Невідомо: 3/);assert.match(shown,/частини відповіді рахуються окремо/);assert.match(shown,/Грошова вартість невідома/);
 await component.load({...component.query,before_revision_id:component.cursor},component.epoch);
 const query=new URL(calls[1].url).searchParams;assert.equal(query.get('since'),data.cohort.start);assert.equal(query.get('until'),data.cohort.end_exclusive);assert.equal(query.get('before_revision_id'),'20');assert.equal(query.has('days'),false);
 assert.equal(new URL(component.export.href).searchParams.get('before_revision_id'),'20');assert.ok(calls.every(call=>call.options.cache==='no-store'&&call.options.credentials==='same-origin'&&!call.options.method));
 assert.match(text(component.body),/5 \/ 50/);assert.ok(!text(component.body).includes('10 / 50'));component.clear();assert.equal(component.body.children.length,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
