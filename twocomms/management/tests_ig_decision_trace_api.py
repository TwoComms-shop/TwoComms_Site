"""Real protected API boundary and actual browser-component rendering contracts."""
import shutil
import subprocess
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

from management.bot_access import (
    META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
)
from management.bot_decision_trace_views import bot_decision_trace_api, bot_decision_trace_index_api
from management.models import IgClient, InstagramBotMessage
from management import tests_ig_decision_trace_read_model as trace_fixtures

urlpatterns = [
    path("login/", lambda request: HttpResponse("login"), name="management_login"),
    path("bot/api/clients/<int:client_id>/decision-traces/", bot_decision_trace_index_api,
         name="management_bot_decision_trace_index_api"),
    path("bot/api/clients/<int:client_id>/turn-revisions/<int:revision_id>/decision-trace/", bot_decision_trace_api,
         name="management_bot_decision_trace_api"),
]


@override_settings(ROOT_URLCONF="management.tests_ig_decision_trace_api", SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class DecisionTraceApiTests(TransactionTestCase):
    def setUp(self):
        self.case = trace_fixtures.DecisionTraceReadModelTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.actor = get_user_model().objects.create_user(username="decision-trace-operator")
        self.grant(self.actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client.force_login(self.actor)

    @staticmethod
    def grant(actor, *permissions):
        actor.user_permissions.add(*(Permission.objects.get(content_type__app_label=value.split(".")[0],
                                       codename=value.split(".")[1]) for value in permissions))

    def detail(self, *, client_id=None, revision_id=None):
        return reverse("management_bot_decision_trace_api", kwargs={
            "client_id": self.case.client_row.pk if client_id is None else client_id,
            "revision_id": self.case.revision.pk if revision_id is None else revision_id,
        })

    def index(self, *, client_id=None):
        return reverse("management_bot_decision_trace_index_api", kwargs={
            "client_id": self.case.client_row.pk if client_id is None else client_id,
        })

    def test_anonymous_and_each_capability_deny_before_either_reader(self):
        self.client.logout()
        with patch("management.services.ig_decision_trace_read_model.read_revision_decision_trace") as detail, \
             patch("management.services.ig_decision_trace_read_model.read_revision_decision_trace_index") as index:
            for url in (self.detail(), self.index()):
                self.assertEqual(self.client.get(url).status_code, 302)
            for number, permissions in enumerate(((), (OPERATE_IG_BOT_PERMISSION,), (VIEW_IG_CONVERSATION_PII_PERMISSION,))):
                actor = get_user_model().objects.create_user(username=f"trace-capability-{number}", is_staff=True)
                self.grant(actor, *permissions); self.client.force_login(actor)
                for url in (self.detail(), self.index()):
                    self.assertEqual(self.client.get(url).status_code, 403)
            detail.assert_not_called(); index.assert_not_called()

    def test_reviewer_is_dominant_deny_even_superuser(self):
        actor = get_user_model().objects.create_superuser(username="trace-reviewer", password="synthetic-password")
        actor.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME)); self.client.force_login(actor)
        with patch("management.services.ig_decision_trace_read_model.read_revision_decision_trace") as detail, \
             patch("management.services.ig_decision_trace_read_model.read_revision_decision_trace_index") as index:
            self.assertEqual(self.client.get(self.detail()).status_code, 403)
            self.assertEqual(self.client.get(self.index()).status_code, 403)
        detail.assert_not_called(); index.assert_not_called()

    def test_inactive_actor_cannot_reach_reader(self):
        actor = get_user_model().objects.create_user(username="trace-inactive", is_active=False)
        self.grant(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION); self.client.force_login(actor)
        with patch("management.services.ig_decision_trace_read_model.read_revision_decision_trace") as reader:
            self.assertIn(self.client.get(self.detail()).status_code, (302, 403))
        reader.assert_not_called()

    def test_get_only_never_cache_and_query_cannot_enable_protected_fields(self):
        for url in (self.detail(), self.index()):
            self.assertEqual(self.client.post(url).status_code, 405)
            response = self.client.get(url, {"include_protected": "1", "raw_prompt": "1"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
            text = response.content.decode()
            for private in ("Що на фото?", "private-key-alias", "signed.invalid", "ig-private/", self.case.revision.snapshot_digest):
                self.assertNotIn(private, text)

    def test_failed_attempt_winner_and_missing_delivery_are_separate_at_api(self):
        data = self.client.get(self.detail()).json()
        self.assertTrue(data["success"])
        self.assertEqual(data["identity"], {"client_id": self.case.client_row.pk, "revision_id": self.case.revision.pk})
        self.assertEqual(data["generation"]["actual_model"], self.case.model)
        self.assertEqual(data["generation"]["requests"][0]["attempts"][0]["failure_kind"], "local_semantic_rejection")
        self.assertFalse(data["delivery"]["normal_reply_complete"])
        self.assertEqual(data["coverage"]["semantic"], "unknown")

    def test_foreign_missing_zero_and_overflow_identity_never_use_latest(self):
        other = IgClient.objects.create(igsid="trace-api-other-client")
        for url, status in ((self.detail(client_id=other.pk), 404), (self.detail(revision_id=self.case.revision.pk + 1000), 404),
                            (self.detail(revision_id=0), 400), (self.detail(client_id=2**63), 400),
                            (self.index(client_id=0), 400), (self.index(client_id=2**63), 400)):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.json()["success"])
            self.assertNotIn("generation", response.json())

    def test_erasure_hidden_and_deleted_owner_hide_index_and_trace(self):
        IgClient.objects.filter(pk=self.case.client_row.pk).update(hidden_at=timezone.now())
        for url in (self.detail(), self.index()):
            self.assertEqual(self.client.get(url).status_code, 404)
        IgClient.objects.filter(pk=self.case.client_row.pk).update(hidden_at=None, privacy_erasure_started_at=timezone.now())
        for url in (self.detail(), self.index()):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("items", response.json())
            self.assertNotIn("context", response.json())
        IgClient.objects.filter(pk=self.case.client_row.pk).delete()
        self.assertEqual(self.client.get(self.detail()).status_code, 404)

    def test_changed_source_conflict_and_cursor_validation(self):
        self.case.source.text = "different-private-source"
        self.case.source.save(update_fields=["text"])
        response = self.client.get(self.detail())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "source_binding_unverified")
        for query in ("limit=0", "limit=26", "limit=01", "limit=1&limit=2", "before_revision_id=0",
                      "before_revision_id=-1", "before_revision_id=01", "before_revision_id=9223372036854775808"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.index() + "?" + query).status_code, 400)

    def test_revision_index_has_exact_cursor_and_unknown_decision_without_graph_inference(self):
        result = self.client.get(self.index(), {"limit": 1}).json()
        self.assertTrue(result["success"])
        self.assertEqual(result["items"][0]["revision_id"], self.case.revision.pk)
        self.assertEqual(result["items"][0]["input_origin"], "unknown")
        self.assertEqual(self.client.get(self.index(), {"before_revision_id": self.case.revision.pk}).json()["items"], [])

    def test_repeated_get_zero_dml_bounded_and_no_provider_or_bootstrap(self):
        before = InstagramBotMessage.objects.count()
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("provider forbidden")) as provider, \
             patch("management.models.InstagramBotSettings.load", side_effect=AssertionError("bootstrap forbidden")) as bootstrap, \
             patch("management.services.instagram_bot.build_prompt_snapshot", side_effect=AssertionError("current prompt forbidden")) as prompt:
            for _ in range(2):
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self.client.get(self.detail()).status_code, 200)
                self.assertLessEqual(len(queries), 22)
                self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self.client.get(self.index()).status_code, 200)
                self.assertLessEqual(len(queries), 12)
                self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        provider.assert_not_called(); bootstrap.assert_not_called(); prompt.assert_not_called()
        self.assertEqual(before, InstagramBotMessage.objects.count())


class DecisionTraceRendererTests(SimpleTestCase):
    def test_actual_component_partial_coverage_cost_and_client_switch_race(self):
        source = (Path(__file__).parent / "static/management/ig_decision_trace.js").read_text()
        program = r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this._value='';this.textContent='';}
  append(...nodes){this.children.push(...nodes);if(this.tag==='select'&&!this._value&&nodes[0])this._value=nodes[0].value;}
  replaceChildren(...nodes){this.children=nodes;if(this.tag==='select')this._value='';this.textContent='';}
  addEventListener(){}setAttribute(k,v){this.attrs[k]=v;}remove(){}
  get value(){return this._value;}set value(v){this._value=v;}
}
global.document={createElement:tag=>new Element(tag)};
global.window={location:{href:'https://twocomms.test/management/bot/'}};
''' + source + r'''
function text(element){return element.textContent+' '+element.children.map(text).join(' ');}
function find(element,cls){return (element.className===cls?[element]:[]).concat(element.children.flatMap(child=>find(child,cls)));}
function response(payload){return {ok:true,status:200,json:async()=>payload};}
const schema_version='ig-decision-trace.v1';
const index=(id,origin)=>({success:true,schema_version,identity:{client_id:id},items:[{revision_id:id*10,input_origin:origin,scope:'current',sealed_at:null}],has_more:false});
const trace=(id)=>({success:true,schema_version,identity:{client_id:id,revision_id:id*10},revision:{id:id*10,scope:'current'},
 context:{proof:'confirmed',source_message_ids:[1]},decision:{proof:'unknown'},
 generation:{proof:'confirmed',actual_model:'gemini-test',requests:[{id:'request-1',proof:'confirmed',attempts:[{attempt_index:1,model:'gemini-rejected',state:'failed',http_status:200,failure_kind:'local_semantic_rejection',validator_layer:'local_semantic',validator_codes:['unverified_price'],usage:{tokens:{total:29}}}]}]},
 proposal:{proof:'unknown'},actions:{manager_case:{proof:'recorded',task_id:8,task_state:'skipped',notification_state:'pending',notification_delivered:false}},
 delivery:{proof:'confirmed',physical_state:'partial',normal_reply_complete:false,effects:[{id:1,state:'sent',part_index:0,part_count:2,receipt_proof:'confirmed',provider_message_id:'actual-mid',transcript_proof:'unknown'},{id:2,state:'unknown',part_index:1,part_count:2,receipt_proof:'unknown',transcript_proof:'unknown'}]},
 semantic:{proof:'unknown',disposition:'unknown',customer_reply_complete:false}});
let calls=[];let deferFirst;
window.fetch=(url,options)=>{calls.push({url,options});if(url.includes('/clients/1/')&&url.includes('/decision-traces/'))return new Promise(resolve=>{deferFirst=resolve;});return Promise.resolve(response(url.includes('/decision-traces/')?index(2,'no_reply'):trace(2)));};
(async()=>{
 const mount=new Element('div');const component=window.IgDecisionTrace.create(mount);assert.equal(calls.length,0);
 const old=component.render(1);assert.ok(deferFirst);await component.render(2);
 deferFirst(response(index(1,'static_reply')));await old;
 assert.equal(component.clientId,2);assert.equal(component.select.value,'20');
 const shown=text(component.root);assert.match(shown,/Без відповіді/);assert.match(shown,/Підтверджено частину/);assert.match(shown,/Повноту не доведено/);assert.match(shown,/Грошова вартість невідома/);assert.match(shown,/Локальна семантика/);assert.match(shown,/unverified_price/);assert.match(shown,/Відображення в історії/);assert.match(shown,/Сповіщення/);
 assert.match(text(component.body.children[0]),/Доставлено лише частину відповіді/);
 assert.equal(find(component.body,'ig-trace-stage').length,7);
 assert.equal(find(component.body,'ig-trace-stage-link').length,5);
 assert.equal(find(component.body,'ig-trace-evidence')[0].open,true);
 assert.ok(find(component.body,'ig-trace-diagnostic').every(item=>!item.open));
 assert.ok(!shown.includes('Хід #10'));
 assert.ok(calls.every(call=>call.options.cache==='no-store'&&call.options.credentials==='same-origin'&&!call.options.method));
 component.clear();assert.equal(component.body.children.length,0);assert.equal(component.clientId,null);assert.equal(component.controls.hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
