"""Backend/renderer agreement for quiet details and evidence-based attention."""
from pathlib import Path
import shutil
import subprocess
from django.test import SimpleTestCase
from management.services.ig_journey_presentation import finalize_display_focus


class JourneyDisplayFocusTests(SimpleTestCase):
    def test_route_wins_over_trace_and_root_uses_same_object(self):
        graph = {"nodes": [{"id": "trace", "current": True, "interpreted_focus": True},
                            {"id": "route", "route_focus": True}], "transcript_reconstruction": {"freshness": "current"}}
        focus = finalize_display_focus(graph)
        self.assertEqual(focus["node_id"], "route")
        self.assertIs(focus, graph["display_focus"])
        self.assertEqual([n["id"] for n in graph["nodes"] if n["current"]], ["route"])

    def test_confirmed_business_wins_over_interpretation(self):
        graph = {"nodes": [{"id": "paid", "current": True, "facts": [{"source": "order.current"}]},
                            {"id": "trace", "current": True, "interpreted_focus": True}]}
        self.assertEqual(finalize_display_focus(graph)["node_id"], "paid")

    def test_ambiguous_focus_is_not_first_arbitrary_node(self):
        graph = {"nodes": [{"id": "a", "current": True}, {"id": "b", "current": True}]}
        self.assertIsNone(finalize_display_focus(graph)["node_id"])

    def test_hidden_case_focus_needs_unique_affected_action(self):
        graph = {"nodes": [{"id": "quote"}, {"id": "case", "current": True, "semantic_key": "objection_case", "presentation_kind": "interpretation", "interpreted_focus": True, "transcript_interpretation": {"last_step_index": 4}}],
                 "edges": [{"from_node_id": "quote", "to_node_id": "case", "last_step_index": 4, "case_id": "price"}],
                 "trace_cases": [{"id": "price", "source_node_id": "quote", "marker_eligible": True}]}
        self.assertEqual(finalize_display_focus(graph)["node_id"], "quote")
        graph["nodes"][0]["current"] = False
        graph["nodes"][1]["current"] = True
        graph["nodes"][1]["transcript_interpretation"]["last_step_index"] = 5  # later routine detail cannot reuse old price concern
        self.assertIsNone(finalize_display_focus(graph)["node_id"])

    def test_history_focus_is_scoped(self):
        graph = {"nodes": [{"id": "old", "current": True}]}
        self.assertEqual(finalize_display_focus(graph, is_history=True)["scope"], "viewed_history")


class JourneyRendererPresentationTests(SimpleTestCase):
    def test_replay_and_indicators_never_promote_possible_steps_to_facts(self):
        source = (Path(__file__).parent / "static/management/ig_journey.js").read_text()
        source = source.replace("window.TwcJourney={create:options=>new Journey(options)};",
                                "window.TwcJourney={Journey,hasActiveWait,hasChannelHandoff,POST_SALE_TAIL};")
        program = "global.window={};\n" + source + r"""
const assert=require('node:assert/strict');
const {Journey,hasActiveWait,hasChannelHandoff,POST_SALE_TAIL}=window.TwcJourney;
const replay=Object.create(Journey.prototype);
const trace={id:'trace',relation:'transcript_interpretation',authority:'none',provenance:'transcript_reconstruction',evidence_refs:[{id:1}],last_step_index:2};
replay.edges=[trace,{...trace,id:'earlier',last_step_index:1},{...trace,id:'hidden'},
  {id:'future',relation:'route',evidence_refs:[]},
  {id:'order',relation:'client_order_lifecycle',evidence_refs:[{id:3}]},
  {...trace,id:'uncited',evidence_refs:[]}];
replay.svg={querySelectorAll:()=>['trace','earlier','future','order','uncited'].map(id=>({dataset:{edgeId:id}}))};
assert.deepEqual(replay.walkEdges().map(e=>e.id),['earlier','trace']);
global.matchMedia=()=>({matches:true});replay.toggleWalk();assert.notEqual(replay.walking,true);
let cancelled=null;global.cancelAnimationFrame=id=>{cancelled=id};
replay.walkFrame=42;replay.walking=true;replay.walker={dataset:{active:'true',orbit:'true'}};
replay.play={setAttribute(){},textContent:''};replay.stopWalk();
assert.equal(cancelled,42);assert.equal(replay.walking,false);assert.equal(replay.walker.dataset.active,'false');
const now=Date.parse('2026-09-27T10:00:00Z');
const timer={status:'running',due_at:'2026-09-27T11:00:00Z',evidence_refs:[{id:1}]};
assert.equal(hasActiveWait({timers:[timer]},now),true);
assert.equal(hasActiveWait({timers:[timer]},now+7200000),false);
assert.equal(hasActiveWait({timers:[{...timer,evidence_refs:[]}]},now),false);
assert.equal(hasChannelHandoff({semantic_key:'channel_consent',tone:'consent'}),false);
assert.equal(hasChannelHandoff({channel_handoff:{channel:'telegram'}}),false);
assert.equal(hasChannelHandoff({channel_handoff:{channel:'telegram',evidence_refs:[{id:1}]}}),true);
assert.ok(POST_SALE_TAIL.includes('channel_grant_checked'));
assert.ok(POST_SALE_TAIL.includes('reward_delivery'));
assert.ok(POST_SALE_TAIL.includes('new_purchase_interest'));
"""
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_objection_fork_and_visible_case_results_survive_live_projection(self):
        source = (Path(__file__).parent / "static/management/ig_journey.js").read_text()
        source = source.replace("window.TwcJourney={create:options=>new Journey(options)};",
                                "window.TwcJourney={Journey,caseOutcome};")
        program = "global.window={};\n" + source + r"""
const assert=require('node:assert/strict');
const {Journey,caseOutcome}=window.TwcJourney;
const journey=Object.create(Journey.prototype);
const catalogue={definitions:[
  {key:'quoted_offer',label:'Пропозиція',route_keys:['commerce']},
  {key:'objection_case',label:'Заперечення',route_keys:['objection']},
  {key:'awaiting_payment',label:'Оплата',route_keys:['payment']}
],transitions:[
  {id:'raise',source_key:'quoted_offer',target_key:'objection_case'},
  {id:'retry',source_key:'objection_case',target_key:'awaiting_payment',outcome:'new_attempt'}
]};
const fork=journey.possibleCatalogue(catalogue);
assert.equal(fork.definitions.length,3);
assert.deepEqual(fork.transitions.map(e=>e.id),['raise','retry']);
assert.equal(fork.transitions[0].condition_label,'Якщо виникне заперечення');
assert.equal(fork.transitions[1].condition_label,'Нова спроба оплати');
assert.equal(catalogue.transitions[0].condition_label,undefined); // do not mutate snapshot
const quote={id:'trace:quote',semantic_key:'quoted_offer',label:'Пропозиція',presentation_kind:'interpretation',current:true};
const sourceGraph={nodes:[quote],edges:[],trace_node_ids:[quote.id],transcript_reconstruction:{scope:'client'}};
const snapshot={catalogue};
let graph=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
assert.ok(graph.nodes.some(n=>n.id==='possible:objection_case'&&n.presentation_kind==='possible'));
assert.equal(graph.edges.length,0); // optional node must not invent a client transition
assert.equal(sourceGraph.nodes.length,1);
journey.modal={};journey.showPossible=true;journey.possibleFamily='all';
graph=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
assert.ok(graph.nodes.some(n=>n.id==='possible:objection_case'));
assert.equal(graph.edges.length,2);
assert.ok(graph.edges.every(e=>e.relation==='route'&&e.evidence_refs.length===0));
assert.equal(graph.edges.find(e=>e.id==='raise').condition_label,'Якщо виникне заперечення');
assert.ok(!graph.edges.some(e=>e.id.startsWith('possible-objection:')));
for(const family of ['catalog','custom']){
  journey.possibleFamily=family;
  const filtered=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
  assert.ok(filtered.nodes.some(n=>n.id==='possible:objection_case'),family+' must keep the cross-cutting objection fork');
  assert.ok(filtered.edges.some(e=>e.id==='raise'));
}

const evidence={id:'edge',from_node_id:quote.id,to_node_id:'detail',relation:'transcript_interpretation',authority:'none',provenance:'transcript_reconstruction',evidence_refs:[{id:4}],reason_code:'objection_raised',presentation_schema_version:'journey-presentation.v1'};
graph=journey.presentEvents({...sourceGraph,nodes:[quote,{id:'detail',semantic_key:'objection_case',presentation_kind:'interpretation',presentation_schema_version:'journey-presentation.v1'}],edges:[evidence],trace_cases:[
  {id:'price',topic:'price',source_node_id:quote.id,source_edge_ids:['edge'],marker_eligible:true,status:'handled',outcome:'unknown'},
  {id:'payment',topic:'payment',source_node_id:quote.id,source_edge_ids:['edge'],marker_eligible:true,status:'unresolved',outcome:'unknown'},
  {id:'routine',source_node_id:quote.id,marker_eligible:false,status:'recorded'}
]});
const cases=graph.nodes.filter(n=>n.semantic_key==='journey_case');
assert.equal(cases.length,2); // generic discussion and repeated edges aren't extra cases
assert.equal(caseOutcome(cases[0]).key,'addressed');
assert.match(caseOutcome(cases[0]).label,/результат невідомий/);
assert.equal(cases[0].presentation_event.tone,'recorded');
assert.equal(caseOutcome(cases[1]).key,'open');
assert.equal(caseOutcome({...cases[1],status:'future_status'}).key,'unknown');
assert.equal(caseOutcome({...cases[1],status:'refused'}).key,'refused');
assert.equal(caseOutcome({...cases[1],status:'resolved'}).key,'resolved');
// The compact UI exposes the same results even if geometry cannot place a marker.
function element(tag){return {tag,children:[],dataset:{},attrs:{},textContent:'',append(...items){this.children.push(...items)},replaceChildren(){this.children=[]},setAttribute(k,v){this.attrs[k]=v},addEventListener(){}};}
global.document={createElement:element};
journey.graph=graph;journey.objections=element('section');journey.objectionButtons=new Map();
journey.renderObjections();
function text(n){return n.textContent+n.children.map(text).join(' ')}
assert.equal(journey.objections.hidden,false);
assert.match(text(journey.objections),/Обговорено: 1/);
assert.match(text(journey.objections),/Не вирішено: 1/);
assert.match(text(journey.objections),/Згода клієнта та вирішення не підтверджені/);
assert.equal(journey.objectionButtons.size,2);
journey.graph={nodes:[{id:'possible:objection_case',semantic_key:'objection_case',label:'Заперечення',presentation_kind:'possible'}]};
journey.renderObjections();
assert.equal(journey.objections.hidden,false);
assert.match(text(journey.objections),/Можливе · подій немає/);
journey.graph={nodes:[quote]};journey.renderObjections();
assert.equal(journey.objections.hidden,true);
"""
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_renderer_uses_materiality_and_keeps_one_anchored_case(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        source = (Path(__file__).parent / "static/management/ig_journey.js").read_text()
        source = source.replace("window.TwcJourney={create:options=>new Journey(options)};",
                                "window.TwcJourney={Journey,edgeTone,exceptional};")
        program = "global.window={};\n" + source + r"""
const assert=require('node:assert/strict');
const {Journey,edgeTone,exceptional}=window.TwcJourney;
const edge={id:'edge',from_node_id:'quote',to_node_id:'case',relation:'transcript_interpretation',authority:'none',provenance:'transcript_reconstruction',evidence_refs:[{id:4}],interpretation_kind:'return',tone:'warning',presentation_schema_version:'journey-presentation.v1',marker_eligible:false};
assert.equal(exceptional(edge),false);
assert.equal(edgeTone(edge),'recorded');
assert.equal(exceptional({...edge,marker_eligible:true,materiality:'material_concern'}),true);
const graph={nodes:[{id:'quote'},{id:'case',semantic_key:'objection_case',presentation_kind:'interpretation',presentation_schema_version:'journey-presentation.v1'}],edges:[edge],inline_main_ids:['quote','case'],trace_cases:[{id:'price',topic:'price',source_node_id:'quote',source_edge_ids:['edge'],marker_eligible:true}]};
const presented=Journey.prototype.presentEvents.call({},graph);
assert.deepEqual(presented.inline_main_ids,['quote']);
assert.equal(presented.nodes.find(n=>n.id==='case').presentation_event.mode,'detail');
assert.equal(presented.nodes.find(n=>n.id==='case').presentation_event.tone,'recorded');
assert.deepEqual(presented.nodes.find(n=>n.id==='price').presentation_event.anchor_ids,['quote']);
assert.equal(presented.nodes.filter(n=>n.presentation_event?.tone==='warning').length,1);
assert.equal(presented.edges.length,1); // no invented causal bypass
"""
        result = subprocess.run([node, "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
