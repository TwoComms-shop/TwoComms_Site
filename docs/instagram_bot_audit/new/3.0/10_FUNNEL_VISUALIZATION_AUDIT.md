# A1 · Funnel visualization audit

**Auditor:** root session (no subagents), 26.09.2026  
**Scope:** inline mini-funnel + full modal map; objection-marker rendering; data model (`ig_journey_snapshot`, `ig_journey_presentation`, `ig_journey_trace_*`); JS (`ig_journey.js`, `ig_journey_geometry.js`); 2.0 plan entries B03, B09 (UX), C15 (contracts); D085 §6 (J1 objection removal complaint).

---

## Executive summary

**Status today (26.09 prod HEAD `c69925d56`):**

- Inline mini-funnel: ✅ live, appears above chat with 3–4 key nodes + current position
- Full modal map: ✅ live, expandable with lane geometry, structural catalogue paths, and incident edges
- Objection nodes (`objection_case`): **conditionally hidden** since commit `ac62e2da3` (14.09.2026). A `objection_case` node with `presentation_kind='possible'` (structural, no evidence) is filtered out of the visible node list unless it has evidence_refs or actual incident edges with priority > 0. The 2.0 plan anticipated visible objection diamonds on every branch (D022, C15); the implementation now hides empty speculative objections.
- Edge markers (price objection, trust, competitor comparison): ✅ drawn as circular buttons on edges with `marker_eligible=true` and a reason_label. Parallel edges (multiple transitions between same two nodes) are **grouped** under one button showing total count; clicking it opens a picker.
- Revision lane: production setting `IG_JOURNEY_TRACE_USE_REVISION_PROPOSAL = True` (per A5 notes from aborted agent); replies go through a propose→review→send flow rather than direct send.

**Findings:**

| ID | Title | Severity | Evidence |
|---|---|---|---|
| **F3-FUN-01** | Objection nodes hidden when empty; contradicts 2.0 plan's "objection diamond on every branch" vision (D022, C15) | P3 · design | `ig_journey.js:155` filter, commit `ac62e2da3` 14.09 |
| **F3-FUN-02** | No visual trace of *how many times* an objection was raised and dismissed vs addressed; repeat_count exists in data but UX shows only "×N" in compact view | P3 · observability | `ig_journey.js:168` uses repeat_count for badge but outcome (addressed/dismissed) not visible |
| **F3-FUN-03** | Structural catalogue bypass edges (`via_objection:true` when objection node is bypassed) render as "Якщо виникне заперечення" in full map but are cosmetic — no evidence that they meaningfully differ from ordinary possibility | P4 · cosmetic | `ig_journey.js:198`, `ig_journey.js:225` |
| **F3-FUN-04** | `marker_eligible` propagation rule is implicit: only when `proven_impact AND anchored` in `ig_journey_trace_projection.py:90`; no documentation of what "proven impact" means for objection vs other case types | P3 · maintainability | `services/ig_journey_trace_projection.py:90` |
| **F3-FUN-05** | Parallel-edge grouping (21.09 commit `21eb25986`) improved legibility but introduced new UX: one button for N transitions → picker. If bot made 5 separate price objections, the map shows "×5" but user must click and read picker to see individual outcomes. | P3 · UX | `ig_journey.js:508–516` edgeCount aggregation |

---

## 1. Data model (snapshot → presentation → trace projection)

### 1.1 Journey snapshot schema

**File:** `services/ig_journey_snapshot.py`  
**What it holds:** one `JourneySnapshot` row per conversation episode, storing:
- `viewed_episode_id`, `conversation_id`, `last_intent_analysis_id`
- `nodes`: JSON list of `{id, semantic_key, route_kind, label, state, facts, evidence_refs, layout, waiting, ...}`
- `edges`: JSON list of `{id, from_node_id, to_node_id, relation, outcome, reason_code, reason_label, summary, interpretation_kind, evidence_refs, tone, ...}`
- `catalogue_schema_version`, `presentation_schema_version`
- `transcript_reconstruction` metadata

**Node types seen in prod (from grep + 2.0 evidence):**
- `semantic_key`: `inbound`, `catalog`, `custom_print`, `dtf_only`, `quoted_offer`, `settlement`, `payment`, `fulfillment`, `objection_case`, `journey_case`, `employment`, `collaboration`, `information_question`, `support`, `post_sale_request`, `consent` (opt-in)
- `route_kind`: derived from journey transitions (e.g., `ad_resolved_product`, `catalog_discovery`, `availability_question`, `payment_help`)
- `presentation_kind`: `'possible'` for structural nodes with no evidence yet; `'incident'` for nodes with real events

### 1.2 Trace projection: how edges get `marker_eligible`

**File:** `services/ig_journey_trace_projection.py:90`

```python
"marker_eligible": bool(proven_impact and anchored),
```

- `proven_impact`: for an `objection_case`, this means the case has a non-empty `source_edge_id` and that edge exists in the traced incidents (line 278–280 shows a second pass that sets `marker_eligible` only for the edge matching `source_edge_id`).
- `anchored`: both source and target nodes must be in the visible node set.

**Implication:** a speculative objection node (`presentation_kind='possible'`, no evidence_refs, no incident edges) will have `marker_eligible=False` for all its structural in/out edges, so none of those edges draw a marker button. The JS then filters the node out entirely (see below).

### 1.3 JS filtering (hiding empty objection nodes)

**File:** `static/management/ig_journey.js:155` (commit `ac62e2da3`, 14.09.2026)

```javascript
const nodes=graph.nodes.filter(n=>n.semantic_key!=='objection_case'||(n.presentation_kind!=='possible'&&((n.evidence_refs||[]).length||graph.edges.some(e=>edgePriority(e)>0&&(e.from_node_id===n.id||e.to_node_id===n.id)))));
```

Translation: keep an `objection_case` node **only if**:
- `presentation_kind !== 'possible'` (it's an incident, not structural), OR
- it has `evidence_refs`, OR
- at least one edge touching it has `edgePriority(edge) > 0` (witnessed or interpreted, not structural)

Otherwise the node is **removed** from the visible map.

**Finding F3-FUN-01 detail:** this contradicts the 2.0 plan's vision (D022, C15.4) of showing objection diamonds on *every* branch as conditional nodes. The plan anticipated a visual grammar where "catalog → [objection?] → offer" would always show the diamond, greyed out if not hit. The implementation decided to hide it when empty, likely to reduce clutter. Owner's D085 §6 J1 complaint ("I don't see objection markers") may stem from this: if a customer hasn't objected yet, no diamond appears.

---

## 2. Edge markers & parallel-edge grouping

### 2.1 What triggers a marker button

**File:** `static/management/ig_journey.js:508–524` (commit `21eb25986`, 09.09.2026)

```javascript
const peers=this.parallelEdges(edge).filter(item=>drawingEdges.some(shownEdge=>shownEdge.id===item.id)&&(!this.inlineSlots||this.modal||!returnEdge(item)));
if(peers.length&&peers[0].id!==edge.id)return; // only first peer draws the button
const total=peers.reduce((sum,item)=>sum+edgeCount(item),0)||edgeCount(edge);
if(edge.structural_path||(!edge.reason_label&&!count&&edge.relation!=='return'&&peers.length<2))return; // skip if no label and not grouped
```

A marker button appears when:
1. The edge (or its peers) have `reason_label` or `summary`, OR
2. The edge is a `return` relation, OR
3. Multiple parallel edges exist between the same two nodes (even if unlabeled, the group gets a button showing "×N")

**Finding F3-FUN-05 detail:** before 21.09, each edge drew its own button. After `21eb25986`, parallel edges are grouped: if the bot made 3 separate attempts to handle a price objection (failed, partial, addressed), the map draws **one** button labeled "×3". Clicking it opens a `<select>` picker listing all three with their outcomes. This is more compact but hides the granularity until clicked. For a user debugging "why didn't the bot address my objection?", they must now click the marker to see the picker and find the edge with `reason_code='objection_dismissed'` vs `'objection_addressed'`.

### 2.2 Objection repeat count

**File:** `ig_journey.js:142–144,168`

For an `objection_case` node, the code extracts:
```javascript
const mentions=(data.facts||[]).find(f=>f.id?.endsWith(':repeat_count'));
const repeats=Number.isInteger(mentions?.value)&&mentions.value>1?mentions.value:0;
```
Then for the inline mini-funnel, it shows a badge "×N" where N is the repeat count. **But** the badge does not distinguish *how* those repeats were handled (3 dismissed + 1 addressed = ×4, vs 4 addressed = ×4, look identical in the compact view). Only in the full map's edge picker can the user see individual outcomes.

**Finding F3-FUN-02:** this is observability debt. The scenario matrix (05_SCENARIO_MATRIX.md row 3.1–3.2) calls out price objection escalation: if the customer says "дорого" twice, the bot should escalate to manager. The funnel should make it obvious whether that escalation happened. Current UX: node badge "×2", edge button, click → picker → read 2 entries. Not terrible, but not the "at a glance" D022 anticipated.

---

## 3. Structural catalogue vs live incident paths

### 3.1 Catalogue transitions with `via_objection`

**File:** `ig_journey.js:149–151` (commit `ac62e2da3`)

```javascript
const key='objection_case',incoming=catalogue.transitions.filter(e=>e.target_key===key),outgoing=catalogue.transitions.filter(e=>e.source_key===key);
// These are conditional structural paths, not occurrences of an objection.
const bypass=incoming.flatMap(a=>outgoing.filter(b=>b.target_key!==a.source_key).map(b=>({id:'possible-objection:'+a.id+':'+b.id,source_key:a.source_key,target_key:b.target_key,outcome:b.outcome,via_objection:true})));
```

For every route that enters `objection_case` from A and exits to B, the code synthesizes a **bypass edge** A→B with `via_objection:true` and condition label "Якщо виникне заперечення". These edges appear in the full map as dashed possibility paths.

**Finding F3-FUN-03:** these are cosmetic. They show "if an objection arises, here's the alternate path" but they're generated mechanically from the catalogue schema, not from the bot's decision log. A real objection incident would draw a **separate** edge with `interpretation_kind='objection'` and `marker_eligible=true`. The bypass edges just clutter the full map with "what if" paths that the owner may already understand from the diamonds. Not harmful, but D022's "objection marker between stages" might have meant *only* incident markers, not these structural possibilities.

---

## 4. Cross-reference with 2.0 plan

| 2.0 task | Status 26.09 | Notes |
|---|---|---|
| **B03.2** "Manual early attention command" | ❓ not in A1 scope (see A8 collab/manager audit) | owner can click a "передати менеджеру" button; verify in A8 |
| **B09.1–9** Visual journey with motion, icons, map, live list | ✅ mostly done | inline + full modal live; motion (pulse rings for timers); icons per semantic_key; compact bands; responsive |
| **C15.4** Objection diamonds on branches | ⚠️ **partially** | diamonds exist but hidden when `presentation_kind='possible'` + no evidence → **F3-FUN-01** |
| **C15.6** Edge markers for objections/repeats | ✅ | markers draw with `marker_eligible`; repeats grouped → **F3-FUN-05** observability note |
| **D022** "show objection between stages; don't force to separate view" | ⚠️ **conflict** | inline mini-funnel shows at most 3–8 nodes; an unraised objection node is hidden; user must expand to full map to see structural possibilities |
| **D085 §6 J1** "removed objection markers" | **reproduced** | likely refers to the `presentation_kind='possible'` filter added 14.09; speculative objections no longer visible |

---

## 5. Recommendations for 3.0

1. **Re-show speculative objection nodes** (at least in full map, optionally in inline if room) with a distinct visual state ("possible, not yet raised"). Restore the D022 grammar where every conditional branch shows its diamond. Inline can still omit them for space, but full map should show the structure.
   
2. **Outcome badge on objection nodes:** instead of just "×3", show "×3 (2 dismissed, 1 addressed)" or colour-code the badge (grey=dismissed, green=addressed, amber=partial). This makes escalation patterns visible without clicking.

3. **Edge picker UX:** when grouping parallel edges, the picker currently says "За перепискою · <reason> ×N" but doesn't highlight which one was the *final* outcome. Add a visual cue (bold, checkmark, or sort order) so the most recent or most significant edge is obvious.

4. **Document `marker_eligible` rule** in a comment or doc string. Right now "proven impact" is implicit in the projection logic; future maintainers will guess.

5. **Bypass edges (`via_objection`):** either remove them (they're redundant if the diamond is visible) or make them dashed + grey so they're clearly structural, not incident.

---

## 6. Code health notes

- **Test coverage:** `tests_ig_journey_trace_projection.py` has 50 lines added in commit `21eb25986` (objection legibility), but no test specifically for "hide objection node when presentation_kind='possible' + no evidence". Add a regression test for F3-FUN-01 decision.
- **Schema version:** `presentation_schema_version='journey-presentation.v1'` appears in Sept commits; this suggests a migration path exists. Confirm with A5 (memory/context audit) whether old snapshots are recomputed or served as-is.
- **Performance:** `parallelEdges(edge)` calls `edgePair(edge)` which concatenates `from_node_id+'\0'+to_node_id` and filters the whole edge array each time. For a conversation with 50+ edges this is O(E²) during layout. Not a bottleneck yet (typical conversations have <20 edges), but worth noting if D085 scale assumptions change (e.g., multi-month conversation with 200+ edges).

---

## Appendix A: commit timeline (objection rendering)

| Date | Commit | Change |
|---|---|---|
| 08.09 | `acc76292c` | Initial inline semantic journey + full map |
| 08.09 | `e92a5c8f2` | Scoped possible paths, collision-aware geometry |
| 08.09 | `b6e84234a` | Expose sourced transitions, bounded objection details |
| 09.09 | `21eb25986` | **Objection legibility:** parallel edge grouping, repeat count, bounded trace reasoning |
| 09.09 | `f142696f9` | Compact full-map bands, clarify branch conditions |
| 09.09 | `a5833ab7a` | Inline branch connections, preserve current context; extras list includes `objection_case` for catalog/custom families |
| 14.09 | `ac62e2da3` | **Hide empty objections:** filter `presentation_kind='possible'` objection nodes with no evidence; journey_case topic icons; exceptional edge predicate updated |

---

**Next:** sync to local, then A2 (overview/ops/alerts).
