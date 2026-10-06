"""Read-side console integrity; no provider or producer mutation under test."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import shutil
import subprocess

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.urls import reverse

from management.models import IgClient, InstagramBotLog, InstagramBotSettings
from management.services.ig_console_read_model import (
    PAGE_MAX, build_console_payload, console_access_allowed, encode_console_event, project_console_row,
)


class ConsoleProjectionTests(SimpleTestCase):
    def row(self, *, detail="", event="legacy", level="info"):
        return SimpleNamespace(pk=12, detail=detail, event=event, level=level, created_at=timezone.now())

    def test_raw_historical_chat_exception_provider_and_event_are_omitted(self):
        secrets = ["Buyer +380501112233", "customer@example.com", "PRIVATE_PROVIDER_BODY", "GEMINI_API", "<script>danger()</script>"]
        item = project_console_row(self.row(detail=" ".join(secrets), event=" ".join(secrets), level="error"))
        self.assertEqual((item["kind"], item["reason"], item["scope"]), ("legacy_event", "legacy_unstructured", {}))
        self.assertTrue(item["actionable"])
        self.assertEqual(item["detail"], "")
        for secret in secrets:
            self.assertNotIn(secret, json.dumps(item))

    def test_extra_fields_and_invalid_nested_scope_cannot_smuggle_content(self):
        valid = json.loads(encode_console_event(kind="reply_failed", scope={"client_id": 3, "attempt_id": 7}, reason="failed"))
        for changed in ({**valid, "provider_body": "PRIVATE"}, {**valid, "scope": {"client_id": 3, "note": "PRIVATE"}},
                {**valid, "reason": "customer_private_name"}, {**valid, "kind": "customer_private_name"},
                {**valid, "scope": {"request_ref": "PRIVATE"}}, {**valid, "scope": {"client_id": True}},
                {**valid, "schema_version": True}, {**valid, "scope": {"task_key": "PRIVATE"}}):
            with self.subTest(changed=changed):
                item = project_console_row(self.row(detail=json.dumps(changed)))
                self.assertFalse(item["structured"])
                self.assertEqual(item["scope"], {})
                self.assertNotIn("PRIVATE", json.dumps(item))

    def test_encoder_rejects_free_text_and_noncanonical_identity_forms(self):
        for values in ({"kind": "private event"}, {"kind": []}, {"kind": "error", "reason": "raw exception"},
                {"kind": "error", "scope": {"client_id": "²"}}, {"kind": "error", "scope": {"message_id": -1}},
                {"kind": "error", "scope": {"phone": "+380501112233"}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                encode_console_event(**values)

    def test_valid_metadata_is_detached_and_only_ids_codes_and_time_survive(self):
        scope = {"client_id": 3, "attempt_id": 8, "request_ref": "greq_" + "a" * 20}
        encoded = encode_console_event(kind="provider_attempt", scope=scope, reason="provider_timeout")
        item = project_console_row(self.row(detail=encoded, event="untrusted event label"))
        scope["attempt_id"] = 99
        self.assertEqual(item["scope"]["attempt_id"], 8)
        self.assertEqual(item["kind"], "provider_attempt")
        self.assertNotIn("untrusted event label", json.dumps(item))
        self.assertTrue(item["structured"])

    def test_oversized_invalid_json_and_unknown_level_fail_closed(self):
        for detail in ("x" * 4001, '{"schema_version":NaN}', '[' * 1000):
            item = project_console_row(self.row(detail=detail, level="private customer label"))
            self.assertEqual(item["level"], "info")
            self.assertFalse(item["structured"])

    def test_denial_is_literal_true_only_and_reads_no_database(self):
        for permission in (False, None, "allowed", 1):
            result = build_console_payload(can_view=permission, after_id=7)
            self.assertEqual(result["access"], "denied")
            self.assertEqual((result["items"], result["next_after_id"]), ([], 7))
            self.assertIsNone(result["range"]["retained_rows"])


    def test_actual_js_controller_preserves_pause_cursor_filters_gap_and_bounded_dom(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable for the isolated browser-state fixture")
        script = Path(__file__).parent / "static" / "management" / "ig_console.js"
        completed = subprocess.run([node, "-", str(script)], input=CONSOLE_NODE_FIXTURE + CONSOLE_STATUS_NODE_FIXTURE,
            text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("behavioural fixtures PASS", completed.stdout)

    def test_actual_js_paused_explicit_refresh_keeps_single_owner_and_rejects_stale_response(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable for the isolated browser-state fixture")
        script = Path(__file__).parent / "static" / "management" / "ig_console.js"
        completed = subprocess.run([node, "-", str(script)], input=CONSOLE_NODE_FIXTURE + CONSOLE_PAUSED_REFRESH_NODE_FIXTURE,
            text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("paused explicit refresh fixtures PASS", completed.stdout)


class ConsoleContinuationTests(TestCase):
    def append(self, *, kind="inbound_received", scope=None, reason="accepted", level="info"):
        return InstagramBotLog.objects.create(level=level, event="PRIVATE raw event",
            detail=encode_console_event(kind=kind, scope=scope or {"client_id": 1, "message_id": 1}, reason=reason))

    def read(self, **kwargs):
        return build_console_payload(can_view=True, retention_target_rows=500, **kwargs)

    def test_more_than_120_events_drain_ascending_without_skipping_earliest(self):
        rows = [self.append(scope={"client_id": 1, "message_id": index + 1}) for index in range(251)]
        cursor, ids, sizes = 0, [], []
        for _page in range(3):
            payload = self.read(after_id=cursor)
            ids.extend(item_id for item in payload["items"] for item_id in item["ids"])
            sizes.append(payload["scanned_rows"])
            self.assertEqual(payload["next_after_id"], rows[len(ids) - 1].pk)
            cursor = payload["next_after_id"]
        self.assertEqual(ids, [row.pk for row in rows])
        self.assertEqual(sizes, [120, 120, 11])
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["range"]["retained_rows"], 251)

    def test_append_between_pages_does_not_duplicate_or_drop_new_event(self):
        rows = [self.append(scope={"message_id": index + 1}) for index in range(121)]
        first = self.read()
        new = self.append(scope={"message_id": 122})
        second = self.read(after_id=first["next_after_id"])
        ids = [value for payload in (first, second) for item in payload["items"] for value in item["ids"]]
        self.assertEqual(ids, [row.pk for row in rows] + [new.pk])
        self.assertFalse(second["has_more"])

    def test_hidden_routine_rows_advance_cursor_and_actionable_health_survives(self):
        for _index in range(120):
            self.append(kind="health", scope={"task_key": "analysis"}, reason="healthy")
        error = self.append(kind="health", scope={"task_key": "analysis"}, reason="stalled", level="error")
        first = self.read()
        self.assertEqual(first["items"], [])
        self.assertTrue(first["has_more"])
        self.assertGreater(first["next_after_id"], 0)
        second = self.read(after_id=first["next_after_id"])
        self.assertEqual([item["id"] for item in second["items"]], [error.pk])
        self.assertTrue(second["items"][0]["actionable"])

    def test_default_skips_120_harmless_legacy_rows_but_preserves_current_and_uncertain_events(self):
        legacy = [InstagramBotLog.objects.create(event="PRIVATE raw event", detail="PRIVATE customer@example.com", level="info")
                  for _index in range(120)]
        current = [self.append(scope={"client_id": 7, "message_id": 22}) for _index in range(2)]
        warning = InstagramBotLog.objects.create(event="PRIVATE warning", detail="PRIVATE +380501112233", level="warning")
        uncertain = self.append(kind="legacy_event", scope={"attempt_id": 5}, reason="delivery_unknown", level="info")
        tail = InstagramBotLog.objects.create(event="PRIVATE raw tail", detail="PRIVATE_PROVIDER_BODY", level="info")
        first = self.read()
        self.assertEqual((first["items"], first["scanned_rows"], first["next_after_id"], first["has_more"]),
                         ([], 120, legacy[-1].pk, True))
        second = self.read(after_id=first["next_after_id"])
        self.assertEqual([item["ids"] for item in second["items"]], [[row.pk for row in current], [warning.pk], [uncertain.pk]])
        self.assertEqual(second["items"][0]["count"], 2)
        self.assertTrue(all(item["actionable"] for item in second["items"][1:]))
        self.assertEqual((second["scanned_rows"], second["next_after_id"], second["has_more"]), (5, tail.pk, False))
        history_first = self.read(filters={"category": "unknown"})
        history_second = self.read(after_id=history_first["next_after_id"], filters={"category": "unknown"})
        self.assertEqual([item["id"] for item in history_first["items"]], [row.pk for row in legacy])
        self.assertTrue(all(item["count"] == 1 for item in history_first["items"]))
        self.assertEqual([item["id"] for item in history_second["items"]], [warning.pk, uncertain.pk, tail.pk])
        self.assertEqual(history_second["next_after_id"], tail.pk)
        self.assertFalse(history_second["has_more"])
        for result in (first, second, history_first, history_second):
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertNotIn("customer@example.com", json.dumps(result))
            self.assertNotIn("+380501112233", json.dumps(result))

    def test_retention_gap_and_scan_cursor_survive_hidden_legacy_page(self):
        discarded = [InstagramBotLog.objects.create(event="PRIVATE", detail="PRIVATE", level="info") for _index in range(10)]
        hidden = [InstagramBotLog.objects.create(event="PRIVATE", detail="PRIVATE", level="info") for _index in range(120)]
        error = InstagramBotLog.objects.create(event="PRIVATE", detail="PRIVATE", level="error")
        InstagramBotLog.objects.filter(pk__lte=discarded[-1].pk).delete()
        first = self.read(after_id=discarded[0].pk)
        self.assertEqual(first["items"], [])
        self.assertEqual((first["retention_gap"], first["gap_reason"], first["lost_rows"]), (True, "before_oldest_available", None))
        self.assertEqual(first["range"]["oldest_available_id"], hidden[0].pk)
        self.assertEqual((first["next_after_id"], first["has_more"]), (hidden[-1].pk, True))
        second = self.read(after_id=first["next_after_id"])
        self.assertEqual([item["id"] for item in second["items"]], [error.pk])
        self.assertFalse(second["retention_gap"])
        self.assertFalse(second["has_more"])
        history = self.read(after_id=discarded[0].pk, filters={"category": "unknown"})
        self.assertEqual([item["id"] for item in history["items"]], [row.pk for row in hidden])
        self.assertTrue(history["retention_gap"])
        self.assertNotIn("PRIVATE", json.dumps(history))

    def test_pause_cursor_after_retention_reports_gap_without_invented_lost_count(self):
        rows = [self.append(scope={"message_id": index + 1}) for index in range(150)]
        paused_cursor = rows[10].pk
        InstagramBotLog.objects.filter(pk__lte=rows[29].pk).delete()
        resumed = self.read(after_id=paused_cursor)
        self.assertTrue(resumed["retention_gap"])
        self.assertEqual(resumed["gap_reason"], "before_oldest_available")
        self.assertIsNone(resumed["lost_rows"])
        self.assertEqual(resumed["range"]["oldest_available_id"], rows[30].pk)
        self.assertEqual([value for item in resumed["items"] for value in item["ids"]], [row.pk for row in rows[30:]])
        self.assertEqual(resumed["retention"]["target_rows"], 500)

    def test_initial_available_history_does_not_claim_pre_retention_completeness(self):
        self.append()
        first = self.read(after_id=0)
        self.assertFalse(first["retention_gap"])
        self.assertIsNone(first["lost_rows"])
        self.assertEqual(first["range"]["retained_rows"], 1)
        unknown_policy = build_console_payload(can_view=True)
        self.assertEqual(unknown_policy["retention"], {"target_rows": None, "policy_source": "unknown"})

    def test_cursor_ahead_is_reported_and_not_rewound_implicitly(self):
        row = self.append()
        cursor = row.pk + 200
        result = self.read(after_id=cursor)
        self.assertEqual(result["gap_reason"], "cursor_ahead_of_available_stream")
        self.assertEqual((result["items"], result["next_after_id"]), ([], cursor))

    def test_grouping_requires_same_kind_reason_client_attempt_and_severity(self):
        same = [self.append(kind="error", scope={"client_id": 1, "attempt_id": 5}, reason="failed", level="error") for _index in range(2)]
        other_client = self.append(kind="error", scope={"client_id": 2, "attempt_id": 5}, reason="failed", level="error")
        other_attempt = self.append(kind="error", scope={"client_id": 2, "attempt_id": 6}, reason="failed", level="error")
        other_reason = self.append(kind="error", scope={"client_id": 2, "attempt_id": 6}, reason="deadline", level="error")
        other_severity = self.append(kind="error", scope={"client_id": 2, "attempt_id": 6}, reason="deadline", level="warning")
        items = self.read()["items"]
        self.assertEqual([item["ids"] for item in items], [[row.pk for row in same], [other_client.pk], [other_attempt.pk], [other_reason.pk], [other_severity.pk]])
        self.assertEqual(items[0]["count"], 2)

    def test_unknown_attempt_scope_and_legacy_rows_never_group(self):
        for _index in range(2):
            self.append(kind="error", scope={"client_id": 1}, reason="failed", level="error")
            InstagramBotLog.objects.create(event="PRIVATE", detail="raw PRIVATE", level="error")
        items = self.read()["items"]
        self.assertEqual(len(items), 4)
        self.assertTrue(all(item["count"] == 1 for item in items))
        self.assertNotIn("PRIVATE", json.dumps(items))

    def test_client_and_reason_filter_preserve_scan_boundary(self):
        first = self.append(kind="error", scope={"client_id": 1, "attempt_id": 3}, reason="failed", level="error")
        second = self.append(kind="error", scope={"client_id": 2, "attempt_id": 4}, reason="deadline", level="error")
        result = self.read(filters={"client_id": 1, "reason": "failed", "category": "errors"})
        self.assertEqual([item["id"] for item in result["items"]], [first.pk])
        self.assertEqual(result["next_after_id"], second.pk)
        self.assertEqual(result["scanned_rows"], 2)
        invalid = self.read(filters={"client_id": "<script>private()</script>"})
        self.assertEqual(invalid["items"], [])
        self.assertNotIn("<script>", json.dumps(invalid))

    def test_routine_filter_is_explicit_and_default_does_not_hide_legacy_errors(self):
        routine = self.append(kind="cron", scope={"task_key": "instagram_periodic"}, reason="completed")
        legacy = InstagramBotLog.objects.create(event="raw SECRET", detail="+380501112233", level="error")
        self.assertEqual([item["id"] for item in self.read()["items"]], [legacy.pk])
        self.assertEqual([item["id"] for item in self.read(filters={"category": "routine"})["items"]], [routine.pk])

    def test_query_budget_is_constant_and_reader_has_zero_dml_and_provider_calls(self):
        for index in range(150):
            self.append(scope={"message_id": index + 1})
        with patch("requests.sessions.Session.request", side_effect=AssertionError("provider I/O")), CaptureQueriesContext(connection) as captured:
            payload = self.read()
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in captured))
        self.assertEqual(payload["scanned_rows"], PAGE_MAX)
        with self.assertNumQueries(0):
            denied = build_console_payload(can_view=False)
        self.assertEqual(denied["access"], "denied")

    def test_oversized_historical_detail_is_bounded_at_sql_boundary(self):
        row = InstagramBotLog.objects.create(event="PRIVATE", detail="PRIVATE" * 100000, level="error")
        with CaptureQueriesContext(connection) as captured:
            payload = self.read()
        self.assertEqual(len(captured), 2)
        self.assertTrue(any("SUBSTR" in query["sql"].upper() for query in captured))
        self.assertEqual(payload["items"][0]["id"], row.pk)
        self.assertEqual(payload["items"][0]["reason"], "legacy_unstructured")
        self.assertNotIn("PRIVATE", json.dumps(payload))

    def test_impact_reason_survives_routine_filter_even_if_producer_level_is_info(self):
        failure = self.append(kind="health", scope={"task_key": "analysis"}, reason="stalled", level="info")
        items = self.read()["items"]
        self.assertEqual([item["id"] for item in items], [failure.pk])
        self.assertTrue(items[0]["actionable"])

    def test_current_capabilities_are_both_required_not_staff_alone(self):
        user = get_user_model().objects.create_user(username="console-reader", is_staff=True)
        self.assertFalse(console_access_allowed(user))
        operate = Permission.objects.get(content_type__app_label="management", codename="operate_ig_bot")
        pii = Permission.objects.get(content_type__app_label="management", codename="view_ig_conversation_pii")
        user.user_permissions.add(operate)
        user = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(console_access_allowed(user))
        user.user_permissions.add(pii)
        user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(console_access_allowed(user))
        user.user_permissions.remove(operate)
        user = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(console_access_allowed(user))
        user.user_permissions.add(operate)
        reviewer, _created = Group.objects.get_or_create(name="Meta Bot Reviewer")
        user.groups.add(reviewer)
        user = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(console_access_allowed(user), "reviewer membership dominates granted capabilities")

    def test_invalid_cursor_is_finite_failure_and_limit_cannot_exceed_existing_page_cap(self):
        for cursor in (-1, True, "²", "private", 2**64):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "console_cursor_invalid"):
                self.read(after_id=cursor)
        self.assertEqual(self.read(limit=100000)["limit"], PAGE_MAX)


CONSOLE_NODE_FIXTURE = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let now=100000,timerId=0,timers=new Map(),intervals=[],stored=new Map(),responses=[],requests=[];
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.scrollTop=0;this.clientHeight=400;this.textContent='';this.listeners={};}
 querySelectorAll(selector){return this.children.flatMap(child=>[...(selector==='details[open][data-overview-section]'&&child.open&&child.dataset.overviewSection?[child]:[]),...child.querySelectorAll(selector)]);}
 get scrollHeight(){return this.children.length*20;}get firstChild(){return this.children[0];}
 appendChild(row){this.children.push(row);}append(...rows){rows.forEach(row=>this.appendChild(row));}
 removeChild(row){this.children.splice(this.children.indexOf(row),1);}replaceChildren(...rows){this.children=[];this.append(...rows);}
 setAttribute(key,value){this[key]=value;}addEventListener(key,fn){this.listeners[key]=fn;}removeEventListener(key){delete this.listeners[key];}
 set innerHTML(_value){throw new Error('unsafe HTML rendering');}
}
const doc={hidden:false,createElement:tag=>new Element(tag),addEventListener(){},removeEventListener(){}};
const fake={document:doc,location:{href:'https://console.example/bot/',origin:'https://console.example'},localStorage:{getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value)},setTimeout:(fn)=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),setInterval:fn=>{intervals.push(fn);return intervals.length;},clearInterval(){},fetch:async(url,options)=>{requests.push({url:String(url),options});return responses.shift();},AbortController,URL,Date:class extends Date{static now(){return now;}},console};
fake.window=fake;vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),fake);
function item(id){return {id,ids:[id],count:1,kind:'inbound_received',level:'info',reason:'accepted',scope:{message_id:id},last_at:'2026-10-06T10:00:00Z',event:'<script>PRIVATE</script>',detail:'PRIVATE'};}
function payload(start,end,more){return {schema_version:1,items:Array.from({length:end-start+1},(_,index)=>item(start+index)),next_after_id:end,has_more:more,retention_gap:false,range:{oldest_available_id:1,newest_available_id:end,retained_rows:end},retention:{target_rows:500},access:'allowed'};}
function response(data){return {ok:true,json:async()=>data};}
function next(){const [id,fn]=timers.entries().next().value;timers.delete(id);return fn();}
const consoleBaselineDone=(async()=>{
 const box=new Element(),status=new Element(),range=new Element(),button=new Element();
 responses=[response(payload(1,120,true)),response(payload(121,240,true)),response(payload(241,360,true)),response(payload(361,480,false))];
 const controller=fake.IgConsole.create({enabled:true,endpoint:'/status/',container:box,statusElement:status,rangeElement:range,pauseButton:button});
 await next();assert.equal(controller.getState().cursor,480);assert.equal(box.children.length,400);assert.equal(box.children[0].dataset.consoleId,'81');
 assert.deepEqual(requests.map(row=>new URL(row.url).searchParams.get('after_id')),['0','120','240','360']);
 assert.ok(box.children.every(row=>!row.children.some(child=>child.textContent.includes('PRIVATE'))));
 let release;responses=[new Promise(resolve=>{release=resolve;})];const inFlight=next();controller.pause();assert.equal(requests.at(-1).options.signal.aborted,true);
 release(response(payload(481,481,false)));await inFlight;assert.equal(controller.getState().cursor,480);
 now+=60000;intervals[0]();assert.ok(status.textContent.includes('60 с'));assert.ok(status.textContent.includes('Пауза'));
 responses=[response(payload(481,481,false))];controller.resume();await next();assert.equal(controller.getState().cursor,481);assert.equal(box.children.length,400);
 controller.setFilters({category:'errors',client_id:'7'});responses=[response({...payload(1,1,false),retention_gap:true})];await next();assert.equal(new URL(requests.at(-1).url).searchParams.get('after_id'),'0');assert.equal(new URL(requests.at(-1).url).searchParams.get('client_id'),'7');assert.ok(status.textContent.includes('Пропуск'));assert.ok(stored.get('ig-console.filters.v1').includes('errors'));
 const stable=controller.getState().cursor;responses=[response({...payload(2,2,false),items:[{...item(2),reason:'<script>PRIVATE</script>'}]})];await next();assert.equal(controller.getState().cursor,stable);assert.ok(status.textContent.includes('недоступне'));
 controller.destroy();const prior=requests.length;const denied=fake.IgConsole.create({enabled:false,endpoint:'/status/',container:new Element()});assert.equal(requests.length,prior);denied.destroy();
 console.log('console behavioural fixtures PASS: ASC drain, cap, safe text, pause cursor, resume, filter reset, persisted preferences, gap, freshness, invalid schema, denied access');
})().catch(error=>{console.error(error);process.exit(1);});
"""


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class ConsoleStatusIntegrationTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="console-status-operator")
        for code in ("operate_ig_bot", "view_ig_conversation_pii"):
            self.actor.user_permissions.add(Permission.objects.get(content_type__app_label="management", codename=code))
        self.client.force_login(self.actor)
        self.url = reverse("management_bot_status_api")
        self.status = {"state": "running", "running": True, "daemon_online": True, "pending": 2, "settings_revision": 7}

    def read(self, **params):
        with patch("management.bot_views.bot.status_snapshot", return_value=self.status), \
                patch("management.bot_overview_views.read_overview_snapshot", return_value={"schema_version": "ig-overview.v1", "components": {}}):
            return self.client.get(self.url, params)

    def test_real_api_continuation_drains_251_without_losing_oldest_and_omits_raw_body(self):
        rows = [InstagramBotLog.objects.create(level="info", event="PRIVATE raw event",
            detail=encode_console_event(kind="inbound_received", scope={"message_id": index + 1}, reason="accepted")) for index in range(251)]
        cursor, observed = 0, []
        for _page in range(3):
            response = self.read(after_id=cursor)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["log"], [])
            self.assertNotIn("PRIVATE", response.content.decode())
            observed.extend(source_id for item in data["console"]["items"] for source_id in item["ids"])
            cursor = data["console"]["next_after_id"]
        self.assertEqual(observed, [row.pk for row in rows])
        self.assertFalse(data["console"]["has_more"])

    def test_paused_stream_refreshes_operational_status_without_ledger_read_or_cursor_change(self):
        with patch("management.models.InstagramBotLog.objects.aggregate") as logs:
            response = self.read(after_id=17, include_console="0")
        logs.assert_not_called()
        data = response.json()
        self.assertEqual(data["status"], self.status)
        self.assertEqual((data["console"]["next_after_id"], data["console"]["items"]), (17, []))
        self.assertIn("overview", data)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_operator_without_pii_and_dominant_reviewer_never_read_log_rows(self):
        self.actor.user_permissions.remove(Permission.objects.get(content_type__app_label="management", codename="view_ig_conversation_pii"))
        for reviewer in (False, True):
            if reviewer:
                group, _created = Group.objects.get_or_create(name="Meta Bot Reviewer")
                self.actor.groups.add(group)
            with patch("management.models.InstagramBotLog.objects.aggregate") as logs, \
                    patch("management.bot_views.bot.status_snapshot", return_value={**self.status, "last_error": "PRIVATE"}), \
                    patch("management.bot_overview_views.read_overview_snapshot", side_effect=lambda can_view: {
                        "schema_version": "ig-overview.v1", "available": can_view, "components": {}}) as overview:
                response = self.client.get(self.url)
            logs.assert_not_called()
            overview.assert_called_once_with(can_view=not reviewer)
            self.assertEqual(response.json()["status"], self.status)
            self.assertEqual(response.json()["console"]["access"], "denied")
            self.assertNotIn("PRIVATE", response.content.decode())

    def test_invalid_cursor_is_finite_and_precedes_status_and_overview_reads(self):
        with patch("management.bot_views.bot.status_snapshot") as status, \
                patch("management.bot_overview_views.read_overview_snapshot") as overview:
            response = self.client.get(self.url, {"after_id": "²PRIVATE"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"success": False, "error": "console_cursor_invalid"})
        status.assert_not_called(); overview.assert_not_called()

    def test_missing_settings_get_never_bootstraps_and_reports_unknown_without_provider_io(self):
        InstagramBotSettings.objects.all().delete()
        with patch("management.models.InstagramBotSettings.load", side_effect=AssertionError("bootstrap")) as load, \
                patch("management.bot_overview_views.read_overview_snapshot", return_value={"schema_version": "ig-overview.v1", "components": {}}), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("provider")) as provider, \
                CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"]["state"], "unavailable")
        self.assertIsNone(response.json()["status"]["pending"])
        self.assertFalse(InstagramBotSettings.objects.exists())
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        load.assert_not_called(); provider.assert_not_called()

    def test_read_status_guard_blocks_future_effects_before_sql_and_returns_finite_unavailable(self):
        from django.db import transaction
        from management.bot_overview_views import read_status_snapshot
        with transaction.atomic():
            result = read_status_snapshot(lambda: InstagramBotLog.objects.create(event="PRIVATE"))
        self.assertEqual(result["unavailable_reason"], "read_only_violation")
        self.assertEqual(InstagramBotLog.objects.count(), 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_actual_cold_overview_status_get_is_zero_dml_no_bootstrap_and_no_provider(self):
        from django.core.cache import cache
        from management.services.ig_overview_read_model import CACHE_KEY
        cache.delete(CACHE_KEY)
        with patch("management.models.InstagramBotSettings.load", side_effect=AssertionError("bootstrap")) as load, \
                patch("requests.sessions.Session.request", side_effect=AssertionError("provider")) as provider, \
                CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overview"]["schema_version"], "ig-overview.v1")
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertLessEqual(len(queries), 90)
        self.assertNotIn("overall_healthy", response.content.decode())
        load.assert_not_called(); provider.assert_not_called()

    def test_writer_preserves_critical_file_incident_but_db_has_only_validated_metadata(self):
        from management.services import instagram_bot
        with patch.object(instagram_bot._INCIDENT_LOGGER, "log") as incident:
            instagram_bot.log("error", "send_unknown", "PRIVATE customer@example.com automatic retry disabled",
                scope={"client_id": 12, "message_id": 31})
        row = InstagramBotLog.objects.get()
        item = project_console_row(row)
        self.assertEqual((item["kind"], item["reason"], item["level"]), ("reply_failed", "delivery_unknown", "error"))
        self.assertEqual(item["scope"], {"client_id": 12, "message_id": 31})
        self.assertNotIn("PRIVATE", row.detail)
        self.assertNotIn("customer@example.com", row.detail)
        self.assertEqual(incident.call_args.args[0], 40)
        self.assertEqual(incident.call_args.args[2], "send_unknown")
        self.assertIn("automatic retry disabled", incident.call_args.args[-1])

    def test_structured_and_legacy_erasure_selector_uses_exact_owner_and_accepts_json_whitespace(self):
        from management.bot_views import _log_rows_for_sender_ids
        target = IgClient.objects.create(pk=12, igsid="5110000001")
        other = IgClient.objects.create(pk=123, igsid="51100000010")
        detail = encode_console_event(kind="inbound_received", scope={"client_id": target.pk, "message_id": 31}, reason="accepted")
        own = InstagramBotLog.objects.create(detail=detail)
        spaced = InstagramBotLog.objects.create(detail=json.dumps(json.loads(detail), indent=2))
        legacy = InstagramBotLog.objects.create(detail=f"{target.igsid}: old source")
        foreign = InstagramBotLog.objects.create(detail=encode_console_event(kind="inbound_received", scope={"client_id": other.pk}, reason="accepted"))
        foreign_legacy = InstagramBotLog.objects.create(detail=f"{other.igsid}: foreign source")
        fabricated = InstagramBotLog.objects.create(detail=f'customer raw text "client_id":{target.pk} PRIVATE')
        self.assertEqual(set(_log_rows_for_sender_ids([target.igsid]).values_list("pk", flat=True)), {own.pk, spaced.pk, legacy.pk})
        _log_rows_for_sender_ids([target.igsid]).delete()
        self.assertEqual(set(InstagramBotLog.objects.values_list("pk", flat=True)), {foreign.pk, foreign_legacy.pk, fabricated.pk})

    def test_unstructured_historical_detail_and_unknown_writer_event_never_enter_new_api(self):
        from management.services import instagram_bot
        InstagramBotLog.objects.create(event="PRIVATE name", detail="PRIVATE_PROVIDER_BODY +380501112233", level="error")
        instagram_bot.log("warning", "PRIVATE event", "PRIVATE body")
        response = self.read(category="unknown")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE", response.content.decode())
        self.assertNotIn("+380501112233", response.content.decode())
        self.assertEqual(response.json()["console"]["items"][0]["reason"], "legacy_unstructured")

CONSOLE_STATUS_NODE_FIXTURE = r"""
(async()=>{
 await consoleBaselineDone;
 const box=new Element(),status=new Element();let observed=0;
 const controller=fake.IgConsole.create({enabled:true,autoPoll:false,statusPolling:true,endpoint:'/status/',container:box,statusElement:status,onSnapshot:()=>observed++});
 assert.equal(timers.size,0,'the outer status loop is the sole poll owner');
 responses=[response({success:true,status:{state:'running'},console:payload(1,1,false)})];await controller.poll();
 assert.equal(controller.getState().cursor,1);assert.equal(observed,1);
 controller.pause();const count=box.children.length;
 responses=[response({success:true,status:{state:'running'},console:{...payload(1,1,false),items:[],access:'denied'}})];await controller.poll();
 assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,1);assert.equal(box.children.length,count);assert.equal(observed,2);
 controller.resume();responses=[response({success:true,status:{state:'running'},console:payload(2,2,false)})];await controller.poll();
 assert.equal(new URL(requests.at(-1).url).searchParams.get('after_id'),'1');assert.equal(controller.getState().cursor,2);assert.equal(box.children.length,2);
 controller.setFilters({client_id:'PRIVATE@example.com'});responses=[response({success:true,status:{state:'running'},console:{...payload(1,1,false),items:[],access:'denied'}})];await controller.poll();
 assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.ok(status.textContent.includes('числовий'));assert.ok(!stored.get('ig-console.filters.v1').includes('PRIVATE'));
 controller.destroy();
 const denied=fake.IgConsole.create({enabled:false,autoPoll:false,statusPolling:true,endpoint:'/status/',container:new Element(),onSnapshot:()=>observed++});
 responses=[response({success:true,status:{state:'running'},console:{...payload(1,1,false),items:[],access:'denied'}})];await denied.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');denied.destroy();
 const overview=new Element();const data={schema_version:'ig-overview.v1',components:{daemon:{available:false,data:{}},attention:{available:true,freshness:'fresh',observation_age_seconds:3,data:{required:true,count:2,human_unknown_cases:{count:1},PRIVATE:'PRIVATE'}},lanes:{available:false,data:{}},quotas:{available:false,data:{}},tasks:{available:false,data:{}},memory_generation:{available:false,data:{}},routes:{available:false,data:{}}}};
 assert.equal(fake.IgOverview.render(overview,data),true);overview.children[1].open=true;fake.IgOverview.render(overview,data);assert.equal(overview.children[1].open,true);
 const text=element=>element.textContent+' '+element.children.map(text).join(' ');assert.ok(text(overview).includes('Потрібна людина'));assert.ok(text(overview).includes('Поступ не підтверджено'));assert.ok(!text(overview).includes('PRIVATE'));assert.ok(!text(overview).includes('overall'));
 assert.equal(fake.IgOverview.render(overview,{schema_version:'unknown',PRIVATE:'PRIVATE'}),false);assert.ok(!text(overview).includes('PRIVATE'));
 process.stdout.write('single-owner status/pause and passive overview fixtures PASS\n');
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""


CONSOLE_PAUSED_REFRESH_NODE_FIXTURE = r"""
(async()=>{
 await consoleBaselineDone;
 const box=new Element(),status=new Element();let observed=0;
 const controller=fake.IgConsole.create({enabled:true,autoPoll:false,statusPolling:true,endpoint:'/status/',container:box,statusElement:status,onSnapshot:()=>observed++});
 const statusOnly=()=>response({success:true,status:{state:'running'},console:{...payload(1,1,false),items:[],access:'denied'}});
 responses=[response({success:true,status:{state:'running'},console:payload(1,3,false)})];await controller.poll();
 controller.pause();controller.setFilters({category:'errors',client_id:'7'});assert.equal(box.children.length,0);assert.equal(controller.getState().cursor,0);
 let prior=requests.length;controller.refresh();assert.equal(requests.length,prior,'refresh must not fetch before root poll');assert.ok(status.textContent.includes('очікує'));
 responses=[response({success:true,status:{state:'running'},console:payload(1,120,true)}),response({success:true,status:{state:'running'},console:payload(121,125,false)})];
 await controller.poll();assert.equal(requests.length-prior,2,'one bounded explicit drain, no duplicate root HTTP');
 assert.ok(requests.slice(prior).every(row=>new URL(row.url).searchParams.get('include_console')==='1'));
 assert.equal(new URL(requests[prior].url).searchParams.get('client_id'),'7');assert.equal(controller.getState().cursor,125);assert.equal(box.children.length,125);assert.equal(controller.getState().paused,true);assert.equal(timers.size,0);
 responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,125);assert.equal(box.children.length,125);
 // Mimic root's polling guard: a refresh during a pending old poll is armed,
 // not independently dispatched. Its aborted response must be entirely inert.
 let release;responses=[new Promise(resolve=>{release=resolve;})];const oldPoll=controller.poll();const oldRequest=requests.at(-1),observedBefore=observed;
 controller.setFilters({category:'manager',client_id:'8'});controller.refresh();prior=requests.length;
 assert.equal(oldRequest.options.signal.aborted,true);assert.equal(await controller.poll(),false);assert.equal(requests.length,prior);assert.ok(status.textContent.includes('очікує'));
 release(response({success:false,status:{state:'stale'},console:{PRIVATE:'stale provider body'}}));await oldPoll;
 assert.equal(observed,observedBefore);assert.equal(controller.getState().cursor,0);assert.equal(box.children.length,0);assert.ok(status.textContent.includes('очікує'));assert.ok(!status.textContent.includes('недоступне'));
 responses=[response({success:true,status:{state:'running'},console:payload(1,2,false)})];await controller.poll();
 assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'1');assert.equal(new URL(requests.at(-1).url).searchParams.get('client_id'),'8');assert.equal(controller.getState().cursor,2);assert.equal(box.children.length,2);assert.equal(controller.getState().paused,true);
 responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,2);
 // The existing per-tick bound also applies to an explicit paused drain.
 controller.refresh();prior=requests.length;responses=Array.from({length:5},(_,i)=>response({success:true,status:{state:'running'},console:payload(i*120+1,(i+1)*120,true)}));await controller.poll();
 assert.equal(requests.length-prior,5);assert.equal(controller.getState().cursor,600);assert.equal(box.children.length,400);assert.equal(controller.getState().paused,true);
 responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,600);
 // Invalid explicit responses neither advance nor arm an automatic retry.
 controller.refresh();responses=[response({success:true,status:{state:'running'},console:{...payload(1,1,false),next_after_id:-1}})];await controller.poll();assert.equal(controller.getState().cursor,0);assert.equal(box.children.length,0);
 responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,0);
 doc.hidden=true;controller.refresh();responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');doc.hidden=false;
 responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(controller.getState().cursor,0);
 controller.setFilters({client_id:'PRIVATE@example.com'});controller.refresh();responses=[statusOnly()];await controller.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.ok(status.textContent.includes('числовий'));assert.ok(!stored.get('ig-console.filters.v1').includes('PRIVATE'));controller.destroy();
 const denied=fake.IgConsole.create({enabled:false,autoPoll:false,statusPolling:true,endpoint:'/status/',container:new Element()});denied.pause();prior=requests.length;denied.refresh();assert.equal(requests.length,prior);responses=[statusOnly()];await denied.poll();assert.equal(new URL(requests.at(-1).url).searchParams.get('include_console'),'0');assert.equal(denied.getState().cursor,0);denied.destroy();
 process.stdout.write('paused explicit refresh fixtures PASS: one owner, bounded drain, pause retained, next status-only, stale abort, privacy/hidden/invalid gates\n');
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""
