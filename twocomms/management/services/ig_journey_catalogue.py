"""Static possible paths from the canonical registry, never client history."""
from functools import lru_cache
from management.services.ig_funnel_nodes import DEFINITION_VERSION, semantic_definitions, structural_transitions


@lru_cache(maxsize=1)
def journey_catalogue():
    return {
        "version": DEFINITION_VERSION,
        "definitions": [{"key": item.key, "label": item.ui_label,
            "route_keys": list(item.route_keys), "semantic_kind": item.semantic_kind}
            for item in semantic_definitions()],
        "transitions": [{"id": f"structural:{i}:{item.source_key}:{item.target_key}",
            "source_key": item.source_key, "target_key": item.target_key,
            "outcome": item.outcome} for i, item in enumerate(structural_transitions())],
        "history": False, "actions": False,
    }
