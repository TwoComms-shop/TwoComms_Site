"""Choose one display focus after read-only journey enrichers have completed."""


def finalize_display_focus(graph, *, is_history=False, client_stage=None):
    nodes = graph.get("nodes", [])
    candidates = [node for node in nodes if node.get("route_focus")]
    provenance, authority = "accepted_conversation_route", "accepted_route"
    if not candidates:
        candidates = [node for node in nodes if node.get("current") and any(
            str(fact.get("source", "")).endswith(".current") for fact in node.get("facts", []))]
        provenance, authority = "bound_business_facts", "business_record"
    if not candidates:
        candidates = [node for node in nodes if node.get("current")]
        provenance, authority = "recorded_history" if is_history else "recorded_event", "display_only"
    chosen = candidates[0] if len(candidates) == 1 else None
    # A completed client may have finished in another channel. The unique
    # carrier-confirmed linked order is a client fact, not an episode binding.
    # Never advance a new active purchase or overwrite an accepted route.
    if (not is_history and client_stage == "done" and chosen
            and chosen.get("interpreted_focus") and chosen.get("semantic_key") == "fulfillment"
            and not any(n.get("route_focus") for n in nodes)):
        orders = [n for n in nodes if n.get("semantic_key") == "client_order_context"]
        if (len(orders) == 1 and orders[0].get("state") == "complete"
                and orders[0].get("fulfillment_progress", {}).get("step") == 4):
            graph["last_dialogue_focus_id"] = chosen["id"]
            chosen = orders[0]
            provenance, authority = "linked_order_state", "order_record"
    if chosen and chosen.get("interpreted_focus"):
        provenance, authority = "transcript_reconstruction", "none"
    if chosen and chosen.get("semantic_key") == "objection_case" and chosen.get("presentation_kind") == "interpretation":
        # A concern is attached to an affected action, not a new sales stage.
        latest_step = (chosen.get("transcript_interpretation") or {}).get("last_step_index")
        latest_case_ids = {edge.get("case_id") for edge in graph.get("edges", [])
                           if latest_step is not None and edge.get("last_step_index") == latest_step
                           and chosen["id"] in (edge.get("from_node_id"), edge.get("to_node_id"))}
        anchors = {case.get("source_node_id") for case in graph.get("trace_cases", [])
                   if case.get("source_node_id") and case.get("marker_eligible")
                   and case.get("id") in latest_case_ids}
        chosen = next((node for node in nodes if node["id"] in anchors), None) if len(anchors) == 1 else None
    focus = {
        "node_id": chosen["id"] if chosen else None,
        "provenance": provenance if chosen else "ambiguous" if candidates else "missing",
        "authority": authority if chosen else "none", "display_only": True,
        "freshness": (graph.get("transcript_reconstruction") or {}).get("freshness", "unknown")
            if provenance == "transcript_reconstruction" else "historical" if is_history else "current",
        "scope": "viewed_history" if is_history else "client_order_context" if provenance == "linked_order_state" else "current_dialogue",
    }
    graph["display_focus"] = focus
    for node in nodes:
        node["current"] = node["id"] == focus["node_id"]
    return focus
