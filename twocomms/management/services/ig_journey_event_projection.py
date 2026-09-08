"""Bounded read-only projection of witnessed offer creation transitions."""
from copy import deepcopy
import re

from django.db import DatabaseError

from management.models import IgCheckoutRevision, IgCommercialEpisodeEvent, InstagramBotMessage


EVENT_LIMIT = 32
REFERENCE_LIMIT = 256
_CLAIMS = {
    "schema_version": 1, "from_node": "configured_line", "to_node": "quoted_offer",
    "trigger": "validated_offer_created", "authority": "validated_checkout_revision",
}
_KEYS = set(_CLAIMS) | {
    "proposal_id", "checkout_revision_id", "revision", "digest", "evidence_message_ids",
}


def _positive(value):
    return type(value) is int and value > 0


def _candidate(row):
    evidence = row["evidence"]
    if (
        not isinstance(evidence, dict) or set(evidence) != _KEYS
        or any(type(evidence[key]) is not type(value) or evidence[key] != value
               for key, value in _CLAIMS.items())
        or any(not _positive(evidence[key]) for key in ("proposal_id", "checkout_revision_id", "revision"))
        or not isinstance(evidence["digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence["digest"]) is None
        or row["source"] != "checkout_revision" or row["event_type"] != "semantic_transition"
        or row["from_state"] or row["to_state"] or row["stage"]
        or row["dedupe_key"] != f"journey:checkout-revision:{evidence['checkout_revision_id']}:validated-offer"
    ):
        return False
    ids = evidence["evidence_message_ids"]
    return (isinstance(ids, list) and len(ids) <= 40
            and all(_positive(value) for value in ids) and len(set(ids)) == len(ids))


def _read(client_id, episode_id, coverage):
    rows = list(IgCommercialEpisodeEvent.objects.filter(
        episode_id=episode_id, episode__client_id=client_id, event_type="semantic_transition",
    ).order_by("-id").values(
        "id", "event_type", "source", "dedupe_key", "from_state", "to_state", "stage", "evidence", "created_at",
    )[:EVENT_LIMIT + 1])
    coverage["truncated"] = len(rows) > EVENT_LIMIT
    candidates, refs_used = [], 0
    for row in rows[:EVENT_LIMIT]:
        if not _candidate(row):
            coverage["rejected"] += 1
            continue
        needed = 3 + len(row["evidence"]["evidence_message_ids"])
        if refs_used + needed > REFERENCE_LIMIT:
            coverage["truncated"] = True
            coverage["rejected"] += 1
            continue
        refs_used += needed
        candidates.append(row)
    if not candidates:
        return []
    revisions = {row["id"]: row for row in IgCheckoutRevision.objects.filter(
        pk__in=[row["evidence"]["checkout_revision_id"] for row in candidates],
        proposal__client_id=client_id, proposal__commercial_episode_id=episode_id,
        proposal__commercial_episode__client_id=client_id, proposal__deal__client_id=client_id,
    ).values("id", "proposal_id", "revision", "digest", "snapshot", "source",
             "evidence_message_ids", "source_watermark_message_id")}
    message_ids = {value for row in candidates for value in row["evidence"]["evidence_message_ids"]}
    owned_ids = set(InstagramBotMessage.objects.filter(
        pk__in=message_ids, client_id=client_id, role__in=InstagramBotMessage.Role.values,
    ).values_list("id", flat=True)) if message_ids else set()
    verified = []
    for row in candidates:
        evidence = row["evidence"]
        revision = revisions.get(evidence["checkout_revision_id"])
        ids = evidence["evidence_message_ids"]
        if (
            revision is None
            or any(revision[key] != evidence[key] for key in ("proposal_id", "revision", "digest"))
            or revision["source"] not in {"bot_create", "bot_update"}
            or not isinstance(revision["snapshot"], dict)
            or revision["snapshot"].get("digest") != evidence["digest"]
            or not isinstance(revision["evidence_message_ids"], list)
            or any(not _positive(value) for value in revision["evidence_message_ids"])
            or revision["evidence_message_ids"] != ids
            or revision["source_watermark_message_id"] != max(ids, default=0)
            or not set(ids).issubset(owned_ids)
        ):
            coverage["rejected"] += 1
        else:
            verified.append(row)
    return verified


def _node(graph, key, episode_id):
    # Legacy guide nodes are scoped by the containing episode graph. A node
    # explicitly bound to another episode must never be reused.
    scoped = [node for node in graph["nodes"] if node.get("episode_id", episode_id) == episode_id]
    if key == "quoted_offer":
        matches = [node for node in scoped if node["id"] == "guide:terms" and node.get("semantic_key") == key]
    else:
        matches = [node for node in scoped if node.get("semantic_key") == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    identifier = f"semantic:{key}"
    existing = [node for node in graph["nodes"] if node["id"] == identifier]
    if existing:
        return existing[0] if len(existing) == 1 and existing[0] in scoped and existing[0].get("semantic_key") == key else None
    node = {
        "id": identifier, "semantic_key": key, "episode_id": episode_id,
        "label": "Вибір налаштовано" if key == "configured_line" else "Пропозицію створено",
        "state": "partial", "current": False, "summary": "Зафіксовано створення перевіреної пропозиції",
        "facts": [], "evidence_refs": [], "layout": {"rank": 2 if key == "configured_line" else 3, "lane": 1},
    }
    graph["nodes"].append(node)
    return node


def append_offer_transitions(graph, *, client_id, episode_id):
    """Return an enriched copy; caller owns authorization and the episode view.

    Only immutable revisions justify an edge. Existing milestone chronology,
    current proposal versions and payment state do not establish transitions.
    The caller combines coverage.offer_transitions with other graph producers.
    """
    result = deepcopy(graph)
    coverage = {"status": "missing_source", "verified": 0, "rejected": 0,
                "truncated": False, "limit": EVENT_LIMIT, "reference_limit": REFERENCE_LIMIT}
    result.setdefault("coverage", {})["offer_transitions"] = coverage
    if not _positive(client_id) or not _positive(episode_id):
        coverage["status"] = "invalid_scope"
        return result
    episode_markers = [node["id"] for node in result["nodes"] if node["id"].startswith("episode:")]
    if episode_markers != [f"episode:{episode_id}"]:
        coverage["status"] = "invalid_scope"
        return result
    try:
        verified = _read(client_id, episode_id, coverage)
    except DatabaseError:
        coverage["status"] = "unavailable"
        return result
    if not verified:
        return result
    source = _node(result, "configured_line", episode_id)
    target = _node(result, "quoted_offer", episode_id)
    if source is None or target is None:
        result["nodes"] = deepcopy(graph["nodes"])
        coverage["status"] = "ambiguous_nodes"
        coverage["rejected"] += len(verified)
        return result
    refs = []
    for row in verified:
        evidence = row["evidence"]
        refs.extend([{"kind": "episode_event", "id": row["id"]},
                     {"kind": "checkout_revision", "id": evidence["checkout_revision_id"]},
                     {"kind": "checkout_proposal", "id": evidence["proposal_id"]}])
        refs.extend({"kind": "message", "id": value} for value in evidence["evidence_message_ids"])
    refs = list({(ref["kind"], ref["id"]): ref for ref in refs}.values())
    dates = sorted(row["created_at"].isoformat() for row in verified)
    visits = {"count": len(verified), "first_at": dates[0], "last_at": dates[-1],
              "history_truncated": coverage["truncated"], "has_backfilled": False, "evidence_refs": refs}
    for node in (source, target):
        if node.get("state") == "open":
            node["state"] = "partial"
        if not node.get("recorded_visits"):
            node["recorded_visits"] = deepcopy(visits)
        for ref in refs:
            if ref not in node.setdefault("evidence_refs", []):
                node["evidence_refs"].append(ref)
    edge = {
        "id": f"semantic:validated-offer:{episode_id}", "from_node_id": source["id"], "to_node_id": target["id"],
        "relation": "semantic_transition", "tone": "neutral", "outcome": "Перевірену пропозицію створено",
        "reason_label": "Комплектацію перевірено, пропозицію створено",
        "label": "Пропозицію створено та перевірено", "evidence_refs": refs,
        "event_ids": [f"episode_event:{row['id']}" for row in verified], "repeated_count": len(verified), "last_at": dates[-1],
    }
    # Reapplying this producer replaces only its own stable aggregate.
    result["edges"] = [item for item in result["edges"] if item["id"] != edge["id"]] + [edge]
    result["overview_node_ids"] = list(dict.fromkeys([*result.get("overview_node_ids", []), source["id"], target["id"]]))
    coverage.update(status="partial", verified=len(verified))
    return result
