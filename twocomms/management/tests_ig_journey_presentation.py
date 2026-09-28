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
    def test_complete_atlas_and_short_path_preserve_sources(self):
        base = Path(__file__).parent / "static/management"
        geometry = (base / "ig_journey_geometry.js").read_text()
        source = (base / "ig_journey.js").read_text().replace(
            "window.TwcJourney={create:options=>new Journey(options)};",
            "window.TwcJourney={Journey,witnessed,contextual,consentView,invoiceCountdown,paymentView,selectionView};")
        from management.services.ig_journey_catalogue import journey_catalogue
        import json
        program = "global.window={};\n" + geometry + source + "\nconst catalogue=" + json.dumps(journey_catalogue()) + r"""
const assert=require('node:assert/strict');
const {Journey,witnessed,contextual,consentView,invoiceCountdown,paymentView,selectionView}=window.TwcJourney;
const journey=Object.create(Journey.prototype);
journey.modal={};journey.mapMode='all';journey.showPossible=true;journey.possibleFamily='catalog';
const sourceGraph={nodes:[{id:'guide:inquiry',semantic_key:'inbound'}],edges:[]};
const before=JSON.stringify(sourceGraph),snapshot={catalogue};
const all=journey.presentGraph(sourceGraph,snapshot);
for(const d of journey.possibleCatalogue(catalogue).definitions)assert.ok(all.nodes.some(n=>(n.structural_key||n.semantic_key)===d.key),d.key);
assert.equal(all.edges.length,journey.possibleCatalogue(catalogue).transitions.length);
assert.equal(JSON.stringify(sourceGraph),before);
for(const width of [320,780,1500]){
 const result=window.TwcJourneyGeometry.atlas({nodes:all.nodes,edges:all.edges,width});
 const routes=window.TwcJourneyGeometry.routeEdges({nodes:all.nodes,edges:all.edges,positions:result.positions,width:result.width,height:result.height,full:true});
 for(const edge of all.edges)assert.ok(routes.has(edge.id),'missing visible route '+edge.id);
 assert.equal(result.positions.size,all.nodes.length);
 const rects=[...result.positions.values()].map(p=>({x:p.cardLeft,y:p.y-22,w:p.cardWidth,h:87}));
 for(let i=0;i<rects.length;i++)for(let j=i+1;j<rects.length;j++){
  const a=rects[i],b=rects[j];assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y,'cards must not overlap');
 }
}
journey.mapMode='short';const short=journey.presentGraph(sourceGraph,snapshot);
assert.ok(short.nodes.length<all.nodes.length);
assert.ok(!short.nodes.some(n=>n.semantic_key==='reward_delivery'));
assert.ok(short.edges.every(e=>!['offer_correction','configuration_correction'].includes(e.outcome)));
journey.showPossible=false;journey.mapMode='actual';
assert.equal(journey.presentGraph(sourceGraph,snapshot).nodes.length,1);
const claim={relation:'client_report_context',evidence_refs:[{kind:'message',id:1}]};
assert.equal(witnessed({...claim,relation:'advertising_attribution'}),false);assert.equal(contextual({...claim,relation:'advertising_attribution'}),true);
assert.equal(witnessed(claim),false);assert.equal(contextual(claim),true);
const report={id:'website',producer:'website_order_report',semantic_key:'website_order_report'};
const traceSource={transcript_reconstruction:{},trace_node_ids:['trace'],nodes:[{id:'trace'},report],edges:[]};
const inline=journey.inlineGraph(traceSource,traceSource.nodes,[],journey.possibleCatalogue(catalogue));
assert.ok(inline.inline_main_ids.includes('website'));
const composed=journey.presentEvents(all);
const represented=new Set(composed.nodes.flatMap(n=>(n.composite_nodes||[n]).map(c=>c.structural_key||c.semantic_key)));
for(const d of journey.possibleCatalogue(catalogue).definitions)assert.ok(represented.has(d.key),'lost definition '+d.key);
const representedEdges=new Set([...composed.edges,...composed.nodes.flatMap(n=>n.composite_edges||[])].map(e=>e.id));
for(const e of all.edges)assert.ok(representedEdges.has(e.id),'lost transition '+e.id);
const boxes=window.TwcJourneyGeometry.atlas({nodes:composed.nodes,width:1500});
const composedRoutes=window.TwcJourneyGeometry.routeEdges({nodes:composed.nodes,edges:composed.edges,positions:boxes.positions,width:boxes.width,height:boxes.height,full:true});
for(const e of composed.edges)assert.ok(composedRoutes.has(e.id),'unrouted composition edge '+e.id);
assert.equal(composed.nodes.find(n=>n.semantic_key==='fulfillment').delivery_progress.stages.length,4);
const payment=composed.nodes.find(n=>n.payment_progress);
assert.equal(payment.payment_progress.items.length,3);
assert.ok(payment.payment_progress.items.every(i=>i.state==='todo'));
const actualGraph={nodes:[{id:'wait',semantic_key:'awaiting_payment',current:true,evidence_refs:[{kind:'message',id:1}]},{id:'paid',semantic_key:'settlement',state:'complete',evidence_refs:[{kind:'payment',id:2}]},{id:'ship',semantic_key:'fulfillment'},{id:'case',semantic_key:'journey_case',presentation_event:{anchor_ids:['paid'],edge_ids:['out']}}],
edges:[{id:'inside',from_node_id:'wait',to_node_id:'paid',evidence_refs:[{id:2}]},{id:'out',from_node_id:'paid',to_node_id:'ship',evidence_refs:[{id:3}]}],display_focus:{node_id:'paid'}};
const combined=journey.mergePayment(actualGraph);
assert.equal(combined.edges.find(e=>e.id==='out').from_node_id,'wait');
assert.equal(combined.display_focus.node_id,'wait');
assert.deepEqual(combined.nodes.find(n=>n.id==='case').presentation_event.anchor_ids,['wait']);
assert.equal(combined.nodes[0].composite_edges[0].id,'inside');
assert.equal(combined.nodes[0].payment_progress.items[2].state,'done');
const optin=composed.nodes.find(n=>n.consent_progress);
assert.ok(optin);
assert.equal(consentView(optin.consent_progress).parts.length,4);
const consent={schema:'journey-consent.v1',channel:'instagram',purpose:'post_purchase_marketing',delivery:{status:'received',evidence_refs:[{id:1}]},invitation:{status:'sent',evidence_refs:[{id:2}]},response:{status:'accepted',evidence_refs:[{id:3}]},permission:{status:'granted',evidence_refs:[{id:4}]}};
assert.equal(consentView(consent).tone,'success');
assert.equal(consentView({...consent,response:{status:'declined',evidence_refs:[{id:3}]}}).tone,'danger');
assert.notEqual(consentView({...consent,permission:{status:'granted',evidence_refs:[]}}).tone,'success');
assert.notEqual(consentView({...consent,purpose:'other'}).tone,'success');
assert.notEqual(consentView({...consent,permission:{status:'revoked',evidence_refs:[{id:5}]}}).tone,'success');

assert.notEqual(consentView({...consent,purpose:undefined}).tone,'success');
assert.notEqual(consentView({...consent,delivery:{status:'waiting'}}).tone,'success');
for(const purpose of ['payment_reminder','restock_notification']){
 const scoped={...consent,purpose};
 assert.notEqual(consentView(scoped).tone,'success','needs specific date or variant');
 assert.equal(consentView({...scoped,subject:{status:'confirmed',evidence_refs:[{id:5}]}}).tone,'success');
 assert.notEqual(consentView({...scoped,channel:'telegram',subject:{status:'confirmed',evidence_refs:[{id:5}]}}).tone,'success');
 assert.ok(composed.nodes.some(n=>n.consent_progress?.purpose===purpose));
}
const time=Date.parse('2026-09-28T10:00:00Z');
const invoice={kind:'invoice_expiry',status:'running',started_at:'2026-09-28T09:00:00Z',due_at:'2026-09-28T11:00:00Z',evidence_refs:[{kind:'payment_attempt',id:1}]};
assert.equal(invoiceCountdown(invoice,time).remaining,.5);
assert.equal(invoiceCountdown(invoice,time+3600000).expired,true);
assert.equal(invoiceCountdown({...invoice,evidence_refs:[]},time),null);
assert.equal(invoiceCountdown({...invoice,status:'cancelled'},time),null);
assert.equal(invoiceCountdown({...invoice,started_at:invoice.due_at},time),null);
assert.equal(invoiceCountdown(invoice,null),null);
assert.equal(paymentView({},[invoice],time).items[1].state,'next');
assert.equal(paymentView({},[invoice],time+3600000).tone,'danger');
assert.equal(paymentView({paid:true},[invoice],time+3600000).tone,'success');
const mixed=journey.mergePayment({nodes:[{id:'possible:wait',semantic_key:'awaiting_payment',presentation_kind:'possible'},{id:'wait-real',semantic_key:'awaiting_payment',current:true,timers:[invoice]},{id:'possible:paid',semantic_key:'settlement',presentation_kind:'possible'}],edges:[]});
assert.equal(mixed.nodes.length,1);assert.equal(mixed.nodes[0].composite_nodes.length,3);
assert.equal(mixed.nodes[0].timers.length,1);assert.equal(mixed.nodes[0].label,'Оплата');
const separate=journey.mergePayment({nodes:[{id:'a',semantic_key:'awaiting_payment',episode_id:1},{id:'b',semantic_key:'settlement',episode_id:2}],edges:[]});assert.equal(separate.nodes.length,2);
const select=selectionView({schema:'journey-selection.v1',total:2,items:[{label:'Товар',required:true,status:'complete'},{label:'Розмір',required:true,status:'open'}]});
assert.equal(select.completed,1);assert.equal(select.total,2);
assert.equal(selectionView({schema:'journey-selection.v1',total:null,items:[{label:'Товар',required:true,status:'complete'}]}).known,false);
assert.ok(composed.nodes.some(n=>n.semantic_key==='advertising_entry'));
assert.ok(composed.nodes.some(n=>n.selection_progress));
assert.equal(composed.nodes.filter(n=>['catalog_discovery','configured_line'].includes(n.structural_key||n.semantic_key)).length,1);
const geometry=window.TwcJourneyGeometry;

const occupied=[{left:72,right:128,top:72,bottom:128}];
const annotation=geometry.annotationPoint({anchor:{x:100,y:100},occupied,width:320,height:180});
assert.ok(annotation);
assert.ok(annotation.x-17>=128||annotation.x+17<=72||annotation.y-13>=128||annotation.y+13<=72);
assert.equal(geometry.annotationPoint({anchor:{x:10,y:10},occupied:[{left:0,right:100,top:0,bottom:100}],width:40,height:40}),null);


"""
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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
function delivery(status,tracking,delivered=false){
 const parent={id:'client-order:1',semantic_key:'client_order_context',facts:[{id:'1:status',value:status},...(tracking?[{id:'1:tracking',value:'123'}]:[]),...(delivered?[{id:'1:delivery',state:'complete'}]:[])]};
 const graph={nodes:[parent,{id:'client-order:1:shipping',semantic_key:'client_order_shipping',state:status==='Відправлено'?'complete':'open'}],edges:[]};
 return replay.mergeDelivery(graph);
}
assert.equal(delivery('Готується',true).nodes[0].delivery_progress.step,1);
assert.equal(delivery('Завершено',true).nodes[0].delivery_progress.step,1);
assert.equal(delivery('Відправлено',true).nodes[0].delivery_progress.step,2);
assert.equal(delivery('Завершено',true,true).nodes[0].delivery_progress.step,4);
assert.equal(delivery('Скасовано',true).nodes[0].delivery_progress.step,0);
assert.equal(delivery('Відправлено',true).nodes.length,1);

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

assert.equal(fork.definitions.length,2);
assert.equal(fork.transitions.length,1);
assert.equal(fork.transitions[0].via_objection,true);
assert.deepEqual(fork.transitions[0].source_transition_ids,['raise','retry']);
assert.equal(fork.transitions[0].source_key,'quoted_offer');
assert.equal(fork.transitions[0].target_key,'awaiting_payment');
assert.equal(catalogue.definitions.length,3); // no mutation
const quote={id:'trace:quote',semantic_key:'quoted_offer',label:'Пропозиція',presentation_kind:'interpretation',current:true};
const sourceGraph={nodes:[quote],edges:[],trace_node_ids:[quote.id],transcript_reconstruction:{scope:'client'}};
const snapshot={catalogue};
let graph=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
assert.ok(!graph.nodes.some(n=>n.id==='possible:objection_case'));
assert.equal(graph.edges.length,0);
assert.equal(sourceGraph.nodes.length,1);
journey.modal={};journey.showPossible=true;journey.possibleFamily='all';
graph=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
assert.ok(!graph.nodes.some(n=>n.semantic_key==='objection_case'));
assert.equal(graph.edges.length,1);
assert.ok(graph.edges.every(e=>e.relation==='route'&&e.evidence_refs.length===0));
assert.equal(graph.edges[0].via_objection,true);
for(const family of ['catalog','custom']){
  journey.possibleFamily=family;
  const filtered=journey.presentEvents(journey.presentGraph(sourceGraph,snapshot));
  assert.ok(!filtered.nodes.some(n=>n.semantic_key==='objection_case'));
  assert.ok(filtered.edges.some(e=>e.via_objection));
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
assert.match(text(journey.objections),/1 обговорено/);
assert.match(text(journey.objections),/1 не вирішено/);
assert.match(text(journey.objections),/згоду клієнта не підтверджено/);
assert.equal(journey.objectionButtons.size,2);
journey.graph={nodes:[{id:'possible:objection_case',semantic_key:'objection_case',label:'Заперечення',presentation_kind:'possible'}]};
journey.renderObjections();
assert.equal(journey.objections.hidden,true);
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

    def test_cart_presentation_preserves_lines_and_original_edges(self):
        source = (Path(__file__).parent / "static/management/ig_journey.js").read_text().replace("window.TwcJourney={create:options=>new Journey(options)};", "window.TwcJourney={Journey,cartView};")
        program = "global.window={};\n" + source + r"""
const assert=require('node:assert/strict');const {Journey,cartView}=window.TwcJourney;
Journey.prototype.closePanel.call({selected:null,selectedEdge:null,buttons:new Map(),edgeButtons:new Map(),detailButtons:new Map(),objectionButtons:new Map(),queueLayout(){}},false);
const cart={schema:'journey-cart.v1',line_count:2,item_count:3,ready_count:1,lines:[{line_id:'a',quantity:2,fields:{total:1,completed:1,items:[{required:true,label:'Size',status:'complete'}]}},{line_id:'b',quantity:1,fields:{total:1,completed:0,items:[{required:true,label:'Size',status:'open'}]}}]};
const view=cartView(cart);assert.equal(view.completed,1);assert.equal(view.total,2);assert.match(view.label,/2 поз. · 3 шт./);
const graph={nodes:[{id:'pick',semantic_key:'catalog_discovery',selection_cart:cart},{id:'configured',semantic_key:'configured_line'},{id:'quote',semantic_key:'quoted_offer'}],edges:[{id:'inside',from_node_id:'pick',to_node_id:'configured'},{id:'out',from_node_id:'configured',to_node_id:'quote'}],inline_main_ids:['pick','configured','quote']};
const result=Journey.prototype.mergeSelection.call({},graph),pick=result.nodes.find(n=>n.id==='pick');
assert.equal(pick.selection_cart.lines.length,2);assert.equal(pick.composite_edges[0].id,'inside');assert.equal(result.edges[0].from_node_id,'pick');assert.equal(result.edges[0].id,'out');
"""
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class CompletedClientJourneyFocusTests(SimpleTestCase):
    def graph(self):
        return {'nodes': [
            {'id': 'trace:fulfillment', 'semantic_key': 'fulfillment', 'current': True, 'interpreted_focus': True},
            {'id': 'client-order:321', 'semantic_key': 'client_order_context', 'state': 'complete',
             'fulfillment_progress': {'step': 4}, 'facts': [{'source': 'order.current'}]},
        ]}

    def test_completed_client_uses_carrier_result_without_binding_order_to_episode(self):
        graph = self.graph()
        focus = finalize_display_focus(graph, client_stage='done')
        self.assertEqual(focus['node_id'], 'client-order:321')
        self.assertEqual(focus['scope'], 'client_order_context')
        self.assertEqual(focus['authority'], 'order_record')
        self.assertEqual(graph['last_dialogue_focus_id'], 'trace:fulfillment')
        self.assertFalse(graph['nodes'][0]['current'])

    def test_new_purchase_history_multiple_orders_and_unknown_delivery_do_not_advance(self):
        for stage, history, extra, step in [('checkout',False,False,4), ('done',True,False,4), ('done',False,True,4), ('done',False,False,1)]:
            graph=self.graph();graph['nodes'][1]['fulfillment_progress']['step']=step
            if extra:graph['nodes'].append({**graph['nodes'][1], 'id':'client-order:other'})
            self.assertEqual(finalize_display_focus(graph,client_stage=stage,is_history=history)['node_id'],'trace:fulfillment')


class DeliveryProgressContractTests(SimpleTestCase):
    def test_done_without_carrier_and_created_waybill_are_explicit(self):
        from types import SimpleNamespace
        from management.services.ig_journey_delivery import delivery_progress
        from django.utils import timezone
        order=SimpleNamespace(status='done',tracking_number='123',tracking_status_code=None,tracking_terminal_at=None,tracking_provider_event_at=None)
        p=delivery_progress(order,{'kind':'order','id':1})
        self.assertEqual(p['step'],1)
        self.assertTrue(p['completion_unverified'])
        order.status='ship';order.tracking_status_code=1
        self.assertTrue(delivery_progress(order,{})['carrier_pending_while_shipped'])
        order.tracking_status_code=7;order.tracking_provider_event_at=timezone.now()
        self.assertEqual(delivery_progress(order,{})['step'],3)
        order.status='done';order.tracking_status_code=9;order.tracking_terminal_at=timezone.now()
        self.assertEqual(delivery_progress(order,{})['step'],4)
        order.status='cancelled'
        self.assertEqual(delivery_progress(order,{})['step'],0)
