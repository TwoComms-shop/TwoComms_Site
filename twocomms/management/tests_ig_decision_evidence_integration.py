"""Production URL and permission-gated dashboard integration without I/O."""
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from django.contrib.auth import get_user_model

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse

from management import bot_decision_trace_views as trace
from management import bot_quality_observation_views as quality


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class DecisionEvidenceDashboardTests(SimpleTestCase):
    def test_real_management_routes_bind_exact_readers(self):
        for name, kwargs, view in (
            ("management_bot_decision_trace_index_api", {"client_id": 17}, trace.bot_decision_trace_index_api),
            ("management_bot_decision_trace_api", {"client_id": 17, "revision_id": 29}, trace.bot_decision_trace_api),
            ("management_bot_quality_observations_api", {}, quality.bot_quality_observations_api),
            ("management_bot_quality_observations_export_api", {}, quality.bot_quality_observations_export_api),
        ):
            with self.subTest(name=name):
                url = reverse(name, kwargs=kwargs)
                self.assertTrue(url.startswith("/bot/api/"))
                match = resolve(url)
                self.assertIs(match.func, view)
                self.assertEqual(match.kwargs, kwargs)

    def test_assets_mount_and_controller_require_both_capabilities(self):
        for operate, pii in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(operate=operate, pii=pii):
                html = render_to_string("management/bot.html", {"bot_can_operate": operate, "bot_can_view_pii": pii,
                    "request": SimpleNamespace(user=get_user_model()(username="dashboard-fixture"), path="/bot/")})
                for marker in ("ig_decision_trace.js", "ig_decision_trace.css", "ig_quality_observations.js",
                               "ig_quality_observations.css", 'id="bot-quality-observations"',
                               "window.IgDecisionTrace.create(", "window.IgQualityObservations.create("):
                    self.assertEqual(marker in html, operate and pii, marker)
                if operate and pii:
                    self.assertIn("/bot/api/clients/0/decision-traces/", html)
                    self.assertIn("/bot/api/clients/0/turn-revisions/0/decision-trace/", html)
                    self.assertIn("/bot/api/quality-observations/", html)
                    # Rendered actual inline script must parse, including reversed URLs.
                    import re
                    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
                    result = subprocess.run([shutil.which("node"), "--check", "-"], input=script, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_login_redirect_and_html_failure_show_finite_status_without_parsing_body(self):
        scripts = Path(__file__).parent / "static/management"
        program = r'''
const assert=require('node:assert/strict');
class Element {constructor(tag){this.tag=tag;this.children=[];this._value='';this.textContent='';}append(...nodes){this.children.push(...nodes);if(this.tag==='select'&&!this._value&&nodes[0])this._value=nodes[0].value;}replaceChildren(...nodes){this.children=nodes;}addEventListener(){}setAttribute(){}remove(){}get value(){return this._value;}set value(v){this._value=v;}}
global.document={createElement:tag=>new Element(tag)};
global.window={location:{href:'https://management.test/bot/'}};
''' + (scripts / "ig_decision_trace.js").read_text() + (scripts / "ig_quality_observations.js").read_text() + r'''
(async()=>{
 const trace=window.IgDecisionTrace.create(new Element('div'));
 const quality=window.IgQualityObservations.create(new Element('div'));
 let bodies=0;
 for(const response of [{redirected:true,status:200},{status:503,headers:{get:()=> 'text/html'}}]){
  window.fetch=async()=>({...response,json:()=>{bodies++;throw Error('private body must not parse');}});
  await trace.render(17);await quality.render({days:7});
  assert.match(trace.status.textContent,response.redirected?/Сесія завершилася/:/тимчасово недоступний/);
  assert.match(quality.status.textContent,response.redirected?/Сесія завершилася/:/тимчасово недоступний/);
 }
 assert.equal(bodies,0);assert.equal(quality.start.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
