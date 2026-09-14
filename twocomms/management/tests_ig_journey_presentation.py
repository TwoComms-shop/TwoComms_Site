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
