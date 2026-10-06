"""Captured public evidence is content-free; local readers never dispatch."""
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.urls import reverse

from management.bot_access import META_REVIEWER_GROUP_NAME
from management.models import GeminiQuotaProfile, GeminiQuotaState, GeminiRequest, GeminiRequestAttempt
from management.services import gemini_v2_read_model as reader
from management.services.gemini_accounting_contract import rotate_quota_state_profile
from management.services.gemini_accounting_runtime import revision_request_execution
from management.services.ig_request_manifest import capture_request_context, capture_dispatch_context
from management.tests_ig_request_context_manifest import policy
from management import tests_gemini_v2_read_api as read_fixture


@override_settings(SECRET_KEY="synthetic-manifest-projection-secret")
class ManifestProjectionTests(SimpleTestCase):
    def setUp(self):
        self.payload = {"contents": [{"role": "user", "parts": [{"text": "PRIVATE customer +380501112233"}]}]}
        self.context = capture_request_context(payload=self.payload, metadata={
            "revision_id": 11, "client_id": 3, "source_message_ids": [31], "history_message_ids": [21, 22],
            "reset_floor": 20, "bundle_digest": "f" * 64, "builder_version": "ig-turn-intelligence.v1",
            "effective_mode": "unified", "selected_block_ids": ["history", "private:block"],
            "omitted_blocks": [{"block_id": "private:omitted", "reason": "budget_exhausted"},
                {"block_id": "other", "reason": "private_reason"}],
            "readiness_codes": ["ready", "private_readiness"], "budgets": {"history_entries": 2},
            "view_versions": {"memory_head_version": "private:head:8", "canonical_selection": "source-selection.v1",
                "state_view_version": "ig-client-state.v1", "memory_capture_digest": "e" * 64},
            "media": {"admitted_part_ids": ["31:0"], "omitted_part_ids": ["31:1"]},
        })
        self.graph = SimpleNamespace(pk=7, request_id="private-request-id", client_id=3, source_message_id=31,
            logical_turn_id="ig-revision:11", source_execution_key="ig-revision:11",
            policy_manifest={**policy(), "request_context": self.context})
        self.attempt = SimpleNamespace(request_graph_id=7, request_id=self.graph.request_id,
            client_id=3, source_message_id=31, logical_turn_id=self.graph.logical_turn_id,
            attempt_index=1, model=reader.MODELS[0], provider_started_at=timezone.now(), http_code=200,
            dispatch_manifest=capture_dispatch_context(payload=self.payload, context=self.context,
                attempt_index=1, model=reader.MODELS[0]))

    def test_captured_projection_has_counts_finite_codes_and_no_customer_identifiers_or_digests(self):
        public, captured = reader.project_request_context(self.graph)
        self.assertEqual(public["status"], "captured")
        self.assertEqual(public["counts"]["source_messages"], 1)
        self.assertEqual(public["counts"]["history_messages"], 2)
        self.assertEqual(public["readiness"], {"other": 1, "ready": 1})
        self.assertEqual(public["omissions"], {"budget_exhausted": 1, "other": 1})
        self.assertEqual(public["publication"]["hash"], "a" * 64)
        self.assertTrue(public["digest_presence"]["memory_capture"])
        serialized = json.dumps(public)
        for private in ("PRIVATE", "+380501112233", "private:", "private_", "request_digest", "source_message_ids",
                        "history_message_ids", self.context["request_digest"], self.context["context_digest"], "f" * 64):
            self.assertNotIn(private, serialized)
        self.assertIsNot(captured, self.context)
        captured["history_message_ids"].append(999)
        self.assertEqual(self.context["history_message_ids"], [21, 22])

    def test_no_current_state_reconstruction_for_legacy_or_malformed_policy(self):
        for value, status, reason in (({}, "uncaptured", "legacy_context_uncaptured"),
                (None, "invalid", "policy_manifest_invalid"),
                ({**self.graph.policy_manifest, "provider_body": "PRIVATE"}, "invalid", "request_context_invalid")):
            self.graph.policy_manifest = value
            public, context = reader.project_request_context(self.graph)
            self.assertEqual((public["status"], public["reason"]), (status, reason))
            self.assertIsNone(context)
            self.assertEqual(public["reconstruction"], "full_payload_not_retained")
            self.assertEqual(public["counts"], {})

    def test_every_owned_graph_binding_mismatch_is_explicit(self):
        for key, value in (("client_id", 4), ("logical_turn_id", "ig-revision:12"),
                ("source_execution_key", "ig-revision:12"), ("source_message_id", 32)):
            with self.subTest(key=key):
                changed = SimpleNamespace(**{**vars(self.graph), key: value})
                public, context = reader.project_request_context(changed)
                self.assertEqual(public["reason"], "request_context_binding_mismatch")
                self.assertIsNone(context)

    def test_dispatch_stage_repair_and_actual_attempt_binding(self):
        public = reader.project_dispatch_manifest(self.attempt, graph=self.graph, context=self.context)
        self.assertEqual(public["status"], "captured")
        self.assertEqual(public["payload_stage"], "http_dispatch")
        self.assertTrue(public["http_receipt_recorded"])
        repaired = capture_dispatch_context(payload={**self.payload, "repair": "PRIVATE repair"},
            context=self.context, attempt_index=1, model=self.attempt.model)
        self.assertNotEqual(repaired["request_digest"], self.attempt.dispatch_manifest["request_digest"])
        self.attempt.dispatch_manifest = repaired
        self.assertEqual(reader.project_dispatch_manifest(self.attempt, graph=self.graph, context=self.context), public)
        for key, value in (("request_graph_id", 8), ("request_id", "other"), ("client_id", 4),
                ("source_message_id", 32), ("logical_turn_id", "other"), ("attempt_index", 2), ("model", reader.MODELS[1])):
            changed = SimpleNamespace(**{**vars(self.attempt), key: value})
            self.assertEqual(reader.project_dispatch_manifest(changed, graph=self.graph, context=self.context)["reason"],
                "dispatch_binding_mismatch")
        self.assertNotIn("digest\":", json.dumps(public))

    def test_dispatch_missing_invalid_and_unknown_http_are_not_delivery_proof(self):
        self.attempt.dispatch_manifest = {}
        self.attempt.http_code = 0
        public = reader.project_dispatch_manifest(self.attempt, graph=self.graph, context=self.context)
        self.assertEqual(public["reason"], "provider_phase_started_without_capture")
        self.assertFalse(public["http_receipt_recorded"])
        self.attempt.provider_started_at = None
        self.assertEqual(reader.project_dispatch_manifest(self.attempt)["reason"], "dispatch_not_captured")
        self.attempt.dispatch_manifest = {"provider_body": "PRIVATE"}
        self.assertEqual(reader.project_dispatch_manifest(self.attempt)["reason"], "dispatch_manifest_invalid")

    def test_local_semantic_and_schema_reason_codes_do_not_echo_error_detail(self):
        for kind, detail, layer, codes, unknown in (
                ("local_semantic_rejection", "configuration_mismatch,unverified_price,PRIVATE@example.com", "local_semantic", ["configuration_mismatch", "unverified_price"], 1),
                ("invalid_response", "schema_invalid_json,PRIVATE", "schema", ["schema_invalid_json"], 1),
                ("invalid_response", "PRIVATE", "unknown", [], 1),
                ("provider_error", "unverified_price,PRIVATE", "none", [], 0)):
            public = reader.project_validation_reasons(SimpleNamespace(failure_kind=kind, error_detail=detail))
            self.assertEqual(public, {"layer": layer, "reason_codes": codes, "unknown_reason_count": unknown})
            self.assertNotIn("PRIVATE", json.dumps(public))

    def test_tpm_observations_do_not_imply_calibrated_headroom_or_generation(self):
        for calibrated, bound, known in ((False, True, False), (True, False, False), (True, True, True)):
            metric = reader._tpm_metric(used=100, limit=1000, complete=True,
                calibrated=calibrated, profile_bound=bound, usage_source="provider_reported")
            self.assertEqual(metric["used"], 100)
            self.assertEqual(metric["headroom_known"], known)
            self.assertEqual(metric["remaining"], 900 if known else None)
        metric = reader._tpm_metric(used=0, limit=1000, complete=True,
            calibrated=False, usage_source="no_observed_usage")
        self.assertEqual(metric["used"], 0)
        self.assertIsNone(metric["remaining"])
        unknown = reader._tpm_metric(used=0, limit=1000, complete=False, calibrated=None, usage_source="no_observed_usage")
        self.assertIsNone(unknown["used"])
        self.assertFalse(unknown["observed_usage_known"])

    def test_nonlive_current_mode_is_distinct_from_accounting_activity(self):
        for mode, active, expected in (("enforce", True, True), ("enforce", False, False),
                ("shadow", True, False), ("invalid-value", True, False)):
            with override_settings(GEMINI_NONLIVE_ADMISSION_MODE=mode):
                public = reader._nonlive_accounting_fields(accounting_active=active)
                self.assertEqual(public["nonlive_enforcement_active"], expected)
                self.assertEqual(public["nonlive_admission_mode"], mode if mode in {"shadow", "enforce"} else "invalid")
                self.assertEqual(public["capacity_authority"], "local_advisory_not_dispatch_permission")

    def test_actual_js_merge_and_viewport_helpers_preserve_history_details_focus_and_bounds(self):
        executable = shutil.which("node")
        if executable is None:
            self.skipTest("Node unavailable")
        script = Path(__file__).parent / "static" / "management" / "gemini_v2_panel.js"
        result = subprocess.run([executable, "-", str(script)], input=LEDGER_NODE_FIXTURE + CONTROLLER_NODE_FIXTURE,
            text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("behavioural fixtures PASS", result.stdout)


class CalibrationReadModelTests(TestCase):
    # Reuse the existing real graph/accounting fixture without inheriting its
    # unrelated test methods or bypassing immutable graph ownership guards.
    setUp = read_fixture.GeminiV2ReadApiTests.setUp
    _graph = read_fixture.GeminiV2ReadApiTests._graph
    _attempt = read_fixture.GeminiV2ReadApiTests._attempt
    _model_row = staticmethod(read_fixture.GeminiV2ReadApiTests._model_row)

    def test_latest_effective_profiles_materialize_one_row_per_model_in_one_read(self):
        model = reader.MODELS[0]
        for index in range(35):
            GeminiQuotaProfile.objects.create(profile_version=f"compact-{index}", model=model,
                rpm_limit=5, input_tpm_limit=250000, rpd_limit=20, permit_limit=1,
                estimator_version="json_bytes_div4_v1", source=GeminiQuotaProfile.Source.OWNER_OBSERVED,
                observed_at=self.now, effective_from=self.now - dt.timedelta(hours=12))
        latest = GeminiQuotaProfile.objects.filter(profile_version="compact-34").get()
        GeminiQuotaProfile.objects.create(profile_version="future", model=model, rpm_limit=5,
            input_tpm_limit=250000, rpd_limit=20, permit_limit=1, estimator_version="json_bytes_div4_v1",
            source=GeminiQuotaProfile.Source.OWNER_OBSERVED, observed_at=self.now,
            effective_from=self.now + dt.timedelta(hours=1))
        materialized = []
        original = GeminiQuotaProfile.from_db
        def record(*args, **kwargs):
            row = original(*args, **kwargs)
            materialized.append(row.pk)
            return row
        with patch.object(GeminiQuotaProfile, "from_db", side_effect=record), CaptureQueriesContext(connection) as queries:
            profiles = reader._active_profiles(self.now)
        self.assertEqual(profiles[model].pk, latest.pk)
        self.assertEqual(len(queries), 1)
        self.assertLessEqual(len(materialized), len(reader.MODELS))
        self.assertEqual(len(set(materialized)), len(materialized))

    def test_uncalibrated_observation_and_current_profile_switch_abstain_then_match(self):
        model = "gemini-3.7-flash"
        graph = self._graph("71")
        self._attempt(graph)
        state = GeminiQuotaState.objects.create(project_identity=self.groups["GEMINI_API"], model=model,
            quota_profile=self.profiles[model], pacific_day=self.now.astimezone(reader.PT).date())
        row = self._model_row(reader.build_quotas_payload(now=self.now))["projects"][0]
        self.assertEqual(row["input_tpm"]["used"], 100)
        self.assertEqual(row["input_tpm"]["calibration"], "uncalibrated")
        self.assertIsNone(row["input_tpm"]["remaining"])
        current = GeminiQuotaProfile.objects.create(profile_version="new-calibrated", model=model,
            rpm_limit=5, input_tpm_limit=250000, rpd_limit=20, permit_limit=1,
            estimator_version="json_bytes_div4_v1", source=GeminiQuotaProfile.Source.OWNER_OBSERVED,
            observed_at=self.now, effective_from=self.now - dt.timedelta(hours=1))
        row = self._model_row(reader.build_quotas_payload(now=self.now))["projects"][0]
        self.assertEqual(row["nonlive_profile"]["runtime_profile_binding"], "different")
        self.assertFalse(row["nonlive_profile"]["eligible_prerequisites"])
        self.assertEqual(state.in_flight_count, 0)
        rotated, audit = rotate_quota_state_profile(
            state_id=state.pk, new_profile_id=current.pk,
            expected_revision=state.revision, now=self.now,
            reason="verified calibration fixture profile rotation",
        )
        self.assertEqual(rotated.quota_profile_id, current.pk)
        self.assertEqual(rotated.revision, state.revision + 1)
        self.assertEqual(audit.action, "ig_gemini.quota_profile_rotated")
        row = self._model_row(reader.build_quotas_payload(now=self.now))["projects"][0]
        self.assertEqual(row["nonlive_profile"]["runtime_profile_binding"], "matched")
        self.assertTrue(row["input_tpm"]["headroom_known"])
        self.assertEqual(row["input_tpm"]["remaining"], 249900)

    def test_local_validation_has_own_status_but_real_provider_block_keeps_precedence(self):
        model = "gemini-3.7-flash"
        state = GeminiQuotaState.objects.create(project_identity=self.groups["GEMINI_API"], model=model,
            quota_profile=self.profiles[model], pacific_day=self.now.astimezone(reader.PT).date(),
            accounting_status=GeminiQuotaState.AccountingStatus.DEGRADED,
            last_failure_kind="local_semantic_rejection", last_failure_at=self.now, last_http_code=200)
        row = self._model_row(reader.build_quotas_payload(now=self.now))["projects"][0]
        self.assertEqual(row["status"], "local_validation_failed")
        self.assertEqual(row["last_failure_kind"], "local_semantic_rejection")
        self.assertEqual(row["last_http_code"], 200)
        self.assertIsNone(row["last_real_evidence"])
        self.assertIsNone(row["last_success_at"])
        GeminiQuotaState.objects.filter(pk=state.pk).update(provider_blocks={"tpm": {
            "until": (self.now + dt.timedelta(minutes=1)).isoformat(), "quota_id": "safe",
            "dimensions": {}, "retry_after_seconds": 60}})
        self.assertEqual(self._model_row(reader.build_quotas_payload(now=self.now))["projects"][0]["status"], "tpm_limited")

    @override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
    def test_existing_operation_permission_and_reviewer_denial_precede_public_reader(self):
        url = reverse("management_bot_gemini_v2_attempts_api")
        with patch.object(reader, "build_attempts_payload", return_value={"schema_version": 1, "items": []}) as build:
            self.client.logout()
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(self.user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.admin.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
            self.client.force_login(self.admin)
            self.assertEqual(self.client.get(url).status_code, 403)
            build.assert_not_called()
            self.admin.groups.clear()
            self.assertEqual(self.client.get(url).status_code, 200)
            build.assert_called_once()

    def test_all_metadata_reads_are_bounded_zero_dml_zero_provider_and_retention_is_truthful(self):
        graph = self._graph("72")
        self._attempt(graph)
        with patch("management.services.call_ai_analysis.requests.post") as http, \
                patch("management.services.gemini_probe.probe_key_metadata") as probe, \
                patch("management.services.gemini_metadata_health.urlopen") as metadata:
            all_queries = []
            for builder, limit in ((reader.build_quotas_payload, 6), (reader.build_routes_payload, 2), (reader.build_attempts_payload, 4)):
                with CaptureQueriesContext(connection) as queries:
                    payload = builder(now=self.now)
                self.assertLessEqual(len(queries), limit)
                all_queries.extend(queries)
            http.assert_not_called(); probe.assert_not_called(); metadata.assert_not_called()
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in all_queries))
        self.assertEqual(payload["retention"], {"technical_ledger_retention": "unbounded_currently",
            "automated_purge": "not_configured", "cursor_ttl_seconds": 30 * 24 * 3600, "render_cap": 100})
        self.assertEqual(payload["items"][0]["context_capture"]["status"], "uncaptured")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class CapturedGraphReadTests(TransactionTestCase):
    def test_real_immutable_graph_and_dispatch_project_without_bootstrap_or_provider(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        case = RevisionLiveTests(methodName="runTest")
        case.setUp(); self.addCleanup(case.doCleanups); case._prepare()
        payload = {"contents": [{"role": "user", "parts": [{"text": "PRIVATE final prompt"}]}]}
        context = capture_request_context(payload=payload, metadata={"revision_id": case.revision.pk,
            "client_id": case.customer.pk, "source_message_ids": [case.source.pk],
            "bundle_digest": case.revision.snapshot_digest, "builder_version": "ig-turn-intelligence.v1", "effective_mode": "unified"})
        execution = f"ig-revision:{case.revision.pk}"
        with revision_request_execution(case.revision.pk, case.token, settings_id=case.settings.pk,
                settings_permission_epoch=case.settings.reply_permission_epoch):
            graph = GeminiRequest.objects.create(request_id="synthetic-public-captured", client_id=case.customer.pk,
                source_message_id=case.source.pk, lane="live", task_class="ordinary_live", logical_turn_id=execution,
                source_execution_key=execution, accounting_mode="shadow", policy_manifest={**policy(), "request_context": context})
        GeminiRequestAttempt.objects.create(request_graph=graph, request_id=graph.request_id, role="chat", key_name="synthetic-key",
            client_id=graph.client_id, source_message_id=graph.source_message_id, logical_turn_id=execution, lane="live",
            model=reader.MODELS[0], attempt_index=1, candidate_index=1, outcome="failed", fsm_state="failed",
            http_code=200, failure_kind="local_semantic_rejection", error_detail="unverified_price,PRIVATE error",
            provider_started_at=timezone.now(), dispatch_pacific_day=timezone.localdate(),
            dispatch_manifest=capture_dispatch_context(payload=payload, context=context, attempt_index=1, model=reader.MODELS[0]))
        with patch("management.services.call_ai_analysis.requests.post") as http, CaptureQueriesContext(connection) as queries:
            public = reader.build_attempts_payload()
        self.assertLessEqual(len(queries), 4)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        http.assert_not_called()
        item = public["items"][0]
        self.assertEqual(item["context_capture"]["status"], "captured")
        self.assertEqual(item["attempts"][0]["dispatch_capture"]["status"], "captured")
        self.assertEqual(item["attempts"][0]["validation"]["reason_codes"], ["unverified_price"])
        for secret in ("PRIVATE", graph.request_id, execution, context["context_digest"], context["request_digest"]):
            self.assertNotIn(secret, json.dumps(public))


LEDGER_NODE_FIXTURE = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const window={document:{getElementById:()=>null,activeElement:null,scrollingElement:{scrollTop:700}},innerHeight:600,
 getComputedStyle:()=>({overflowY:'visible'})};
const sandbox={window,document:window.document};vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),sandbox);
const {merge,captureViewport,restoreViewport}=window.GeminiV2LedgerState;
const old=Array.from({length:100},(_,index)=>({request_ref:'ref'+index,attempts:[{value:index}]}));
let merged=merge(old,[{request_ref:'new'}, {request_ref:'ref0',attempts:[{value:999}]}],{capturedAt:'new-snapshot'});
assert.equal(merged.items.length,100);assert.equal(merged.unshownNew,1);assert.equal(merged.items[0].request_ref,'ref0');
assert.equal(merged.items[0].attempts[0].value,999);assert.equal(old[0].attempts[0].value,0);assert.equal(merged.items[99].request_ref,'ref99');
merged=merge(old.slice(0,2),[{request_ref:'new'},{request_ref:'new'},{request_ref:'ref0'}],{cap:4,capturedAt:'captured'});
assert.deepEqual(JSON.parse(JSON.stringify(merged.items.map(item=>item.request_ref))),['new','ref0','ref1']);
assert.equal(merged.items[0]._snapshot_at,'captured');assert.equal(merged.items[2]._snapshot_at,undefined);
const summary={focus:options=>{assert.equal(options.preventScroll,true);summary.focused=true;}};
const original={dataset:{graphRef:'ref0'},getBoundingClientRect:()=>({top:30,bottom:200}),querySelector:()=>summary};
const container={querySelectorAll:()=>[original],scrollHeight:400,clientHeight:400,parentElement:null,contains:row=>row===original};
window.document.activeElement={closest:()=>original};let snapshot=captureViewport(container);
const replaced={dataset:{graphRef:'ref0'},getBoundingClientRect:()=>({top:110,bottom:280}),querySelector:()=>summary};
container.querySelectorAll=()=>[replaced];restoreViewport(container,snapshot);
assert.equal(window.document.scrollingElement.scrollTop,780);assert.equal(summary.focused,true);
// The same viewport rule applies inside the panel's own scrollable ancestor.
const parent={scrollHeight:900,clientHeight:300,parentElement:null,scrollTop:25};
container.parentElement=parent;window.getComputedStyle=element=>({overflowY:element===parent?'auto':'visible'});
window.document.activeElement=null;snapshot=captureViewport(container);assert.equal(snapshot.scroller,parent);
container.querySelectorAll=()=>[{...replaced,getBoundingClientRect:()=>({top:150,bottom:300})}];restoreViewport(container,snapshot);
assert.equal(parent.scrollTop,65);
process.stdout.write('behavioural fixtures PASS\n');
"""

CONTROLLER_NODE_FIXTURE = r"""
// Exercise the actual panel's event handlers, fetch paths and DOM replacement.
(async()=>{
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.events={};this.className='';this._text='';this.scrollTop=0;this.scrollHeight=400;this.clientHeight=400;this.hidden=false;this.open=false;
  this.classList={contains:name=>this.className.split(' ').includes(name),add:name=>{if(!this.classList.contains(name))this.className+=' '+name;},remove:name=>{this.className=this.className.split(' ').filter(value=>value!==name).join(' ');},toggle:(name,on)=>on?this.classList.add(name):this.classList.remove(name)};}
 append(...nodes){nodes.forEach(child=>{child.parentElement=this;this.children.push(child);});}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
 get firstElementChild(){return this.children[0];}get lastElementChild(){return this.children.at(-1);}
 setAttribute(key,value){this.attrs[key]=value;}getAttribute(key){return this.attrs[key]??null;}removeAttribute(key){delete this.attrs[key];}
 insertAdjacentElement(_position,node){this.parentElement.append(node);}
 addEventListener(type,callback){this.events[type]=callback;}click(){if(!this.disabled&&this.events.click)this.events.click({});}
 querySelectorAll(selector){const matches=child=>selector==='[data-graph-ref]'?!!child.dataset.graphRef:selector==='details[open][data-graph-ref]'?child.tagName==='details'&&child.open&&!!child.dataset.graphRef:selector==='summary'?child.tagName==='summary':selector==='details'||selector==='details[data-capture-key]'||selector==='[data-capture-key]'?child.tagName==='details'&&!!child.dataset.captureKey:selector==='details[open][data-capture-key]'?child.tagName==='details'&&child.open&&!!child.dataset.captureKey:false;
  return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){if(selector==='[data-graph-ref]'&&this.dataset.graphRef)return this;return this.parentElement?this.parentElement.closest(selector):null;}
 contains(target){return this===target||this.children.some(child=>child.contains(target));}
 focus(){doc.activeElement=this;}getBoundingClientRect(){return{top:30,bottom:150};}
}
const ids=new Map(),get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);};
const panel=get('gemini-v2-panel'),outer=new Element();outer.className='bot-panel active';outer.append(panel);
const controls=new Element();controls.append(get('gemini-v2-refresh'));
const tabs=Object.fromEntries(['quotas','routes','attempts'].map(name=>[name,new Element('button')]));
const views=Object.fromEntries(['quotas','routes','attempts'].map(name=>[name,new Element()]));
panel.querySelector=selector=>{const match=selector.match(/data-gemini-(tab|view)="([a-z]+)"/);return match?(match[1]==='tab'?tabs:views)[match[2]]:null;};
panel.closest=()=>outer;panel.dataset={schemaVersion:'1',quotasUrl:'/quotas',routesUrl:'/routes',attemptsUrl:'/attempts',probeUrl:'/probe',refreshInterval:'60000'};
const doc={getElementById:get,createElement:tag=>new Element(tag),createTextNode:text=>{const element=new Element('text');element.textContent=text;return element;},querySelector:()=>null,addEventListener:()=>{},hidden:false,activeElement:null,scrollingElement:{scrollTop:0}};
const calls=[],responses=[];let timers=0;
const browser={document:doc,location:{origin:'https://local.invalid'},innerHeight:600,getComputedStyle:()=>({overflowY:'visible'}),setInterval:()=>++timers,clearInterval:()=>{}};
const fetcher=async(url,options)=>{calls.push({url,options});if(!responses.length)throw new Error('unplanned read');return {ok:true,json:async()=>responses.shift()};};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{window:browser,document:doc,URL,AbortController,fetch:fetcher,requestAnimationFrame:callback=>callback()});
const ref=number=>'greq_'+number.toString(16).padStart(20,'0');
const item=number=>({request_ref:ref(number),created_at:'2026-10-06T10:00:00Z',candidate_plan:[],attempts:[],winner:null,
 resolution:{state:'pending',reason:''},reply:{state:'not_linked',provider_receipt_present:false},lane:'live',task_class:'ordinary_live'});
const page=(numbers,cursor)=>({schema_version:1,generated_at:'2026-10-06T10:00:00Z',items:numbers.map(item),next_cursor:cursor,
 retention:{technical_ledger_retention:'unbounded_currently',automated_purge:'not_configured',cursor_ttl_seconds:2592000,render_cap:100}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
responses.push(page([3,2],'original-cursor'));tabs.attempts.click();await settle();
const ledger=get('gemini-v2-attempts-content');let rows=ledger.querySelectorAll('[data-graph-ref]');assert.equal(rows.length,2);rows[1].open=true;rows[1].querySelector('[data-capture-key]').open=true;rows[1].querySelector('[data-capture-key]').querySelector('summary').focus();
responses.push(page([4,3],'replacement-head-cursor'));get('gemini-v2-refresh').click();await settle();
responses.push(page([1],null));get('gemini-v2-load-more').click();await settle();assert.ok(calls.at(-1).url.includes('original-cursor'));assert.equal(ledger.querySelectorAll('[data-graph-ref]').length,4);
const pause=controls.children.find(element=>element.textContent==='Пауза оновлення');assert.ok(pause);pause.click();const count=calls.length;
assert.equal(pause.getAttribute('aria-pressed'),'true');assert.equal(get('gemini-v2-load-more').disabled,true);
assert.equal(await browser.GeminiV2Panel.load(),false);assert.equal(calls.length,count);
responses.push(page([5,4],'different-head-cursor'));pause.click();await settle();
rows=ledger.querySelectorAll('[data-graph-ref]');assert.deepEqual(rows.map(row=>row.dataset.graphRef),[ref(5),ref(4),ref(3),ref(2),ref(1)]);
assert.equal(rows.find(row=>row.dataset.graphRef===ref(2)).open,true);assert.equal(rows.find(row=>row.dataset.graphRef===ref(2)).querySelector('[data-capture-key]').open,true);assert.equal(doc.activeElement.closest('[data-graph-ref]').dataset.graphRef,ref(2));
// A refreshed head cannot replace the tail continuation cursor (null here).
assert.equal(get('gemini-v2-attempts-more').hidden,true);
responses.push({...page([9],'bad'),schema_version:999});get('gemini-v2-refresh').click();await settle();
assert.equal(ledger.querySelectorAll('[data-graph-ref]').length,5);assert.ok(get('gemini-v2-attempts-error').textContent.includes('історію'));
assert.equal(calls.filter(call=>call.options.method!=='GET'||call.url.includes('/probe')).length,0);
process.stdout.write('controller pause/refresh fixtures PASS\n');
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""
